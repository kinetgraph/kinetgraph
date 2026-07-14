# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
core.world.components -- ECS components for the Solution tier.

These are the framework-level primitives that materialise
the in-flight tool call state in the World. They are
**derived** from `tool.requested` / `tool.completed` /
`tool.failed` events via the `project_tool_calls`
projection (see `projection_tool_calls`).

Design contract (Iter 28 FU 8, ADR-034):

  - Components are **immutable** (frozen + slots). State
    transitions are archetype migrations, not field
    mutations. A `ToolCallRequest` is created on
    `tool.requested`; a `ToolCallCompletion` is added
    on `tool.completed`/`tool.failed`. The agent's
    archetype evolves from `{Request}` to
    `{Request, Completion}`.

  - Components are **cache derived**. The EventLog is
    the source of truth. A component can always be
    re-derived from the events via
    `World.fold(events, projection=project_tool_calls)`.
    No version field is needed in the component because
    the version is on the event that created it.

  - The `request_event_id` is the join key between
    `ToolCallRequest` and `ToolCallCompletion`. The
    completion event's `causation_id` points to the
    request's `event_id`; the projection joins on this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional
from uuid import UUID

from .._typing import JsonValue


@dataclass(frozen=True, slots=True)
class ToolCallTTL:
    """
    Per-tool TTL configuration for ``ToolCallRequest``
    entries (ADR-045).

    A request whose ``expires_at`` is in the past at
    overlay time is removed from the slot. The TTL
    bounds the memory used by the slot and the
    staleness of orphaned requests (e.g. after a
    worker crash, a DLQ escalation, or a worker
    process restart).

    The default TTL is **5 minutes** (covers the
    99th percentile of legitimate tool latencies —
    LLM calls, network I/O, batch tools — plus a
    generous safety margin). Per-tool overrides
    accommodate tools with very different latency
    profiles:

      - **Synchronous helpers** (in-process DB
        queries, ``pii_redaction``): tight TTL
        (e.g. 60s) is a backstop; the completion
        typically lands in the same tick.
      - **Long-running batch tools** (video
        transcoders, large document parsers):
        loose TTL (e.g. 1h) accommodates the
        worker latency.

    A TTL of ``0`` (or negative) **disables** the
    TTL enforcement (``expires_at`` is set to
    ``None`` on the request). This is useful for
    tests and for tools that the operator wants
    to opt out of the safety net (NOT recommended
    in production).
    """

    default_ttl_seconds: float = 300.0
    per_tool_ttls: Mapping[str, float] = field(default_factory=dict)

    def ttl_for(self, tool_name: str) -> float:
        """Return the TTL (in seconds) for ``tool_name``.

        The lookup is exact (``per_tool_ttls`` is
        keyed by the tool name). Falls back to
        ``default_ttl_seconds`` if no override is
        configured.
        """
        return self.per_tool_ttls.get(tool_name, self.default_ttl_seconds)


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """
    ECS component representing a tool call in flight.

    Materialized from `tool.requested` events. Immutable
    (frozen + slots). The state of the request (pending
    or resolved) is NOT a field on this component; it
    is determined by the presence (or absence) of a
    sibling `ToolCallCompletion` in the same agent's
    archetype.

    Required fields (all filled by the projection):
      - `request_event_id`: the `event_id` of the
        `tool.requested` event.
      - `tool_name`: the name of the tool that was
        invoked (from `event.data["tool"]`).
      - `agent_id`: the agent that issued the call.
      - `params`: a read-only view of the request
        payload (`event.data`).
      - `requested_at`: the timestamp of the request.
    """

    request_event_id: str
    tool_name: str
    agent_id: str
    params: Mapping[str, JsonValue]
    requested_at: datetime
    # ADR-037: the flow id (stable across the
    # request -> completion). Materialised from
    # the source event's
    # ``correlation.correlation_id``. Systems
    # that need to emit downstream events in
    # the same flow read this to pin their own
    # ``correlation``.
    correlation_id: Optional[UUID] = None
    # ADR-045: the wall-clock time at which the
    # request expires (UTC, timezone-aware).
    # Computed at materialisation time as
    # ``requested_at + ttl(tool_name)`` where
    # ``ttl`` is configured per-tool on the
    # dispatcher. ``None`` means "no TTL" (the
    # request lives until completion-driven
    # eviction, per ADR-044). A request whose
    # ``expires_at`` is in the past at overlay
    # time is evicted by the framework
    # (``overlay_tool_calls``); the eviction
    # is silent (no event is emitted).
    expires_at: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class ToolCallCompletion:
    """
    ECS component representing a tool call that has
    resolved (success or failure).

    Materialized from `tool.completed` or `tool.failed`
    events. Immutable (frozen + slots). The `status`
    field discriminates success from failure.

    The completion's `request_event_id` is the same as
    the corresponding `ToolCallRequest.request_event_id`
    (joined via the completion event's `causation_id`).

    Archetype evolution: this component is added to the
    entity that already has `ToolCallRequest`. The
    `ArchetypeStorage` indexes the migration in O(1)
    amortized.

    Required fields:
      - `request_event_id`: the join key (== the
        request's `event_id`).
      - `status`: "completed" | "failed" (the suffix
        of the source event_type).
      - `result`: a read-only view of the result
        payload (for `status="completed"`); None for
        failures.
      - `error`: the error string (for
        `status="failed"`); None for successes.
      - `completed_at`: the timestamp of the completion.
      - `latency_ms`: the duration in milliseconds
        (completed_at - request.requested_at).
    """

    request_event_id: str
    status: str
    result: Optional[Mapping[str, JsonValue]] = None
    error: Optional[str] = None
    completed_at: Optional[datetime] = None
    latency_ms: Optional[float] = None
    # ADR-037: see ``ToolCallRequest.correlation_id``.
    # Inherited from the source completion event's
    # ``correlation.correlation_id`` and from the
    # originating request (both should be equal in
    # a well-behaved flow).
    correlation_id: Optional[UUID] = None


__all__ = [
    "ToolCallCompletion",
    "ToolCallRequest",
    "ToolCallTTL",
]
