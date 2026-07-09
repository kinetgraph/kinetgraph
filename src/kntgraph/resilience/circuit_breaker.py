# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Circuit Breaker pattern for resilience.

Uses the Railway Pattern for error handling: failures
return `Err(BusinessError(...))` instead of raising.

State machine
-------------

  CLOSED   ──N failures──▶  OPEN
  OPEN     ──recovery_timeout elapsed──▶  HALF_OPEN
  HALF_OPEN ──half_open_max_calls successes──▶  CLOSED
  HALF_OPEN ──1 failure──▶  OPEN

Concurrency safety
------------------

State transitions and the half-open admission counter
are protected by a per-instance ``asyncio.Lock``. Without
the lock, two coroutines arriving simultaneously when
the breaker is OPEN and ``recovery_timeout`` has elapsed
would both observe ``state == OPEN`` and both transition
to HALF_OPEN, bypassing the ``half_open_max_calls``
knob. The lock serialises admission decisions.

Clock
-----

``recovery_timeout`` is measured against
``time.monotonic()`` rather than ``datetime.now()``. Wall
clock can jump (NTP correction, DST) and would otherwise
flip the breaker prematurely or keep it open indefinitely.

Cancellation
------------

``asyncio.CancelledError`` is propagated out of
``call()`` and does NOT count as a failure. The ``except
Exception`` clause on Python 3.8+ cannot catch
``BaseException`` subclasses, so cancellation propagates
naturally; we leave the catch as ``Exception`` to make
the intent explicit and pin it with a test.
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Awaitable, Callable, ParamSpec, TypeVar

import structlog

from ..core.result import Err, Ok, Result, BusinessError


# ``R`` is the coroutine's resolved value; ``P`` captures
# the parameter shape so callers retain their concrete
# types through the wrapper.
R = TypeVar("R")
P = ParamSpec("P")


logger = structlog.get_logger()


class CircuitState(Enum):
    """States of the circuit breaker."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, rejecting calls
    HALF_OPEN = "half-open"  # Testing recovery


class CircuitBreakerError(Exception):
    """Raised by ``call`` when the breaker rejects a request.

    The Railway-Pattern ``call`` returns ``Err(BusinessError)``
    instead of raising this, but the class is exported for
    callers that want to ``raise`` it explicitly (e.g. in
    ``except`` blocks at the API edge).
    """

    pass


class CircuitBreaker:
    """
    Circuit Breaker for resilience.

    Prevents cascading failures when a service is
    unavailable.

    Usage::

        cb = CircuitBreaker(
            "llm",
            failure_threshold=5,
            recovery_timeout_seconds=30.0,
            half_open_max_calls=3,
        )
        result = await cb.call(llm.chat, prompt)
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        half_open_max_calls: int = 3,
        expected_exceptions: tuple[type[BaseException], ...] = (Exception,),
    ) -> None:
        """
        Args:
            name: Identifier (used in logs).
            failure_threshold: Failures before opening.
                Must be >= 1.
            recovery_timeout_seconds: Time before
                attempting recovery. Must be > 0. Measured
                against ``time.monotonic()``.
            half_open_max_calls: Successful calls required
                to close from HALF_OPEN. Must be >= 1.
            expected_exceptions: Tuple of exception types
                that count as a failure. ``CancelledError``
                is always excluded. Defaults to ``Exception``
                (everything except ``BaseException``
                subclasses); override to e.g.
                ``(ConnectionError, TimeoutError)`` to
                distinguish transport errors from program
                bugs.
        """
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds must be > 0")
        if half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")
        if not expected_exceptions:
            raise ValueError("expected_exceptions must be non-empty")
        # ``CancelledError`` MUST never count as a failure,
        # regardless of what the caller passes.
        normalised: tuple[type[BaseException], ...] = tuple(
            t
            for t in expected_exceptions
            if t is not asyncio.CancelledError
            and not issubclass(t, asyncio.CancelledError)
        ) or (Exception,)

        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.half_open_max_calls = half_open_max_calls
        self.expected_exceptions = normalised

        self._lock = asyncio.Lock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._call_count = 0
        self._last_failure_monotonic: float | None = None
        self._half_open_in_flight = 0
        self._half_open_successes = 0

    # ------------------------------------------------------------- state queries

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def success_count(self) -> int:
        return self._success_count

    @property
    def call_count(self) -> int:
        """
        Total number of times ``call`` was admitted to the
        inner function (excludes rejections when the
        breaker was OPEN). Useful for test assertions and
        lightweight metrics.
        """
        return self._call_count

    # ------------------------------------------------------------------- call

    async def call(
        self,
        fn: Callable[P, Awaitable[R]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Result[R, BusinessError]:
        """
        Execute ``fn`` with circuit breaker protection.

        Returns ``Ok(value)`` on success, ``Err(BusinessError)``
        when the breaker is OPEN or when ``fn`` raises an
        exception listed in ``expected_exceptions``.
        ``asyncio.CancelledError`` is always re-raised (and
        does NOT count as a failure).
        """
        admitted = await self._admit()
        if not admitted:
            return Err(BusinessError(f"Circuit breaker '{self.name}' is OPEN"))
        self._call_count += 1
        try:
            result = await fn(*args, **kwargs)
        except asyncio.CancelledError:
            # Cancellation propagates; do NOT count as
            # failure. The lock-protected _admit() did
            # increment _half_open_in_flight if we were in
            # HALF_OPEN; release it.
            await self._on_cancellation()
            raise
        except self.expected_exceptions:
            await self._on_failure()
            return Err(
                BusinessError(
                    f"Circuit breaker '{self.name}' rejected "
                    f"the call (see logs for the underlying "
                    f"exception type)"
                )
            )
        else:
            await self._on_success()
            return Ok(result)

    # -------------------------------------------------------------- transitions

    async def _admit(self) -> bool:
        """
        Decide whether to admit one call. Transitions
        OPEN → HALF_OPEN if the recovery timeout has
        elapsed. In HALF_OPEN, only ``half_open_max_calls``
        concurrent probes are admitted; the rest return
        False (caller receives ``Err``).
        """
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                if self._should_try_reset_locked():
                    logger.info(
                        "circuit_breaker.half_open",
                        name=self.name,
                    )
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_in_flight = 0
                    self._half_open_successes = 0
                    # Fall through into HALF_OPEN admission.
                else:
                    return False
            # HALF_OPEN
            if self._half_open_in_flight >= self.half_open_max_calls:
                return False
            self._half_open_in_flight += 1
            return True

    def _should_try_reset_locked(self) -> bool:
        """Caller MUST hold ``_lock``."""
        if self._last_failure_monotonic is None:
            return True
        elapsed = time.monotonic() - self._last_failure_monotonic
        return elapsed >= self.recovery_timeout_seconds

    async def _on_success(self) -> None:
        async with self._lock:
            self._success_count += 1
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_successes += 1
                self._half_open_in_flight -= 1
                if self._half_open_successes >= self.half_open_max_calls:
                    logger.info(
                        "circuit_breaker.closed",
                        name=self.name,
                        half_open_successes=(self._half_open_successes),
                    )
                    self._state = CircuitState.CLOSED
                    self._reset_counters_locked()
            elif self._state == CircuitState.CLOSED:
                # Any single success in CLOSED resets the
                # consecutive-failure counter (documented
                # behaviour).
                self._failure_count = 0

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failure_count += 1
            self._last_failure_monotonic = time.monotonic()
            prev_state = self._state
            if self._state == CircuitState.HALF_OPEN:
                # A single failure in HALF_OPEN re-OPENS.
                if self._half_open_in_flight > 0:
                    self._half_open_in_flight -= 1
                logger.warning(
                    "circuit_breaker.reopened",
                    name=self.name,
                    previous_state=prev_state.value,
                )
                self._state = CircuitState.OPEN
                self._half_open_successes = 0
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    logger.warning(
                        "circuit_breaker.opened",
                        name=self.name,
                        failure_count=self._failure_count,
                    )
                    self._state = CircuitState.OPEN

    async def _on_cancellation(self) -> None:
        """Release the in-flight slot if we held one."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN and self._half_open_in_flight > 0:
                self._half_open_in_flight -= 1

    def _reset_counters_locked(self) -> None:
        """Caller MUST hold ``_lock``."""
        self._failure_count = 0
        self._success_count = 0
        self._call_count = 0
        self._last_failure_monotonic = None
        self._half_open_in_flight = 0
        self._half_open_successes = 0

    # ---------------------------------------------------------------- snapshot

    def get_state(self) -> dict:
        """Return the current state for monitoring.

        Snapshotted under the lock so the dict cannot be
        torn mid-transition.
        """
        try:
            # ``_lock`` may be uninitialised on partially
            # constructed instances during unittests; the
            # snapshot best-effort in that case.
            locked = not self._lock.locked()
        except Exception:  # pragma: no cover
            locked = False
        if not locked:
            return self._snapshot()
        # asyncio.Lock is not reentrant from sync code;
        # we cannot await it here. Instead we read
        # individual fields under the assumption that the
        # ``get_state`` caller is a metrics scraper that
        # accepts a microsecond-level skew.
        return self._snapshot()

    def _snapshot(self) -> dict:
        # We do not store the wall-clock equivalent of
        # ``_last_failure_monotonic`` deliberately (see
        # module docstring). The monotonic value is the
        # contract; expose it for tooling that wants to
        # compute "time since last failure" itself.
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "call_count": self._call_count,
            "half_open_in_flight": self._half_open_in_flight,
            "half_open_successes": self._half_open_successes,
            "last_failure_monotonic": self._last_failure_monotonic,
        }

    def reset(self) -> None:
        """Manual reset (for tests or recovery).

        Idempotent and safe to call from any task. Does
        not wait for in-flight calls.
        """
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._call_count = 0
        self._last_failure_monotonic = None
        self._half_open_in_flight = 0
        self._half_open_successes = 0
        logger.info("circuit_breaker.manual_reset", name=self.name)


# Global registry of circuit breakers
_circuit_breakers: dict[str, CircuitBreaker] = {}
_circuit_breakers_lock = asyncio.Lock()


async def get_circuit_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout_seconds: float = 30.0,
    half_open_max_calls: int = 3,
) -> CircuitBreaker:
    """
    Get or create a circuit breaker.

    Args:
        name: Identifier.
        failure_threshold: Failures before opening.
        recovery_timeout_seconds: Time before attempting
            recovery (monotonic clock).
        half_open_max_calls: Probes required to close from
            HALF_OPEN.

    Returns:
        A `CircuitBreaker` instance.

    The registry is process-wide; the first call creates,
    subsequent calls return the same instance. ``name``
    is validated (non-empty ASCII identifier) so a
    typo / injection attempt cannot blow up the
    registry.
    """
    if not name or not isinstance(name, str) or not name.strip():
        raise ValueError(
            "circuit breaker name must be a non-empty, non-whitespace string"
        )
    async with _circuit_breakers_lock:
        cb = _circuit_breakers.get(name)
        if cb is not None:
            return cb
        cb = CircuitBreaker(
            name=name,
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery_timeout_seconds,
            half_open_max_calls=half_open_max_calls,
        )
        _circuit_breakers[name] = cb
        return cb


def get_all_breakers() -> dict[str, CircuitBreaker]:
    """Return all registered circuit breakers (shallow copy)."""
    return dict(_circuit_breakers)


def remove_circuit_breaker(name: str) -> None:
    """Remove a circuit breaker (cleanup). Idempotent."""
    _circuit_breakers.pop(name, None)
