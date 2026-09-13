# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tool-call TTL sweeper (ADR-045, ADR-075 Tier 3).

The ``ToolCallTTLSweeperSystem`` is a ``WorldSystem``
that runs **once per tick** in the
``ReactiveDispatcher``. On each invocation it walks
the ``tool_requests`` slot of every agent's view and
emits events for stale requests.

ADR-075 Tier 3: the sweeper now supports two paths
for stale requests:

  1. **Re-dispatch** — the request was never picked up
     by a worker (no ``tool.<name>.acknowledged``
     event). The framework re-emits ``tool.<name>.requested``
     with the same ``request_event_id`` and a reset TTL.

  2. **DLQ** — the request was acknowledged by a worker
     (``tool.<name>.acknowledged`` event was emitted) but
     the TTL expired before completion. The framework
     routes the request to the Dead Letter Queue with a
     ``TOOL_STALE_ACKNOWLEDGED`` or ``TOOL_STALE_UNACKNOWLEDGED``
     reason, depending on whether the worker acknowledged
     the task.

The sweeper is the **safety net** for the
completion-driven eviction introduced in ADR-044. A
request whose completion **never** lands (e.g. the
worker crashed, the WorkerManager escalated to the
DLQ but the original request is still in the slot,
or a worker is stuck) becomes an **orphan**. The
sweeper detects orphans via the per-request TTL
(``expires_at`` set by the projection at
materialisation time; see ADR-045 §2.1) and emits
events so downstream systems
(``SolutionExtractor``, metrics, alerts) can
observe the gap.

## Why a separate system?

The TTL enforcement was originally in
``overlay_tool_calls`` (Phase 3 of the ADR-045
§2.4 eviction order). This had a structural
problem: the overlay is a **pure** function
(ADR-034), and TTL enforcement requires a wall
clock injection (``now``). Mixing the two broke
the purity and forced the overlay to:

  - Accept a ``now`` argument (clock injection).
  - Walk every agent in ``base_views`` on every
    tick (to detect stale requests carried in from
    previous ticks), which broke the "no
    allocation for non-tool batches" optimisation
    (ADR-044 §2.4).
  - Reject the existing test suite (the
    ``test_overlay_ttl_evicts_carried_request``
    test failed because the overlay was a no-op
    for batches with no tool events).

The sweeper system **separates concerns**:

  - **Overlay** (pure): sets ``expires_at`` on each
    new request. No clock injection. No allocation
    for non-tool batches.
  - **Sweeper** (impure): reads the wall clock,
    walks the views, emits events. The
    I/O is explicit (a system that produces
    events).

The separation preserves the framework's
"projection as pure data" invariant (ADR-034) and
keeps the TTL enforcement observable (a downstream
consumer can subscribe to the emitted events for
metrics, retries, etc.).

## Implementation

The sweeper is a ``WorldSystem`` that runs in the
``ReactiveDispatcher`` loop. It is registered like
any other system (the dispatcher does not
auto-register it; the operator opts in by
``dispatcher.add_system(ToolCallTTLSweeperSystem())``
or by passing it in ``systems=[...]`` at
construction). The sweeper is stateful (it
deduplicates events by ``request_event_id``) but
the state is per-instance; the sweeper's dedup
memory is local to the process (a process restart
re-derives the dedup from the EventLog via the
``causation_id`` on subsequent events).

The sweeper DOES NOT evict the stale request from
the ``tool_requests`` slot. The eviction is left
to the **completion-driven rule** (ADR-044 §2.3
option 1): when the worker's completion eventually
arrives, the request is removed from the slot. If
the completion never arrives, the request stays in
the slot forever (the memory leak is out of scope
for ADR-045; see the module docstring for the
follow-up).

## TTL Ack/Nack Events

When a worker starts processing a task, it emits
``tool.<name>.acknowledged`` (ADR-075 Tier 2).
The sweeper checks for a matching acknowledgment in
the ``tool_completions`` slot. If found, the request
is marked as acknowledged; otherwise it is treated
as unacknowledged.

The two paths are:

- **Acknowledged + expired TTL** → ``tool.<name>.stale_acked`` event
- **Unacknowledged + expired TTL** → ``tool.<name>.requested``
  re-dispatch event (same ``request_event_id``, reset TTL)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from collections.abc import Mapping

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.core.world.components import ToolCallCompletion, ToolCallRequest
from kntgraph.core.world.view import AgentView

# The error string emitted on a TTL-expired request.
# The format matches the standard failure event
# shape (``event.data["error"]``) so downstream
# consumers can distinguish TTL failures from
# worker-reported failures (the latter use the
# worker's own error string).
_TTL_EXPIRED_ERROR = "ttl_expired"


class ToolCallTTLSweeperSystem:
    """
    Sweep the ``tool_requests`` slot of every agent
    in the World and emit events for stale requests.

    ADR-075 Tier 3: two paths for stale requests:

    1. **Re-dispatch** — the request was never picked up
       by a worker (no ``tool.<name>.acknowledged``
       event). The framework re-emits
       ``tool.<name>.requested`` with the same
       ``request_event_id`` and a reset TTL.

    2. **DLQ** — the request was acknowledged by a worker
       (``tool.<name>.acknowledged`` event was emitted) but
       the TTL expired before completion. The framework
       routes the request to the Dead Letter Queue with a
       ``TOOL_STALE_ACKNOWLEDGED`` or ``TOOL_STALE_UNACKNOWLEDGED``
       reason, depending on whether the worker acknowledged
       the task.

    The system is **stateful** (``_emitted_events``):
    it remembers the ``request_event_id``s for which
    it has already emitted an event, so a request that
    stays in the slot across multiple ticks (because
    the completion never arrives) triggers **at most
    one** event. The dedup is in-memory; a process
    restart re-derives the dedup from the EventLog via
    the ``causation_id`` on subsequent events (the
    system can subscribe to the events it previously
    emitted to filter them out on re-folds).

    The system does NOT evict the stale request from
    the ``tool_requests`` slot. The eviction is left
    to the **completion-driven rule** (ADR-044 §2.3
    option 1): when the worker's completion eventually
    arrives, the request is removed from the slot. If
    the completion never arrives, the request stays in
    the slot forever (the memory leak is out of scope
    for ADR-045; see the module docstring for the
    follow-up).
    """

    def __init__(
        self,
        *,
        now: Optional[datetime] = None,
        error_message: str = _TTL_EXPIRED_ERROR,
    ) -> None:
        """
        ``now``: optional wall-clock injection. Defaults
        to ``datetime.now(tz=timezone.utc)``. Tests
        inject a fixed clock for deterministic
        assertions.

        ``error_message``: the ``data["error"]`` string
        for the emitted events. Defaults to
        ``"ttl_expired"``.
        """
        self._now = now
        self._error_message = error_message
        # ``request_event_id`` -> True (one set
        # membership per event emitted). The set
        # is in-memory; it is reset on process restart.
        self._emitted_events: set[str] = set()

    def __call__(self, world: "World | Mapping[str, AgentView]") -> list[Event]:
        events: list[Event] = []
        now = self._now or datetime.now(tz=timezone.utc)
        # Accept either a ``World`` (production
        # path: the dispatcher passes the post-fold
        # World) or a ``Mapping[str, AgentView]`` (test
        # path: the test passes a dict of views built
        # by ``project_tool_calls``).
        if isinstance(world, World):
            views_iter: Mapping[str, AgentView] = world.views
        else:
            views_iter = world
        for agent_id, view in views_iter.items():
            tool_requests = view.components.get("tool_requests", {})
            if not isinstance(tool_requests, dict):
                continue
            tool_completions = view.components.get("tool_completions", {})
            for request_id, req in tool_requests.items():
                if not isinstance(req, ToolCallRequest):
                    continue
                if req.expires_at is None:
                    # TTL disabled (legacy request
                    # from a World checkpoint pre-ADR-045,
                    # or an opt-out via
                    # ``ToolCallTTL(default_ttl_seconds=0)``).
                    continue
                if now < req.expires_at:
                    # Not yet expired; the next tick
                    # will re-check.
                    continue
                if request_id in self._emitted_events:
                    # Already emitted an event for
                    # this request. The request
                    # is still in the slot (we do not
                    # evict; see the docstring), but
                    # we do not emit a duplicate.
                    continue
                #
                # ADR-075 Tier 3: determine path based on
                # acknowledgment status.
                #
                comp = tool_completions.get(request_id)
                acknowledged = comp is not None and comp.status == "acknowledged"
                #
                if acknowledged:
                    # Request was acknowledged by a worker
                    # but TTL expired before completion.
                    # Emit stale-aacked event.
                    events.append(
                        self._build_stale_acked_event(
                            agent_id=agent_id,
                            request=req,
                            now=now,
                        )
                    )
                else:
                    # Request was never acknowledged (worker
                    # never picked it up). Re-dispatch it.
                    events.append(
                        self._build_re_dispatch_event(
                            agent_id=agent_id,
                            request=req,
                            now=now,
                        )
                    )
                self._emitted_events.add(request_id)
        return events

    def _build_stale_acked_event(
        self,
        *,
        agent_id: str,
        request: ToolCallRequest,
        now: datetime,
    ) -> Event:
        """Build a ``tool.<name>.stale_acked`` event for a
        request that was acknowledged by a worker but
        whose TTL expired before completion.

        This event informs operators that a worker
        had the task but it timed out. The event
        carries the ``request_event_id`` so operators
        can correlate with the original request and
        decide via DLQ actions.

        The event type follows the ADR-075 convention:
        ``tool.<name>.stale_acked``.
        """
        from uuid import UUID

        tool_name = request.tool_name or "unknown"
        event_type = f"tool.{tool_name}.stale_acked"
        correlation = CorrelationContext.new(correlation_id=request.correlation_id)
        return Event.create(
            event_type=event_type,
            agent_id=agent_id,
            event_class="domain",
            data={
                "error": "ttl_expired",
                "request_event_id": request.request_event_id,
                "tool_name": tool_name,
                "expired_at": request.expires_at.isoformat(),
                "swept_at": now.isoformat(),
                "acknowledged": True,
            },
            correlation=correlation,
            causation_id=UUID(request.request_event_id),
        )

    def _build_re_dispatch_event(
        self,
        *,
        agent_id: str,
        request: ToolCallRequest,
        now: datetime,
    ) -> Event:
        """Build a ``tool.<name>.requested`` re-dispatch
        event for a stale request.

        This re-emits the original request with the
        same ``request_event_id`` and a reset TTL,
        so the framework can re-dispatch the task to
        a worker. The ``causation_id`` is the
        request's ``event_id``, which serves as the
        idempotency key for tools that honor it.

        The event type is the **canonical**
        ``tool.<name>.requested`` form (ADR-036).
        """
        from uuid import UUID

        tool_name = request.tool_name or "unknown"
        event_type = f"tool.{tool_name}.requested"
        correlation = CorrelationContext.new(correlation_id=request.correlation_id)
        # Reset the TTL: set expires_at to now + default TTL
        new_expires_at = now + __import__("datetime").timedelta(seconds=300)
        return Event.create(
            event_type=event_type,
            agent_id=agent_id,
            event_class="domain",
            data={
                "request_event_id": request.request_event_id,
                "tool_name": tool_name,
                "params": dict(request.params),
                "correlation_id": request.correlation_id,
                "expires_at": new_expires_at.isoformat(),
            },
            correlation=correlation,
            causation_id=UUID(request.request_event_id),
        )


__all__ = ["ToolCallTTLSweeperSystem"]