# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the resilience module rewrites.

These tests pin the contracts that were either missing or
broken before the audit fixes (June 2026):

  - ``retry_with_backoff`` actually awaits coroutines
    (the previous sync-based ``tenacity.retry`` returned a
    coroutine object to the caller).
  - ``retry_async`` is async and returns the awaited
    result, not a coroutine.
  - ``CancelledError`` propagates through retry / timeout
    / fallback / circuit-breaker and is NOT counted as a
    failure.
  - ``CircuitBreaker`` admits only ``half_open_max_calls``
    concurrent probes (the previous implementation let
    every coroutine through when the breaker was OPEN and
    the recovery timeout had elapsed).
  - ``CircuitBreaker`` measures ``recovery_timeout`` on
    ``time.monotonic()`` and is not affected by wall-clock
    drift (we mock ``time.monotonic`` to advance it).
  - ``bulkhead`` registry is bounded by ``_MAX_BULKHEADS``
    and rejects malformed ``tenant_id`` keys.
  - ``with_timeout_and_retry`` honours ``base_delay`` (the
    previous hard-coded ``2**attempt`` ignored the
    parameter) and respects ``max_total_seconds``.
  - ``with_fallback`` logs the exception TYPE only, never
    the full ``str(exception)`` (PII guard).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from kntgraph.resilience import (
    BackoffPolicy,
    BulkheadPool,
    CircuitBreaker,
    CircuitState,
    get_bulkhead,
    get_circuit_breaker,
    remove_bulkhead,
    retry_async,
    retry_with_backoff,
    with_fallback,
    with_timeout_and_retry,
)

# `with_timeout_and_retry` raises ``asyncio.TimeoutError``
# (the builtin since 3.11). We import the resilience
# module's TimeoutError separately if we need to assert
# against the legacy class — for these tests the builtin
# is sufficient.
ResilienceTimeoutError = TimeoutError  # alias for readability

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# retry: must actually await coroutines
# ---------------------------------------------------------------------------


class TestRetryAwaitsCoroutines:
    async def test_retry_with_backoff_returns_value_not_coroutine(self):
        """
        Pin the regression: the previous sync ``tenacity.retry``
        returned a coroutine OBJECT to the caller.
        """

        @retry_with_backoff(max_attempts=2, base_delay=0.01)
        async def fn() -> str:
            return "ok"

        result = fn()
        # The wrapped function must itself be a coroutine
        # function (so callers can `await fn()`).
        assert asyncio.iscoroutinefunction(fn), (
            "retry_with_backoff must preserve async-ness"
        )
        # Awaiting yields the value, not a coroutine object.
        assert await result == "ok"

    async def test_retry_with_backoff_retries_on_failure(self):
        attempts: list[int] = []

        @retry_with_backoff(max_attempts=3, base_delay=0.01, max_delay=0.05)
        async def fn() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("boom")
            return "ok"

        result = await fn()
        assert result == "ok"
        assert len(attempts) == 3

    async def test_retry_async_awaits_result(self):
        async def fn(x: int) -> int:
            return x * 2

        result = await retry_async(fn, 21, max_attempts=1, base_delay=0)
        assert result == 42

    async def test_retry_propagates_cancellation(self):
        @retry_with_backoff(max_attempts=5, base_delay=0.01)
        async def fn() -> None:
            await asyncio.sleep(0.05)
            raise ConnectionError("retryable")

        task = asyncio.create_task(fn())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# timeout: base_delay honoured + budget respected + cancellation
# ---------------------------------------------------------------------------


class TestTimeoutBackoff:
    async def test_with_timeout_and_retry_uses_base_delay(self):
        """
        The previous implementation hard-coded
        ``2**attempt`` and ignored ``base_delay``. With
        ``base_delay=0.5`` and ``max_attempts=3``, the
        first sleep must be ~0.5s (not 1s), the second
        ~1s. We use generous tolerances to avoid flakes.
        """
        sleeps: list[float] = []

        real_sleep = asyncio.sleep

        async def fake_sleep(d: float) -> None:
            sleeps.append(d)
            # Cap each fake sleep so the test is fast.
            await real_sleep(min(d, 0.02))

        async def slow_fn() -> None:
            raise ResilienceTimeoutError("nope")

        with patch("kntgraph.resilience.timeout.asyncio.sleep", fake_sleep):
            with pytest.raises(ResilienceTimeoutError):
                await with_timeout_and_retry(
                    slow_fn,
                    timeout_seconds=0.001,
                    backoff=BackoffPolicy(
                        max_attempts=3,
                        base_delay=0.5,
                        max_delay=10.0,
                        retry_on=(ResilienceTimeoutError,),
                    ),
                )

        # Two sleeps (between attempts 1-2 and 2-3).
        assert len(sleeps) == 2
        # Jitter is in [0.5, 1.0] of the computed delay;
        # the base is 0.5 then 1.0.
        assert 0.25 <= sleeps[0] <= 0.5
        assert 0.5 <= sleeps[1] <= 1.0

    async def test_max_total_seconds_short_circuits(self):
        sleeps: list[float] = []

        real_sleep = asyncio.sleep

        async def fake_sleep(d: float) -> None:
            sleeps.append(d)
            await real_sleep(min(d, 0.02))

        async def slow_fn() -> None:
            raise ResilienceTimeoutError("nope")

        with patch("kntgraph.resilience.timeout.asyncio.sleep", fake_sleep):
            # 10 attempts allowed but only 0.05s budget.
            with pytest.raises(ResilienceTimeoutError):
                await with_timeout_and_retry(
                    slow_fn,
                    timeout_seconds=0.001,
                    backoff=BackoffPolicy(
                        max_attempts=10,
                        base_delay=10.0,
                        max_delay=60.0,
                        max_total_seconds=0.05,
                        retry_on=(ResilienceTimeoutError,),
                    ),
                )

        # The cap on `max_total_seconds` must prevent the
        # unbounded sleeps.
        assert all(s <= 0.05 for s in sleeps), f"sleeps exceeded budget: {sleeps}"


# ---------------------------------------------------------------------------
# circuit breaker: half-open race + monotonic clock + cancellation
# ---------------------------------------------------------------------------


class TestCircuitBreakerHalfOpen:
    async def test_half_open_admits_only_max_concurrent_probes(self):
        """
        100 concurrent calls arrive when the breaker is
        OPEN and the recovery timeout has just elapsed.
        Exactly ``half_open_max_calls`` are admitted; the
        rest return ``Err`` without running.
        """
        cb = CircuitBreaker(
            "test",
            failure_threshold=1,
            recovery_timeout_seconds=0.01,
            half_open_max_calls=3,
        )
        ran: list[int] = []

        async def slow() -> str:
            ran.append(1)
            # Hold the slot long enough for every other
            # coroutine to arrive at _admit().
            await asyncio.sleep(0.05)
            return "ok"

        # First, trip the breaker.
        async def boom() -> None:
            raise ConnectionError("down")

        await cb.call(boom)

        assert cb.state == CircuitState.OPEN

        # Wait for the recovery_timeout to elapse.
        await asyncio.sleep(0.02)

        # Now fire 100 concurrent calls. Only 3 should
        # actually run; the other 97 must return Err
        # immediately.
        tasks = [asyncio.create_task(cb.call(slow)) for _ in range(100)]
        results = await asyncio.gather(*tasks)

        ok_count = sum(1 for r in results if r.is_ok())
        err_count = sum(1 for r in results if r.is_err())
        assert ok_count == 3, f"expected 3 admitted, got {ok_count}"
        assert err_count == 97
        # Sanity: the slow function only ran 3 times.
        assert len(ran) == 3, f"slow() ran {len(ran)} times"

    async def test_recovery_uses_monotonic_clock(self):
        """
        Mock ``time.monotonic`` to verify the breaker does
        NOT consult ``datetime.now``.
        """
        cb = CircuitBreaker(
            "test",
            failure_threshold=1,
            recovery_timeout_seconds=10.0,
        )

        async def boom() -> None:
            raise ConnectionError("down")

        # Trip the breaker at monotonic=100.0
        with patch(
            "kntgraph.resilience.circuit_breaker.time.monotonic",
            return_value=100.0,
        ):
            await cb.call(boom)
        assert cb.state == CircuitState.OPEN

        # Without enough monotonic time elapsed, stay open.
        with patch(
            "kntgraph.resilience.circuit_breaker.time.monotonic",
            return_value=105.0,
        ):

            async def good() -> str:
                return "ok"

            r = await cb.call(good)
            assert r.is_err()

        # Now advance monotonic past recovery_timeout.
        with patch(
            "kntgraph.resilience.circuit_breaker.time.monotonic",
            return_value=111.0,
        ):
            r = await cb.call(good)
            assert r.is_ok()
        # The single successful probe leaves the breaker
        # in HALF_OPEN (half_open_max_calls defaults to 3).
        assert cb.state == CircuitState.HALF_OPEN

    async def test_cancellation_not_counted_as_failure(self):
        cb = CircuitBreaker(
            "test",
            failure_threshold=2,
            recovery_timeout_seconds=10.0,
        )

        async def slow() -> None:
            await asyncio.sleep(0.05)
            raise ConnectionError("down")

        task = asyncio.create_task(cb.call(slow))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Cancellation must NOT have been counted as a
        # failure.
        assert cb.failure_count == 0
        assert cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# bulkhead: bounded registry + key validation
# ---------------------------------------------------------------------------


class TestBulkheadRegistry:
    async def test_invalid_tenant_id_rejected(self):
        with pytest.raises(ValueError):
            await get_bulkhead("a" * 200)
        with pytest.raises(ValueError):
            await get_bulkhead("tenant with spaces")
        with pytest.raises(ValueError):
            await get_bulkhead("")
        # Valid key works.
        pool = await get_bulkhead("tenant-1")
        assert isinstance(pool, BulkheadPool)

    async def test_registry_is_bounded_by_max_bulkheads(self):
        # We do not want to create 1025 pools in a test
        # (slow + memory). Patch the cap to 4.
        from kntgraph.resilience import bulkhead as bh_mod

        original = bh_mod._MAX_BULKHEADS
        bh_mod._MAX_BULKHEADS = 4
        try:
            await get_bulkhead("t-1")
            await get_bulkhead("t-2")
            await get_bulkhead("t-3")
            await get_bulkhead("t-4")
            # 5th call must evict the LRU (t-1).
            await get_bulkhead("t-5")
            assert "t-1" not in bh_mod._bulkheads
            assert "t-5" in bh_mod._bulkheads
        finally:
            bh_mod._MAX_BULKHEADS = original

    async def test_remove_bulkhead_is_async_and_idempotent(self):
        await get_bulkhead("t-x")
        await remove_bulkhead("t-x")
        await remove_bulkhead("t-x")  # idempotent
        await remove_bulkhead("never-existed")


# ---------------------------------------------------------------------------
# fallback: PII guard (no str(exception) in logs)
# ---------------------------------------------------------------------------


class TestFallbackPiiGuard:
    async def test_with_fallback_logs_type_not_str(self, capsys):
        # The structlog pipeline writes to stdout/stderr
        # via the default factory; we use capsys to
        # capture and assert the message does NOT contain
        # the user-controlled payload.
        user_payload = "user_id=12345 leaked into logs"

        async def primary() -> str:
            raise ValueError(user_payload)

        async def secondary() -> str:
            return "fallback"

        result = await with_fallback(primary, secondary, operation_name="user.fetch")
        assert result == "fallback"

        captured = capsys.readouterr().out + capsys.readouterr().err
        assert user_payload not in captured, (
            f"exception str leaked into logs: {captured!r}"
        )


# ---------------------------------------------------------------------------
# circuit breaker: registry is async + validates name
# ---------------------------------------------------------------------------


class TestCircuitBreakerRegistry:
    async def test_invalid_name_rejected(self):
        with pytest.raises(ValueError):
            await get_circuit_breaker("")
        with pytest.raises(ValueError):
            await get_circuit_breaker("  ")

    async def test_registry_returns_same_instance(self):
        cb1 = await get_circuit_breaker("llm")
        cb2 = await get_circuit_breaker("llm")
        assert cb1 is cb2
