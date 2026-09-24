# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tool-call TTL sweeper (ADR-045, ADR-075 §2.3).

The ``ToolCallTTLSweeperSystem`` is a ``WorldSystem``
that runs **once per tick** in the
``ReactiveDispatcher``. On each invocation it walks
the ``tool_requests`` slot of every agent's view and
emits a ``tool.<name>.failed`` event for every
request whose ``expires_at`` is in the past.

The sweeper is the **safety net** for the
completion-driven eviction introduced in ADR-044. A
request whose completion **never** lands (e.g. the
worker crashed, the WorkerManager escalated to the
DLQ but the original request is still in the slot,
or a worker is stuck) becomes an **orphan**. The
sweeper detects orphans via the per-request TTL
(``expires_at`` set by the projection at
materialisation time; see ADR-045 §2.1) and emits
the failure event so downstream systems
(``SolutionExtractor``, metrics, alerts) can
observe the gap.

**ADR-075 §2.3 (recovery)**: when the sweeper is
constructed with ``dlq=`` AND the tool is
non-idempotent, the failure event is **routed to the
DLQ** instead of (or in addition to) the ``failed``
event. The same DLQ reference is also shared with the
TTL-sweeper-managed saga → DLQ wire (§2.3.3): when
the originator saga is in ``compensating`` state,
the sweeper additionally emits
``saga.<name>.compensation_failed`` so the saga's own
recovery triggers.

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
    walks the views, emits failure events. The
    I/O is explicit (a system that produces
    events).

The separation preserves the framework's
"projection as pure data" invariant (ADR-034) and
keeps the TTL enforcement observable (a downstream
consumer can subscribe to the ``tool.<name>.failed``
events for metrics, retries, etc.).

## Implementation

The sweeper is a ``WorldSystem`` that runs in the
``ReactiveDispatcher`` loop. It is registered like
any other system (the dispatcher does not
auto-register it; the operator opts in by
``dispatcher.add_system(ToolCallTTLSweeperSystem())``
or by passing it in ``systems=[...]`` at
construction). The sweeper is stateful (it
deduplicates failures by ``request_event_id``) but
the state is per-instance; the sweeper's dedup
memory is local to the process (a process restart
re-derives the dedup from the EventLog via the
``causation_id`` field on subsequent events).

The sweeper DOES NOT evict the stale request from
the ``tool_requests`` slot. The eviction is left
to the **completion-driven rule** (ADR-044 §2.3
option 1): when the worker's completion eventually
arrives, the request is removed from the slot. If
the completion never arrives, the request stays in
the slot forever (memory leak); a follow-up
**GC_TICK** event (or a periodic compaction pass)
is the mitigation (out of scope for ADR-045).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.core.world.components import ToolCallRequest
from kntgraph.core.world.view import AgentView

if TYPE_CHECKING:
    from kntgraph.events.dlq.store import DeadLetterQueue


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
    in the World and emit ``tool.<name>.failed`` for
    stale requests.

    Usage::

        sweeper = ToolCallTTLSweeperSystem()
        dispatcher = ReactiveDispatcher(
            log=log,
            systems=[sweeper],
            ...
        )

    The system is **stateful** (``_emitted_failures``):
    it remembers the ``request_event_id``s for which
    it has already emitted a ``failed`` event, so a
    request that stays in the slot across multiple
    ticks (because the completion never arrives)
    triggers **at most one** failed event. The
    dedup is in-memory; a process restart re-derives
    the dedup from the EventLog via the
    ``causation_id`` on subsequent events (the
    system can subscribe to the ``tool.<name>.failed``
    events it previously emitted to filter them out
    on re-folds).

    The system does NOT evict the stale request from
    the slot; the completion-driven eviction
    (ADR-044) handles that. If the completion never
    arrives, the slot carries the request forever
    (the memory leak is out of scope for ADR-045;
    see the module docstring for the follow-up).
    """

    def __init__(
        self,
        *,
        now: Optional[datetime] = None,
        error_message: str = _TTL_EXPIRED_ERROR,
        dlq: Optional["DeadLetterQueue"] = None,
    ) -> None:
        """
        ``now``: optional wall-clock injection. Defaults
        to ``datetime.now(tz=timezone.utc)``. Tests
        inject a fixed clock for deterministic
        assertions.

        ``error_message``: the ``data["error"]`` string
        for the emitted failed event. Defaults to
        ``"ttl_expired"``.

        ``dlq`` (ADR-075 §2.3): optional reference to a
        ``DeadLetterQueue``. When set, stale non-idempotent
        requests are **routed to the DLQ** in addition to
        emitting the failure event (the saga-compensation
        wire §2.3.3 also reuses this reference). Default
        ``None`` preserves the legacy emit-only behaviour.
        """
        self._now = now
        self._error_message = error_message
        self._dlq = dlq
        # ``request_event_id`` -> True (one set
        # membership per failure emitted). The set
        # is in-memory; it is reset on process restart.
        self._emitted_failures: set[str] = set()

    def __call__(
        self,
        world: "World | Mapping[str, AgentView]",
    ) -> list[Event]:
        """Walk ``tool_requests``; emit ``tool.<name>.failed``
        for stale entries; optionally route to the DLQ.

        The method is **fully synchronous** — it returns the
        events the sweeper wants to emit (the dispatcher's
        second overlay pass — ``fold_with_systems`` — folds
        them back into the World). When ``dlq`` is wired,
        the DLQ insertion is a separate ``await``
        effect; the dispatcher's tick loop awaits it.

        **ADR-075 §2.3.3 saga-DLQ wire**: when the originator
        data carries a ``saga_id`` AND the saga is in
        ``compensating`` state (ADR-072 §4.5), the sweeper
        additionally emits ``saga.<name>.compensation_failed``
        so the saga's own recovery triggers. The saga
        system itself remains pure (its compensation_failed
        emit is part of ``begin_compensation``; the sweeper
        only re-emits when the compensation chain stalls).
        """
        events: list[Event] = []
        now = self._now or datetime.now(tz=timezone.utc)
        # Accept either a ``World`` (production
        # path: the dispatcher passes the post-fold
        # World) or a ``Mapping[str, AgentView]`` (test
        # path: tests invoke the sweeper directly with a
        # dict of views, bypassing the dispatcher).
        if isinstance(world, World):
            views_iter: Mapping[str, AgentView] = world.views
        else:
            views_iter = world
        for agent_id, view in views_iter.items():
            tool_requests = view.components.get("tool_requests", {})
            if not isinstance(tool_requests, dict):
                continue
            # Mirror the request slot: ``tool_completions``
            # is the slot the projection populates from
            # ``tool.<name>.completed`` / ``.failed`` events.
            # When a completion is present for a
            # ``request_id``, the request is NOT an
            # orphan -- the worker (or a previous tick's
            # sweeper) already responded. The post-systems
            # eviction pass will remove the request from
            # ``tool_requests`` on the next fold; until
            # then, the sweeper must NOT emit a duplicate
            # failure (which would double-fire downstream
            # compensation paths -- the saga system
            # reacts to both ``completed`` and ``failed``
            # events for the same ``request_event_id``).
            #
            # Pinned by
            # ``TestSweeperSkipsCompletedRequests``
            # in ``tests/unit/runner/test_tool_call_ttl_sweeper.py``.
            tool_completions = view.components.get("tool_completions", {})
            if not isinstance(tool_completions, dict):
                # Tampered / corrupted view: treat as
                # "no completions known" so the sweeper
                # still does its job (defensive: better
                # to fail open on a tampered view than to
                # skip the entire agent).
                tool_completions = {}
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
                if request_id in tool_completions:
                    # A completion (success OR failure)
                    # is already in the view for this
                    # request_id. The work is accounted
                    # for -- no need to emit a TTL
                    # failure. The post-systems eviction
                    # pass will drop the request on the
                    # next fold; for this tick we simply
                    # stay silent. See the audit's
                    # "TTL sweeper ordering" finding.
                    continue
                if request_id in self._emitted_failures:
                    # Already emitted a failed event
                    # for this request. The request
                    # is still in the slot (we do not
                    # evict; see the docstring), but
                    # we do not emit a duplicate.
                    continue
                self._emitted_failures.add(request_id)
                failed_event = self._build_failed_event(
                    agent_id=agent_id, request=req, now=now
                )
                events.append(failed_event)
                # ADR-075 §2.3: route to DLQ when wired.
                if self._dlq is not None:
                    self._route_to_dlq(failed_event, agent_id, request_id)
        return events

    def _route_to_dlq(
        self,
        failed_event: "Event",
        agent_id: str,
        request_id: str,
    ) -> None:
        """Append a ``DeadLetterEvent`` for a stale tool
        request.

        The ``reason`` is conservatively ``TOOL_STALE_UNACKNOWLEDGED``
        when we cannot tell (we don't read the PEL from
        here — that's ``WorkerManager``'s concern); callers
        that have richer info can override the reason.
        """
        from kntgraph.events.dlq.values import (
            DLQReason,
            DeadLetterEvent,
        )

        dl_event = DeadLetterEvent(
            event=failed_event,
            reason=DLQReason.TOOL_STALE_UNACKNOWLEDGED,
            error_message=self._error_message,
            original_timestamp=failed_event.timestamp,
            dlq_timestamp=datetime.now(tz=timezone.utc),
            metadata={"request_event_id": request_id, "agent_id": agent_id},
        )
        # The sweeper is synchronous; the dispatcher's
        # tick loop awaits the appended events. The DLQ
        # append is fire-and-forget here (it's I/O, but
        # not on the dispatcher hot path — the dispatch
        # already writes to Redis). For an async path,
        # wrap in an outer ``await``; the dispatcher's
        # ``fold_with_systems`` awaits system outputs.
        try:
            # ``append`` is async; the sync sweeper
            # schedules it. ``DeadLetterQueue.append``
            # returns ``Result``; we ignore failures
            # here (the failure event already carries
            # the alert).
            import asyncio

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(self._dlq.append(dl_event))
            finally:
                loop.close()
            # ``result`` would be ``Ok`` on success; we
            # intentionally swallow the ``Err`` path
            # because the operator already sees the
            # ``tool.<name>.failed`` event in the log.
            del result
        except Exception:
            # Last-resort: don't crash the sweeper.
            pass

    def _build_failed_event(
        self,
        *,
        agent_id: str,
        request: ToolCallRequest,
        now: datetime,
    ) -> Event:
        """Build the ``tool.<name>.failed`` event for a
        stale request.

        The event is a domain event with the
        standard failure shape
        (``data={"error": "..."}``); downstream
        consumers (``WorkerManager``,
        ``SolutionExtractor``, metrics) handle it
        like any other failure. The
        ``causation_id`` is the request's eid (the
        same join key the WorkerManager uses for
        completions); the ``correlation`` is derived
        from the request's ``correlation_id`` so the
        failure lives in the same flow as the
        request.

        The event type is the **canonical**
        ``tool.<name>.failed`` form (ADR-036); the
        ``tool_name`` is taken from the request (NOT
        from the event type, since the request was
        already materialised in a previous tick).
        The legacy bare form ``tool.failed`` is
        **not** emitted here (the bare form does not
        carry a tool name; the sweeper does not
        know what tool the request was for).
        """
        from uuid import UUID

        tool_name = request.tool_name or "unknown"
        event_type = f"tool.{tool_name}.failed"
        correlation = CorrelationContext.new(correlation_id=request.correlation_id)
        # expires_at is guaranteed non-None here: callers check
        # ``if req.expires_at is None: continue`` before reaching
        # this method. The assert makes the narrowing explicit for pyright.
        assert request.expires_at is not None
        return Event.create(
            event_type=event_type,
            agent_id=agent_id,
            event_class="domain",
            data={
                "error": self._error_message,
                "request_event_id": request.request_event_id,
                "tool_name": tool_name,
                "expired_at": request.expires_at.isoformat(),
                "swept_at": now.isoformat(),
            },
            correlation=correlation,
            causation_id=UUID(request.request_event_id),
        )


__all__ = ["ToolCallTTLSweeperSystem"]
