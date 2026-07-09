# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.agents.memory.solution_promoter -- ``SolutionPromoterSystem``.

Iter 28 FU 8 (ADR-034): I/O ``WorldSystem`` that
consumes ``solution.candidate_extracted`` events and
writes each candidate to FalkorDB (via the
framework's ``GraphPool``). Emits
``solution.promoted`` events with stats.

The promoter is the I/O counterpart of the pure
``SolutionExtractorSystem``. It runs in the
``ReactiveDispatcher`` loop after the extractor; the
dispatcher's outgoing events flow into the promoter.

Iter 28 FU 8 scope:
  - FalkorDB MERGE write via ``GraphPool.upsert_solution``.
  - PII redaction: TODO (separate iter; for now the
    promoter writes raw data — operators must
    configure PII redaction at the gate).
  - Embedding: TODO (separate iter; the promoter
    currently writes without embedding).

The structure is fail-soft: a failed write is
counted in ``failed`` but does not abort the pump.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kntgraph.core.event.event import Event


class GraphPoolLike(Protocol):
    """Subset of ``GraphPool`` used by the promoter.
    The protocol keeps the system decoupled from the
    concrete FalkorDB adapter.
    """

    def upsert_solution(self, candidate: dict) -> int: ...


@dataclass(frozen=True)
class PromoteStats:
    """Per-pump stats. Cumulative across pumps in the
    promoter instance."""

    upserts: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.upserts + self.failed


class SolutionPromoterSystem:
    """
    I/O WorldSystem: writes ``solution.candidate_extracted``
    events to FalkorDB and emits ``solution.promoted``
    events with stats.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        graph_pool: GraphPoolLike,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id must be set")
        self._tenant_id = tenant_id
        self._pool = graph_pool
        # Cumulative stats; preserved across pumps.
        self._stats = PromoteStats()

    def __call__(self, events: list[Event]) -> list[Event]:
        out: list[Event] = []
        upserts = 0
        failed = 0
        for ev in events:
            if ev.event_type != "solution.candidate_extracted":
                continue
            try:
                self._pool.upsert_solution(
                    {
                        "agent_id": ev.agent_id,
                        "request_event_id": ev.data.get("request_event_id"),
                        "tool_name": ev.data.get("tool_name"),
                        "params": ev.data.get("params", {}),
                        "requested_at": ev.data.get("requested_at"),
                        "latency_ms": ev.data.get("latency_ms"),
                        "cross_agent_count": ev.data.get("cross_agent_count", 1),
                        "result": ev.data.get("result", {}),
                        "tenant_id": self._tenant_id,
                    }
                )
                upserts += 1
                out.append(self._emit_promoted(ev, status="upserted"))
            except Exception:
                failed += 1
                out.append(self._emit_promoted(ev, status="failed"))
        # Update cumulative stats (replace, not mutate).
        self._stats = PromoteStats(
            upserts=self._stats.upserts + upserts,
            failed=self._stats.failed + failed,
        )
        return out

    def _emit_promoted(
        self,
        source: Event,
        *,
        status: str,
    ) -> Event:
        """Emit ``solution.promoted`` carrying the
        source candidate's request_event_id and the
        promote status."""
        return Event.create(
            event_type="solution.promoted",
            agent_id=source.agent_id,
            event_class="domain",
            data={
                "request_event_id": source.data.get("request_event_id"),
                "status": status,
                "tenant_id": self._tenant_id,
            },
            correlation=source.correlation,
        )

    @property
    def stats(self) -> PromoteStats:
        return self._stats


__all__ = [
    "GraphPoolLike",
    "PromoteStats",
    "SolutionPromoterSystem",
]
