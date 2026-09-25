# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
dlq.values -- Data types and constants for the Dead Letter Queue.

Three layers, kept on this module because each is small
and they are consumed together by ``events/dlq/store.py``:

  - `DLQReason` (enum): the closed set of failure modes
    that can land an event in the DLQ.

  - `DeadLetterEvent` (frozen dataclass): the cached
    payload of a DLQ entry. Carries the original `Event`
    plus failure metadata (reason, error_message,
    retry_count, original_timestamp, dlq_timestamp,
    metadata).

  - The four Redis key **suffix templates** (stream + 3
    indexes). These are bare ``"knt:dlq:..."`` strings;
    the storage adapter composes them with the namespace
    prefix at every read/write (ADR-076). Single source of
    truth lives in :mod:`kntgraph.infra.redis._dlq`; this
    module re-exports them so legacy callers (and the
    ``DeadLetterQueue`` orchestrator) do not need to import
    across the framework boundary.

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
from typing import TYPE_CHECKING, Mapping, cast
from uuid import UUID

from ...core.event import CorrelationContext, Event

# ADR-076: Redis key suffix templates are the single source
# of truth in ``infra.redis._dlq``. Re-export them here so
# the vertical (``events/dlq``) does not duplicate the
# wire format -- the prefix composition lives in the
# adapter, the suffixes here are pure strings.
from ...infra.redis._dlq import (
    DLQ_AGENT_INDEX,
    DLQ_EVENT_INDEX,
    DLQ_REASON_INDEX,
    DLQ_STREAM_KEY,
)

if TYPE_CHECKING:
    from ...core._typing import JsonValue


__all__ = [
    "DLQ_AGENT_INDEX",
    "DLQ_EVENT_INDEX",
    "DLQ_REASON_INDEX",
    "DLQ_STREAM_KEY",
    "DLQReason",
    "DeadLetterEvent",
]


class DLQReason(str, Enum):
    """Why an event ended up in the DLQ.

    Reasons 0–5 cover worker-side failures (the worker hard
    crashed, exceeded its retry budget, etc.). **Reasons
    6–7 cover tool-task recoveries emitted by the TTL
    sweeper** when a stale request couldn't be safely
    re-dispatched (ADR-075 §2.3.2). The existing reasons
    stay unchanged so existing operators' dashboards don't
    break.
    """

    PROCESSING_FAILED = "processing_failed"
    MAX_RETRIES_EXCEEDED = "max_retries_exceeded"
    VALIDATION_ERROR = "validation_error"
    TIMEOUT = "timeout"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    POISON_PILL = "poison_pill"
    UNKNOWN_ERROR = "unknown_error"

    # ADR-075 §3.1 — emitted by ToolCallTTLSweeperSystem when
    # a stale request cannot be re-dispatched because the tool
    # is non-idempotent. The worker may have started the task
    # (acked) before crashing.
    TOOL_STALE_ACKNOWLEDGED = "tool_stale_acknowledged"

    # ADR-075 §3.1 — emitted by ToolCallTTLSweeperSystem when
    # a stale request was never picked up by any worker
    # (message stuck in the queue; no XPENDING entry).
    TOOL_STALE_UNACKNOWLEDGED = "tool_stale_unacknowledged"


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
    metadata: Mapping[str, "JsonValue"] = field(
        default_factory=lambda: cast("Mapping[str, JsonValue]", {})
    )

    @property
    def dlq_id(self) -> str:
        """Stable id for the DLQ entry: based on event_id only.
        Re-failures of the same event with the same id map to the
        same dlq_id (used as the idempotency key)."""
        return f"dlq:{self.event.event_id}"

    def to_dict(self) -> dict[str, str]:
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
    def from_dict(cls, data: Mapping[str, str]) -> "DeadLetterEvent":
        def s(key: str, default: str = "") -> str:
            # ``Mapping.get`` returns ``str | None`` even when
            # a ``default`` is supplied; narrow with the
            # ``or`` short-circuit so the returned value is
            # always ``str``.
            value = data.get(key, default)
            return value if value is not None else default

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
