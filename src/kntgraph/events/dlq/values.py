# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
dlq.values -- Data types and constants for the Dead Letter Queue.

Two layers:

  - `DLQReason` (enum): the closed set of failure modes
    that can land an event in the DLQ.

  - `DeadLetterEvent` (frozen dataclass): the cached
    payload of a DLQ entry. Carries the original `Event`
    plus failure metadata (reason, error_message,
    retry_count, original_timestamp, dlq_timestamp,
    metadata).

  - The four Redis key constants (stream + 3 indexes).

The codec (`to_dict` / `from_dict`) lives on
`DeadLetterEvent` because the shape is intrinsically
tied to the data class — splitting it into a separate
`codec.py` module buys nothing here.

No I/O, no Redis, no event construction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID

from ...core.event import CorrelationContext, Event


# Redis keys for the DLQ.
DLQ_STREAM_KEY = "knt:dlq:events"
DLQ_REASON_INDEX = "knt:dlq:reasons"
DLQ_AGENT_INDEX = "knt:dlq:by_agent"
DLQ_EVENT_INDEX = "knt:dlq:by_event_id"


class DLQReason(str, Enum):
    """Why an event ended up in the DLQ."""

    PROCESSING_FAILED = "processing_failed"
    MAX_RETRIES_EXCEEDED = "max_retries_exceeded"
    VALIDATION_ERROR = "validation_error"
    TIMEOUT = "timeout"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    POISON_PILL = "poison_pill"
    UNKNOWN_ERROR = "unknown_error"


@dataclass(frozen=True, slots=True)
class DeadLetterEvent:
    """
    A DLQ entry: the original Event plus failure metadata.
    """

    event: Event
    reason: DLQReason
    error_message: str
    original_timestamp: datetime
    dlq_timestamp: datetime
    retry_count: int = 0
    metadata: dict = field(default_factory=dict)

    @property
    def dlq_id(self) -> str:
        """Stable id for the DLQ entry: based on event_id only.
        Re-failures of the same event with the same id map to the
        same dlq_id (used as the idempotency key)."""
        return f"dlq:{self.event.event_id}"

    def to_dict(self) -> dict:
        return {
            "event_id": str(self.event.event_id),
            "agent_id": self.event.agent_id,
            "event_type": self.event.event_type,
            "event_class": self.event.event_class,
            "event_data": json.dumps(
                dict(self.event.data), default=str, sort_keys=True
            ),
            "event_timestamp": self.event.timestamp.isoformat(),
            "correlation_id": str(self.event.correlation.correlation_id),
            "causation_id": str(self.event.correlation.causation_id)
            if self.event.correlation.causation_id
            else "",
            "span_id": str(self.event.correlation.span_id)
            if self.event.correlation.span_id
            else "",
            "metadata": json.dumps(
                dict(self.event.correlation.metadata), default=str, sort_keys=True
            ),
            "reason": self.reason.value,
            "error_message": self.error_message,
            "retry_count": str(self.retry_count),
            "original_timestamp": self.original_timestamp.isoformat(),
            "dlq_timestamp": self.dlq_timestamp.isoformat(),
            "extra_metadata": json.dumps(
                dict(self.metadata), default=str, sort_keys=True
            ),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeadLetterEvent":
        def s(key: str, default: str = "") -> str:
            return data.get(key, default)

        correlation = CorrelationContext(
            correlation_id=UUID(s("correlation_id")),
            causation_id=UUID(s("causation_id")) if s("causation_id") else None,
            span_id=UUID(s("span_id")) if s("span_id") else None,
            metadata=json.loads(s("metadata", "{}")),
        )
        event = Event(
            event_id=UUID(s("event_id")),
            agent_id=s("agent_id"),
            event_type=s("event_type"),
            event_class=s("event_class"),  # type: ignore[arg-type]
            timestamp=datetime.fromisoformat(s("event_timestamp")),
            data=json.loads(s("event_data", "{}")),
            correlation=correlation,
        )
        return cls(
            event=event,
            reason=DLQReason(s("reason")),
            error_message=s("error_message"),
            original_timestamp=datetime.fromisoformat(s("original_timestamp")),
            dlq_timestamp=datetime.fromisoformat(s("dlq_timestamp")),
            retry_count=int(s("retry_count", "0")),
            metadata=json.loads(s("extra_metadata", "{}")),
        )


__all__ = [
    "DLQ_AGENT_INDEX",
    "DLQ_EVENT_INDEX",
    "DLQ_REASON_INDEX",
    "DLQ_STREAM_KEY",
    "DLQReason",
    "DeadLetterEvent",
]
