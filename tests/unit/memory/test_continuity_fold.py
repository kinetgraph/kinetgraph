# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the pure fold function in
``memory/continuity.py`` (ADR-014).

Tests the fold behaviour directly with synthetic events;
no Redis I/O.
"""

from __future__ import annotations

import uuid


from kntgraph.core.event import Event, CorrelationContext
from kntgraph.memory.continuity import (
    ContinuityEventType,
    ContinuityManager,
    _fold_continuity_events,
)


def _make_continuity_created(tenant_id, user_id):
    return Event.domain_from(
        agent_id=f"continuity:{tenant_id}:{user_id}",
        type=ContinuityEventType.CREATED,
        data={"tenant_id": tenant_id, "user_id": user_id},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


def _make_tool_used(
    tenant_id,
    user_id,
    tool,
    params_fingerprint="sha256:abc",
    result_signature="sha256:def",
    latency_ms=100,
):
    return Event.domain_from(
        agent_id=f"continuity:{tenant_id}:{user_id}",
        type=ContinuityEventType.TOOL_USED,
        data={
            "tool": tool,
            "params_fingerprint": params_fingerprint,
            "result_signature": result_signature,
            "latency_ms": latency_ms,
        },
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


def _make_entity_seen(tenant_id, user_id, kind, value_hash, source):
    return Event.domain_from(
        agent_id=f"continuity:{tenant_id}:{user_id}",
        type=ContinuityEventType.ENTITY_SEEN,
        data={
            "kind": kind,
            "value_hash": value_hash,
            "source": source,
        },
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


def _make_category_chosen(tenant_id, user_id, slot, value):
    return Event.domain_from(
        agent_id=f"continuity:{tenant_id}:{user_id}",
        type=ContinuityEventType.CATEGORY_CHOSEN,
        data={"slot": slot, "value": value},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


def _make_cleared(tenant_id, user_id, reason="lgpd_erasure"):
    return Event.domain_from(
        agent_id=f"continuity:{tenant_id}:{user_id}",
        type=ContinuityEventType.CLEARED,
        data={"reason": reason},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


class TestContinuityFold:
    def test_no_created_returns_none(self):
        state = _fold_continuity_events("t", "u", [])
        assert state is None

    def test_created_only(self):
        e = _make_continuity_created("t", "u")
        state = _fold_continuity_events("t", "u", [e])
        assert state is not None
        assert state.tenant_id == "t"
        assert state.user_id == "u"
        assert state.last_tools == {}
        assert state.last_entities == {}
        assert state.last_categories == {}
        assert state.cleared_at is None

    def test_tool_used_records_signature(self):
        events = [
            _make_continuity_created("t", "u"),
            _make_tool_used(
                "t",
                "u",
                "invoice.issue",
                result_signature="sha256:zzz",
                latency_ms=250,
            ),
        ]
        state = _fold_continuity_events("t", "u", events)
        assert state is not None
        assert "invoice.issue" in state.last_tools
        assert "sha256:zzz" in state.last_tools["invoice.issue"]
        assert "250" in state.last_tools["invoice.issue"]

    def test_tool_used_overrides_previous_for_same_tool(self):
        """
        Latest tool_used for the same tool name wins. Earlier
        records are overwritten (sliding recency).
        """
        events = [
            _make_continuity_created("t", "u"),
            _make_tool_used(
                "t",
                "u",
                "invoice.issue",
                result_signature="sha256:old",
                latency_ms=100,
            ),
            _make_tool_used(
                "t",
                "u",
                "invoice.issue",
                result_signature="sha256:new",
                latency_ms=200,
            ),
        ]
        state = _fold_continuity_events("t", "u", events)
        assert "sha256:new" in state.last_tools["invoice.issue"]

    def test_entity_seen_uses_value_hash(self):
        events = [
            _make_continuity_created("t", "u"),
            _make_entity_seen(
                "t",
                "u",
                "cnpj",
                ContinuityManager.hash_value("12.345.678/0001-90"),
                "tool_result",
            ),
        ]
        state = _fold_continuity_events("t", "u", events)
        assert state is not None
        # Keyed by "kind:value_hash"
        keys = list(state.last_entities.keys())
        assert len(keys) == 1
        assert keys[0].startswith("cnpj:sha256:")

    def test_category_chosen_stores_value(self):
        events = [
            _make_continuity_created("t", "u"),
            _make_category_chosen("t", "u", "cfop", "6102"),
        ]
        state = _fold_continuity_events("t", "u", events)
        assert state is not None
        assert state.last_categories["cfop"].startswith("6102|")

    def test_category_overrides_previous(self):
        events = [
            _make_continuity_created("t", "u"),
            _make_category_chosen("t", "u", "cfop", "5102"),
            _make_category_chosen("t", "u", "cfop", "6102"),
        ]
        state = _fold_continuity_events("t", "u", events)
        assert state.last_categories["cfop"].startswith("6102|")

    def test_cleared_resets_state(self):
        events = [
            _make_continuity_created("t", "u"),
            _make_category_chosen("t", "u", "cfop", "6102"),
            _make_tool_used("t", "u", "invoice.issue"),
            _make_cleared("t", "u", reason="lgpd_erasure"),
        ]
        state = _fold_continuity_events("t", "u", events)
        assert state is not None
        assert state.is_cleared()
        assert state.last_tools == {}
        assert state.last_entities == {}
        assert state.last_categories == {}

    def test_post_clear_events_start_fresh(self):
        """
        Events after a clear are valid and populate the
        state from scratch. They do NOT replay pre-clear
        data.
        """
        events = [
            _make_continuity_created("t", "u"),
            _make_category_chosen("t", "u", "cfop", "5102"),
            _make_cleared("t", "u"),
            _make_category_chosen("t", "u", "cfop", "6102"),
        ]
        state = _fold_continuity_events("t", "u", events)
        assert state is not None
        assert state.is_cleared() is False
        assert state.last_categories["cfop"].startswith("6102|")
        # Pre-clear tools must be empty post-clear.
        assert state.last_tools == {}

    def test_fold_is_pure(self):
        events = [
            _make_continuity_created("t", "u"),
            _make_tool_used("t", "u", "invoice.issue"),
            _make_category_chosen("t", "u", "cfop", "6102"),
        ]
        s1 = _fold_continuity_events("t", "u", events)
        s2 = _fold_continuity_events("t", "u", events)
        assert s1 == s2

    def test_deterministic_across_replay(self):
        events = [
            _make_continuity_created("t", "u"),
            _make_category_chosen("t", "u", "cfop", "5102"),
            _make_category_chosen("t", "u", "cfop", "6102"),
            _make_tool_used(
                "t",
                "u",
                "invoice.issue",
                result_signature="sha256:abc",
                latency_ms=42,
            ),
        ]
        s1 = _fold_continuity_events("t", "u", events)
        s2 = _fold_continuity_events("t", "u", list(events))
        assert s1.last_tools == s2.last_tools
        assert s1.last_categories == s2.last_categories
        assert s1.created_at == s2.created_at
        assert s1.updated_at == s2.updated_at


class TestContinuityHashValue:
    """
    The hash_value helper is the PII gate: it MUST be the
    only way callers convert a raw entity value into the
    stored fingerprint.
    """

    def test_returns_sha256_prefixed_hex(self):
        h = ContinuityManager.hash_value("12.345.678/0001-90")
        assert h.startswith("sha256:")
        # 16 hex chars after the prefix.
        assert len(h) == len("sha256:") + 16

    def test_same_input_same_output(self):
        h1 = ContinuityManager.hash_value("abc")
        h2 = ContinuityManager.hash_value("abc")
        assert h1 == h2

    def test_different_input_different_output(self):
        h1 = ContinuityManager.hash_value("aaa")
        h2 = ContinuityManager.hash_value("bbb")
        assert h1 != h2
