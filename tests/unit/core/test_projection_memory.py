# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the memory hydration projection
(ADR-042).

The ``project_memory`` projection materialises
``SessionComponent``, ``ProfileComponent``, and
``ContinuityComponent`` from ``session.*`` /
``profile.*`` / ``continuity.*`` events on the
``AgentView``. It is a **pure** function
(ADR-034): deterministic, replayable, no side
effects.

This test is the deletion gate. If a future
refactor removes the projection or breaks its
contract, this test fails.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from uuid import UUID


from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.event.event import Event
from kntgraph.core.components.memory import (
    ContinuityComponent,
    ProfileComponent,
    SessionComponent,
)
from kntgraph.core.world.projection_memory import project_memory
from kntgraph.core.world.view import AgentView


def _ts(offset_s: int = 0) -> datetime:
    base = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    from datetime import timedelta

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
        data=dict(data or {}),
        correlation=CorrelationContext.new(),
        causation_id=causation_id,
        timestamp=timestamp or _ts(),
    )


class TestProjectMemoryEmpty:
    def test_empty_events_returns_empty_views(self) -> None:
        """No events → no views (the projection is
        no-op for empty input)."""
        out = project_memory([])
        assert out == {}


class TestProjectMemorySession:
    def test_session_started_creates_session_component(self) -> None:
        """A ``session.started`` event materialises
        the ``SessionComponent`` (with
        ``messages=()``, ``ended_at=None``)."""
        # ``session_id`` is derived from ``agent_id``
        # (strip the ``session:`` prefix). The
        # ``data["session_id"]`` field is the
        # canonical session id; the projection
        # reads it from the event payload.
        event = _event(
            event_type="session.started",
            agent_id="session:s1",
            data={"user_id": "u1", "tenant_id": "t1"},
        )
        out = project_memory([event])
        view = out["session:s1"]
        sc = view.components.get(SessionComponent)
        assert sc is not None
        assert sc.session_id == "s1"
        assert sc.user_id == "u1"
        assert sc.tenant_id == "t1"
        assert sc.messages == ()
        assert sc.ended_at is None

    def test_session_message_appends_to_messages(self) -> None:
        """A ``session.message`` event appends a
        message to ``SessionComponent.messages``."""
        start = _event(
            event_type="session.started",
            agent_id="session:s1",
            data={"user_id": "u1", "tenant_id": "t1"},
        )
        msg1 = _event(
            event_type="session.message",
            agent_id="session:s1",
            data={"role": "user", "content": "hello"},
        )
        msg2 = _event(
            event_type="session.message",
            agent_id="session:s1",
            data={"role": "assistant", "content": "hi"},
        )
        out = project_memory([start, msg1, msg2])
        sc = out["session:s1"].components[SessionComponent]
        assert len(sc.messages) == 2
        assert sc.messages[0] == {"role": "user", "content": "hello"}
        assert sc.messages[1] == {"role": "assistant", "content": "hi"}

    def test_session_ended_sets_ended_at(self) -> None:
        """A ``session.ended`` event sets
        ``SessionComponent.ended_at``."""
        start = _event(
            event_type="session.started",
            agent_id="session:s1",
            data={"user_id": "u1", "tenant_id": "t1"},
            timestamp=_ts(0),
        )
        end = _event(
            event_type="session.ended",
            agent_id="session:s1",
            timestamp=_ts(120),
        )
        out = project_memory([start, end])
        sc = out["session:s1"].components[SessionComponent]
        assert sc.ended_at is not None
        # ``ended_at`` is a ``datetime`` matching the
        # ``session.ended`` event's timestamp.
        assert sc.ended_at == end.timestamp

    def test_session_context_keyvalue(self) -> None:
        """A ``session.context`` event sets a key
        in ``SessionComponent.context``."""
        start = _event(
            event_type="session.started",
            agent_id="session:s1",
            data={"user_id": "u1", "tenant_id": "t1"},
        )
        ctx = _event(
            event_type="session.context",
            agent_id="session:s1",
            data={"key": "channel", "value": "demo"},
        )
        out = project_memory([start, ctx])
        sc = out["session:s1"].components[SessionComponent]
        assert sc.context.get("channel") == "demo"

    def test_session_intent_event_id_tracks_last_domain_event(self) -> None:
        """``SessionComponent.intent_event_id`` is the
        ``event_id`` of the **last domain event** in
        the batch (the ``user.intent`` that triggered
        this turn). Tool events do NOT clobber the
        intent_event_id (per ADR-045 + the projection's
        filter for ``session.*`` / ``tool.*``)."""
        start = _event(
            event_type="session.started",
            agent_id="session:s1",
            data={"user_id": "u1", "tenant_id": "t1"},
        )
        intent = _event(
            event_type="user.intent",
            agent_id="session:s1",
            data={"intent": "chat"},
        )
        # A tool event lands AFTER the user.intent
        # (the system emitted a tool call). The
        # intent_event_id should still be the
        # user.intent's eid, NOT the tool event's
        # eid.
        tool = _event(
            event_type="tool.chat_llm.requested",
            agent_id="session:s1",
            data={"tool": "chat_llm"},
        )
        out = project_memory([start, intent, tool])
        sc = out["session:s1"].components[SessionComponent]
        assert sc.intent_event_id == str(intent.event_id)
        # The tool event is NOT a session event; it
        # does not affect the messages / context /
        # started_at / etc.
        assert len(sc.messages) == 0


class TestProjectMemoryProfile:
    def test_profile_created_creates_profile_component(self) -> None:
        """A ``profile.created`` event materialises
        the ``ProfileComponent`` (with the initial
        preferences and tier)."""
        event = _event(
            event_type="profile.created",
            agent_id="agent-1",
            data={
                "tenant_id": "t1",
                "user_id": "u1",
                "preferences": {"language": "pt-BR", "tone": "formal"},
                "tier": "premium",
            },
        )
        out = project_memory([event])
        view = out["agent-1"]
        pc = view.components.get(ProfileComponent)
        assert pc is not None
        assert pc.tenant_id == "t1"
        assert pc.user_id == "u1"
        assert pc.preferences == {"language": "pt-BR", "tone": "formal"}
        assert pc.tier == "premium"

    def test_profile_preference_set_updates_preferences(self) -> None:
        """A ``profile.preference_set`` event sets a
        key in ``ProfileComponent.preferences``."""
        created = _event(
            event_type="profile.created",
            data={"tenant_id": "t1", "user_id": "u1", "tier": "standard"},
        )
        set_lang = _event(
            event_type="profile.preference_set",
            data={"key": "language", "value": "en"},
        )
        set_tone = _event(
            event_type="profile.preference_set",
            data={"key": "tone", "value": "casual"},
        )
        out = project_memory([created, set_lang, set_tone])
        pc = out["agent-1"].components[ProfileComponent]
        assert pc.preferences == {"language": "en", "tone": "casual"}
        # ``updated_at`` is the timestamp of the
        # latest preference change.
        assert pc.updated_at is not None
        assert pc.updated_at == set_tone.timestamp

    def test_profile_tier_changed_updates_tier(self) -> None:
        """A ``profile.tier_changed`` event updates
        ``ProfileComponent.tier``."""
        created = _event(
            event_type="profile.created",
            data={"tenant_id": "t1", "user_id": "u1", "tier": "standard"},
        )
        upgraded = _event(
            event_type="profile.tier_changed",
            data={"tier": "premium"},
        )
        out = project_memory([created, upgraded])
        pc = out["agent-1"].components[ProfileComponent]
        assert pc.tier == "premium"

    def test_profile_preference_unset_removes_key(self) -> None:
        """A ``profile.preference_unset`` event
        removes a key from
        ``ProfileComponent.preferences``."""
        created = _event(
            event_type="profile.created",
            data={
                "tenant_id": "t1",
                "user_id": "u1",
                "preferences": {"language": "en", "tone": "casual"},
            },
        )
        unset = _event(
            event_type="profile.preference_unset",
            data={"key": "tone"},
        )
        out = project_memory([created, unset])
        pc = out["agent-1"].components[ProfileComponent]
        assert pc.preferences == {"language": "en"}


class TestProjectMemoryContinuity:
    def test_continuity_created_creates_continuity_component(self) -> None:
        """A ``continuity.created`` event materialises
        the ``ContinuityComponent`` (with empty
        tools / entities / categories)."""
        event = _event(
            event_type="continuity.created",
            data={"tenant_id": "t1", "user_id": "u1"},
        )
        out = project_memory([event])
        view = out["agent-1"]
        cc = view.components.get(ContinuityComponent)
        assert cc is not None
        assert cc.tenant_id == "t1"
        assert cc.user_id == "u1"
        assert cc.last_tools == {}
        assert cc.last_entities == {}
        assert cc.last_categories == {}
        assert cc.cleared_at is None

    def test_continuity_tool_used_records_last_tools(self) -> None:
        """A ``continuity.tool_used`` event records
        the tool's last-used timestamp in
        ``ContinuityComponent.last_tools``."""
        created = _event(
            event_type="continuity.created",
            data={"tenant_id": "t1", "user_id": "u1"},
        )
        used = _event(
            event_type="continuity.tool_used",
            data={
                "tool": "weather_api",
                "result_signature": "abc123",
            },
            timestamp=_ts(60),
        )
        out = project_memory([created, used])
        cc = out["agent-1"].components[ContinuityComponent]
        assert "weather_api" in cc.last_tools
        # The value is "result_signature|timestamp".
        assert "abc123" in cc.last_tools["weather_api"]

    def test_continuity_cleared_resets_state(self) -> None:
        """A ``continuity.cleared`` event resets
        ``last_tools`` / ``last_entities`` /
        ``last_categories`` to empty dicts and sets
        ``cleared_at``."""
        created = _event(
            event_type="continuity.created",
            data={"tenant_id": "t1", "user_id": "u1"},
        )
        used = _event(
            event_type="continuity.tool_used",
            data={"tool": "weather_api", "result_signature": "abc"},
            timestamp=_ts(60),
        )
        cleared = _event(
            event_type="continuity.cleared",
            timestamp=_ts(120),
        )
        out = project_memory([created, used, cleared])
        cc = out["agent-1"].components[ContinuityComponent]
        assert cc.last_tools == {}
        assert cc.last_entities == {}
        assert cc.last_categories == {}
        assert cc.cleared_at is not None


class TestProjectMemoryMultiAgent:
    def test_multi_agent_keeps_separate_components(self) -> None:
        """Two agents each have their own Session /
        Profile / Continuity. The projection keeps
        them separate (no cross-agent leakage)."""
        e1 = _event(
            event_type="session.started",
            agent_id="session:s1",
            data={"user_id": "u1", "tenant_id": "t1"},
        )
        e2 = _event(
            event_type="session.started",
            agent_id="session:s2",
            data={"user_id": "u2", "tenant_id": "t2"},
        )
        out = project_memory([e1, e2])
        sc1 = out["session:s1"].components[SessionComponent]
        sc2 = out["session:s2"].components[SessionComponent]
        assert sc1.session_id == "s1"
        assert sc2.session_id == "s2"
        # The two are independent.
        assert sc1.user_id == "u1"
        assert sc2.user_id == "u2"


class TestProjectMemoryMultiTick:
    def test_session_preserved_across_ticks_via_base_session(self) -> None:
        """Tick N has a ``session.message``; tick N+1
        has another. The ``SessionComponent.messages``
        tuple accumulates across ticks (multi-tick
        safe via ``base_views``).

        This is the contract pinned by
        ``_fold_session(..., base_session=...)``: the
        base component is reused, and the new events
        are appended.
        """
        tick1 = [
            _event(
                event_type="session.started",
                agent_id="session:s1",
                data={"user_id": "u1", "tenant_id": "t1"},
            ),
            _event(
                event_type="session.message",
                agent_id="session:s1",
                data={"role": "user", "content": "hello"},
            ),
        ]
        view1 = project_memory(tick1)["session:s1"]
        sc1 = view1.components[SessionComponent]
        assert len(sc1.messages) == 1
        # Tick N+1: the projection is called with
        # ``base_views={"session:s1": view1}`` so the
        # base component is preserved.
        tick2 = [
            _event(
                event_type="session.message",
                agent_id="session:s1",
                data={"role": "assistant", "content": "hi"},
            ),
        ]
        view2 = project_memory(tick2, base_views={"session:s1": view1})["session:s1"]
        sc2 = view2.components[SessionComponent]
        # The base messages are preserved.
        assert len(sc2.messages) == 2
        assert sc2.messages[0] == {"role": "user", "content": "hello"}
        assert sc2.messages[1] == {"role": "assistant", "content": "hi"}
        # The base user_id / tenant_id are preserved.
        assert sc2.user_id == "u1"
        assert sc2.tenant_id == "t1"

    def test_profile_preference_preserved_across_ticks(self) -> None:
        """Tick N sets a preference; tick N+1 sets
        another. The accumulated preferences
        dictionary is preserved across ticks (the
        base component is reused)."""
        tick1 = [
            _event(
                event_type="profile.created",
                agent_id="agent-p1",
                data={
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "preferences": {"language": "en"},
                },
            ),
        ]
        view1 = project_memory(tick1)["agent-p1"]
        pc1 = view1.components[ProfileComponent]
        assert pc1.preferences == {"language": "en"}
        tick2 = [
            _event(
                event_type="profile.preference_set",
                agent_id="agent-p1",
                data={"key": "tone", "value": "formal"},
            ),
        ]
        view2 = project_memory(tick2, base_views={"agent-p1": view1})["agent-p1"]
        pc2 = view2.components[ProfileComponent]
        # The base "language" preference is preserved.
        assert pc2.preferences == {"language": "en", "tone": "formal"}


# Helper for the multi-tick test (inline, kept for
# readability).
def _dummy_helper(view: AgentView) -> dict:
    return view.components
