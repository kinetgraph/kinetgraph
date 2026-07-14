# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the ``ToolCallTTLSweeperSystem``
(ADR-045).

The sweeper is a ``WorldSystem`` that runs once per
tick in the ``ReactiveDispatcher``. On each
invocation it walks the ``tool_requests`` slot of
every agent's view and emits a
``tool.<name>.failed`` event for every request whose
``expires_at`` is in the past.

These tests exercise the sweeper in isolation (a
``World`` is built manually and passed to the
sweeper; the emitted events are asserted).
"""

from __future__ import annotations

from datetime import datetime, timezone


from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.core.world.components import ToolCallTTL
from kntgraph.core.world.projection_tool_calls import (
    project_tool_calls,
)
from kntgraph.runner.tool_call_ttl_sweeper import (
    ToolCallTTLSweeperSystem,
)


AGENT_ID = "agent-ttl-test"


def _ctx() -> CorrelationContext:
    return CorrelationContext.new()


def _ts(offset_s: int = 0) -> datetime:
    base = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    from datetime import timedelta

    return base + timedelta(seconds=offset_s)


def _request_event(*, tool_name: str, ts: datetime | None = None) -> Event:
    return Event.create(
        event_type=f"tool.{tool_name}.requested",
        agent_id=AGENT_ID,
        event_class="domain",
        data={"tool": tool_name, "params": {}},
        correlation=_ctx(),
        timestamp=ts or _ts(),
    )


def _world_with_request(request: Event, ttl_seconds: float = 300.0) -> World:
    """Build a World whose ``tool_requests`` slot
    contains the given request (with the given
    TTL)."""
    from kntgraph.core.world.projection_tool_calls import (
        project_tool_calls,
    )

    return project_tool_calls(
        [request],
        ttl=ToolCallTTL(default_ttl_seconds=ttl_seconds),
    )


class TestSweeperEmits:
    def test_stale_request_emits_failed_event(self) -> None:
        """A request whose ``expires_at`` is in the
        past at sweeper time triggers a
        ``tool.<name>.failed`` event.
        """
        request = _request_event(tool_name="chat_llm", ts=_ts(0))
        world = _world_with_request(request, ttl_seconds=300.0)
        # ``now`` is 10 minutes after the request
        # (well past the 5-minute TTL).
        now = _ts(600)
        sweeper = ToolCallTTLSweeperSystem(now=now)
        events = sweeper(world)
        # One failed event, for the chat_llm request.
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "tool.chat_llm.failed"
        assert ev.agent_id == AGENT_ID
        assert ev.data["error"] == "ttl_expired"
        assert ev.data["request_event_id"] == str(request.event_id)
        assert ev.data["tool_name"] == "chat_llm"
        assert ev.causation_id == request.event_id
        # The correlation is the request's flow id.
        assert ev.correlation.correlation_id == request.correlation.correlation_id

    def test_fresh_request_emits_nothing(self) -> None:
        """A request whose ``expires_at`` is in the
        future at sweeper time does NOT trigger a
        failed event.
        """
        request = _request_event(tool_name="fast_tool", ts=_ts(0))
        world = _world_with_request(request, ttl_seconds=300.0)
        # ``now`` is 1 second after the request (well
        # within the 5-minute TTL).
        now = _ts(1)
        sweeper = ToolCallTTLSweeperSystem(now=now)
        events = sweeper(world)
        assert events == []

    def test_ttl_disabled_emits_nothing(self) -> None:
        """A request with ``expires_at=None`` (TTL
        disabled, via ``ToolCallTTL(default_ttl_seconds=0)``)
        is never emitted as failed (the operator has
        explicitly opted out of the safety net).
        """
        request = _request_event(tool_name="no_ttl", ts=_ts(0))
        # ``ttl_seconds=0`` => ``expires_at=None``.
        world = project_tool_calls(
            [request],
            ttl=ToolCallTTL(default_ttl_seconds=0),
        )
        now = _ts(10**8)  # 10000 days later; would be
        # ``expired`` if the TTL was enabled.
        sweeper = ToolCallTTLSweeperSystem(now=now)
        events = sweeper(world)
        assert events == []


class TestSweeperDedup:
    def test_dedup_emits_once_for_same_request(self) -> None:
        """A request that stays in the slot across
        multiple ticks (because the completion never
        arrives) triggers AT MOST ONE failed event.
        """
        request = _request_event(tool_name="stuck", ts=_ts(0))
        world = _world_with_request(request, ttl_seconds=300.0)
        sweeper = ToolCallTTLSweeperSystem(now=_ts(600))
        # First tick: emit the failed event.
        events1 = sweeper(world)
        assert len(events1) == 1
        # Second tick (same world; no new events): the
        # sweeper remembers the request_id and does
        # not emit again.
        events2 = sweeper(world)
        assert events2 == []
        # Third tick: still no duplicate.
        events3 = sweeper(world)
        assert events3 == []

    def test_dedup_scoped_to_instance(self) -> None:
        """Two sweeper instances do NOT share dedup
        memory; a fresh instance re-emits the failed
        event (the in-memory dedup is per-process; a
        process restart is expected to re-derive the
        dedup from the EventLog, which is the
        downstream consumer's responsibility).
        """
        request = _request_event(tool_name="x", ts=_ts(0))
        world = _world_with_request(request, ttl_seconds=300.0)
        # First sweeper instance.
        s1 = ToolCallTTLSweeperSystem(now=_ts(600))
        evs1 = s1(world)
        assert len(evs1) == 1
        # Second instance: re-emits.
        s2 = ToolCallTTLSweeperSystem(now=_ts(700))
        evs2 = s2(world)
        assert len(evs2) == 1
        assert evs2[0].data["request_event_id"] == str(request.event_id)


class TestSweeperMultiAgent:
    def test_sweeper_handles_multiple_agents(self) -> None:
        """The sweeper walks every agent in the World
        and emits a failed event for each stale
        request, regardless of agent.
        """
        r1 = Event.create(
            event_type="tool.chat_llm.requested",
            agent_id="agent-1",
            event_class="domain",
            data={"tool": "chat_llm", "params": {}},
            correlation=_ctx(),
            timestamp=_ts(0),
        )
        r2 = Event.create(
            event_type="tool.transcoder.requested",
            agent_id="agent-2",
            event_class="domain",
            data={"tool": "transcoder", "params": {}},
            correlation=_ctx(),
            timestamp=_ts(0),
        )
        world = project_tool_calls(
            [r1, r2],
            ttl=ToolCallTTL(default_ttl_seconds=300.0),
        )
        now = _ts(600)
        sweeper = ToolCallTTLSweeperSystem(now=now)
        events = sweeper(world)
        # Two failed events, one per agent.
        assert len(events) == 2
        by_agent = {e.agent_id: e for e in events}
        assert "agent-1" in by_agent
        assert "agent-2" in by_agent
        assert by_agent["agent-1"].event_type == "tool.chat_llm.failed"
        assert by_agent["agent-2"].event_type == "tool.transcoder.failed"

    def test_sweeper_emits_only_for_stale_in_multi_agent(self) -> None:
        """The sweeper emits failed events only for
        stale requests; fresh requests are
        preserved.
        """
        r_old = Event.create(
            event_type="tool.slow.requested",
            agent_id="agent-old",
            event_class="domain",
            data={"tool": "slow", "params": {}},
            correlation=_ctx(),
            timestamp=_ts(0),
        )
        r_fresh = Event.create(
            event_type="tool.fast.requested",
            agent_id="agent-fresh",
            event_class="domain",
            data={"tool": "fast", "params": {}},
            correlation=_ctx(),
            timestamp=_ts(290),  # 10s before TTL
        )
        world = project_tool_calls(
            [r_old, r_fresh],
            ttl=ToolCallTTL(default_ttl_seconds=300.0),
        )
        # ``now`` is at t=310; r_old is stale (TTL was
        # at t=300), r_fresh is fresh (TTL at t=590).
        now = _ts(310)
        sweeper = ToolCallTTLSweeperSystem(now=now)
        events = sweeper(world)
        assert len(events) == 1
        assert events[0].agent_id == "agent-old"
        assert events[0].event_type == "tool.slow.failed"


class TestSweeperEmptyWorld:
    def test_empty_world_emits_nothing(self) -> None:
        """The sweeper handles an empty World (no
        agents) gracefully: emits nothing.
        """
        sweeper = ToolCallTTLSweeperSystem(now=_ts(0))
        events = sweeper(World.empty())
        assert events == []


class TestSweeperFailsLoudForUnknownTool:
    def test_request_with_empty_tool_name_still_emits(self) -> None:
        """A request with an empty ``tool_name`` (the
        legacy bare ``tool.requested`` form) still
        triggers a failure event (the failure event's
        ``event_type`` is ``tool.unknown.failed``; the
        sweeper does not crash on an empty tool name).
        """
        request = Event.create(
            event_type="tool.requested",  # bare form
            agent_id=AGENT_ID,
            event_class="domain",
            data={"tool": "x", "params": {}},
            correlation=_ctx(),
            timestamp=_ts(0),
        )
        # The bare form yields ``tool_name="x"`` in
        # the request (the projection falls back to
        # ``data["tool"]``; see ``_build_request``).
        # The TTL is still applied.
        world = project_tool_calls(
            [request],
            ttl=ToolCallTTL(default_ttl_seconds=300.0),
        )
        now = _ts(600)
        sweeper = ToolCallTTLSweeperSystem(now=now)
        events = sweeper(world)
        assert len(events) == 1
        assert events[0].event_type == "tool.x.failed"
        assert events[0].data["tool_name"] == "x"
