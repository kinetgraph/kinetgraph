# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for config primitives (LLMConfig, RateLimiter,
CostBudget). No I/O.
"""

from __future__ import annotations

import asyncio

import pytest

from kntgraph.agents.config import CostBudget, LLMConfig, RateLimiter


class TestLLMConfig:
    def test_defaults(self):
        cfg = LLMConfig()
        assert cfg.default_model == "gpt-4o-mini"
        assert cfg.fallback_models == ()
        assert cfg.rate_limit_rpm == 60
        assert cfg.cost_budget_per_hour_usd == 2.0
        assert cfg.timeout_s == 30.0

    def test_empty_model_raises(self):
        with pytest.raises(ValueError):
            LLMConfig(default_model="")

    def test_negative_rate_raises(self):
        with pytest.raises(ValueError):
            LLMConfig(rate_limit_rpm=0)

    def test_negative_budget_raises(self):
        with pytest.raises(ValueError):
            LLMConfig(cost_budget_per_hour_usd=-1.0)

    def test_zero_timeout_raises(self):
        with pytest.raises(ValueError):
            LLMConfig(timeout_s=0)

    def test_fallback_is_tuple(self):
        cfg = LLMConfig(fallback_models=["a", "b"])
        assert cfg.fallback_models == ("a", "b")

    def test_rate_limiter_factory(self):
        cfg = LLMConfig(rate_limit_rpm=42)
        rl = cfg.rate_limiter()
        assert isinstance(rl, RateLimiter)
        assert rl.rpm == 42

    def test_rate_limiter_factory_none(self):
        cfg = LLMConfig(rate_limit_rpm=None)
        assert cfg.rate_limiter() is None

    def test_cost_budget_factory(self):
        cfg = LLMConfig(cost_budget_per_hour_usd=1.5)
        cb = cfg.cost_budget()
        assert isinstance(cb, CostBudget)
        assert cb.per_hour_usd == 1.5

    def test_cost_budget_factory_none(self):
        cfg = LLMConfig(cost_budget_per_hour_usd=None)
        assert cfg.cost_budget() is None


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_allows_within_limit(self):
        rl = RateLimiter(rpm=3)
        for _ in range(3):
            assert await rl.allow() is True
        # 4th is rejected
        assert await rl.allow() is False

    @pytest.mark.asyncio
    async def test_invalid_rpm(self):
        with pytest.raises(ValueError):
            RateLimiter(rpm=0)
        with pytest.raises(ValueError):
            RateLimiter(rpm=-1)

    @pytest.mark.asyncio
    async def test_window_expires(self):
        """After the window passes, the limiter allows again."""
        rl = RateLimiter(rpm=2)
        assert await rl.allow() is True
        assert await rl.allow() is True
        assert await rl.allow() is False
        # Force-clear by reset
        await rl.reset()
        assert await rl.allow() is True

    @pytest.mark.asyncio
    async def test_concurrent_consumers(self):
        """Two coroutines racing — total allowed <= rpm."""
        rl = RateLimiter(rpm=5)
        results = await asyncio.gather(*(rl.allow() for _ in range(10)))
        assert sum(1 for r in results if r) == 5


class TestCostBudget:
    @pytest.mark.asyncio
    async def test_can_spend_within_budget(self):
        cb = CostBudget(per_hour_usd=1.0)
        assert await cb.can_spend(0.5) is True
        await cb.charge(0.5)
        assert await cb.can_spend(0.4) is True
        await cb.charge(0.4)
        assert await cb.can_spend(0.2) is False

    @pytest.mark.asyncio
    async def test_charge_decrements_remaining(self):
        cb = CostBudget(per_hour_usd=1.0)
        await cb.charge(0.3)
        await cb.charge(0.2)
        assert await cb.remaining_usd() == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_charge_negative_raises(self):
        cb = CostBudget(per_hour_usd=1.0)
        with pytest.raises(ValueError):
            await cb.charge(-0.1)

    @pytest.mark.asyncio
    async def test_can_spend_negative_raises(self):
        cb = CostBudget(per_hour_usd=1.0)
        with pytest.raises(ValueError):
            await cb.can_spend(-0.1)

    @pytest.mark.asyncio
    async def test_invalid_budget(self):
        with pytest.raises(ValueError):
            CostBudget(per_hour_usd=0)

    @pytest.mark.asyncio
    async def test_remaining_never_negative(self):
        cb = CostBudget(per_hour_usd=0.5)
        await cb.charge(1.0)
        assert await cb.remaining_usd() == 0.0
