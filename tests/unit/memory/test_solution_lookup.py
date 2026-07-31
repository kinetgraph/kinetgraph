# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for ``kntgraph.agents.memory.solution_lookup`` (ADR-049).

Covers:

  - ``InMemorySolutionStore``: add / find_match / remove.
  - ``SolutionStoreLike`` runtime Protocol check.
  - ``SolutionLookupSystem``: cache hit / miss / bypass
    (low confidence + not in allowlist).
  - Stats: cumulative counters.
  - ``run_pending_lookups``: async drain, async-store
    exception handling.
  - Idempotency on ``seen`` (the same request is never
    looked up twice).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import pytest

from kntgraph.agents.memory.solution_lookup import (
    CachedSolution,
    InMemorySolutionStore,
    LookupStats,
    SolutionLookupSystem,
    SolutionStoreLike,
)
from kntgraph.core.world import AgentView, World
from kntgraph.core.world.components import ToolCallRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    *,
    tool_name: str = "weather_api",
    params: Optional[dict] = None,
    correlation_id=None,
) -> ToolCallRequest:
    if params is None:
        params = {"city": "São Paulo", "country": "BR"}
    if correlation_id is None:
        correlation_id = uuid4()
    return ToolCallRequest(
        request_event_id=str(uuid4()),
        tool_name=tool_name,
        agent_id="a-1",
        params=params,
        requested_at=datetime.now(tz=timezone.utc),
        correlation_id=correlation_id,
    )


def _make_world_with_requests(
    requests: dict[str, ToolCallRequest],
    *,
    agent_id: str = "a-1",
) -> World:
    """Build a minimal ``World`` carrying the given
    ``tool_requests`` slot for ``agent_id``."""
    view = AgentView(
        agent_id=agent_id,
        components={"tool_requests": requests},
    )
    return World(
        tick=1,
        storage=None,
        views={agent_id: view},
    )


def _empty_world() -> World:
    return World(tick=1, storage=None, views={})


# ---------------------------------------------------------------------------
# InMemorySolutionStore
# ---------------------------------------------------------------------------


class TestInMemorySolutionStore:
    def test_add_then_find_match_returns_solution(self):
        store = InMemorySolutionStore()
        sol = CachedSolution(
            tool_name="weather_api",
            params_fingerprint="abc123",
            confidence=5,
            result={"temp_c": 22},
            source_completion_event_id="orig-1",
        )
        store.add(sol)
        result = asyncio.run(
            store.find_match(
                tool_name="weather_api",
                params_fingerprint="abc123",
                min_confidence=3,
            )
        )
        assert result is sol

    def test_find_match_below_min_confidence_returns_none(self):
        store = InMemorySolutionStore()
        store.add(
            CachedSolution(
                tool_name="weather_api",
                params_fingerprint="abc",
                confidence=2,
                result={},
            )
        )
        result = asyncio.run(
            store.find_match(
                tool_name="weather_api",
                params_fingerprint="abc",
                min_confidence=3,
            )
        )
        assert result is None

    def test_find_match_unknown_key_returns_none(self):
        store = InMemorySolutionStore()
        result = asyncio.run(
            store.find_match(
                tool_name="weather_api",
                params_fingerprint="unknown",
                min_confidence=1,
            )
        )
        assert result is None

    def test_remove_drops_solution(self):
        store = InMemorySolutionStore()
        store.add(
            CachedSolution(
                tool_name="x",
                params_fingerprint="fp",
                confidence=10,
                result={},
            )
        )
        store.remove("x", "fp")
        result = asyncio.run(
            store.find_match(tool_name="x", params_fingerprint="fp", min_confidence=1)
        )
        assert result is None

    def test_last_add_wins(self):
        store = InMemorySolutionStore()
        store.add(
            CachedSolution(
                tool_name="x", params_fingerprint="fp", confidence=1, result={"v": 1}
            )
        )
        store.add(
            CachedSolution(
                tool_name="x", params_fingerprint="fp", confidence=5, result={"v": 5}
            )
        )
        result = asyncio.run(
            store.find_match(tool_name="x", params_fingerprint="fp", min_confidence=1)
        )
        assert result.confidence == 5


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestSolutionStoreLikeProtocol:
    def test_in_memory_satisfies_protocol(self):
        assert isinstance(InMemorySolutionStore(), SolutionStoreLike)

    def test_non_conforming_type_does_not_satisfy_protocol(self):
        class NotAStore:
            pass

        assert not isinstance(NotAStore(), SolutionStoreLike)

    def test_constructor_rejects_non_conforming_type(self):
        with pytest.raises(TypeError):
            SolutionLookupSystem(
                solution_store=object(),  # type: ignore[arg-type]
                min_confidence=3,
            )


# ---------------------------------------------------------------------------
# SolutionLookupSystem (sync pump + async drain)
# ---------------------------------------------------------------------------


class TestSolutionLookupSystem:
    def test_cache_hit_emits_synthetic_completion(self):
        from kntgraph.agents.memory.solutions._fingerprints import (
            fingerprint_params,
        )

        store = InMemorySolutionStore()
        req = _make_request(params={"city": "Rio de Janeiro"})
        fp = fingerprint_params(req.params)
        store.add(
            CachedSolution(
                tool_name=req.tool_name,
                params_fingerprint=fp,
                confidence=5,
                result={"temp_c": 28},
            )
        )
        sys = SolutionLookupSystem(solution_store=store, min_confidence=3)
        # First pump: discovers the request, queues
        # the lookup. No event yet (the lookup is
        # drained on ``run_pending_lookups``).
        events = sys(_make_world_with_requests({req.request_event_id: req}))
        assert events == []
        # Drain the pending lookups.
        asyncio.run(sys.run_pending_lookups())
        # Second pump: returns the synthetic
        # completions from the prior drain.
        events = sys(_empty_world())
        assert len(events) == 1
        completion = events[0]
        assert completion.event_type == "tool.weather_api.completed"
        assert completion.data["request_event_id"] == req.request_event_id
        assert completion.data["result"] == {"temp_c": 28}
        assert completion.data["source"] == "solution_lookup"
        # Stats: one hit, no misses.
        stats = sys.stats
        assert stats.cache_hit == 1
        assert stats.cache_miss == 0

    def test_cache_miss_emits_no_event_and_counts_miss(self):
        store = InMemorySolutionStore()
        req = _make_request()
        sys = SolutionLookupSystem(solution_store=store, min_confidence=3)
        sys(_make_world_with_requests({req.request_event_id: req}))
        asyncio.run(sys.run_pending_lookups())
        sys(_empty_world())
        assert sys.stats.cache_miss == 1
        assert sys.stats.cache_hit == 0

    def test_bypass_when_tool_not_in_allowlist(self):
        store = InMemorySolutionStore()
        req = _make_request(tool_name="weather_api")
        store.add(
            CachedSolution(
                tool_name="weather_api",
                params_fingerprint="",
                confidence=5,
                result={"temp_c": 28},
            )
        )
        # Allowlist that EXCLUDES the tool.
        sys = SolutionLookupSystem(
            solution_store=store,
            min_confidence=3,
            allowlist=frozenset({"other_tool"}),
        )
        sys(_make_world_with_requests({req.request_event_id: req}))
        asyncio.run(sys.run_pending_lookups())
        sys(_empty_world())
        # No completion produced (the tool is not in
        # the allowlist, so the lookup was bypassed
        # before queueing).
        assert sys.stats.bypass_not_in_allowlist == 1
        assert sys.stats.cache_hit == 0
        assert sys.stats.cache_miss == 0

    def test_bypass_when_confidence_below_threshold(self):
        store = InMemorySolutionStore()
        req = _make_request(params={"city": "X"})
        # Add a solution with low confidence; the
        # store's find_match will skip it because
        # min_confidence > solution.confidence.
        store.add(
            CachedSolution(
                tool_name=req.tool_name,
                params_fingerprint="",
                confidence=1,
                result={"temp_c": 28},
            )
        )
        sys = SolutionLookupSystem(solution_store=store, min_confidence=3)
        sys(_make_world_with_requests({req.request_event_id: req}))
        asyncio.run(sys.run_pending_lookups())
        sys(_empty_world())
        assert sys.stats.cache_miss == 1
        assert sys.stats.cache_hit == 0

    def test_same_request_is_not_re_looked_up_across_pumps(self):
        store = InMemorySolutionStore()
        req = _make_request()
        sys = SolutionLookupSystem(solution_store=store, min_confidence=3)
        # Pump 1: discovers + queues.
        sys(_make_world_with_requests({req.request_event_id: req}))
        asyncio.run(sys.run_pending_lookups())
        # Pump 2: returns the completion (cache miss
        # because the store has nothing for this
        # request).
        sys(_empty_world())
        assert sys.stats.cache_miss == 1
        # Pump 3: would normally re-queue because the
        # request is still on the view... but the
        # ``seen`` cache prevents the queue. The
        # ``__call__`` returns ``[]`` and the stats
        # do not increment.
        sys(_make_world_with_requests({req.request_event_id: req}))
        asyncio.run(sys.run_pending_lookups())
        sys(_empty_world())
        assert sys.stats.cache_miss == 1
        assert sys.stats.total == 1

    def test_world_without_tool_requests_is_noop(self):
        store = InMemorySolutionStore()
        sys = SolutionLookupSystem(solution_store=store, min_confidence=3)
        events = sys(_empty_world())
        assert events == []
        assert sys.stats.total == 0

    def test_synthetic_completion_carries_request_correlation(self):
        from kntgraph.agents.memory.solutions._fingerprints import (
            fingerprint_params,
        )

        store = InMemorySolutionStore()
        corr_id = uuid4()
        req = _make_request(correlation_id=corr_id)
        fp = fingerprint_params(req.params)
        store.add(
            CachedSolution(
                tool_name=req.tool_name,
                params_fingerprint=fp,
                confidence=5,
                result={},
            )
        )
        sys = SolutionLookupSystem(solution_store=store, min_confidence=3)
        sys(_make_world_with_requests({req.request_event_id: req}))
        asyncio.run(sys.run_pending_lookups())
        events = sys(_empty_world())
        assert len(events) == 1
        assert events[0].correlation.correlation_id == corr_id


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class _BrokenStore:
    """Always raises on ``find_match`` (the async store
    hit a network error)."""

    async def find_match(
        self,
        *,
        tool_name: str,
        params_fingerprint: str,
        min_confidence: int,
    ) -> Optional[CachedSolution]:
        raise ConnectionError("store down")


class TestSolutionLookupSystemErrorHandling:
    def test_store_error_counted_as_miss(self):
        req = _make_request()
        sys = SolutionLookupSystem(
            solution_store=_BrokenStore(),  # type: ignore[arg-type]
            min_confidence=3,
        )
        sys(_make_world_with_requests({req.request_event_id: req}))
        asyncio.run(sys.run_pending_lookups())
        sys(_empty_world())
        assert sys.stats.cache_miss == 1
        assert sys.stats.cache_hit == 0

    def test_store_error_does_not_abort_pump(self):
        """A failing lookup must not poison other
        queued lookups in the same ``run_pending_lookups``
        call."""
        InMemorySolutionStore()
        # Two requests: one for a broken lookup path
        # and one for a working path. The system has
        # only one store; for this test we patch the
        # store per-request by raising on the first
        # call only.
        call_count = {"n": 0}

        class FlakeyStore:
            async def find_match(self, **kwargs):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise ConnectionError("flakey")
                return CachedSolution(
                    tool_name=kwargs["tool_name"],
                    params_fingerprint=kwargs["params_fingerprint"],
                    confidence=5,
                    result={"ok": True},
                )

        req_a = _make_request(params={"a": 1})
        req_b = _make_request(params={"b": 2})
        sys = SolutionLookupSystem(
            solution_store=FlakeyStore(),  # type: ignore[arg-type]
            min_confidence=3,
        )
        sys(
            _make_world_with_requests(
                {
                    req_a.request_event_id: req_a,
                    req_b.request_event_id: req_b,
                }
            )
        )
        asyncio.run(sys.run_pending_lookups())
        events = sys(_empty_world())
        # The second request produces a completion;
        # the first request is logged as a miss.
        assert len(events) == 1
        assert sys.stats.cache_hit == 1
        assert sys.stats.cache_miss == 1


# ---------------------------------------------------------------------------
# LookupStats
# ---------------------------------------------------------------------------


class TestLookupStats:
    def test_default_zero(self):
        assert LookupStats().total == 0

    def test_as_dict_keys(self):
        s = LookupStats(
            cache_hit=1,
            cache_miss=2,
            bypass_low_confidence=3,
            bypass_not_in_allowlist=4,
        )
        d = s.as_dict()
        assert d == {
            "cache_hit": 1,
            "cache_miss": 2,
            "bypass_low_confidence": 3,
            "bypass_not_in_allowlist": 4,
        }

    def test_total_sum(self):
        s = LookupStats(
            cache_hit=1,
            cache_miss=2,
            bypass_low_confidence=3,
            bypass_not_in_allowlist=4,
        )
        assert s.total == 10
