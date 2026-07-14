# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``SolutionExtractorSystem``
(Iter 28 FU 8 / ADR-034).

The system is a pure ``WorldSystem``: it reads the
World (which already has ``ToolCallRequest`` and
``ToolCallCompletion`` components materialised by
``project_tool_calls``) and emits
``solution.candidate_extracted`` events for completed
tool calls that meet the cross-agent threshold.

Pure means: no I/O, no Redis, no FalkorDB. The system
is safe to call from any tick; the I/O is delegated
to other systems (promoter, review publisher).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Mapping, Optional
from uuid import UUID

import pytest

from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.event.event import Event
from kntgraph.core.world.projection_tool_calls import (
    project_tool_calls,
)
from kntgraph.core.world.world import World

from kntgraph.agents.memory.solution_extractor import (
    SolutionExtractorSystem,
)


def _ts(offset_s: int = 0) -> datetime:
    base = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_s)


def _event(
    *,
    event_type: str,
    agent_id: str = "agent-1",
    data: Optional[Mapping[str, Any]] = None,
    causation_id: Optional[UUID] = None,
    timestamp: Optional[datetime] = None,
) -> Event:
    return Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class="domain",
        data=MappingProxyType(dict(data or {})),
        correlation=CorrelationContext.new(),
        causation_id=causation_id,
        timestamp=timestamp or _ts(),
    )


def _world(events: list[Event]) -> World:
    """Build a World with the tool_calls projection.

    ADR-045: the projection defaults to a 5-minute
    TTL; the test events have timestamps in
    ``2026-06-30`` and a real wall clock would evict
    the requests. Disable the TTL for the
    tests that don't exercise the eviction logic.
    """
    from kntgraph.core.world.components import ToolCallTTL

    return World.fold(
        events,
        tick=events[-1].timestamp if events else 0,
        projection=lambda evs: project_tool_calls(
            evs, ttl=ToolCallTTL(default_ttl_seconds=0)
        ),
    )


class TestSolutionExtractorSystemEmpty:
    def test_empty_world_emits_no_events(self) -> None:
        """No events → no candidates."""
        sys = SolutionExtractorSystem(bump_min_agents=1)
        world = World.empty()
        out = sys(world)
        assert out == []


class TestSolutionExtractorSystemNoCompletion:
    def test_pending_request_emits_no_event(self) -> None:
        """A request without a completion is in flight;
        no candidate is emitted."""
        request = _event(
            event_type="tool.requested",
            data={"tool": "x"},
        )
        world = _world([request])
        sys = SolutionExtractorSystem(bump_min_agents=1)
        out = sys(world)
        assert out == []


class TestSolutionExtractorSystemFailed:
    def test_failed_tool_emits_no_candidate(self) -> None:
        """A `status="failed"` completion does NOT
        produce a candidate. The system only emits
        for successful completions.
        """
        request = _event(
            event_type="tool.requested",
            data={"tool": "x"},
            timestamp=_ts(0),
        )
        completion = _event(
            event_type="tool.failed",
            data={"error": "rate_limited"},
            causation_id=request.event_id,
            timestamp=_ts(2),
        )
        world = _world([request, completion])
        sys = SolutionExtractorSystem(bump_min_agents=1)
        out = sys(world)
        assert out == []


class TestSolutionExtractorSystemEmits:
    def test_single_agent_completed_emits_one_candidate(self) -> None:
        """A successful completion (single-agent) emits
        a ``solution.candidate_extracted`` event."""
        request = _event(
            event_type="tool.requested",
            data={"tool": "x"},
            timestamp=_ts(0),
        )
        completion = _event(
            event_type="tool.completed",
            data={"text": "ok"},
            causation_id=request.event_id,
            timestamp=_ts(2),
        )
        world = _world([request, completion])
        # bump_min_agents=1: a single agent suffices.
        sys = SolutionExtractorSystem(bump_min_agents=1)
        out = sys(world)

        assert len(out) == 1
        event = out[0]
        assert event.event_type == "solution.candidate_extracted"
        assert event.agent_id == "agent-1"
        # The event carries the source provenance.
        assert event.data.get("request_event_id") == str(request.event_id)
        assert event.data.get("tool_name") == "x"
        # The completion provenance.
        assert event.data.get("completion_status") == "completed"
        assert event.data.get("latency_ms") == pytest.approx(2000.0, rel=1e-3)


class TestSolutionExtractorSystemCrossAgent:
    def test_single_agent_below_threshold_emits_nothing(self) -> None:
        """With bump_min_agents=2 and only one agent
        using the same params, no candidate is emitted
        (cross-agent threshold not met).
        """
        request = _event(
            event_type="tool.requested",
            data={"tool": "x", "query": "abc"},
            timestamp=_ts(0),
        )
        completion = _event(
            event_type="tool.completed",
            data={"text": "ok"},
            causation_id=request.event_id,
            timestamp=_ts(2),
        )
        world = _world([request, completion])
        # bump_min_agents=2: requires cross-agent match.
        sys = SolutionExtractorSystem(bump_min_agents=2)
        out = sys(world)
        assert out == []


class TestSolutionExtractorSystemNoIOPure:
    def test_system_does_not_import_io_modules(self) -> None:
        """The system is pure: no Redis, no FalkorDB
        imports. The system reads from the World
        only.
        """
        import kntgraph.agents.memory.solution_extractor as mod

        source = mod.__file__
        with open(source, "r", encoding="utf-8") as f:
            content = f.read()
        # No I/O module imports.
        for forbidden in (
            "kntgraph.infra.redis",
            "falkordb",
            "falkordblite",
            "redis.asyncio",
        ):
            assert forbidden not in content, (
                f"SolutionExtractorSystem must not import {forbidden}; "
                f"it is a pure system."
            )
