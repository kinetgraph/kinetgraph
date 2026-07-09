# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``SolutionReviewPublisherSystem``
(Iter 28 FU 8 / ADR-034).

The review publisher is an I/O ``WorldSystem``: it
routes candidates below the review threshold
(or in the tenant's approval list) to the Redis
Stream review queue for human review.

For Iter 28 FU 8 the publisher is a thin wrapper that
emits ``solution.review_required`` events with the
candidate payload. The actual Redis write is
encapsulated in a `ReviewQueueLike` Protocol for
testability.
"""

from __future__ import annotations

from types import MappingProxyType


from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.event.event import Event

from kntgraph.agents.memory.solution_review_publisher import (
    ReviewPublisherStats,
    SolutionReviewPublisherSystem,
)


def _candidate_event(
    *,
    agent_id: str = "agent-1",
    cross_agent_count: int = 1,
    tool_name: str = "x",
    request_event_id: str = "req-1",
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
                "requested_at": "2026-06-30T12:00:00+00:00",
                "completion_status": "completed",
                "latency_ms": 100.0,
                "cross_agent_count": cross_agent_count,
            }
        ),
        correlation=CorrelationContext.new(),
    )


class FakeReviewQueue:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def publish(self, entry: dict) -> bool:
        self.entries.append(dict(entry))
        return True


class TestReviewPublisherSystemEmpty:
    def test_no_events_no_publishes(self) -> None:
        queue = FakeReviewQueue()
        sys = SolutionReviewPublisherSystem(
            tenant_id="t-1",
            review_queue=queue,
        )
        out = sys([])
        assert out == []
        assert queue.entries == []


class TestReviewPublisherSystemBelowThreshold:
    def test_below_threshold_publishes(self) -> None:
        """A candidate with `cross_agent_count <
        review_threshold` is published to the review
        queue and emits `solution.review_required`."""
        queue = FakeReviewQueue()
        sys = SolutionReviewPublisherSystem(
            tenant_id="t-1",
            review_queue=queue,
            review_threshold=2,
        )
        candidate = _candidate_event(cross_agent_count=1)
        out = sys([candidate])

        # One entry published.
        assert len(queue.entries) == 1
        # The entry is the candidate payload.
        entry = queue.entries[0]
        assert entry["request_event_id"] == "req-1"
        assert entry["tool_name"] == "x"
        assert entry["agent_id"] == "agent-1"
        # One event emitted.
        assert len(out) == 1
        event = out[0]
        assert event.event_type == "solution.review_required"
        assert event.agent_id == "agent-1"
        assert event.data.get("request_event_id") == "req-1"


class TestReviewPublisherSystemAboveThreshold:
    def test_above_threshold_skips(self) -> None:
        """A candidate with `cross_agent_count >=
        review_threshold` is NOT published. The
        auto-promote path (SolutionPromoterSystem)
        handles it.
        """
        queue = FakeReviewQueue()
        sys = SolutionReviewPublisherSystem(
            tenant_id="t-1",
            review_queue=queue,
            review_threshold=2,
        )
        candidate = _candidate_event(cross_agent_count=3)
        out = sys([candidate])
        assert queue.entries == []
        assert out == []


class TestReviewPublisherSystemMixed:
    def test_filters_correctly(self) -> None:
        """Mixed batch: some candidates need review,
        others don't."""
        queue = FakeReviewQueue()
        sys = SolutionReviewPublisherSystem(
            tenant_id="t-1",
            review_queue=queue,
            review_threshold=2,
        )
        events = [
            _candidate_event(request_event_id="req-low", cross_agent_count=1),
            _candidate_event(request_event_id="req-high", cross_agent_count=3),
            _candidate_event(request_event_id="req-edge", cross_agent_count=2),
        ]
        out = sys(events)
        # Below threshold: low, edge. Above: high.
        # Note: edge (== threshold) is not below, so not published.
        assert len(queue.entries) == 1
        assert queue.entries[0]["request_event_id"] == "req-low"
        # Emitted events match published.
        assert len(out) == 1
        assert out[0].data["request_event_id"] == "req-low"


class TestReviewPublisherStats:
    def test_empty_stats(self) -> None:
        s = ReviewPublisherStats()
        assert s.published == 0
        assert s.skipped == 0

    def test_stats_total(self) -> None:
        s = ReviewPublisherStats(published=2, skipped=1)
        assert s.total == 3
