# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the pure fold functions in memory/.
"""

from __future__ import annotations

import uuid


from kntgraph.core.event import Event, CorrelationContext
from kntgraph.memory.profile import (
    ProfileEventType,
    _fold_profile_events,
)
from kntgraph.memory.session import (
    SessionEventType,
    _fold_session_events,
)


def _make_session_started(session_id, user_id, tenant_id, t=None):
    return Event.domain_from(
        agent_id=f"session:{session_id}",
        type=SessionEventType.STARTED,
        data={
            "session_id": session_id,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "metadata": {"channel": "web"},
        },
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


def _make_session_message(session_id, role, content, t=None):
    return Event.domain_from(
        agent_id=f"session:{session_id}",
        type=SessionEventType.MESSAGE,
        data={"role": role, "content": content},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


def _make_session_context(session_id, key, value, t=None):
    return Event.domain_from(
        agent_id=f"session:{session_id}",
        type=SessionEventType.CONTEXT,
        data={"key": key, "value": value},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


def _make_session_ended(session_id, t=None):
    return Event.domain_from(
        agent_id=f"session:{session_id}",
        type=SessionEventType.ENDED,
        data={},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


class TestSessionFold:
    def test_no_started_returns_none(self):
        state = _fold_session_events("sid", [])
        assert state is None

    def test_minimal_started(self):
        e = _make_session_started("sid", "u-1", "t-1")
        state = _fold_session_events("sid", [e])
        assert state is not None
        assert state.session_id == "sid"
        assert state.user_id == "u-1"
        assert state.tenant_id == "t-1"
        assert state.messages == ()
        assert state.context == {"channel": "web"}
        assert state.is_active()

    def test_messages_aggregate(self):
        events = [
            _make_session_started("sid", "u", "t"),
            _make_session_message("sid", "user", "olá"),
            _make_session_message("sid", "assistant", "oi!"),
            _make_session_message("sid", "user", "tudo bem?"),
        ]
        state = _fold_session_events("sid", events)
        assert state is not None
        assert len(state.messages) == 3
        assert state.messages[0]["content"] == "olá"
        assert state.messages[1]["role"] == "assistant"

    def test_context_overrides(self):
        events = [
            _make_session_started("sid", "u", "t"),
            _make_session_context("sid", "scratchpad", {"todo": "x"}),
            _make_session_context("sid", "scratchpad", {"todo": "y"}),
        ]
        state = _fold_session_events("sid", events)
        assert state.context["scratchpad"] == {"todo": "y"}

    def test_ended_marks_inactive(self):
        events = [
            _make_session_started("sid", "u", "t"),
            _make_session_ended("sid"),
        ]
        state = _fold_session_events("sid", events)
        assert not state.is_active()
        assert state.ended_at is not None


# -----------------------------------------------------------------------------
# Profile fold
# -----------------------------------------------------------------------------


def _make_profile_created(tenant_id, user_id, preferences=None, tier="standard"):
    return Event.domain_from(
        agent_id=f"profile:{tenant_id}:{user_id}",
        type=ProfileEventType.CREATED,
        data={
            "tenant_id": tenant_id,
            "user_id": user_id,
            "preferences": dict(preferences or {}),
            "tier": tier,
        },
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


def _make_pref_set(tenant_id, user_id, key, value):
    return Event.domain_from(
        agent_id=f"profile:{tenant_id}:{user_id}",
        type=ProfileEventType.PREFERENCE_SET,
        data={"key": key, "value": value},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


def _make_pref_unset(tenant_id, user_id, key):
    return Event.domain_from(
        agent_id=f"profile:{tenant_id}:{user_id}",
        type=ProfileEventType.PREFERENCE_UNSET,
        data={"key": key},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


def _make_tier_changed(tenant_id, user_id, from_tier, to_tier):
    return Event.domain_from(
        agent_id=f"profile:{tenant_id}:{user_id}",
        type=ProfileEventType.TIER_CHANGED,
        data={"from_tier": from_tier, "to_tier": to_tier},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


class TestProfileFold:
    def test_no_created_returns_none(self):
        state = _fold_profile_events("t", "u", [])
        assert state is None

    def test_created_only(self):
        e = _make_profile_created("t", "u", {"lang": "pt-BR"}, tier="vip")
        state = _fold_profile_events("t", "u", [e])
        assert state is not None
        assert state.preferences == {"lang": "pt-BR"}
        assert state.tier == "vip"

    def test_pref_set_adds(self):
        events = [
            _make_profile_created("t", "u"),
            _make_pref_set("t", "u", "lang", "pt-BR"),
            _make_pref_set("t", "u", "currency", "BRL"),
        ]
        state = _fold_profile_events("t", "u", events)
        assert state.preferences == {"lang": "pt-BR", "currency": "BRL"}

    def test_pref_set_overrides(self):
        events = [
            _make_profile_created("t", "u", {"lang": "pt-BR"}),
            _make_pref_set("t", "u", "lang", "en-US"),
        ]
        state = _fold_profile_events("t", "u", events)
        assert state.preferences == {"lang": "en-US"}

    def test_pref_unset_removes(self):
        events = [
            _make_profile_created("t", "u", {"lang": "pt-BR"}),
            _make_pref_set("t", "u", "currency", "BRL"),
            _make_pref_unset("t", "u", "lang"),
        ]
        state = _fold_profile_events("t", "u", events)
        assert "lang" not in state.preferences
        assert state.preferences["currency"] == "BRL"

    def test_tier_change_overrides(self):
        events = [
            _make_profile_created("t", "u", tier="standard"),
            _make_tier_changed("t", "u", "standard", "vip"),
        ]
        state = _fold_profile_events("t", "u", events)
        assert state.tier == "vip"

    def test_fold_is_pure(self):
        events = [
            _make_profile_created("t", "u", {"lang": "pt-BR"}),
            _make_pref_set("t", "u", "currency", "BRL"),
        ]
        s1 = _fold_profile_events("t", "u", events)
        s2 = _fold_profile_events("t", "u", events)
        assert s1 == s2

    def test_deterministic_across_replay(self):
        """
        Different invocations with the same events produce the
        same state (the fold is pure).
        """
        events = [
            _make_profile_created("t", "u", {"lang": "pt-BR"}),
            _make_pref_set("t", "u", "lang", "en-US"),
            _make_pref_set("t", "u", "currency", "BRL"),
        ]
        s1 = _fold_profile_events("t", "u", events)
        s2 = _fold_profile_events("t", "u", list(events))
        assert s1.preferences == s2.preferences
        assert s1.tier == s2.tier
