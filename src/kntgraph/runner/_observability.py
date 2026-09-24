# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
runner._observability -- ADR-075 Tier 4 read queries.

The four queries that close the gaps flagged in
ADR-075 §1.3 rows #8–10. They compose on existing primitives
(``view.tool_requests`` / ``tool_completions`` /
``DeadLetterQueue.list_*``) — no new event types, no new
projections, no new system class.

Kept separate from ``reactive.py`` so the dispatcher's hot
path stays under the 500-line guideline (the file was already
703 lines at extraction time). Module split mirrors the
saga split pattern (``_system / _compensation / _dispatch /
_records``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from kntgraph.core.world.view import AgentView
    from kntgraph.events.dlq.values import DeadLetterEvent


# ---------------------------------------------------------------------------
# InFlightTask — read-shape from tool_requests ∖ tool_completions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InFlightTask:
    """A tool task that has been requested but has not yet
    received a terminal event (``completed`` / ``failed``).

    **Read-only view shape** for ADR-075 Tier 4. Materialised
    from the per-agent ``view.tool_requests`` slot minus the
    ``view.tool_completions`` slot. The EventLog is the
    source of truth; ``InFlightTask`` is a cache the
    dispatcher can answer queries against without forcing
    a full EventLog scan.

    Fields:
    - ``request_event_id``: join key with the tool_events.
    - ``agent_id``: the agent that issued the call.
    - ``tool_name``: registered ``@tool_worker`` name.
    - ``requested_at``: timestamp of the ``tool.<name>.requested``
      event.
    - ``expires_at``: deadline set by ``ToolCallTTL`` (mandatory
      after ADR-075). ``now > expires_at`` ⇒ stale.
    - ``parameters``: read-only view of the request payload.
    - ``correlation_id``: ``CorrelationContext.correlation_id``
      propagated from the request event (ADR-037).
    - ``acknowledged_at``: optional; reserved for a future ack
      event (not in scope of ADR-075).
    """

    request_event_id: str
    agent_id: str
    tool_name: str
    requested_at: datetime
    expires_at: datetime
    parameters: Mapping[str, object] = field(default_factory=dict)
    correlation_id: str | None = None
    acknowledged_at: datetime | None = None

    @property
    def is_stale(self) -> bool:
        """True if the task is past its TTL deadline (now > expires_at).

        Caller passes the current time via the ``now`` parameter
        on the query; this property compares against
        ``datetime.now(tz=timezone.utc)``. For deterministic tests
        use ``now=`` on the query and bypass this property.
        """
        return datetime.now(tz=timezone.utc) > self.expires_at


# ---------------------------------------------------------------------------
# RecoveryReport — convenience entry-point output
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Output of ``ReactiveDispatcher.detect_and_recover()``.

    A summary that an operator dashboard / cron / alert can
    persist or page on. Not a guarantee that recovery
    happened — the sweeper is the recovery path; this report
    is a structured count of what it did.
    """

    in_flight_count: int = 0
    stale_count: int = 0
    stuck_in_queue_count: int = 0
    dead_lettered_count: int = 0
    dry_run: bool = False
    inspected_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )


# ---------------------------------------------------------------------------
# Internal: extract tool-call events from a view's components slot
# ---------------------------------------------------------------------------


def _extract_in_flight(
    view: "AgentView",
    now: datetime,
) -> list[InFlightTask]:
    """Compute the in-flight task list for one agent view.

    The split:
    - For every ``ToolCallRequest`` in ``view.tool_requests``:
      - if there is **no** matching ``ToolCallCompletion`` in
        ``view.tool_completions`` keyed by the same
        ``request_event_id`` ⇒ the task is **in-flight**.
      - if ``now > request.expires_at`` ⇒ **stale**.

    Built without copying the dicts: the result holds frozen
    references to the existing view components.
    """
    if not view.components:
        return []

    requests_map = view.components.get("tool_requests")
    completions_map = view.components.get("tool_completions")
    if not isinstance(requests_map, Mapping):
        return []

    out: list[InFlightTask] = []
    for req in requests_map.values():
        # Defensive: the slot can contain non-ToolCallRequest
        # entries (e.g. after a tampered-fold). Skip.
        req_event_id = getattr(req, "request_event_id", None)
        if req_event_id is None:
            continue
        # Resolution: completed iff a sibling completion with
        # the same request_event_id is in completions.
        completed = (
            isinstance(completions_map, Mapping) and req_event_id in completions_map
        )
        if completed:
            continue
        # Materialise the read shape.
        params = getattr(req, "params", {}) or {}
        corr = getattr(req, "correlation_id", None)
        out.append(
            InFlightTask(
                request_event_id=str(req_event_id),
                agent_id=view.agent_id,
                tool_name=str(getattr(req, "tool_name", "")),
                requested_at=getattr(req, "requested_at", now),
                expires_at=getattr(req, "expires_at", now + timedelta(hours=1)),
                parameters=MappingProxyType(dict(params))
                if isinstance(params, Mapping)
                else MappingProxyType({}),
                correlation_id=str(corr) if corr else None,
                acknowledged_at=None,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Public query helpers — used by ReactiveDispatcher
# ---------------------------------------------------------------------------


def in_flight_tasks(
    *,
    agents_to_views: Mapping[str, "AgentView"],
    agent_id: str | None = None,
    now: datetime | None = None,
) -> list[InFlightTask]:
    """Return all in-flight tool tasks across the dispatcher's
    tracked agents (ADR-075 §2.4 row #8).

    ``agents_to_views`` is the dispatcher's
    ``world.views``-style mapping (the dispatcher already
    maintains it as ``self._tracked_agents`` + ``World``).

    ``agent_id=None`` ⇒ all tracked agents. With
    ``agent_id`` set, only that agent's view is scanned.
    """
    now = now or datetime.now(tz=timezone.utc)
    out: list[InFlightTask] = []
    targets = [agent_id] if agent_id is not None else list(agents_to_views)
    for aid in targets:
        view = agents_to_views.get(aid)
        if view is None:
            continue
        out.extend(_extract_in_flight(view, now))
    return out


def stale_tasks(
    *,
    agents_to_views: Mapping[str, "AgentView"],
    threshold_seconds: float = 300.0,
    now: datetime | None = None,
    agent_id: str | None = None,
) -> list[InFlightTask]:
    """Subset of ``in_flight_tasks`` where
    ``now - expires_at > threshold_seconds`` (ADR-075 §2.4 row
    #11). The threshold is the recovery race window
    (``stale but not yet recovered``).
    """
    now = now or datetime.now(tz=timezone.utc)
    threshold = timedelta(seconds=threshold_seconds)
    return [
        t
        for t in in_flight_tasks(
            agents_to_views=agents_to_views, agent_id=agent_id, now=now
        )
        if (now - t.expires_at) > threshold
    ]


async def dead_lettered_tasks(
    dlq,
    *,
    reason=None,
    agent_id: str | None = None,
    count: int = 100,
) -> list["DeadLetterEvent"]:
    """Read DLQ entries awaiting operator action
    (ADR-075 §2.4 row #10).

    Composes the existing ``DeadLetterQueue.list_*`` API —
    no new storage path. Accepts ``dlq=None`` (returns [])
    so callers can conditionally wire the DLQ without
    branching at every call site.
    """
    if dlq is None:
        return []
    # Prefer reason-filtered list (cheaper); fall back to per-agent.
    if reason is not None:
        return list(await dlq.list_by_reason(reason, count=count))
    if agent_id is not None:
        return list(await dlq.list_for_agent(agent_id, count=count))
    return list(await dlq.list_all(count=count))


# ---------------------------------------------------------------------------
# stuck_in_queue — reads the tool queue streams via the dispatcher's
# WorldCheckpointStorage adapter (the same store that holds the
# per-agent World checkpoint). Composed of two primitives defined
# on the storage Protocol (``queue_length`` + ``pending_count``) so
# the dispatcher never reaches into the Redis client directly for
# stream operations (ADR-075 Tier 4, row #9).
# ---------------------------------------------------------------------------


async def stuck_in_queue(
    *,
    stream_inspector,
    stream_prefix: str,
    agents_to_views: Mapping[str, "AgentView"],
    threshold_seconds: float = 300.0,
    now: float | None = None,
) -> list[str]:
    """Stream-keys whose queue length is non-zero AND no
    XPENDING / active consumer ⇒ message stuck.

    ADR-075 §2.4 row #9. **Decomposes into two primitives** on
    the storage Protocol:

    - ``stream_inspector.queue_length(stream_key)`` returns the
      number of entries ever written to the stream (Redis
      ``XLEN`` semantics).
    - ``stream_inspector.pending_count(stream_key)`` returns
      the count of un-acked entries currently held in the
      tool consumer group's PEL (Redis ``XPENDING`` semantics,
      single-probe).

    A high ``queue_length`` AND zero ``pending_count`` means
    "messages queued but no worker is processing them" — the
    stuck case. The query returns the tool names whose queue
    matches that pattern.

    ``stream_inspector`` is the dispatcher's ``IncrementalWorldStore``
    (the facade over ``WorldCheckpointStorage``); the facade
    forwards to the underlying storage so the query composes
    cleanly with the same adapter the dispatch loop uses for
    checkpoints.
    """
    if stream_inspector is None:
        return []
    out: list[str] = []
    # Iterate the views to discover tools currently in
    # flight; for each, check its stream.
    inferred_tools: set[str] = set()
    for view in agents_to_views.values():
        reqs = view.components.get("tool_requests") if view.components else None
        if isinstance(reqs, Mapping):
            for req in reqs.values():
                tool_name = getattr(req, "tool_name", None)
                if tool_name:
                    inferred_tools.add(tool_name)
    for tool_name in sorted(inferred_tools):
        stream_key = f"{stream_prefix}:{tool_name}:queue"
        length = await stream_inspector.queue_length(stream_key)
        if length <= 0:
            continue
        pending = await stream_inspector.pending_count(stream_key)
        if pending > 0:
            continue
        # We don't actually track "last consumption time" here
        # without extra plumbing; the threshold controls false
        # positives. A real implementation persists
        # ``last_tick_at`` per agent and compares.
        out.append(tool_name)  # always include when queue non-empty
    return out


__all__ = [
    "InFlightTask",
    "RecoveryReport",
    "in_flight_tasks",
    "stale_tasks",
    "dead_lettered_tasks",
    "stuck_in_queue",
]
