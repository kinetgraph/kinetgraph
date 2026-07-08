# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.falkordb._categorize -- routing logic for events.

Split from the original ``adapter.py`` (Iter 8) to keep each
file under 300 lines (AGENTS.md §3). The categoriser is the
single source of truth for which events become ToolCall
nodes vs Document nodes vs ignored.

Pure: no I/O, no FalkorDB, no embedding provider. The
projector awaits the embedding provider only for events in
``document_candidates``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ...core.event import Event
from ...core.tool_event import ToolEventKind, is_tool_event


@dataclass(frozen=True, slots=True)
class CategorizedEvents:
    """
    Routing result for a single agent's event list.

    Three buckets:
      - ``completed_tool_events`` — ``tool.<x>.completed`` events
      - ``failed_tool_events`` — ``tool.<x>.failed`` events
      - ``document_candidates`` — domain events whose type is
        in the document allow-list (see ``_is_document_candidate``).

    Bucket separation keeps the categoriser sync and pure:
    no I/O, no embeddings, no FalkorDB calls. The projector
    awaits the embedding provider only for the events in
    ``document_candidates``.
    """

    completed_tool_events: list[Event] = field(default_factory=list)
    failed_tool_events: list[Event] = field(default_factory=list)
    document_candidates: list[Event] = field(default_factory=list)


def _is_document_candidate(event_type: str) -> bool:
    """
    Returns True for event types whose payload should be
    indexed as a Document in FalkorDB. For the MVP, we index
    a few high-signal types. Applications can extend.
    """
    return event_type in {
        "nf.received",
        "nf.validated",
        "nf.transmitted",
        "company.upserted",
    }


def _categorize_events(events: Iterable[Event]) -> CategorizedEvents:
    """
    Route each event to one of three buckets. Pure: no I/O,
    no embedding provider, no FalkorDB. The projector
    delegates embedding + Cypher emission to the helpers
    above.
    """
    completed: list[Event] = []
    failed: list[Event] = []
    docs: list[Event] = []
    for e in events:
        if is_tool_event(e.event_type, ToolEventKind.COMPLETED):
            completed.append(e)
        elif is_tool_event(e.event_type, ToolEventKind.FAILED):
            failed.append(e)
        elif e.event_class == "domain" and _is_document_candidate(e.event_type):
            docs.append(e)
    return CategorizedEvents(
        completed_tool_events=completed,
        failed_tool_events=failed,
        document_candidates=docs,
    )


__all__ = [
    "CategorizedEvents",
    "_categorize_events",
    "_is_document_candidate",
]
