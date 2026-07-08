# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.agents.memory.solution_review_publisher --
``SolutionReviewPublisherSystem``.

Iter 28 FU 8 (ADR-034): I/O ``WorldSystem`` that
routes candidates below the review threshold to the
Redis Stream review queue for human review.

The publisher is the I/O counterpart of the
``SolutionExtractorSystem``: it consumes the
``solution.candidate_extracted`` events emitted by
the extractor and decides which ones need human
review (low confidence / cross-agent count) before
auto-promotion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from kntgraph.core.event.event import Event


class ReviewQueueLike(Protocol):
    """Subset of the Redis Stream review queue used
    by the publisher. Decoupled for testability."""

    def publish(self, entry: Mapping[str, Any]) -> bool: ...


@dataclass(frozen=True)
class ReviewPublisherStats:
    """Per-pump stats. Cumulative across pumps in the
    publisher instance."""

    published: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.published + self.skipped


class SolutionReviewPublisherSystem:
    """
    I/O WorldSystem: routes low-confidence candidates
    to the review queue. Emits ``solution.review_required``
    events for candidates that were published.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        review_queue: ReviewQueueLike,
        review_threshold: int = 2,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id must be set")
        if review_threshold < 1:
            raise ValueError(f"review_threshold must be >= 1, got {review_threshold}")
        self._tenant_id = tenant_id
        self._queue = review_queue
        self._threshold = review_threshold
        # Cumulative stats.
        self._stats = ReviewPublisherStats()

    def __call__(self, events: list[Event]) -> list[Event]:
        out: list[Event] = []
        published = 0
        skipped = 0
        for ev in events:
            if ev.event_type != "solution.candidate_extracted":
                continue
            cross = int(ev.data.get("cross_agent_count", 1))
            if cross >= self._threshold:
                # Auto-promote path handles it.
                skipped += 1
                continue
            entry: dict[str, Any] = {
                "request_event_id": ev.data.get("request_event_id"),
                "tool_name": ev.data.get("tool_name"),
                "agent_id": ev.agent_id,
                "params": ev.data.get("params", {}),
                "requested_at": ev.data.get("requested_at"),
                "latency_ms": ev.data.get("latency_ms"),
                "cross_agent_count": cross,
                "tenant_id": self._tenant_id,
            }
            self._queue.publish(entry)
            published += 1
            out.append(
                Event.create(
                    event_type="solution.review_required",
                    agent_id=ev.agent_id,
                    event_class="domain",
                    data={
                        "request_event_id": entry["request_event_id"],
                        "tenant_id": self._tenant_id,
                    },
                    correlation=ev.correlation,
                )
            )
        self._stats = ReviewPublisherStats(
            published=self._stats.published + published,
            skipped=self._stats.skipped + skipped,
        )
        return out

    @property
    def stats(self) -> ReviewPublisherStats:
        return self._stats


__all__ = [
    "ReviewPublisherStats",
    "ReviewQueueLike",
    "SolutionReviewPublisherSystem",
]
