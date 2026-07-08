# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for ``kntgraph.resilience.rate_limit.RateLimiter``.

Originally in ``fmh_core/tests/test_rate_limit.py``
(standalone package). Moved here when the package was
merged into ``kntgraph``. The class is the shared
primitive used by both the HTTP middleware in
``kntgraph`` and the LLM throttling in
``kntgraph.agents``. The contract under test:

  - ``allow(key)`` returns True for the first ``rpm``
    requests in the window and False afterwards.
  - Stale timestamps outside the window are evicted
    automatically (sliding window).
  - Different keys are tracked independently.
  - ``reset(key)`` clears a single bucket;
    ``reset()`` (no arg) clears all.
  - Validation: ``rpm >= 1``, ``window_s > 0``.
"""

from __future__ import annotations

import asyncio

import pytest

from kntgraph.resilience.rate_limit import (
    RateLimiter,
    RateLimiterProtocol,
)

pytestmark = pytest.mark.asyncio


class TestBasic:
    async def test_first_rpm_requests_are_allowed(self):
        rl = RateLimiter(rpm=3)
        for _ in range(3):
            assert await rl.allow("k") is True

    async def test_request_beyond_rpm_is_denied(self):
        rl = RateLimiter(rpm=3)
        for _ in range(3):
            assert await rl.allow("k") is True
        assert await rl.allow("k") is False

    async def test_keys_are_independent(self):
        rl = RateLimiter(rpm=2)
        assert await rl.allow("a") is True
        assert await rl.allow("a") is True
        assert await rl.allow("a") is False
        # `b` is a fresh bucket.
        assert await rl.allow("b") is True
        assert await rl.allow("b") is True
        assert await rl.allow("b") is False
        # `a` is still exhausted.
        assert await rl.allow("a") is False

    async def test_default_key_is_a_single_bucket(self):
        rl = RateLimiter(rpm=2)
        assert await rl.allow() is True
        assert await rl.allow() is True
        assert await rl.allow() is False

    async def test_sliding_window_evicts_stale_timestamps(self):
        """Timestamps outside the window are evicted so
        the bucket recovers its capacity over time.
        """
        rl = RateLimiter(rpm=2, window_s=0.05)
        assert await rl.allow("k") is True
        assert await rl.allow("k") is True
        assert await rl.allow("k") is False
        # Wait for the window to expire.
        await asyncio.sleep(0.06)
        # Now we should be allowed again.
        assert await rl.allow("k") is True


class TestReset:
    async def test_reset_specific_key(self):
        rl = RateLimiter(rpm=1)
        assert await rl.allow("a") is True
        assert await rl.allow("a") is False
        assert await rl.allow("b") is True
        assert await rl.allow("b") is False

        await rl.reset("a")
        # `a` is back to capacity; `b` is unchanged.
        assert await rl.allow("a") is True
        assert await rl.allow("b") is False

    async def test_reset_all_keys(self):
        rl = RateLimiter(rpm=1)
        await rl.allow("a")
        await rl.allow("b")
        await rl.reset()
        # All buckets cleared.
        assert await rl.allow("a") is True
        assert await rl.allow("b") is True

    async def test_reset_unknown_key_is_noop(self):
        rl = RateLimiter(rpm=2)
        await rl.reset("never-existed")  # no error


class TestStats:
    async def test_stats_reflects_current_buckets(self):
        rl = RateLimiter(rpm=3)
        await rl.allow("a")
        await rl.allow("a")
        await rl.allow("b")
        s = rl.stats()
        assert s["rpm"] == 3
        assert s["buckets"]["a"] == 2
        assert s["buckets"]["b"] == 1


class TestValidation:
    def test_rpm_must_be_positive(self):
        with pytest.raises(ValueError):
            RateLimiter(rpm=0)
        with pytest.raises(ValueError):
            RateLimiter(rpm=-1)

    def test_window_s_must_be_positive(self):
        with pytest.raises(ValueError):
            RateLimiter(rpm=1, window_s=0)
        with pytest.raises(ValueError):
            RateLimiter(rpm=1, window_s=-1)


class TestProtocolConformance:
    def test_implements_protocol(self):
        """The class advertises ``RateLimiterProtocol``
        so callers can type-hint against the protocol
        and pass an in-memory implementation without
        leaking storage details.
        """
        rl = RateLimiter(rpm=5)
        assert isinstance(rl, RateLimiterProtocol)


class TestAsyncSafety:
    async def test_concurrent_allow_respects_rpm(self):
        """With 50 concurrent callers and rpm=10,
        exactly 10 should succeed.
        """
        rl = RateLimiter(rpm=10)
        results = await asyncio.gather(*(rl.allow("shared") for _ in range(50)))
        assert sum(results) == 10
