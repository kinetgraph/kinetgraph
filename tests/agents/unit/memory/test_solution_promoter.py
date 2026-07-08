# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``SolutionPromoterSystem``
(Iter 28 FU 8 / ADR-034).

The promoter is an I/O ``WorldSystem``: it consumes
``solution.candidate_extracted`` events from the
dispatcher's outgoing events and writes to FalkorDB
(via the framework's ``GraphPool``). It also routes
candidates below the review threshold to the Redis
Stream review queue (handled by
``SolutionReviewPublisherSystem``; the promoter
itself only writes to FalkorDB).

For Iter 28 FU 8 the promoter is a thin wrapper that:
  1. Consumes ``solution.candidate_extracted`` events
     via the world's outgoing events.
  2. Writes each candidate to FalkorDB (mocked here;
     real PII redaction + MERGE lives in the existing
     ``kntgraph.agents.memory.solutions._promoter`` helper).
  3. Emits ``solution.promoted`` events with stats.

The system is the I/O counterpart of the pure
``SolutionExtractorSystem``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType


from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.event.event import Event

from kntgraph.agents.memory.solution_promoter import (
    PromoteStats,
    SolutionPromoterSystem,
)


def _ts(offset_s: int = 0) -> datetime:
    from datetime import timedelta

    base = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_s)


def _candidate_event(
    *,
    agent_id: str = "agent-1",
    request_event_id: str = "req-1",
    tool_name: str = "x",
    cross_agent_count: int = 1,
    completion_status: str = "completed",
    latency_ms: float = 100.0,
) -> Event:
    return Event.create(
        event_type="solution.candidate_extracted",
        agent_id=agent_id,
        event_class="domain",
        data=MappingProxyType(
            {
                "request_event_id": request_event_id,
                "tool_name": tool_name,
                "params": {"tool": tool_name},
                "requested_at": _ts(0).isoformat(),
                "completion_status": completion_status,
                "latency_ms": latency_ms,
                "cross_agent_count": cross_agent_count,
            }
        ),
        correlation=CorrelationContext.new(),
    )


class FakeGraphPool:
    """Records all `upsert_solution` calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def upsert_solution(self, candidate: dict) -> int:
        self.calls.append(dict(candidate))
        return 1


class TestSolutionPromoterSystemEmpty:
    def test_no_events_emits_no_promoted(self) -> None:
        """No incoming events → no outgoing events."""
        pool = FakeGraphPool()
        sys = SolutionPromoterSystem(tenant_id="t-1", graph_pool=pool)
        out = sys([])
        assert out == []
        assert pool.calls == []


class TestSolutionPromoterSystemWrites:
    def test_candidate_extracted_writes_to_falkordb(self) -> None:
        """A `solution.candidate_extracted` event is
        written to FalkorDB. The system then emits a
        `solution.promoted` event with stats.
        """
        pool = FakeGraphPool()
        sys = SolutionPromoterSystem(tenant_id="t-1", graph_pool=pool)
        candidate = _candidate_event()
        out = sys([candidate])

        # One write.
        assert len(pool.calls) == 1
        # The write contains the candidate data.
        assert pool.calls[0]["request_event_id"] == "req-1"
        assert pool.calls[0]["tool_name"] == "x"
        assert pool.calls[0]["agent_id"] == "agent-1"
        # One promoted event emitted.
        assert len(out) == 1
        promoted = out[0]
        assert promoted.event_type == "solution.promoted"
        assert promoted.agent_id == "agent-1"
        # Cross-references: the promoted event names
        # the source candidate.
        assert promoted.data.get("request_event_id") == "req-1"
        assert promoted.data.get("status") == "upserted"

    def test_multiple_candidates_multiple_writes(self) -> None:
        """N candidates → N writes + N promoted events."""
        pool = FakeGraphPool()
        sys = SolutionPromoterSystem(tenant_id="t-1", graph_pool=pool)
        candidates = [_candidate_event(request_event_id=f"req-{i}") for i in range(3)]
        out = sys(candidates)
        assert len(pool.calls) == 3
        assert len(out) == 3
        assert all(e.event_type == "solution.promoted" for e in out)


class TestSolutionPromoterSystemFailure:
    def test_failed_promoter_does_not_block(self) -> None:
        """A failed write (FalkorDB down) does NOT
        abort the pump. The candidate is counted in
        `failed`; subsequent candidates still get
        processed. The system is fail-soft."""

        class FailingPool:
            def __init__(self) -> None:
                self.calls = 0

            def upsert_solution(self, candidate: dict) -> int:
                self.calls += 1
                raise RuntimeError("falkordb down")

        pool = FailingPool()
        sys = SolutionPromoterSystem(tenant_id="t-1", graph_pool=pool)
        out = sys([_candidate_event(), _candidate_event()])

        # Both candidates were attempted.
        assert pool.calls == 2
        # Both emitted as `solution.promoted` with
        # status="failed".
        assert len(out) == 2
        assert all(e.data["status"] == "failed" for e in out)


class TestPromoteStats:
    def test_empty_stats(self) -> None:
        s = PromoteStats()
        assert s.upserts == 0
        assert s.failed == 0

    def test_stats_total(self) -> None:
        s = PromoteStats(upserts=2, failed=1)
        assert s.total == 3
