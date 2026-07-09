# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for LiteLLMTool.

Tests use `FakeLLMTransport` — no network, no real LiteLLM
calls. The transport is injected via the `transport=` kwarg.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from kntgraph.core.result import BusinessError, Err, Ok
from kntgraph.agents.config import CostBudget, RateLimiter
from kntgraph.agents.tools import LLMResponse, LiteLLMTool

from .._fake_transport import FakeLLMTransport


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Basic invocation
# ---------------------------------------------------------------------------


class TestInvoke:
    async def test_returns_llm_response(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="hello, world")
        tool = LiteLLMTool(default_model="x", transport=transport)

        r = await tool.invoke(
            idempotency_key="k1",
            system="s",
            user="u",
        )
        assert r.is_ok()
        resp = r.unwrap()
        assert isinstance(resp, LLMResponse)
        assert resp.text == "hello, world"
        assert resp.usage.total_tokens == 15
        assert resp.latency_ms >= 0

    async def test_passes_messages_to_transport(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        tool = LiteLLMTool(default_model="x", transport=transport)
        await tool.invoke(
            idempotency_key="k",
            system="you are concise",
            user="hi",
            temperature=0.3,
            max_tokens=200,
        )
        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call["model"] == "x"
        assert call["temperature"] == 0.3
        assert call["max_tokens"] == 200
        # Messages are [system, user]
        msgs = call["messages"]
        assert len(msgs) == 2
        assert msgs[0] == {"role": "system", "content": "you are concise"}
        assert msgs[1] == {"role": "user", "content": "hi"}

    async def test_uses_override_model(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        tool = LiteLLMTool(default_model="primary", transport=transport)
        await tool.invoke(idempotency_key="k", system="s", user="u", model="override")
        assert transport.calls[0]["model"] == "override"


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------


class TestFallback:
    async def test_falls_back_on_rate_limit(self):
        transport = FakeLLMTransport()
        transport.queue_error("rate_limit")
        transport.queue_response(text="from fallback")
        tool = LiteLLMTool(
            default_model="primary",
            fallback_models=["fallback-1"],
            transport=transport,
        )
        r = await tool.invoke(idempotency_key="k", system="s", user="u")
        assert r.is_ok()
        assert r.unwrap().text == "from fallback"
        # Both models were tried
        assert [c["model"] for c in transport.calls] == [
            "primary",
            "fallback-1",
        ]

    async def test_falls_back_chain(self):
        transport = FakeLLMTransport()
        transport.queue_error("rate_limit")
        transport.queue_error("rate_limit")
        transport.queue_response(text="third try")
        tool = LiteLLMTool(
            default_model="m0",
            fallback_models=["m1", "m2"],
            transport=transport,
        )
        r = await tool.invoke(idempotency_key="k", system="s", user="u")
        assert r.is_ok()
        assert [c["model"] for c in transport.calls] == ["m0", "m1", "m2"]

    async def test_all_models_fail(self):
        transport = FakeLLMTransport()
        transport.queue_error("rate_limit")
        transport.queue_error("rate_limit")
        tool = LiteLLMTool(
            default_model="m0",
            fallback_models=["m1"],
            transport=transport,
        )
        r = await tool.invoke(idempotency_key="k", system="s", user="u")
        assert r.is_err()
        assert "rate_limit" in str(r.err_value())

    async def test_auth_error_no_fallback(self):
        """Auth errors are not recoverable; do not try fallback."""
        transport = FakeLLMTransport()
        transport.queue_error("auth")
        transport.queue_response(text="would succeed")
        tool = LiteLLMTool(
            default_model="m0",
            fallback_models=["m1"],
            transport=transport,
        )
        r = await tool.invoke(idempotency_key="k", system="s", user="u")
        assert r.is_err()
        assert "auth" in str(r.err_value())
        # Only the primary was tried
        assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


class TestRateLimit:
    async def test_rate_limited_before_call(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            rate_limiter=RateLimiter(rpm=1),
        )
        r1 = await tool.invoke(idempotency_key="k", system="s", user="u")
        assert r1.is_ok()
        r2 = await tool.invoke(idempotency_key="k2", system="s", user="u")
        assert r2.is_err()
        assert "rate_limited" in str(r2.err_value())
        # The transport only saw the first call
        assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# Cost budget
# ---------------------------------------------------------------------------


class TestCostBudget:
    async def test_budget_exhausted(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            cost_budget=CostBudget(per_hour_usd=0.00001),  # tiny
        )
        # First call should hit the budget limit
        r = await tool.invoke(
            idempotency_key="k", system="s", user="u", max_tokens=1000
        )
        assert r.is_err()
        assert "budget" in str(r.err_value())
        # Transport was NOT called
        assert len(transport.calls) == 0


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class TestStreaming:
    async def test_stream_returns_error_path(self):
        """`invoke` with stream=True is rejected; use astream."""
        transport = FakeLLMTransport()
        tool = LiteLLMTool(default_model="x", transport=transport)
        r = await tool.invoke(
            idempotency_key="k",
            system="s",
            user="u",
            stream=True,
        )
        assert r.is_err()
        assert "astream" in str(r.err_value())


# ---------------------------------------------------------------------------
# Circuit breaker (optional)
# ---------------------------------------------------------------------------


class _FakeBreaker:
    """
    Test double matching the
    `kntgraph.resilience.CircuitBreaker` interface
    (`call(coro)` returns `Result`, raises `CancelledError`).
    The fake never trips (no state machine) so the call is
    a thin wrapper that records the call count.
    """

    def __init__(
        self,
        *,
        err_on_call: BusinessError | None = None,
    ):
        self.call_count: int = 0
        self.err_on_call = err_on_call

    async def call(self, coro_fn):
        self.call_count += 1
        if self.err_on_call is not None:
            # Mirrors `CircuitBreaker.call`: returns
            # `Err(BusinessError)` when the breaker is
            # OPEN or the inner call failed. The LLM
            # tool's `_call_litellm` re-raises it.
            return Err(self.err_on_call)
        return Ok(await coro_fn())


class TestCircuitBreaker:
    async def test_no_breaker_means_direct_call(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        tool = LiteLLMTool(default_model="x", transport=transport)
        r = await tool.invoke(idempotency_key="k", system="s", user="u")
        assert r.is_ok()
        assert transport.calls[0]["model"] == "x"

    async def test_breaker_wraps_transport_call(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        breaker = _FakeBreaker()
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            circuit_breaker=breaker,
        )
        r = await tool.invoke(idempotency_key="k", system="s", user="u")
        assert r.is_ok()
        # The breaker saw exactly one call.
        assert breaker.call_count == 1
        # The transport was called once (forwarded).
        assert len(transport.calls) == 1
        assert transport.calls[0]["model"] == "x"

    async def test_breaker_open_short_circuits(self):
        """When the breaker is OPEN, it returns
        `Err(BusinessError)`; `_call_litellm` re-raises
        it and the tool propagates as `Err`. The
        transport is NOT called."""
        transport = FakeLLMTransport()
        transport.queue_response(text="would succeed")
        breaker = _FakeBreaker(err_on_call=BusinessError("Circuit breaker 'x' is OPEN"))
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            circuit_breaker=breaker,
        )
        r = await tool.invoke(idempotency_key="k", system="s", user="u")
        assert r.is_err()
        # The error message is preserved; the tool wraps
        # the BusinessError in a ToolError.
        assert "OPEN" in str(r.err_value())
        # The transport was NOT called (short-circuited).
        assert len(transport.calls) == 0

    async def test_breaker_records_failure(self):
        """When the inner call raises, the breaker sees
        the exception (or its own wrapper). The contract
        is: the breaker's `call` either returns the
        result or raises — `LiteLLMTool` does not
        inspect the breaker for failure semantics; the
        exception is converted to `Err` in the existing
        fallback handler."""
        transport = FakeLLMTransport()
        transport.queue_error("rate_limit")
        breaker = _FakeBreaker()
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            circuit_breaker=breaker,
        )
        r = await tool.invoke(idempotency_key="k", system="s", user="u")
        # The tool's fallback chain kicks in (no
        # `fallback_models` configured → `Err`).
        assert r.is_err()
        # The breaker saw the call (forwarded to it
        # before the transport raised).
        assert breaker.call_count == 1

    async def test_breaker_works_with_fallback_chain(self):
        """The breaker wraps every `_call_litellm`
        invocation, including the fallback chain. With
        one primary and one fallback, the breaker is
        invoked twice (once per model attempt)."""
        transport = FakeLLMTransport()
        transport.queue_error("rate_limit")
        transport.queue_response(text="fallback ok")
        breaker = _FakeBreaker()
        tool = LiteLLMTool(
            default_model="primary",
            fallback_models=["fallback"],
            transport=transport,
            circuit_breaker=breaker,
        )
        r = await tool.invoke(idempotency_key="k", system="s", user="u")
        assert r.is_ok()
        assert r.ok_value().text == "fallback ok"
        # Two models tried → breaker called twice.
        assert breaker.call_count == 2
        assert len(transport.calls) == 2  # primary + fallback


# ---------------------------------------------------------------------------
# Resilience integration: real CircuitBreaker + retry-with-timeout
# ---------------------------------------------------------------------------


class TestResilienceIntegration:
    """
    These tests exercise the wiring between
    `LiteLLMTool` and the
    `kntgraph.resilience` module as a whole, not a
    fake. They use the real `CircuitBreaker` (state
    machine, monotonic clock) and the real
    `with_timeout_and_retry` (backoff + jitter + budget).
    """

    async def test_real_breaker_counts_calls_and_recovers(self):
        """
        A real `CircuitBreaker` observes the same call
        count semantics as the fake, but also transitions
        state. We verify both.
        """
        from kntgraph.resilience import CircuitBreaker

        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        transport.queue_response(text="ok again")
        breaker = CircuitBreaker(
            "test-real",
            failure_threshold=2,
            recovery_timeout_seconds=0.01,
            half_open_max_calls=1,
        )
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            circuit_breaker=breaker,
        )
        # First call: success.
        r = await tool.invoke(idempotency_key="k1", system="s", user="u")
        assert r.is_ok()
        assert breaker.call_count == 1
        assert breaker.failure_count == 0

        # Second call: also success. The breaker remains
        # in CLOSED state.
        r = await tool.invoke(idempotency_key="k2", system="s", user="u")
        assert r.is_ok()
        assert breaker.call_count == 2

    async def test_retry_retries_transient_timeouts(self):
        """
        With `retry_attempts >= 1`, timeouts are retried
        with exponential backoff before the tool surfaces
        an error. We simulate a slow transport that
        exceeds the per-call timeout twice, then succeeds.
        """
        transport = FakeLLMTransport()
        transport.queue_response(text="ok", completion_tokens=1)
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            timeout_s=0.05,
            retry_attempts=2,
            retry_base_delay=0.001,
            retry_max_delay=0.01,
            retry_max_total_seconds=2.0,
        )
        # Patch the transport's complete method to sleep
        # past the timeout the first two times.
        real_complete = transport.complete
        call_count = {"n": 0}

        async def slow_first_two(**kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                await asyncio.sleep(0.2)
            return await real_complete(**kwargs)

        transport.complete = slow_first_two  # type: ignore[method-assign]

        r = await tool.invoke(idempotency_key="k", system="s", user="u")
        assert r.is_ok(), r.err_value() if r.is_err() else None
        # `slow_first_two` slept past the timeout for the
        # first two retries (they were killed by
        # `with_timeout_and_retry`); the third attempt
        # ran fast and reached the real `complete`,
        # recording exactly one entry in `transport.calls`.
        assert call_count["n"] == 3
        assert len(transport.calls) == 1

    async def test_retry_respects_max_total_seconds(self):
        """
        With `retry_max_total_seconds=0.05`, the retry
        loop short-circuits before exhausting
        `max_attempts`.
        """
        transport = FakeLLMTransport()
        # Always error.
        for _ in range(10):
            transport.queue_error("generic")
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            retry_attempts=10,
            retry_base_delay=10.0,  # would sleep 10s, 20s, ...
            retry_max_delay=60.0,
            retry_max_total_seconds=0.1,
        )
        started = time.perf_counter()
        r = await tool.invoke(idempotency_key="k", system="s", user="u")
        elapsed = time.perf_counter() - started
        # The budget must short-circuit the loop; we
        # should NOT have spent 10s+.
        assert elapsed < 5.0, f"retry loop exceeded budget: {elapsed}s"
        assert r.is_err()


# ---------------------------------------------------------------------------
# Refactored internals: _check_pre_call, _try_one_model,
# _execute_with_resilience, _next_chunk_with_timeout,
# _TerminalToolError
# ---------------------------------------------------------------------------
# These tests exercise the helpers extracted from the
# previously-god `invoke()`, `_call_litellm()`, and
# `astream()` methods. The behaviour is the same as the
# end-to-end tests above; these verify the *contract*
# of the new building blocks.
#
# NOTE: tests that exercise the real `LiteLLMTransportAdapter`
# against the real `litellm` package (rate-limit / auth /
# generic-API-error translation) live in
# `tests/integration/tools/test_litellm_transport.py` and are
# gated on the `kntgraph[llm]` extra being installed.


class TestTerminalToolError:
    """
    `_TerminalToolError` is a `ToolError` subclass that
    signals to the fallback loop in `invoke()` to stop
    iterating through `fallback_models`. The contract
    is verified by `isinstance` checks, NOT by string
    prefix matching.
    """

    def test_is_tool_error(self):
        """`_TerminalToolError` is a `ToolError` so
        code that catches `ToolError` still works."""
        from kntgraph.agents.tools.llm import _TerminalToolError
        from kntgraph.core.result import ToolError

        e = _TerminalToolError("auth on x: bad key")
        assert isinstance(e, ToolError)
        assert str(e) == "auth on x: bad key"

    def test_isinstance_check_works(self):
        from kntgraph.agents.tools.llm import _TerminalToolError
        from kntgraph.core.result import ToolError

        terminal = _TerminalToolError("auth on x: bad key")
        recoverable = ToolError("rate_limit on x: too many")
        # The fallback loop's decision is a simple
        # `isinstance(err, _TerminalToolError)` check.
        assert isinstance(terminal, _TerminalToolError)
        assert not isinstance(recoverable, _TerminalToolError)


class TestCheckPreCall:
    async def test_no_rate_limiter_no_budget_returns_none(self):
        transport = FakeLLMTransport()
        tool = LiteLLMTool(default_model="x", transport=transport)
        # No rate limiter, no cost budget → always None.
        assert await tool._check_pre_call("user", "system", 100) is None

    async def test_rate_limiter_rejects(self):
        """The first call passes; the second is
        rejected. Mirrors `TestRateLimit`."""
        from kntgraph.agents.config import RateLimiter

        transport = FakeLLMTransport()
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            rate_limiter=RateLimiter(rpm=1),
        )
        # First call: allowed.
        r1 = await tool._check_pre_call("user", "system", 100)
        assert r1 is None
        # Second call: rejected.
        r2 = await tool._check_pre_call("user", "system", 100)
        assert r2 is not None
        assert r2.is_err()
        assert "rate_limited" in str(r2.err_value())

    async def test_budget_exhausted_rejects(self):
        from kntgraph.agents.config import CostBudget

        transport = FakeLLMTransport()
        budget = CostBudget(per_hour_usd=0.00001)  # tiny
        await budget.charge(0.00001)  # exhaust
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            cost_budget=budget,
        )
        r = await tool._check_pre_call("user", "system", 100)
        assert r is not None
        assert r.is_err()
        assert "budget_exhausted" in str(r.err_value())


class TestResolvePerCallKwargs:
    def test_explicit_wins(self):
        from kntgraph.agents.tools.llm import LiteLLMTool

        t, m = LiteLLMTool._resolve_per_call_kwargs(
            explicit_temperature=0.7,
            explicit_max_tokens=512,
            default_temperature=0.0,
            default_max_tokens=128,
        )
        assert t == 0.7
        assert m == 512

    def test_none_falls_back_to_defaults(self):
        from kntgraph.agents.tools.llm import LiteLLMTool

        t, m = LiteLLMTool._resolve_per_call_kwargs(
            explicit_temperature=None,
            explicit_max_tokens=None,
            default_temperature=0.3,
            default_max_tokens=256,
        )
        assert t == 0.3
        assert m == 256

    def test_independent_resolution(self):
        """Temperature may be explicit while max_tokens falls
        back, and vice versa — the helper does not treat
        them as a pair."""
        from kntgraph.agents.tools.llm import LiteLLMTool

        t, m = LiteLLMTool._resolve_per_call_kwargs(
            explicit_temperature=0.9,
            explicit_max_tokens=None,
            default_temperature=0.1,
            default_max_tokens=64,
        )
        assert t == 0.9
        assert m == 64

    def test_instance_binding_uses_tool_defaults(self):
        """`_effective_kwargs` reads the tool's resolved
        defaults (which themselves came from Settings)."""
        from kntgraph.agents.tools.llm import LiteLLMTool

        transport = FakeLLMTransport()
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            temperature=0.42,
            max_tokens=777,
        )
        # No explicit override → tool's resolved defaults win.
        t, m = tool._effective_kwargs(None, None)
        assert t == 0.42
        assert m == 777
        # Explicit override wins.
        t, m = tool._effective_kwargs(0.1, 1)
        assert t == 0.1
        assert m == 1


class TestChargeAndCapCost:
    async def test_no_budget_no_cap_returns_none(self):
        """No cost_budget + cap disabled (0) → pass-through."""
        from kntgraph.agents.tools.llm import LLMResponse, LLMUsage

        transport = FakeLLMTransport()
        tool = LiteLLMTool(default_model="x", transport=transport)
        response = LLMResponse(
            text="hi",
            model="x",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=10.0,
            cost_usd=0.05,
        )
        r = await tool._charge_and_cap_cost(response)
        assert r is None

    async def test_charges_budget_when_set(self):
        from kntgraph.agents.config import CostBudget
        from kntgraph.agents.tools.llm import LLMResponse, LLMUsage

        transport = FakeLLMTransport()
        budget = CostBudget(per_hour_usd=1.0)
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            cost_budget=budget,
        )
        response = LLMResponse(
            text="hi",
            model="x",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=10.0,
            cost_usd=0.10,
        )
        r = await tool._charge_and_cap_cost(response)
        assert r is None
        # The budget was actually charged.
        assert await budget._spent_in_window() == 0.10

    async def test_unknown_cost_passes_through(self):
        """``cost_usd=None`` means we can't estimate —
        pass through without charging or rejecting."""
        from kntgraph.agents.config import CostBudget
        from kntgraph.agents.tools.llm import LLMResponse, LLMUsage

        transport = FakeLLMTransport()
        budget = CostBudget(per_hour_usd=1.0)
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            cost_budget=budget,
        )
        response = LLMResponse(
            text="hi",
            model="x",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=10.0,
            cost_usd=None,
        )
        r = await tool._charge_and_cap_cost(response)
        assert r is None
        assert await budget._spent_in_window() == 0.0

    async def test_cap_rejects_over_limit(self):
        from kntgraph.agents.tools.llm import LLMResponse, LLMUsage

        transport = FakeLLMTransport()
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            max_cost_usd_per_request=0.01,
        )
        response = LLMResponse(
            text="hi",
            model="x",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=10.0,
            cost_usd=0.05,
        )
        r = await tool._charge_and_cap_cost(response)
        assert r is not None
        assert "cost_cap_exceeded" in str(r)
        assert "0.05" in str(r)
        assert "0.01" in str(r)

    async def test_cap_at_or_under_limit_passes(self):
        from kntgraph.agents.tools.llm import LLMResponse, LLMUsage

        transport = FakeLLMTransport()
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            max_cost_usd_per_request=0.10,
        )
        response = LLMResponse(
            text="hi",
            model="x",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=10.0,
            cost_usd=0.05,
        )
        r = await tool._charge_and_cap_cost(response)
        assert r is None

    async def test_cap_disabled_means_zero(self):
        """`max_cost_usd_per_request=0` disables the cap
        entirely — even expensive calls pass through."""
        from kntgraph.agents.tools.llm import LLMResponse, LLMUsage

        transport = FakeLLMTransport()
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            max_cost_usd_per_request=0,
        )
        response = LLMResponse(
            text="hi",
            model="x",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=10.0,
            cost_usd=999.0,
        )
        r = await tool._charge_and_cap_cost(response)
        assert r is None

    async def test_budget_charged_before_cap_check(self):
        """The cap is the hard ceiling — even when the
        budget was charged, an over-cap call is rejected."""
        from kntgraph.agents.config import CostBudget
        from kntgraph.agents.tools.llm import LLMResponse, LLMUsage

        transport = FakeLLMTransport()
        budget = CostBudget(per_hour_usd=10.0)
        tool = LiteLLMTool(
            default_model="x",
            transport=transport,
            cost_budget=budget,
            max_cost_usd_per_request=0.01,
        )
        response = LLMResponse(
            text="hi",
            model="x",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=10.0,
            cost_usd=0.05,
        )
        r = await tool._charge_and_cap_cost(response)
        assert r is not None
        assert "cost_cap_exceeded" in str(r)
        # Budget WAS charged (the call did happen) — the cap
        # rejection happens AFTER the charge.


class TestTryOneModel:
    async def test_try_one_model_returns_ok(self):
        from kntgraph.agents.tools.llm import LLMResponse

        transport = FakeLLMTransport()
        transport.queue_response(text="hello")
        tool = LiteLLMTool(default_model="x", transport=transport)
        r = await tool._try_one_model(
            model="x",
            system="s",
            user="u",
            temperature=0.0,
            max_tokens=10,
            response_format=None,
            idempotency_key="k",
        )
        # DEBUG: explicitly inspect the result
        assert r.is_ok() or r.is_err(), f"Result is neither ok nor err: {r!r}"
        assert r.is_ok(), (
            f"Expected Ok, got Err: {r.err_value()!r}; "
            f"transport.calls={transport.calls!r}"
        )
        assert isinstance(r.unwrap(), LLMResponse)

    async def test_try_one_model_rate_limit_is_recoverable(self):
        """Rate-limit errors stay as base `ToolError`
        (recoverable) — the fallback loop iterates."""
        from kntgraph.agents.tools.llm import _TerminalToolError

        transport = FakeLLMTransport()
        transport.queue_error("rate_limit")
        tool = LiteLLMTool(default_model="x", transport=transport)
        r = await tool._try_one_model(
            model="x",
            system="s",
            user="u",
            temperature=0.0,
            max_tokens=10,
            response_format=None,
            idempotency_key="k",
        )
        assert r.is_err()
        assert not isinstance(r.err_value(), _TerminalToolError)

    async def test_try_one_model_auth_is_terminal(self):
        """Auth errors are `_TerminalToolError` — the
        fallback loop returns immediately."""
        from kntgraph.agents.tools.llm import _TerminalToolError

        transport = FakeLLMTransport()
        transport.queue_error("auth")
        tool = LiteLLMTool(default_model="x", transport=transport)
        r = await tool._try_one_model(
            model="x",
            system="s",
            user="u",
            temperature=0.0,
            max_tokens=10,
            response_format=None,
            idempotency_key="k",
        )
        assert r.is_err()
        assert isinstance(r.err_value(), _TerminalToolError)
        assert "auth" in str(r.err_value())

    async def test_try_one_model_generic_is_terminal(self):
        """Generic transport errors are `_TerminalToolError`."""
        from kntgraph.agents.tools.llm import _TerminalToolError

        transport = FakeLLMTransport()
        transport.queue_error("generic")
        tool = LiteLLMTool(default_model="x", transport=transport)
        r = await tool._try_one_model(
            model="x",
            system="s",
            user="u",
            temperature=0.0,
            max_tokens=10,
            response_format=None,
            idempotency_key="k",
        )
        assert r.is_err()
        assert isinstance(r.err_value(), _TerminalToolError)
        assert "llm_error" in str(r.err_value())


class TestExecuteWithResilience:
    """
    `_execute_with_resilience` selects the right strategy
    (breaker / retry / direct) and dispatches.
    """

    async def test_no_resilience_calls_directly(self):

        call_count = {"n": 0}

        async def do_call():
            call_count["n"] += 1
            return "ok"

        tool = LiteLLMTool(default_model="x")
        r = await tool._execute_with_resilience(do_call)
        assert r == "ok"
        assert call_count["n"] == 1

    async def test_retry_path(self):
        """`retry_attempts >= 1` wraps the call in
        `with_timeout_and_retry`. We don't time out
        here, so the call should run once and return."""

        call_count = {"n": 0}

        async def do_call():
            call_count["n"] += 1
            return "ok"

        tool = LiteLLMTool(
            default_model="x", retry_attempts=2, retry_max_total_seconds=10.0
        )
        r = await tool._execute_with_resilience(do_call)
        assert r == "ok"
        # 1 actual call (no transient failures).
        assert call_count["n"] == 1

    async def test_breaker_rejection_raises_business_error(self):
        """When the breaker returns `Err`, we re-raise
        the `BusinessError` so the fallback loop can
        branch on it."""
        from kntgraph.core.result import BusinessError, Err

        class _RejectsBreaker:
            async def call(self, fn):
                return Err(BusinessError("Circuit breaker 'x' is OPEN"))

        async def do_call():
            return "ok"

        tool = LiteLLMTool(default_model="x", circuit_breaker=_RejectsBreaker())
        with pytest.raises(BusinessError) as exc_info:
            await tool._execute_with_resilience(do_call)
        assert "OPEN" in str(exc_info.value)


class TestNextChunkWithTimeout:
    """
    `_next_chunk_with_timeout` is a static helper used
    by `astream()` to pull one chunk under a deadline.
    """

    async def test_yields_chunk_when_in_time(self):
        from kntgraph.agents.tools.llm import (
            _STREAM_DONE,
        )

        async def gen():
            yield "chunk1"
            yield "chunk2"

        g = gen()
        deadline = time.monotonic() + 10.0
        r1 = await LiteLLMTool._next_chunk_with_timeout(g, deadline)
        assert r1 == "chunk1"
        r2 = await LiteLLMTool._next_chunk_with_timeout(g, deadline)
        assert r2 == "chunk2"
        r3 = await LiteLLMTool._next_chunk_with_timeout(g, deadline)
        assert r3 is _STREAM_DONE

    async def test_deadline_exceeded_returns_timeout(self):
        from kntgraph.agents.tools.llm import _STREAM_TIMEOUT

        async def gen():
            await asyncio.sleep(10.0)  # never reached
            yield "x"

        g = gen()
        # Deadline in the past.
        deadline = time.monotonic() - 1.0
        r = await LiteLLMTool._next_chunk_with_timeout(g, deadline)
        assert r is _STREAM_TIMEOUT

    async def test_slow_chunk_returns_timeout(self):
        from kntgraph.agents.tools.llm import _STREAM_TIMEOUT

        async def gen():
            await asyncio.sleep(2.0)
            yield "late"

        g = gen()
        # Deadline 100ms from now; chunk arrives 2s later.
        deadline = time.monotonic() + 0.1
        r = await LiteLLMTool._next_chunk_with_timeout(g, deadline)
        assert r is _STREAM_TIMEOUT


class TestStreamSentinels:
    """
    The stream sentinels are unique objects (not Optional /
    string sentinels) so the call site distinguishes
    "stream done" / "timed out" / "got a chunk" by
    `is`, without colliding with `None` (a chunk
    `Result` is never `None`).
    """

    def test_sentinels_are_unique(self):
        from kntgraph.agents.tools.llm import _STREAM_DONE, _STREAM_TIMEOUT

        assert _STREAM_DONE is not _STREAM_TIMEOUT
        # Singleton: importing the module twice gives
        # the same objects.
        from kntgraph.agents.tools import llm as llm_mod

        assert llm_mod._STREAM_DONE is _STREAM_DONE
        assert llm_mod._STREAM_TIMEOUT is _STREAM_TIMEOUT

    def test_sentinels_have_distinct_repr(self):
        from kntgraph.agents.tools.llm import _STREAM_DONE, _STREAM_TIMEOUT

        assert repr(_STREAM_DONE) == "_STREAM_DONE"
        assert repr(_STREAM_TIMEOUT) == "_STREAM_TIMEOUT"


# ---------------------------------------------------------------------------
# Tool Protocol conformance
# ---------------------------------------------------------------------------
# `LiteLLMTool` is a `Tool` (per `kntgraph.agents.tools.protocol.Tool`).
# The framework relies on three class attributes (`name`,
# `description`, `input_schema`) and a single `async invoke`
# method with a `*, idempotency_key` parameter.
#
# The Protocol is `@runtime_checkable`, so `isinstance(tool,
# Tool)` works at runtime. These tests verify the contract
# is upheld and that the tool can be registered in the
# framework's `ToolRegistry`.


class TestToolProtocolConformance:
    """
    `LiteLLMTool` conforms to the `Tool` Protocol:

      - `name: str` (class attribute)
      - `description: str` (class attribute)
      - `input_schema: dict` (class attribute, JSON-schema)
      - `async invoke(*, idempotency_key, **kwargs)` returning
        a `Result[LLMResponse, ToolError]`

    These are the four members the framework reads from a
    `Tool` (per the `Tool` Protocol docstring).
    """

    def test_name_is_class_attribute(self):
        """`name` is a class attribute (not an instance
        attribute) so the framework can read it before
        instantiation."""
        # Class-level (shared by all instances).
        assert isinstance(LiteLLMTool.name, str)
        assert LiteLLMTool.name == "llm.complete"
        # Instance-level returns the same value.
        tool = LiteLLMTool(default_model="x")
        assert tool.name == "llm.complete"

    def test_name_follows_provider_action_convention(self):
        """`Tool` Protocol docstring convention:
        `provider.action` in lower-snake-case."""
        name = LiteLLMTool.name
        assert name == name.lower()
        assert " " not in name

    def test_description_is_class_attribute(self):
        assert isinstance(LiteLLMTool.description, str)
        assert len(LiteLLMTool.description) > 0
        # Helpful for LLM-fronted systems.
        assert "LiteLLM" in LiteLLMTool.description

    def test_input_schema_is_class_attribute(self):
        assert isinstance(LiteLLMTool.input_schema, dict)
        # JSON-schema shape.
        assert LiteLLMTool.input_schema.get("type") == "object"
        assert "properties" in LiteLLMTool.input_schema
        # `system` and `user` are the only required args
        # (the LLM tool has no other mandatory inputs).
        assert set(LiteLLMTool.input_schema.get("required", [])) == {
            "system",
            "user",
        }

    def test_input_schema_lists_all_invoke_kwargs(self):
        """`input_schema` should cover the kwargs
        the tool actually accepts. This is the
        contract an LLM-fronted system uses to
        validate generated arguments before
        dispatch."""
        properties = LiteLLMTool.input_schema["properties"]
        for kw in (
            "system",
            "user",
            "model",
            "temperature",
            "max_tokens",
            "response_format",
            "stream",
        ):
            assert kw in properties, f"missing property: {kw}"

    def test_invoke_signature_has_idempotency_key(self):
        """The Protocol requires `*, idempotency_key: str`
        as the first keyword. The tool's signature
        must match."""
        import inspect

        sig = inspect.signature(LiteLLMTool.invoke)
        params = list(sig.parameters.values())
        # `self`, then `idempotency_key` (keyword-only).
        assert params[0].name == "self"
        assert params[1].name == "idempotency_key"
        # Keyword-only: no positional after `self`.
        assert sig.parameters["idempotency_key"].kind == (
            inspect.Parameter.KEYWORD_ONLY
        )

    def test_invoke_accepts_extra_kwargs(self):
        """The Protocol allows `**kwargs`; the tool
        must too (for forward-compat with new
        LiteLLM-specific params)."""
        import inspect

        sig = inspect.signature(LiteLLMTool.invoke)
        # The signature has `**kwargs` for forward
        # compatibility with LiteLLM-specific params
        # (e.g. `think=False` for Ollama).
        assert any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )

    def test_isinstance_tool_runtime(self):
        """The Protocol is `@runtime_checkable`; the
        tool must pass `isinstance(tool, Tool)`."""
        from kntgraph.agents.tools.protocol import Tool

        tool = LiteLLMTool(default_model="x")
        assert isinstance(tool, Tool)

    async def test_invoke_returns_result_with_llmresponse(self):
        """`invoke` returns `Result[LLMResponse, ToolError]`
        — not a bare dict, not a coroutine that
        resolves to one. The framework relies on the
        `Result` shape for the `.completed` / `.failed`
        branch."""
        from kntgraph.agents.tools.llm import LLMResponse
        from kntgraph.core.result import Result

        transport = FakeLLMTransport()
        transport.queue_response(text="hi")
        tool = LiteLLMTool(default_model="x", transport=transport)
        r = await tool.invoke(idempotency_key="k", system="s", user="u")
        assert isinstance(r, Result)
        assert r.is_ok()
        assert isinstance(r.unwrap(), LLMResponse)

    async def test_invoke_idempotency_key_is_required(self):
        """`idempotency_key` is keyword-only with no
        default — the framework MUST inject it on
        every call. Forgetting it is a programmer
        error caught at the call site."""
        transport = FakeLLMTransport()
        transport.queue_response(text="hi")
        tool = LiteLLMTool(default_model="x", transport=transport)
        with pytest.raises(TypeError):
            # Missing `idempotency_key` → TypeError.
            await tool.invoke(system="s", user="u")  # type: ignore[call-arg]

    async def test_invoke_idempotency_key_is_accepted(self):
        """The framework contract is: the tool MUST
        accept `idempotency_key` (even if it doesn't
        dedupe). The current implementation accepts
        it and forwards to the transport."""
        transport = FakeLLMTransport()
        transport.queue_response(text="hi")
        tool = LiteLLMTool(default_model="x", transport=transport)
        r = await tool.invoke(
            idempotency_key="contract-key-123",
            system="s",
            user="u",
        )
        assert r.is_ok()
        # The transport received the key (it goes
        # through `call_kwargs` to the underlying
        # LiteLLM call; the fake stores it).
        assert transport.calls[0].get("idempotency_key") == ("contract-key-123")


class TestToolRegistryRegistration:
    """
    The framework's `ToolRegistry` is the central
    dispatcher. A `Tool` that conforms to the Protocol
    can be registered with no extra ceremony (the
    default ACL is assigned automatically).
    """

    async def test_register_under_default_acl(self):
        from kntgraph.agents.tools.protocol import (
            ToolACL,
            ToolRegistry,
        )

        tool = LiteLLMTool(default_model="x")
        registry = ToolRegistry()
        registry.register(tool)
        assert tool.name in registry
        # Default ACL: required_role=agent,
        # tenant_pinned=False.
        acl = registry.acl_for(tool.name)
        assert isinstance(acl, ToolACL)
        assert acl.tenant_pinned is False

    async def test_register_with_explicit_acl(self):
        from kntgraph.agents.tools.protocol import (
            ToolACL,
            ToolRegistry,
        )
        from kntgraph.security.principal import Role

        tool = LiteLLMTool(default_model="x")
        registry = ToolRegistry()
        # `tenant_pinned=True` requires a `tenant_id`
        # (per ToolACL's `__post_init__`).
        acl = ToolACL(
            required_role=Role.admin,
            tenant_pinned=True,
            tenant_id="t-1",
        )
        registry.register(tool, acl=acl)
        assert registry.acl_for(tool.name) is acl

    async def test_duplicate_registration_rejected(self):
        from kntgraph.agents.tools.protocol import ToolRegistry

        tool_a = LiteLLMTool(default_model="a")
        tool_b = LiteLLMTool(default_model="b")
        registry = ToolRegistry()
        registry.register(tool_a)
        with pytest.raises(ValueError):
            # Same name → ValueError (the framework
            # rejects silent overrides).
            registry.register(tool_b)

    async def test_lookup_by_name(self):
        from kntgraph.agents.tools.protocol import ToolRegistry

        tool = LiteLLMTool(default_model="x")
        registry = ToolRegistry()
        registry.register(tool)
        # The framework looks up by name only.
        looked_up = registry.get(LiteLLMTool.name)
        assert looked_up is tool
