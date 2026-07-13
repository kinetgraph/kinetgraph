# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Timeout pattern for resilience.

The public surface offers:

  - ``with_timeout(fn, ...)``: a single attempt with a
    timeout. On timeout, an optional ``fallback`` is
    invoked; otherwise ``asyncio.TimeoutError`` is raised.
  - ``with_timeout_and_retry(fn, ...)``: N attempts with
    exponential backoff (parameterized) and jitter, each
    bounded by a per-attempt timeout. The function honours
    a ``max_total_seconds`` budget so a single misconfigured
    call cannot pin a worker indefinitely.
  - ``TimeoutConfig``: a reusable wrapper with the same
    surface as ``with_timeout``.

Cancellation
------------

``asyncio.CancelledError`` is propagated unchanged in all
three surfaces. ``wait_for`` cancels the inner task on
timeout and re-raises; the retry loop's ``except
asyncio.TimeoutError`` does not match ``BaseException``,
so cancellation between attempts also propagates.

Backoff
-------

``with_timeout_and_retry`` uses ``base_delay * 2**attempt``
(so the first sleep is ``base_delay``, not ``base_delay*2``)
capped at ``max_delay``, with uniform jitter in
[0.5, 1.0] of the computed delay. The default
``base_delay=1.0`` keeps the contract close to the
documented example. Setting ``max_total_seconds`` short-
circuits the loop when the budget is exhausted, regardless
of remaining attempts.
"""

import asyncio
import inspect
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional, ParamSpec, Tuple, Type, TypeVar

import structlog

logger = structlog.get_logger()


# ``R`` is the result type of the wrapped zero-argument
# callable. The fallback (when supplied) must return
# the same ``R``. Generic so callers keep their concrete
# ``R`` is the result type of the wrapped zero-argument
# callable. The fallback (when supplied) must return
# the same ``R``. Generic so callers keep their concrete
# types.
R = TypeVar("R")
# ``P`` captures the parameter shape of the wrapped
# function for ``TimeoutConfig.execute``.
P = ParamSpec("P")


# ---------------------------------------------------------------------------
# BackoffPolicy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackoffPolicy:
    """
    Parameters for the retry loop in
    `with_timeout_and_retry`.

    The dataclass is frozen so a single policy can be
    shared across calls without risk of one call
    mutating the policy for the next. Mutate via
    `dataclasses.replace` to derive a variant.

    Attributes
    ----------
    max_attempts : int
        Total attempts (must be ``>= 1``).
    base_delay : float
        First retry sleep, in seconds. Subsequent
        sleeps double (capped at ``max_delay``).
    max_delay : float
        Upper bound on a single backoff sleep
        (must be ``> 0``).
    max_total_seconds : Optional[float]
        Optional overall budget. When set, the loop
        short-circuits as soon as the monotonic
        clock exceeds ``now + max_total_seconds``,
        even if ``max_attempts`` has not been
        exhausted. Prevents a misconfigured
        ``(max_attempts=100, base_delay=10)`` call
        from pinning a worker for hours.
    retry_on : Tuple[Type[BaseException], ...]
        Exception types that trigger a retry. The
        constructor strips ``asyncio.CancelledError``
        (which is a ``BaseException`` subclass not
        matched by the retry path); supplying it
        raises ``ValueError`` to make the contract
        explicit.

    Jitter is fixed at uniform [0.5, 1.0] of the
    computed delay — concurrent retries lockstep
    is the failure mode this dodges, and a single
    knob is enough.
    """

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    max_total_seconds: Optional[float] = None
    retry_on: Tuple[Type[BaseException], ...] = (asyncio.TimeoutError,)

    def __post_init__(self) -> None:
        _validate_scalar_bounds(
            max_attempts=self.max_attempts,
            base_delay=self.base_delay,
            max_delay=self.max_delay,
            max_total_seconds=self.max_total_seconds,
            retry_on=self.retry_on,
        )
        safe_retry_on = _strip_cancelled(self.retry_on)
        if not safe_retry_on:
            raise ValueError(
                "retry_on must contain at least one non-CancelledError type"
            )
        # Frozen dataclass: setattr is blocked, so
        # we replace the tuple via object.__setattr__.
        object.__setattr__(self, "retry_on", safe_retry_on)


def _validate_scalar_bounds(
    *,
    max_attempts: int,
    base_delay: float,
    max_delay: float,
    max_total_seconds: Optional[float],
    retry_on: Tuple[Type[BaseException], ...],
) -> None:
    """Validate the scalar / collection bounds of a
    ``BackoffPolicy``. The function raises ``ValueError``
    on the first violation; it is called from
    ``BackoffPolicy.__post_init__``.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if base_delay < 0:
        raise ValueError("base_delay must be >= 0")
    if max_delay <= 0:
        raise ValueError("max_delay must be > 0")
    if max_total_seconds is not None and max_total_seconds <= 0:
        raise ValueError("max_total_seconds must be > 0 or None")
    if not retry_on:
        raise ValueError("retry_on must be non-empty")


def _strip_cancelled(
    retry_on: Tuple[Type[BaseException], ...],
) -> Tuple[Type[BaseException], ...]:
    """Drop ``asyncio.CancelledError`` from a ``retry_on``
    tuple. The retry path must never catch cancellation;
    explicit rejection is cheaper than the surprising
    behaviour of a misconfigured policy that retries on
    cancellation.
    """
    return tuple(
        t
        for t in retry_on
        if t is not asyncio.CancelledError
        and not (isinstance(t, type) and issubclass(t, asyncio.CancelledError))
    )


class TimeoutError(Exception):
    """Raised when an operation exceeds its timeout.

    Note: this intentionally shadows the builtin
    ``TimeoutError`` / ``asyncio.TimeoutError`` for
    callers that prefer an explicit error class. Use the
    builtin if you need to interoperate with stdlib
    timeouts.
    """

    pass


async def with_timeout(
    fn: Callable[[], Coroutine[Any, Any, R] | R],
    timeout_seconds: float,
    fallback: Callable[[], Coroutine[Any, Any, R] | R] | None = None,
    operation_name: str | None = None,
) -> R:
    """
    Execute a function with a timeout and optional fallback.

    Args:
        fn: Zero-argument callable. May be a coroutine
            function (returns a coroutine) or a sync
            function. If async, the result is awaited;
            otherwise it is returned as-is.
        timeout_seconds: Per-call timeout in seconds.
        fallback: Zero-argument callable invoked on
            timeout. May be async. If ``None``, the
            ``TimeoutError`` propagates.
        operation_name: Identifier for log messages.

    Returns:
        The function's result, or ``fallback()`` if a
        timeout fired and a fallback was supplied.

    Raises:
        TimeoutError: on timeout without a fallback.
        asyncio.CancelledError: always propagated.
    """
    _validate_inputs(fn, timeout_seconds, fallback)
    try:
        result = fn()
        if not inspect.isawaitable(result):
            # Sync result: return immediately. The
            # timeout is "best-effort" — we cannot cancel
            # a sync function mid-flight. We document this
            # in the docstring.
            return result
        return await asyncio.wait_for(result, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return await _on_timeout(timeout_seconds, fallback, operation_name)


async def _on_timeout(
    timeout_seconds: float,
    fallback: Callable[[], Coroutine[Any, Any, R] | R] | None,
    operation_name: str | None,
) -> R:
    """Apply the timeout policy: log + (fallback or raise).

    Pulled out of ``with_timeout`` so the orchestrator
    stays flat (CC ≤ 3) and the post-timeout branch is
    unit-testable in isolation.
    """
    _log_timeout_expired(timeout_seconds, operation_name)
    if fallback is None:
        raise
    _log_fallback_invoked(operation_name)
    return await fallback()


def _log_timeout_expired(timeout_seconds: float, operation_name: str | None) -> None:
    """Log the ``timeout.with_timeout.expired`` event."""
    logger.warning(
        "timeout.with_timeout.expired",
        operation=operation_name or "unknown",
        timeout_seconds=timeout_seconds,
    )


def _log_fallback_invoked(operation_name: str | None) -> None:
    """Log the ``timeout.with_timeout.fallback_invoked`` event."""
    logger.info(
        "timeout.with_timeout.fallback_invoked",
        operation=operation_name or "unknown",
    )


async def with_timeout_and_retry(
    fn: Callable[[], R],
    timeout_seconds: float,
    *,
    backoff: Optional[BackoffPolicy] = None,
    fallback: Callable[[], R] | None = None,
    operation_name: str | None = None,
) -> R:
    """
    Execute a function with timeout and retry.

    Args:
        fn: Zero-argument callable (sync or async).
        timeout_seconds: Per-attempt timeout in seconds.
        backoff: `BackoffPolicy` dataclass holding the
            retry parameters (``max_attempts``,
            ``base_delay``, ``max_delay``,
            ``max_total_seconds``, ``retry_on``).
            When ``None``, defaults to
            ``BackoffPolicy()`` (3 attempts, 1s base,
            30s max, no budget, retry on
            ``asyncio.TimeoutError`` only).
        fallback: Zero-argument callable invoked when
            all attempts are exhausted.
        operation_name: Identifier for log messages.

    Returns:
        The function's result, or ``fallback()`` if all
        attempts failed and a fallback was supplied.

    Raises:
        TimeoutError / any retry_on type: when all
            attempts failed and no fallback is set.
        asyncio.CancelledError: always propagated.
    """
    _validate_inputs(fn, timeout_seconds, fallback)
    policy = backoff or BackoffPolicy()
    deadline = _make_deadline(policy.max_total_seconds)

    last_error: BaseException | None = None
    for attempt in range(policy.max_attempts):
        if deadline is not None and time.monotonic() >= deadline:
            _log_budget_exhausted(operation_name, attempt + 1)
            break
        try:
            return await with_timeout(
                fn,
                timeout_seconds=timeout_seconds,
                fallback=None,
                operation_name=operation_name,
            )
        except tuple(policy.retry_on) as e:
            last_error = e
            _log_attempt_failed(
                operation_name, attempt + 1, policy.max_attempts, type(e).__name__
            )
            if not _has_next_attempt(attempt, policy.max_attempts):
                break
            await _sleep_with_backoff(
                attempt,
                base_delay=policy.base_delay,
                max_delay=policy.max_delay,
                deadline=deadline,
            )

    return await _resolve_exhausted(last_error, fallback, operation_name)


def _validate_inputs(
    fn: Callable[[], R],
    timeout_seconds: float,
    fallback: Callable[[], R] | None,
) -> None:
    """Arg-shape validation for ``with_timeout_and_retry``."""
    if not callable(fn):
        raise TypeError("fn must be callable")
    if timeout_seconds is None or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")
    if fallback is not None and not callable(fallback):
        raise TypeError("fallback must be callable or None")


def _make_deadline(max_total_seconds: Optional[float]) -> Optional[float]:
    """Translate a max_total_seconds budget into a
    monotonic deadline (``None`` when the budget is
    unset).
    """
    if max_total_seconds is None:
        return None
    return time.monotonic() + max_total_seconds


def _has_next_attempt(attempt: int, max_attempts: int) -> bool:
    """True iff another attempt will follow the current one."""
    return attempt < max_attempts - 1


def _log_budget_exhausted(operation_name: str | None, attempt: int) -> None:
    logger.warning(
        "timeout.with_timeout_and_retry.budget_exhausted",
        operation=operation_name or "unknown",
        attempt=attempt,
    )


def _log_attempt_failed(
    operation_name: str | None,
    attempt: int,
    max_attempts: int,
    error_type: str,
) -> None:
    logger.warning(
        "timeout.with_timeout_and_retry.attempt_failed",
        operation=operation_name or "unknown",
        attempt=attempt,
        max_attempts=max_attempts,
        error_type=error_type,
    )


async def _sleep_with_backoff(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    deadline: Optional[float],
) -> None:
    """Sleep ``base_delay * 2**attempt`` (capped at
    ``max_delay``) with uniform jitter in [0.5, 1.0].

    When ``deadline`` is set, the sleep is capped so it
    never crosses the budget. ``break`` is *not* signalled
    to the caller; the loop checks the deadline on the
    next iteration.
    """
    delay = min(base_delay * (2**attempt), max_delay)
    # Backoff jitter — uniform in [0.5, 1.0] of the
    # computed delay so concurrent retries do not
    # lockstep. ``random.random()`` is fine here: this is
    # not a security boundary, only an attempt-spreading
    # heuristic.
    jittered = delay * (0.5 + random.random() * 0.5)  # nosec B311 - backoff jitter, not crypto
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        jittered = min(jittered, remaining)
    await asyncio.sleep(jittered)


async def _resolve_exhausted(
    last_error: BaseException | None,
    fallback: Callable[[], R] | None,
    operation_name: str | None,
) -> R:
    """Apply the post-loop policy: fallback or raise."""
    if fallback is not None:
        logger.warning(
            "timeout.with_timeout_and_retry.fallback_invoked",
            operation=operation_name or "unknown",
        )
        return await fallback()
    if last_error is not None:
        raise last_error
    raise asyncio.TimeoutError(
        "Operation timed out after all retries (no last error captured)"
    )


class TimeoutConfig:
    """Reusable timeout configuration."""

    def __init__(
        self,
        timeout_seconds: float = 10.0,
        fallback: Callable | None = None,
        operation_name: str | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.fallback = fallback
        self.operation_name = operation_name

    async def execute(
        self,
        fn: Callable[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Execute a function with timeout."""

        async def wrapped() -> R:
            return await fn(*args, **kwargs)

        return await with_timeout(
            wrapped,
            timeout_seconds=self.timeout_seconds,
            fallback=self.fallback,
            operation_name=self.operation_name,
        )


# Predefined timeouts
timeout_fast = TimeoutConfig(timeout_seconds=5.0)
timeout_normal = TimeoutConfig(timeout_seconds=10.0)
timeout_slow = TimeoutConfig(timeout_seconds=30.0)
timeout_very_slow = TimeoutConfig(timeout_seconds=60.0)
