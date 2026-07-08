# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for code review item #7:
BulkheadPool.__aexit__ double-decrement.

The async-context-manager protocol on BulkheadPool had a
double-decrement bug: __aenter__ internally calls
`execute(lambda: None)` which already increments and
decrements `active` AND acquires/releases the semaphore.
Then __aexit__ decremented and released AGAIN.

Net effect of a single `async with bulkhead:`:
  - `active` ended at -1
  - the semaphore had one extra permit (max_concurrent + 1)
  - get_stats() reported a negative active count

These tests pin the contract that:
  - `async with bulkhead:` (no execute in body) leaves the
    pool in a clean state.
  - `bulkhead.execute(fn)` is the canonical entry point and
    does not double-count.
  - Stats are accurate after a series of operations.
  - The semaphore's permit count matches `max_concurrent`
    after the context manager exits.

The tests are in unit/ because BulkheadPool is pure in-memory
(no Redis, no external resources).
"""

from __future__ import annotations

import asyncio

import pytest

from kntgraph.resilience.bulkhead import BulkheadPool

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _noop() -> None:
    """A no-op coroutine used to fill the bulkhead."""
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Bug: double-decrement after `async with bulkhead:`
# ---------------------------------------------------------------------------


class TestAsyncWithCleanExit:
    async def test_active_is_zero_after_empty_async_with(self):
        """
        `async with bulkhead:` (no execute in body) must
        leave `active` at zero. Currently, the pool ends
        with `active == -1` because both __aenter__ (via
        `execute(lambda: None)`) AND __aexit__ decrement.
        """
        b = BulkheadPool("t-1", max_concurrent=3)
        async with b:
            pass
        assert b.active == 0, (
            f"active should be 0 after empty `async with`, got {b.active}"
        )

    async def test_async_with_runs_body(self):
        """
        The body of `async with bulkhead:` must run; the
        previous implementation called `execute(lambda: None)`
        which raised ``BusinessError: object NoneType can't
        be used in 'await' expression`` on every entry.
        """
        b = BulkheadPool("t-1", max_concurrent=3)
        executed: list[int] = []

        async def work():
            executed.append(1)
            return "ok"

        async with b:
            await work()

        assert executed == [1]
        assert b.active == 0

    async def test_async_with_raises_on_full(self):
        """
        With max_concurrent=1, a second concurrent
        `async with` must raise ``BusinessError`` (or
        return Err) on entry — the body must NOT run.
        """
        from kntgraph.core.result import BusinessError

        b = BulkheadPool("t-1", max_concurrent=1)

        async def slow():
            await asyncio.sleep(0.5)
            return "slow"

        async def first_enter():
            async with b:
                await asyncio.sleep(0.5)

        first = asyncio.create_task(first_enter())
        await asyncio.sleep(0.05)
        # Now the bulkhead is full. The second enter must
        # fail (raise) within the 0.1s acquire timeout
        # because the first holds the only slot for 0.5s.
        with pytest.raises(BusinessError):
            async with b:
                await slow()
        await first
        # The first enter exited cleanly.
        assert b.active == 0

    async def test_semaphore_value_is_max_concurrent_after_async_with(self):
        """
        The semaphore must hold exactly `max_concurrent`
        permits after the context manager exits. The
        double-release bug would leave it at
        `max_concurrent + 1`.
        """
        b = BulkheadPool("t-1", max_concurrent=3)
        async with b:
            pass
        # The semaphore does not expose a public `value`,
        # but we can probe by acquiring without blocking.
        acquired = []
        for _ in range(b.max_concurrent + 2):
            try:
                await asyncio.wait_for(b.semaphore.acquire(), timeout=0.01)
                acquired.append(True)
            except asyncio.TimeoutError:
                break
        # The pool was supposed to leave exactly `max_concurrent`
        # permits. If the bug is present, we get one extra.
        assert len(acquired) == b.max_concurrent, (
            f"semaphore value after `async with` is wrong: "
            f"acquired {len(acquired)} permits, expected "
            f"{b.max_concurrent} (the bug leaves 1 extra)"
        )
        # Clean up: release what we acquired.
        for _ in acquired:
            b.semaphore.release()

    async def test_stats_after_empty_async_with(self):
        """
        The reported `active` field must match reality.
        After a single empty `async with`, the pool has
        nothing running.
        """
        b = BulkheadPool("t-1", max_concurrent=3)
        async with b:
            pass
        stats = b.get_stats()
        assert stats["active"] == 0
        assert stats["utilization"] == 0.0


class TestExecuteCorrectness:
    async def test_execute_returns_ok_on_success(self):
        b = BulkheadPool("t-1", max_concurrent=2)

        async def good():
            return 42

        result = await b.execute(good)
        assert result.is_ok()
        assert result.ok_value() == 42
        assert b.active == 0

    async def test_execute_increments_and_decrements_active(self):
        """
        During execution, `active` should be 1; after,
        it should be 0.
        """
        b = BulkheadPool("t-1", max_concurrent=2)
        observed: list[int] = []

        async def probe():
            observed.append(b.active)
            return "ok"

        await b.execute(probe)
        assert observed == [1], (
            f"expected active=1 during execution, observed {observed}"
        )
        assert b.active == 0

    async def test_execute_returns_err_on_exception(self):
        b = BulkheadPool("t-1", max_concurrent=2)

        async def boom():
            raise RuntimeError("kaboom")

        result = await b.execute(boom)
        assert result.is_err()
        assert b.total_failed == 1
        assert b.active == 0

    async def test_execute_returns_err_when_full(self):
        """
        With max_concurrent=1, the second concurrent call
        must be rejected (Err) within the small acquire
        timeout.
        """
        b = BulkheadPool("t-1", max_concurrent=1)

        async def slow():
            await asyncio.sleep(0.5)
            return "slow"

        # Start one slow call (it occupies the only slot).
        first = asyncio.create_task(b.execute(slow))
        # Let the first call acquire the semaphore.
        await asyncio.sleep(0.05)
        # Now the second call must be rejected (the slot
        # is still held for another ~450ms; the acquire
        # timeout is 100ms).
        result = await b.execute(_noop)
        assert result.is_err()
        # Wait for the first to finish so the test is
        # deterministic.
        await first
        assert b.total_rejected == 1
        assert b.total_executed == 1
        assert b.active == 0


class TestRejectionDoesNotCorruptCounters:
    async def test_rejected_call_does_not_increment_active(self):
        """
        A rejected call must NOT increment `active`. The
        acquire timeout returns Err before the `try` block
        that does `active += 1`.
        """
        b = BulkheadPool("t-1", max_concurrent=1)

        async def slow():
            await asyncio.sleep(0.5)
            return "slow"

        first = asyncio.create_task(b.execute(slow))
        await asyncio.sleep(0.05)
        result = await b.execute(_noop)
        assert result.is_err()
        # Wait for the first call to finish before checking
        # `active`: while the first call runs, `active` is
        # legitimately 1.
        await first
        assert b.active == 0, f"rejected call leaked active count: {b.active}"


class TestSemaphoreInvariant:
    async def test_semaphore_returns_to_max_after_mixed_workload(self):
        """
        After a series of successful and rejected calls,
        the semaphore must return to exactly `max_concurrent`
        permits (no leaks, no extra permits).
        """
        b = BulkheadPool("t-1", max_concurrent=1)

        async def good():
            return 1

        # 5 successful calls
        for _ in range(5):
            r = await b.execute(good)
            assert r.is_ok()

        # 5 rejected calls (the semaphore is never held here)
        for _ in range(5):

            async def hold():
                await asyncio.sleep(0.5)

            first = asyncio.create_task(b.execute(hold))
            await asyncio.sleep(0.05)
            r = await b.execute(_noop)
            assert r.is_err()
            await first

        # Probe the semaphore
        acquired = []
        for _ in range(b.max_concurrent + 2):
            try:
                await asyncio.wait_for(b.semaphore.acquire(), timeout=0.01)
                acquired.append(True)
            except asyncio.TimeoutError:
                break
        for _ in acquired:
            b.semaphore.release()
        assert len(acquired) == b.max_concurrent, (
            f"semaphore corrupted: {len(acquired)} permits, expected {b.max_concurrent}"
        )
        assert b.active == 0
