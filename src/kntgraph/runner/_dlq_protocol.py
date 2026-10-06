# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
runner._dlq_protocol -- Framework-canonical ``DLQAdapter`` Protocol.

Per the dependency rule (AGENTS.md §1.2, ADR-019 §2.1):
framework never imports from vertical. The runner
(``tool_call_ttl_sweeper``, ``_observability``,
``_dlq_writer``) reads from the DLQ to:

  - convert timed-out tool requests into DLQ entries
    (``tool_call_ttl_sweeper`` -- ADR-045 §3.1);
  - expose observability queries for the metrics sink
    (``_observability.dead_lettered_tasks`` --
    ADR-075 §1.3 row #10);
  - forward ``*.dlq`` events into the storage facade
    (``_dlq_writer.append_dlq_events`` -- ADR-069 §5.2).

Before this module, the runner imported three vertical
types directly:

    from kntgraph.events.dlq.store import DeadLetterQueue   (one site)
    from kntgraph.events.dlq.values import DeadLetterEvent  (two sites)
    from kntgraph.events.dlq.values import DLQReason        (one site)

All four imports are framework→vertical leaks; the
``_dlq`` audit row C.2-C.4 listed them as the structural
debt to fix in this PR. The fix mirrors the
``CachedSolution`` move (see ``core/components/solution.py``
for the same pattern): the canonical home is the
framework; the vertical re-exports.

The ``DLQAdapter`` Protocol defines the surface the
runner needs. The concrete ``DeadLetterQueue`` in
``events/dlq/store.py`` implements it (the Protocol
methods are a subset of the class's public surface,
inherited transparently via Python's structural
typing).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..core._typing import JsonValue
from ..core.event import Event

if TYPE_CHECKING:
    from ..core.result import PersistenceError, Result


__all__ = [
    "DLQAdapter",
    "DLQReason",
    "DeadLetterEvent",
]


# ---------------------------------------------------------------------------
# DLQReason
# ---------------------------------------------------------------------------


class DLQReason(str, Enum):
    """Why an event ended up in the DLQ.

    Reasons 0-5 cover worker-side failures (the worker
    hard crashed, exceeded its retry budget, etc.).
    Reasons 6-7 cover tool-task recoveries emitted by the
    TTL sweeper (ADR-045). The closed set keeps the
    ``by_reason`` index and the operator dashboard
    bounded; new failure modes require a new constant
    here (which the sweeper and the facade both reference).

    Subclassing ``str`` keeps the value wire-compatible
    with the EventLog JSON payload (the reason travels
    as a plain string in the ``*.dlq`` events' ``data``
    field).
    """

    PROCESSING_FAILED = "processing_failed"
    MAX_RETRIES_EXCEEDED = "max_retries_exceeded"
    VALIDATION_ERROR = "validation_error"
    TIMEOUT = "timeout"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    POISON_PILL = "poison_pill"
    UNKNOWN_ERROR = "unknown_error"

    # ADR-075 §3.1 -- emitted by ToolCallTTLSweeperSystem
    # when a stale request cannot be re-dispatched because
    # the tool is non-idempotent. The worker may have
    # started the task (acked) before crashing.
    TOOL_STALE_ACKNOWLEDGED = "tool_stale_acknowledged"

    # ADR-075 §3.1 -- emitted by ToolCallTTLSweeperSystem
    # when a stale request was never picked up by any
    # worker (message stuck in the queue; no XPENDING
    # entry).
    TOOL_STALE_UNACKNOWLEDGED = "tool_stale_unacknowledged"


# ---------------------------------------------------------------------------
# DeadLetterEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeadLetterEvent:
    """A DLQ entry: the original Event plus failure metadata.

    The class is a frozen dataclass (structural
    equality, safe to share across async boundaries per
    ADR-005 §3). The wire-format codec
    (``to_dict`` / ``from_dict``) lives here, on the
    dataclass, because the shape is intrinsically tied
    to the data class -- splitting it into a separate
    module buys nothing here and creates a confusing
    import.

    The class preserves the wire format that operators
    have been depending on (the flat-dict shape with
    decomposed event fields, ``causation_id``,
    ``span_id``, ``event_id`` etc.). Changing the wire
    format is a wire-protocol change that requires
    operator coordination -- not done in this refactor.
    """

    event: Event
    reason: DLQReason
    error_message: str
    original_timestamp: datetime
    dlq_timestamp: datetime
    retry_count: int = 0
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    @property
    def dlq_id(self) -> str:
        """Stable id for the DLQ entry: based on event_id only.

        Re-failures of the same event with the same id map
        to the same dlq_id (used as the idempotency key).
        """
        return f"dlq:{self.event.event_id}"

    def to_dict(self) -> dict[str, str]:
        """Serialise to the Redis-stream flat-dict shape.

        The Redis stream is string-only; the values are
        JSON-encoded at the call site. The dict shape is
        what ``DLQStorage.append`` consumes and what
        operators' dashboards parse.
        """
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
    def from_dict(cls, data: dict[str, str]) -> DeadLetterEvent:
        """Inverse of :meth:`to_dict`.

        The string serialisation is the Redis stream's
        contract; the JSON decoding is here.
        """
        from uuid import UUID as _UUID

        from ..core.event import CorrelationContext as _Correlation

        def s(key: str, default: str = "") -> str:
            # ``data.get`` returns ``str | None`` even when a
            # ``default`` is supplied; narrow with the
            # ``or`` short-circuit so the returned value is
            # always ``str``.
            value = data.get(key, default)
            return value if value is not None else default

        correlation = _Correlation(
            correlation_id=_UUID(s("correlation_id")),
            causation_id=_UUID(s("causation_id")) if s("causation_id") else None,
            span_id=_UUID(s("span_id")) if s("span_id") else None,
            metadata=json.loads(s("metadata", "{}")),
        )
        event = Event(
            event_id=_UUID(s("event_id")),
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


# ---------------------------------------------------------------------------
# DLQAdapter
# ---------------------------------------------------------------------------


@runtime_checkable
class DLQAdapter(Protocol):
    """The surface the runner needs from the DLQ.

    The runner queries three operations on the DLQ
    facade:

      1. ``append`` -- ``_dlq_writer.append_dlq_events``
         forwards every ``*.dlq`` event into the storage
         (ADR-069 §5.2). Returns the stream id of the
         stored entry on success; ``PLACEHOLDER`` on
         idempotency dedup.

      2. ``get_event`` -- ``tool_call_ttl_sweeper`` and
         ``_dlq_writer`` look up an existing entry by
         event id to check idempotency.

      3. ``list_*`` -- ``_observability.dead_lettered_tasks``
         runs one of the three list helpers
         (``list_by_reason`` / ``list_for_agent`` /
         ``list_all``) for the metrics sink.

    The Protocol is ``@runtime_checkable`` so callers
    can do ``isinstance(queue, DLQAdapter)`` for defensive
    config checks (e.g. the dispatcher's constructor
    accepts ``dlq=None`` and treats absence as "DLQ
    not configured"; the Protocol is the runtime test
    that the configured object actually implements the
    surface).
    """

    async def append(
        self, dl_event: DeadLetterEvent
    ) -> Result[str, PersistenceError]:
        """Append a DLQ entry. Idempotent on
        ``(event_id, reason)``: a second call with the
        same pair returns the original stream id
        without creating a duplicate.
        """
        ...

    async def get_event(
        self, event_id: str
    ) -> Result[DeadLetterEvent | None, PersistenceError]:
        """Read the first DLQ entry for ``event_id``
        across all reasons, or ``None`` on miss.
        """
        ...

    async def list_by_reason(
        self, reason: DLQReason, count: int = 100
    ) -> Result[list[DeadLetterEvent], PersistenceError]:
        """List DLQ entries with a given reason (full scan).
        """
        ...

    async def list_for_agent(
        self, agent_id: str, count: int = 100
    ) -> Result[list[DeadLetterEvent], PersistenceError]:
        """List DLQ entries for one agent (forward-scan
        from the head).
        """
        ...

    async def list_all(
        self, count: int = 100
    ) -> Result[list[DeadLetterEvent], PersistenceError]:
        """List every DLQ entry (full scan).
        """
        ...
