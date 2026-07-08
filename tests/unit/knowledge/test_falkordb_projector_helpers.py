# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``FalkorDBProjector`` helpers.

The helpers (`_categorize_events`, `_agent_node_params`,
`_document_node_params`, `_tool_call_node_params`,
`_edge_params`) are pure functions over the Event list. They
do NOT touch FalkorDB — the projector pulls them together
into a single async `_project_agent` call.

These tests exist because the original `_project_agent` was
a god-method (CC=9) that interleaved classification,
parameter building, and Cypher emission in one block. The
split (Iter 8, ADR-019 epílogo) makes each step testable
in isolation.
"""

from __future__ import annotations

import json
from uuid import uuid4

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.tool_event import ToolEventKind


def _make_event(
    *,
    agent_id: str = "session-42",
    event_type: str = "x",
    event_class: str = "domain",
    data: dict | None = None,
) -> Event:
    return Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class=event_class,
        data=data or {"k": "v"},
        correlation=CorrelationContext(
            correlation_id=uuid4(),
            causation_id=None,
            span_id=uuid4(),
        ),
    )


# ---------------------------------------------------------------------------
# _categorize_events
# ---------------------------------------------------------------------------


class TestCategorizeEvents:
    """The categoriser routes an Event to one of three buckets:

    - ``tool_call_completed``: ``tool.<x>.completed`` events
    - ``tool_call_failed``: ``tool.<x>.failed`` events
    - ``document_candidate``: ``nf.*`` or ``company.*`` events
    - ignored: everything else
    """

    def test_completed_tool_event(self):
        from kntgraph.knowledge.falkordb.adapter import (
            _categorize_events,
        )

        e = _make_event(
            event_type="tool.invoice.issue.completed",
            data={"tool": "invoice.issue", "request_id": "abc"},
        )
        result = _categorize_events([e])
        assert len(result.completed_tool_events) == 1
        assert result.document_candidates == []

    def test_failed_tool_event(self):
        from kntgraph.knowledge.falkordb.adapter import (
            _categorize_events,
        )

        e = _make_event(
            event_type="tool.invoice.issue.failed",
            data={"tool": "invoice.issue"},
        )
        result = _categorize_events([e])
        assert len(result.failed_tool_events) == 1

    def test_nf_received_is_a_document(self):
        from kntgraph.knowledge.falkordb.adapter import (
            _categorize_events,
        )

        e = _make_event(
            event_type="nf.received",
            data={"document_id": "NF-001", "amount": 100.0},
        )
        result = _categorize_events([e])
        assert len(result.document_candidates) == 1
        assert result.completed_tool_events == []

    def test_company_upserted_is_a_document(self):
        from kntgraph.knowledge.falkordb.adapter import (
            _categorize_events,
        )

        e = _make_event(
            event_type="company.upserted",
            data={"cnpj": "12.345"},
        )
        result = _categorize_events([e])
        assert len(result.document_candidates) == 1

    def test_unrelated_event_is_ignored(self):
        from kntgraph.knowledge.falkordb.adapter import (
            _categorize_events,
        )

        e = _make_event(event_type="session.started")
        result = _categorize_events([e])
        assert result.document_candidates == []
        assert result.completed_tool_events == []
        assert result.failed_tool_events == []

    def test_mixed_events(self):
        from kntgraph.knowledge.falkordb.adapter import (
            _categorize_events,
        )

        events = [
            _make_event(event_type="tool.x.completed"),
            _make_event(event_type="tool.y.failed"),
            _make_event(event_type="nf.received"),
            _make_event(event_type="company.upserted"),
            _make_event(event_type="session.started"),
        ]
        result = _categorize_events(events)
        assert len(result.completed_tool_events) == 1
        assert len(result.failed_tool_events) == 1
        assert len(result.document_candidates) == 2


# ---------------------------------------------------------------------------
# _agent_node_params
# ---------------------------------------------------------------------------


class TestAgentNodeParams:
    def test_last_seen_uses_latest_event(self):
        from kntgraph.knowledge.falkordb.adapter import (
            _agent_node_params,
        )

        e1 = _make_event(agent_id="NF-001", event_type="x")
        e2 = _make_event(agent_id="NF-001", event_type="y")
        params = _agent_node_params("NF-001", [e1, e2], tenant_id="t1")
        assert params["agent_id"] == "NF-001"
        assert params["tenant_id"] == "t1"
        assert params["last_seen"] == e2.timestamp.isoformat()

    def test_empty_events_yields_empty_last_seen(self):
        from kntgraph.knowledge.falkordb.adapter import (
            _agent_node_params,
        )

        params = _agent_node_params("NF-001", [], tenant_id="t1")
        assert params["last_seen"] == ""


# ---------------------------------------------------------------------------
# _document_node_params
# ---------------------------------------------------------------------------


class TestDocumentNodeParams:
    """Builds the dict consumed by ``MERGE (d:Document ...)``.
    Embedding is awaited outside the categoriser so the
    categoriser stays sync.
    """

    def test_computes_id_as_agent_event(self):
        from kntgraph.knowledge.falkordb.adapter import (
            _document_node_params,
        )

        e = _make_event(
            agent_id="NF-001",
            event_type="nf.received",
            data={"k": "v"},
        )
        embedding = [0.1, 0.2, 0.3]
        params = _document_node_params(e, embedding=embedding)
        assert params["id"] == f"NF-001:{e.event_id}"
        assert params["agent_id"] == "NF-001"
        assert params["event_type"] == "nf.received"
        assert params["data_json"] == json.dumps({"k": "v"}, sort_keys=True)
        assert params["embedding"] == embedding


# ---------------------------------------------------------------------------
# _tool_call_node_params
# ---------------------------------------------------------------------------


class TestToolCallNodeParams:
    def test_completed_event_carries_latency(self):
        from kntgraph.knowledge.falkordb.adapter import (
            _tool_call_node_params,
        )

        e = _make_event(
            agent_id="NF-001",
            event_type="tool.invoice.issue.completed",
            data={
                "tool": "invoice.issue",
                "request_id": "abc",
                "latency_ms": 12.3,
            },
        )
        params = _tool_call_node_params(e, kind=ToolEventKind.COMPLETED)
        assert params["tool"] == "invoice.issue"
        assert params["request_id"] == "abc"
        assert params["status"] == "completed"
        assert params["latency_ms"] == 12.3
        assert params["agent_id"] == "NF-001"

    def test_failed_event_omits_latency(self):
        from kntgraph.knowledge.falkordb.adapter import (
            _tool_call_node_params,
        )

        e = _make_event(
            agent_id="NF-001",
            event_type="tool.x.failed",
            data={"tool": "x", "request_id": "abc"},
        )
        params = _tool_call_node_params(e, kind=ToolEventKind.FAILED)
        assert params["status"] == "failed"
        assert "latency_ms" not in params
