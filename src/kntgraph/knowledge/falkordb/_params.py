# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.falkordb._params -- pure params builders for Cypher.

Split from ``adapter.py`` (Iter 8). Each function returns the
``params`` dict consumed by a single Cypher ``MERGE``. Keeping
these pure (no I/O) makes them trivially unit-testable and
avoids growing ``adapter.py`` past AGENTS.md §3's 500-line
guideline.
"""

from __future__ import annotations

import json

from ...core.event import Event
from ...core.tool_event import ToolEventKind


def _agent_node_params(
    agent_id: str, events: list[Event], *, tenant_id: str
) -> dict[str, str]:
    """
    Build the params dict consumed by ``MERGE (a:Agent ...)``.

    ``last_seen`` is the ISO timestamp of the latest event
    (empty string when no events). The graph picks the
    maximum naturally on replay, but using the EventLog's
    newest event keeps the projection deterministic.
    """
    return {
        "agent_id": agent_id,
        "last_seen": (events[-1].timestamp.isoformat() if events else ""),
        "tenant_id": tenant_id,
    }


def _document_node_params(event: Event, *, embedding: list[float]) -> dict:
    """
    Build the params dict consumed by ``MERGE (d:Document ...)``.

    The Document id is ``"<agent_id>:<event_id>"`` — unique
    across agents and replays. ``data_json`` is a sorted-key
    JSON serialisation so the same payload always hashes to
    the same bytes (helpful for cache-key scenarios).
    """
    return {
        "id": f"{event.agent_id}:{event.event_id}",
        "agent_id": event.agent_id,
        "event_type": event.event_type,
        "data_json": json.dumps(dict(event.data), default=str, sort_keys=True),
        "embedding": embedding,
    }


def _tool_call_node_params(event: Event, *, kind: ToolEventKind) -> dict:
    """
    Build the params dict consumed by ``MERGE (t:ToolCall ...)``.

    The ``latency_ms`` field is present only for COMPLETED
    events — failed events do not carry a meaningful latency
    (the operation was aborted or raised before timing out).
    """
    params: dict = {
        "id": str(event.event_id),
        "tool": event.data.get("tool", "unknown"),
        "request_id": event.data.get("request_id", ""),
        "status": kind.value,
        "agent_id": event.agent_id,
    }
    if kind is ToolEventKind.COMPLETED:
        params["latency_ms"] = event.data.get("latency_ms")
    return params


def _doc_text(agent_id: str, event: Event) -> str:
    """
    Builds the text that the embedding provider consumes for
    a Document. Concatenates agent id, event type and the
    JSON of the payload. The provider is responsible for any
    tokenisation / truncation.
    """
    payload = json.dumps(dict(event.data), default=str, sort_keys=True)
    return f"{agent_id} | {event.event_type} | {payload}"


__all__ = [
    "_agent_node_params",
    "_doc_text",
    "_document_node_params",
    "_tool_call_node_params",
]
