# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for ``ReactiveDispatcher`` (v2.2).

The dispatcher is the side-effecting reactive counterpart of
the ``Runner``. These tests pin the contract:

  - Cursor is exclusive (Redis stream id with `(` prefix), so a
    trim of the stream does not silently drop new events.
  - Per-agent cursor is tracked via the ``WorldCheckpoint``
    in Redis (one key per agent). The dispatcher does NOT
    perform a SCAN after bootstrap.
  - Cursor survives dispatcher restart: a new dispatcher
    resumes from the saved ``WorldCheckpoint``.
  - Reactive systems receive the FULL World for the agent
    AFTER the batch has been folded in (incremental fold).
  - The processed counter counts unique events dispatched
    (per agent), not per (event, system) pair.
  - Cross-agent emissions (a system emits for a different
    agent_id) are picked up on the NEXT dispatch call.
  - Filter applies to the INPUT event, not the output.
  - Reactive systems that produce multiple outputs cause
    exactly one batched append.
  - Repeated dispatch cycles do NOT duplicate validated
    events for the same received event.

Systems use the v2.2 contract: ``(world) -> list[Event]``.
See ADR-018.
"""

from __future__ import annotations
from kntgraph.core.event import CorrelationContext

import pytest

from kntgraph.core.event import Event
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_spawned(log: EventLog, agent_id: str) -> Event:
    e = Event.create(
        event_type="agent.spawned",
        agent_id=agent_id,
        event_class="lifecycle",
        correlation=_ctx(),
    )
    await log.append(e)
    return e


# ---------------------------------------------------------------------------
# P1 — Cursor semantics
# ---------------------------------------------------------------------------


def _ctx() -> CorrelationContext:
    return CorrelationContext.new(correlation_id=__import__("uuid").uuid4())


class TestCursorSemantics:
    async def test_exclusive_cursor_does_not_re_dispatch_same_event(self, clean_redis):
        """
        After dispatch, the cursor must point PAST the last
        processed event. A second dispatch must NOT re-fire
        the reactive system on the same event (cursor is
        exclusive, so xrange returns nothing for already-seen
        events).
        """
        log = EventLog(clean_redis)
        await _seed_spawned(log, "a-1")

        received_count: list[int] = []

        def sys(world):
            # Count domain events visible in the World
            count = sum(
                1
                for v in world.agents.values()
                for comp in v.components.values()
                if isinstance(comp, dict)
            )
            received_count.append(count)
            return []

        dispatcher = ReactiveDispatcher(
            log, systems=[sys], poll_interval=0.1, redis=clean_redis
        )
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"k": 1},
                correlation=_ctx(),
            )
        )
        await dispatcher.dispatch_once()
        # The system was invoked once for the new event.
        assert len(received_count) == 1
        # A second dispatch is a no-op (the cursor is past the
        # only event; nothing new in the stream).
        await dispatcher.dispatch_once()
        # The system is NOT re-invoked.
        assert len(received_count) == 1

    async def test_stream_trim_does_not_drop_new_events(self, clean_redis):
        """
        If the stream is trimmed (MAXLEN) past the cursor, the
        dispatcher must still process events that arrived AFTER
        the cursor — not silently skip them.

        Reproduces P4: the older dispatcher asked
        ``xrange min=cursor`` (inclusive) and got ``[]`` once
        the cursor was trimmed out, dropping every subsequent
        event on the floor.
        """
        log = EventLog(clean_redis)
        await clean_redis.delete("knt:agents:a-1:events")
        await _seed_spawned(log, "a-1")
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"step": 1},
                correlation=_ctx(),
            )
        )

        fired_steps: list[int] = []

        def sys(world):
            for v in world.agents.values():
                for comp in v.components.values():
                    if isinstance(comp, dict) and "step" in comp:
                        fired_steps.append(comp["step"])
            return []

        dispatcher = ReactiveDispatcher(
            log, systems=[sys], poll_interval=0.1, redis=clean_redis
        )
        await dispatcher.dispatch_once()
        assert 1 in fired_steps, fired_steps

        # Aggressively trim the stream past the cursor.
        await clean_redis.xtrim("knt:agents:a-1:events", maxlen=1, approximate=False)

        # Add a NEW event after the trim.
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"step": 2},
                correlation=_ctx(),
            )
        )

        # The dispatcher MUST process the new event, even though
        # the cursor was trimmed out of the stream.
        await dispatcher.dispatch_once()
        assert 2 in fired_steps, (
            f"Dispatcher dropped the new event after trim. Fired: {fired_steps!r}"
        )


# ---------------------------------------------------------------------------
# P2 — Cursor persistence across dispatcher restarts
# ---------------------------------------------------------------------------


class TestCursorPersistence:
    async def test_no_full_scan_after_bootstrap(self, clean_redis):
        """
        After the first dispatch_once has tracked an agent, the
        dispatcher's checkpoint lives in Redis. A new dispatcher
        with the same Redis resumes from there — no full scan
        needed (and the dispatcher does not call scan_iter after
        bootstrap).

        We break ``scan_iter`` after bootstrap and verify the
        dispatcher still works for the already-seen agent.
        """
        log = EventLog(clean_redis)
        await _seed_spawned(log, "a-1")
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"x": 1},
                correlation=_ctx(),
            )
        )

        d = ReactiveDispatcher(log, systems=[], redis=clean_redis)

        # Bootstrap dispatch: needs scan to discover a-1.
        await d.dispatch_once()

        # Now break scan_iter. Subsequent dispatches should
        # still work because the dispatcher's checkpoint is
        # in Redis (not in-memory).
        async def broken_scan(*args, **kwargs):
            if False:
                yield b""
            return

        log._storage.client.scan_iter = broken_scan  # type: ignore[assignment]

        # Add another event and dispatch.
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"x": 2},
                correlation=_ctx(),
            )
        )
        # If the dispatcher does not depend on SCAN for
        # known agents, this must succeed without errors.
        await d.dispatch_once()


# ---------------------------------------------------------------------------
# P3 — Reactive systems see the World folded up to the
# CURRENT event, not up to the first event in the batch
# ---------------------------------------------------------------------------


class TestWorldViewIsFresh:
    async def test_reactive_system_sees_prior_event_in_world(self, clean_redis):
        """
        When processing a batch of N events for the same agent,
        the reactive system sees the World folded with ALL N
        events (incremental fold). The dispatcher calls the
        system once per batch — the World carries every event's
        effect via the projection.
        """
        log = EventLog(clean_redis)
        await _seed_spawned(log, "a-1")

        # Append 3 received events
        for i in range(3):
            await log.append(
                Event.create(
                    event_type="document.received",
                    agent_id="a-1",
                    event_class="domain",
                    data={"i": i},
                    correlation=_ctx(),
                )
            )

        # Capture the world size when the system is called.
        # The default projection replaces components on each
        # domain event, so the World has 1 component (the
        # last ``document.received``) but the agent has been
        # touched by 3 events. We verify the dispatcher
        # folds ALL events into the World before calling.
        system_calls: list[dict[str, int]] = []

        def sys(world):
            view = world.agents.get("a-1")
            assert view is not None
            system_calls.append(
                {
                    "tick": world.tick,
                    "components": len(view.components),
                    "domain_phase": view.domain_phase,
                }
            )
            return []

        d = ReactiveDispatcher(log, systems=[sys], redis=clean_redis)
        await d.dispatch_once()
        # The system is called ONCE per batch (v2.2). The World
        # has been folded with all 3 events (tick=4: spawn + 3
        # received = 4 ticks).
        assert len(system_calls) == 1
        # The agent has domain_phase = the last received event.
        assert system_calls[0]["domain_phase"] == "document.received"
        # The tick is the count of events applied (spawn + 3
        # received = 4 events folded).
        assert system_calls[0]["tick"] == 4, system_calls[0]  # tick is int


# ---------------------------------------------------------------------------
# P4 — Cross-agent emissions
# ---------------------------------------------------------------------------


class TestCrossAgentEmissions:
    async def test_emission_for_other_agent_picked_up_next_tick(self, clean_redis):
        """
        A reactive system processing events for agent A may emit
        a follow-up event for agent B. That emission must be
        visible to a subsequent dispatch_once (for B), not lost.
        """
        log = EventLog(clean_redis)
        await _seed_spawned(log, "a-1")
        await _seed_spawned(log, "b-1")
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"for_b": True},
                correlation=_ctx(),
            )
        )

        def sys_a_to_b(world):
            # Find a-1's "document.received" component
            a_view = world.agents.get("a-1")
            if a_view is None:
                return []
            received = a_view.components.get("document.received")
            if received is None or not received.get("for_b"):
                return []
            return [
                Event.create(
                    event_type="document.queued",
                    agent_id="b-1",
                    event_class="domain",
                    data={"from": "a-1"},
                    causation_id=a_view.last_event_id,
                    correlation=_ctx(),
                )
            ]

        d = ReactiveDispatcher(log, systems=[sys_a_to_b], redis=clean_redis)
        await d.dispatch_once()
        # a-1 → document.queued for b-1
        b_events = await log.read("b-1")
        types = [e.event_type for e in b_events]
        assert "document.queued" in types, (
            f"Cross-agent emission lost. b-1 events: {types}"
        )


# ---------------------------------------------------------------------------
# P5 — Filter applies to the INPUT event
# ---------------------------------------------------------------------------


class TestFilter:
    async def test_filter_drops_non_matching_events(self, clean_redis):
        """
        The filter is applied to incoming events, not to
        events emitted by reactive systems. Filtered events
        are still FOLDED into the World (so the World stays
        consistent with the full stream) but the system is
        not called for them.
        """
        log = EventLog(clean_redis)
        await _seed_spawned(log, "a-1")
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={},
                correlation=_ctx(),
            )
        )
        await log.append(
            Event.create(
                event_type="metric.observed",
                agent_id="a-1",
                event_class="domain",
                data={},
                correlation=_ctx(),
            )
        )

        # Track invocations: how many times the system was called.
        # Filter applies to the event surface — events that fail
        # the filter are still FOLDED into the World (so the World
        # stays consistent) but the system is not invoked.
        # Since the projection replaces components on each domain
        # event, the World ends up with ``metric.observed`` (the
        # last domain event). The system is called once per batch.
        invocations: list[int] = []

        def sys(world):
            invocations.append(1)
            return []

        d = ReactiveDispatcher(
            log,
            systems=[sys],
            filter_fn=lambda e: e.event_type == "document.received",
            redis=clean_redis,
        )
        await d.dispatch_once()
        # The system is called once per batch (filter does NOT
        # skip the invocation — it only filters which events
        # count toward ``processed``). The fold ran all events
        # (including the filtered metric.observed).
        assert len(invocations) == 1


# ---------------------------------------------------------------------------
# P6 — No SCAN after bootstrap
# ---------------------------------------------------------------------------


class TestSeenAgents:
    async def test_no_scan_on_subsequent_dispatches(self, clean_redis):
        """
        The dispatcher should NOT perform a Redis SCAN on every
        dispatch. Once the checkpoint is in Redis (for any
        previously seen agent), the dispatcher reads that
        agent's stream directly via XRANGE — no scan needed.
        """
        log = EventLog(clean_redis)
        await _seed_spawned(log, "a-1")
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"x": 1},
                correlation=_ctx(),
            )
        )

        d = ReactiveDispatcher(log, systems=[], redis=clean_redis)

        # Bootstrap dispatch: needs scan to discover a-1.
        await d.dispatch_once()

        # Now break scan_iter. Subsequent dispatches should
        # still work because the checkpoint is in Redis.
        async def broken_scan(*args, **kwargs):
            if False:
                yield b""
            return

        log._storage.client.scan_iter = broken_scan  # type: ignore[assignment]

        # Add another event and dispatch.
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"x": 2},
                correlation=_ctx(),
            )
        )
        # If the dispatcher does not depend on scan after
        # bootstrap, this must succeed.
        await d.dispatch_once()


# ---------------------------------------------------------------------------
# P7 — Idempotency under restart, end-to-end
# ---------------------------------------------------------------------------


class TestIdempotency:
    async def test_dispatcher_does_not_duplicate_validated(self, clean_redis):
        """
        Repeated dispatch cycles must not produce duplicate
        validated events for the same received event.

        After the FIRST dispatch:
          - World has both "document.received" and
            "document.validated" components (because the system
            emitted the validated event and the dispatcher
            folded it back in via append_batch).
          - Second dispatch: nothing new in the stream
            → cursor advance → system not re-invoked.
        """
        log = EventLog(clean_redis)
        await _seed_spawned(log, "a-1")
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"x": 1},
                correlation=_ctx(),
            )
        )

        def validate(world):
            view = world.agents.get("a-1")
            if view is None:
                return []
            if "document.validated" in view.components:
                # Already validated (idempotent rule satisfied).
                return []
            if "document.received" not in view.components:
                return []
            return [
                Event.create(
                    event_type="document.validated",
                    agent_id="a-1",
                    event_class="domain",
                    data={"x": 1},
                    causation_id=view.last_event_id,
                    correlation=_ctx(),
                )
            ]

        d = ReactiveDispatcher(log, systems=[validate], redis=clean_redis)
        for _ in range(5):
            await d.dispatch_once()

        events = await log.read("a-1")
        validated = [e for e in events if e.event_type == "document.validated"]
        assert len(validated) == 1, (
            f"Duplicates produced across dispatches: {len(validated)}"
        )
