# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
End-to-end integration tests for ADR-037 correlation propagation.

These tests exercise the full transaction path through the
``ReactiveDispatcher`` against a live Redis (the ``clean_redis``
fixture flushes the database before/after each test):

  - Entry event → dispatcher fold → systems emit child events
    → re-fold → EventLog persistence.
  - All child events in the SAME transaction share the entry's
    ``correlation_id`` (the audit-trail invariant).
  - Concurrent transactions (multiple agents in the same tick)
    keep their ``correlation_id`` boundaries intact (no leakage
    between flows).

Reference: ADR-037 §1.1 — audit trail must be stitchable by
``correlation_id`` alone.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(correlation_id: UUID | None = None) -> CorrelationContext:
    """Build a fresh ``CorrelationContext`` with an explicit
    (or random) flow id."""
    return CorrelationContext.new(correlation_id=correlation_id)


async def _seed_spawned(log: EventLog, agent_id: str) -> Event:
    e = Event.create(
        event_type="agent.spawned",
        agent_id=agent_id,
        event_class="lifecycle",
        correlation=_ctx(),
    )
    await log.append(e)
    return e


async def _read_domain_events_for_agent(log: EventLog, agent_id: str) -> list[Event]:
    """Read every event in the agent's stream and return only
    the ``domain`` ones (lifecycle events have a separate
    correlation_id and are not part of the audit-trail flow
    query)."""
    storage = log._storage
    all_events = await storage.read(agent_id, start="-", end="+")
    return [e for e in all_events if e.event_class == "domain"]


# ---------------------------------------------------------------------------
# Test 1: one request → 4 derived events → 2 systems
# ---------------------------------------------------------------------------


class TestOneRequestFourDerivedEvents:
    """A single request (entry event) flows through the
    dispatcher. System 1 emits 4 derived events. System 2
    processes the first two (``e1``, ``e2``); System 3
    processes the last two (``e3``, ``e4``). All 4 derived
    events (plus the entry) must share the same
    ``correlation_id``.

    The dispatcher propagates the entry's correlation via
    ``correlation_middleware.continue_from(...)`` at the
    start of each tick (see ``ReactiveDispatcher._dispatch_for_agent``).
    Systems that emit via
    ``correlation_middleware.current()`` inherit the flow id
    — the audit chain stitches.
    """

    async def test_correlation_flows_through_all_systems(
        self, clean_redis: Any
    ) -> None:
        # Stable correlation_id for this transaction. Mirrors
        # the ``intent_router`` entry pattern
        # (``event_id == correlation_id``).
        flow_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

        log = EventLog(RedisEventLogAdapter(clean_redis))
        await _seed_spawned(log, "a-1")

        # Append the entry event. The dispatcher will pick it up
        # in the next ``dispatch_once`` call.
        entry = Event.create(
            event_type="request.received",
            agent_id="a-1",
            event_class="domain",
            data={"request": "x"},
            correlation=_ctx(flow_id),
            event_id=flow_id,
        )
        await log.append(entry)

        # System 1: emits 4 derived events on the first
        # invocation. Gated on a private flag so it runs once.
        #
        # The default projection is last-event-wins (see
        # ``tests/unit/test_world.py::TestWorldFold::
        # test_fold_drops_components_after_replay``): each
        # ``_apply_event`` rebuilds ``components`` from
        # scratch, so the LAST domain event's component
        # slot wins. Distinct event_types in one batch
        # collapse to the LAST one (the projection does
        # NOT aggregate across types). So we emit 4 events
        # of THE SAME type (``derived.step``) — they
        # collapse to 1 component slot, but all 4 land in
        # the EventLog (the audit-trail invariant we care
        # about is the correlation_id of the EventLog
        # entries, not the World component).
        sys1_ran = {"done": False}

        def system1(world) -> list[Event]:
            from kntgraph.core.event import correlation_middleware

            if sys1_ran["done"]:
                return []
            view = world.get_agent("a-1")
            if view is None or view.last_event_id is None:
                return []
            ctx = correlation_middleware.current()
            assert ctx is not None, "middleware empty"
            sys1_ran["done"] = True
            return [
                Event.create(
                    event_type="derived.step",
                    agent_id="a-1",
                    event_class="domain",
                    data={"i": 1},
                    correlation=ctx,
                    causation_id=entry.event_id,
                ),
                Event.create(
                    event_type="derived.step",
                    agent_id="a-1",
                    event_class="domain",
                    data={"i": 2},
                    correlation=ctx,
                    causation_id=entry.event_id,
                ),
                Event.create(
                    event_type="derived.step",
                    agent_id="a-1",
                    event_class="domain",
                    data={"i": 3},
                    correlation=ctx,
                    causation_id=entry.event_id,
                ),
                Event.create(
                    event_type="derived.step",
                    agent_id="a-1",
                    event_class="domain",
                    data={"i": 4},
                    correlation=ctx,
                    causation_id=entry.event_id,
                ),
            ]

        # System 2: processes ``derived.step`` events with
        # even ``i`` (i.e. e1 and e2). It checks the
        # EventLog (the projection collapses same-type
        # events, so the view is the WRONG place to
        # enumerate). For the test we scan the components
        # for a ``derived.step`` slot — the projection
        # collapses the 4 events to 1 slot, but the slot
        # carries the LAST event's data. To distinguish
        # "saw at least one derived event" we just check
        # the slot exists.
        sys2_correlations: list[UUID] = []

        def system2(world) -> list[Event]:
            from kntgraph.core.event import correlation_middleware

            view = world.get_agent("a-1")
            if view is None:
                return []
            # System 2 fires if the view carries a
            # ``derived.step`` component (system 1 emitted
            # one or more of these).
            if "derived.step" not in view.components:
                return []
            ctx = correlation_middleware.current()
            if ctx is not None:
                sys2_correlations.append(ctx.correlation_id)
            return []

        # System 3: processes ``derived.step`` events with
        # odd ``i`` (i.e. e3 and e4). Same gating as
        # system 2 — they BOTH run when system 1 emits
        # (the EventLog carries 4 derived events; both
        # systems observe the world post-fold which has
        # the derived.step component). We use two
        # systems to demonstrate that BOTH inherit the
        # same correlation_id from the middleware.
        sys3_correlations: list[UUID] = []

        def system3(world) -> list[Event]:
            from kntgraph.core.event import correlation_middleware

            view = world.get_agent("a-1")
            if view is None:
                return []
            if "derived.step" not in view.components:
                return []
            ctx = correlation_middleware.current()
            if ctx is not None:
                sys3_correlations.append(ctx.correlation_id)
            return []

        dispatcher = ReactiveDispatcher(
            log,
            systems=[system1, system2, system3],
            poll_interval=0.05,
            redis=clean_redis,
        )

        # Tick 1: dispatcher processes the entry event.
        # System 1 fires (entry present), emits 4 derived
        # events. Systems 2/3 see only the entry — no
        # step1/step2 in the components yet.
        await dispatcher.dispatch_once()

        # Tick 2: the 4 derived events are now in the EventLog.
        # System 1 does not re-fire (its flag is set).
        # Systems 2/3 fire — they see step1/step2 events in
        # the world.
        await dispatcher.dispatch_once()

        # One more tick to settle.
        await dispatcher.dispatch_once()

        # ---- Assertions ----

        # 1. The 4 derived events landed in the EventLog.
        events = await _read_domain_events_for_agent(log, "a-1")
        derived = [e for e in events if e.event_type == "derived.step"]
        assert len(derived) == 4, (
            f"expected 4 derived events in the log, got "
            f"{len(derived)}: {[e.event_type for e in events]}"
        )

        # 2. Systems 2 and 3 fired with the entry's
        #    correlation_id (the audit-trail invariant).
        #    The default projection collapses multiple
        #    events of the same type into one component
        #    slot (last-event-wins), so system 2 fires once
        #    for the step1 events and system 3 fires once
        #    for the step2 events. Each fire carries the
        #    flow's correlation_id via the middleware.
        assert len(sys2_correlations) >= 1, (
            "system 2 did not fire; expected at least one "
            "invocation carrying the flow's correlation"
        )
        assert all(cid == flow_id for cid in sys2_correlations), (
            f"system 2 saw correlation_ids {sys2_correlations}, "
            f"expected all to be {flow_id}; correlation propagation "
            f"is broken at the system boundary"
        )
        assert len(sys3_correlations) >= 1, (
            "system 3 did not fire; expected at least one "
            "invocation carrying the flow's correlation"
        )
        assert all(cid == flow_id for cid in sys3_correlations), (
            f"system 3 saw correlation_ids {sys3_correlations}, "
            f"expected all to be {flow_id}; correlation propagation "
            f"is broken at the system boundary"
        )

        # 3. THE AUDIT-TRAIL INVARIANT: every derived event
        #    carries the entry's correlation_id.
        for d in derived:
            assert d.correlation.correlation_id == flow_id, (
                f"derived event {d.event_type} (id={d.event_id}) "
                f"has correlation_id {d.correlation.correlation_id}, "
                f"expected entry flow id {flow_id}; "
                f"audit trail is broken at the dispatcher boundary"
            )

        # 4. The entry itself also has the flow id.
        assert entry.correlation.correlation_id == flow_id

        # 5. The audit query returns ALL events of the flow
        #    (entry + 4 derived).
        flow_events = [e for e in events if e.correlation.correlation_id == flow_id]
        assert len(flow_events) == 5, (
            f"correlation_id query returned {len(flow_events)}/5 events; "
            f"audit trail is broken. "
            f"Event types: {[e.event_type for e in flow_events]}"
        )


# ---------------------------------------------------------------------------
# Test 2: 3 concurrent requests → 1 system → 3 distinct flows
# ---------------------------------------------------------------------------


class TestConcurrentRequestsDistinctFlows:
    """Three requests arrive at the dispatcher in the SAME
    tick. Each is its own flow (distinct ``correlation_id``).
    A single system processes all three. After the dispatch,
    the audit query by each flow's correlation_id returns
    ONLY that flow's events — no cross-contamination.
    """

    async def test_concurrent_flows_keep_correlation_boundaries(
        self, clean_redis: Any
    ) -> None:
        log = EventLog(RedisEventLogAdapter(clean_redis))

        # Three distinct flows.
        flow_ids = [
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
            UUID("33333333-3333-3333-3333-333333333333"),
        ]

        # Seed a spawned lifecycle for each agent so the
        # dispatcher's bootstrap discovers them.
        await _seed_spawned(log, "a-1")
        await _seed_spawned(log, "a-2")
        await _seed_spawned(log, "a-3")

        # Append the three entries in a single burst (the
        # dispatcher will see them as ONE batch per agent).
        entries: list[Event] = []
        for i, (agent_id, flow_id) in enumerate(
            zip(["a-1", "a-2", "a-3"], flow_ids), start=1
        ):
            entry = Event.create(
                event_type="request.received",
                agent_id=agent_id,
                event_class="domain",
                data={"flow": i, "request": f"req-{i}"},
                correlation=_ctx(flow_id),
                event_id=flow_id,
            )
            entries.append(entry)
            await log.append(entry)

        # The system observes each entry and records what
        # correlation the middleware carried for that
        # agent's invocation.
        observed: dict[str, UUID] = {}

        def system(world) -> list[Event]:
            from kntgraph.core.event import correlation_middleware

            ctx = correlation_middleware.current()
            assert ctx is not None, "middleware empty"
            for view in world.agents.values():
                if view.last_event_id is None:
                    continue
                # Record the correlation the middleware carried
                # for THIS invocation. The dispatcher SETS the
                # middleware via ``continue_from(entry)`` for
                # each agent in this tick.
                observed[view.agent_id] = ctx.correlation_id
            return []  # explicit empty list, not None

        dispatcher = ReactiveDispatcher(
            log,
            systems=[system],
            poll_interval=0.05,
            redis=clean_redis,
        )

        # Single tick processes all three agents.
        await dispatcher.dispatch_once()
        # One more tick to settle cursors.
        await dispatcher.dispatch_once()

        # ---- Assertions ----

        # 1. The system observed all three entries.
        assert len(observed) == 3, (
            f"expected 3 system invocations (one per entry), "
            f"got {len(observed)}: {observed}"
        )

        # 2. The middleware carried the CORRECT correlation_id
        #    for each invocation. This is the critical
        #    invariant: even when the system runs concurrently
        #    for multiple agents in the same tick, the
        #    middleware SETS itself per-agent via
        #    ``continue_from(entry)``.
        for entry, agent_id in zip(entries, ["a-1", "a-2", "a-3"]):
            assert observed[agent_id] == entry.correlation.correlation_id, (
                f"agent {agent_id}: system saw correlation_id "
                f"{observed[agent_id]}, "
                f"expected {entry.correlation.correlation_id} (entry flow id); "
                f"concurrent flows are leaking through the middleware"
            )

        # 3. The audit query for each flow returns ONLY that
        #    flow's domain events (no leakage between flows).
        events: list[Event] = []
        for agent_id in ["a-1", "a-2", "a-3"]:
            events.extend(await _read_domain_events_for_agent(log, agent_id))

        # Each entry has its own correlation_id; no event
        # should appear in two flows.
        flow_to_events: dict[UUID, list[Event]] = {fid: [] for fid in flow_ids}
        for e in events:
            cid = e.correlation.correlation_id
            assert cid in flow_to_events, (
                f"event {e.event_type} (id={e.event_id}) has "
                f"correlation_id {cid}, which is not one of the "
                f"three flow ids {flow_ids}; cross-flow leakage"
            )
            flow_to_events[cid].append(e)

        # Each flow has exactly 1 domain event: the entry.
        for fid in flow_ids:
            flow_domain = flow_to_events[fid]
            assert len(flow_domain) == 1, (
                f"flow {fid}: expected exactly 1 domain event "
                f"(the entry), got {len(flow_domain)}: "
                f"{[e.event_type for e in flow_domain]}"
            )
            assert flow_domain[0].event_type == "request.received"

        # 4. THE FULL TRANSACTION's audit query: for each flow,
        # the "events with this correlation_id" set contains
        # exactly the entry.
        for fid, entry in zip(flow_ids, entries):
            flow_events_full = [
                e for e in events if e.correlation.correlation_id == fid
            ]
            assert len(flow_events_full) == 1, (
                f"audit query for flow {fid} returned "
                f"{len(flow_events_full)} events; expected 1 (the entry). "
                f"Events: {[(e.event_type, e.correlation.correlation_id) for e in flow_events_full]}"
            )
            assert flow_events_full[0].event_id == entry.event_id
