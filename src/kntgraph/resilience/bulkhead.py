# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Bulkhead pattern for resource isolation.

Uses the Railway Pattern for error handling.
"""

import asyncio
from collections import OrderedDict
from types import TracebackType
from typing import Callable, ParamSpec, TypeVar

import structlog

from ..core.agent_id import AGENT_ID_RE
from ..core.result import Result, Ok, Err, BusinessError

# ``R`` is the return type of the wrapped function;
# ``P`` captures its parameter shape. The wrapper is
# generic so callers keep their concrete types instead
# of inheriting an unbounded escape hatch.
R = TypeVar("R")
P = ParamSpec("P")

logger = structlog.get_logger()


class BulkheadFullError(Exception):
    """Raised when the bulkhead is full.

    The Railway-Pattern ``execute`` returns
    ``Err(BusinessError)`` instead of raising this, but
    the class is exported for callers that want to
    ``raise`` it explicitly.
    """

    pass


# Reject new acquisitions after this many seconds. Keeps the
# rejected-call latency bounded under saturation.
DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 0.1

# Hard cap on the number of bulkheads the global registry
# may hold. An attacker that can call ``get_bulkhead`` with
# arbitrary keys (e.g. via a tenant header) used to be able
# to grow the dict without bound; LRU eviction caps the
# memory footprint at ``_MAX_BULKHEADS`` entries.
_MAX_BULKHEADS = 1024


class BulkheadPool:
    """
    Bulkhead pattern to isolate resources per tenant/agent.

    Prevents one tenant from affecting others (noisy
    neighbour).

    Two usage shapes are supported:

      1. ``await pool.execute(fn, *args, **kwargs)`` —
         canonical entry point. Returns ``Result``; never
         blocks on exceptions inside ``fn``; rejects on
         saturation with a short acquire timeout.

      2. ``async with pool:`` — acquires one slot, runs the
         body, releases on exit. Rejected with a
         ``BusinessError`` if the pool is full (the error
         propagates into the ``async with`` body).

    Both shapes share the SAME semaphore and counters. The
    counters are incremented exactly once per slot acquisition
    and decremented exactly once per release.
    """

    def __init__(
        self,
        name: str,
        max_concurrent: int = 50,
        acquire_timeout: float = DEFAULT_ACQUIRE_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(name, str) or not AGENT_ID_RE.match(name):
            raise ValueError(f"bulkhead name must match {AGENT_ID_RE.pattern}")
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if acquire_timeout <= 0:
            raise ValueError("acquire_timeout must be > 0")
        self.name = name
        self.max_concurrent = max_concurrent
        self.acquire_timeout = acquire_timeout
        self.semaphore = asyncio.Semaphore(max_concurrent)

        # Stats. ``active`` is the running count of in-flight
        # calls. It must match the number of un-acquired-by-not-released
        # permits: ``active == max_concurrent - permits_available``.
        self.active = 0
        self.total_executed = 0
        self.total_rejected = 0
        self.total_failed = 0

    # ------------------------------------------------------------------ internal

    async def _acquire(self) -> bool:
        """
        Acquire one slot. Returns True on success, False on
        rejection (full within the acquire timeout).

        On success, ``active`` is incremented. The caller MUST
        pair every successful ``_acquire`` with exactly one
        ``_release``.
        """
        try:
            await asyncio.wait_for(
                self.semaphore.acquire(),
                timeout=self.acquire_timeout,
            )
        except asyncio.TimeoutError:
            self.total_rejected += 1
            logger.warning(
                "Bulkhead full - request rejected",
                name=self.name,
                active=self.active,
                max_concurrent=self.max_concurrent,
            )
            return False
        self.active += 1
        return True

    def _release(self) -> None:
        """
        Release one slot. Counterpart to a successful
        ``_acquire``. Decrements ``active`` and releases the
        semaphore permit.
        """
        self.active -= 1
        self.semaphore.release()

    # ------------------------------------------------------------------ canonical

    async def execute(
        self,
        fn: Callable[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Result[R, BusinessError]:
        """
        Execute a function with a concurrency limit.

        Railway Pattern: returns a ``Result`` instead of
        raising (except ``asyncio.CancelledError``, which
        propagates for clean task cancellation).

        Args:
            fn: Async or sync function. If async, it is
                awaited. If sync, it is called and the
                return value is wrapped in ``Ok`` without
                awaiting.
            *args: Positional arguments.
            **kwargs: Keyword arguments.

        Returns:
            ``Ok(value)`` on success, ``Err(BusinessError)``
            on rejection or on a raised exception inside ``fn``.
        """
        if not await self._acquire():
            return Err(
                BusinessError(
                    f"Bulkhead '{self.name}' is full ({self.max_concurrent} concurrent)"
                )
            )

        try:
            self.total_executed += 1
            result = fn(*args, **kwargs)
            # If ``fn`` returned a coroutine, await it. If it
            # returned a plain value, pass it through.
            if asyncio.iscoroutine(result):
                result = await result
            return Ok(result)
        except asyncio.CancelledError:
            # Task was cancelled mid-execution. Propagate so
            # the task ends properly. The ``finally`` block
            # still releases the slot.
            raise
        except Exception as e:
            self.total_failed += 1
            logger.warning(
                "Bulkhead execution failed",
                name=self.name,
                error=str(e),
            )
            return Err(BusinessError(f"Bulkhead execution failed: {e}"))
        finally:
            self._release()

    # ------------------------------------------------------------------ context manager

    async def __aenter__(self) -> "BulkheadPool":
        """
        Acquire one slot for the duration of the ``async with``
        block. If the pool is full, ``BusinessError`` is
        raised BEFORE the body runs (so the user does not
        see partial work).
        """
        if not await self._acquire():
            raise BusinessError(
                f"Bulkhead '{self.name}' is full ({self.max_concurrent} concurrent)"
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """
        Release the slot acquired in ``__aenter__``. Counts
        as one execution (in the sense that the slot was
        used) but does NOT increment ``total_executed`` (which
        tracks calls to ``execute``).
        """
        self._release()

    # ------------------------------------------------------------------ stats

    def get_stats(self) -> dict:
        """Return stats for monitoring."""
        return {
            "name": self.name,
            "active": self.active,
            "max_concurrent": self.max_concurrent,
            "total_executed": self.total_executed,
            "total_rejected": self.total_rejected,
            "total_failed": self.total_failed,
            "utilization": (
                self.active / self.max_concurrent if self.max_concurrent > 0 else 0
            ),
        }

    def is_healthy(self) -> bool:
        """Check whether the bulkhead is healthy."""
        rejection_rate = (
            self.total_rejected / self.total_executed if self.total_executed > 0 else 0
        )
        return rejection_rate < 0.1  # < 10% rejection


# Global registry of bulkheads.
#
# Bounded LRU (max ``_MAX_BULKHEADS`` entries) so an attacker
# that can call ``get_bulkhead`` with arbitrary keys cannot
# grow the dict without bound. ``tenant_id`` is validated at
# construction (see ``AGENT_ID_RE``), and ``get_bulkhead``
# rejects keys that fail the shape check before they reach the
# registry.
_bulkheads: "OrderedDict[str, BulkheadPool]" = OrderedDict()
_bulkheads_lock = asyncio.Lock()


async def get_bulkhead(tenant_id: str, max_concurrent: int = 50) -> BulkheadPool:
    """
    Get or create a bulkhead for a tenant.

    Args:
        tenant_id: Tenant identifier. Must match
            ``AGENT_ID_RE`` (ASCII identifier, 1-128
            chars; ``[A-Za-z0-9._:-]``).
        max_concurrent: Maximum concurrent executions.

    Returns:
        A ``BulkheadPool`` instance (always the same for a
        given ``tenant_id``).

    Raises:
        ValueError: if ``tenant_id`` does not match the
            shape regex. The registry is the trust boundary
            between an external caller (which may carry a
            tenant header from an HTTP request) and the
            internal pool keyed on the tenant; we reject
            malformed keys at this seam instead of letting
            them accumulate.
    """
    if not isinstance(tenant_id, str) or not AGENT_ID_RE.match(tenant_id):
        raise ValueError(f"tenant_id must match {AGENT_ID_RE.pattern}")
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be >= 1")
    async with _bulkheads_lock:
        pool = _bulkheads.get(tenant_id)
        if pool is not None:
            _bulkheads.move_to_end(tenant_id)
            return pool
        if len(_bulkheads) >= _MAX_BULKHEADS:
            evicted_id, _ = _bulkheads.popitem(last=False)
            logger.warning(
                "bulkhead.registry_evict",
                evicted_tenant=evicted_id,
                registry_size=len(_bulkheads),
            )
        pool = BulkheadPool(tenant_id, max_concurrent)
        _bulkheads[tenant_id] = pool
        return pool


def get_all_bulkheads() -> dict[str, BulkheadPool]:
    """Return a shallow copy of the registry."""
    return dict(_bulkheads)


async def remove_bulkhead(tenant_id: str) -> None:
    """Remove a bulkhead (cleanup). Idempotent.

    Does NOT wait for in-flight work; the semaphore is
    dropped and the in-flight call's eventual release
    will free a now-unreferenced permit (which the GC
    will collect). Callers that care about in-flight
    work MUST drain before removing.
    """
    async with _bulkheads_lock:
        _bulkheads.pop(tenant_id, None)
