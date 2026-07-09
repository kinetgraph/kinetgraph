# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for the ``Runner`` and ``ReactiveDispatcher``.

The ``Runner`` is the side-effecting counterpart of pure systems. It:
  1. Folds the World from the EventLog (pure).
  2. Applies cyclic systems (pure).
  3. Appends the new events to the EventLog (idempotent).

The ``ReactiveDispatcher`` (v2.2) folds new events incrementally
into a per-agent World, calls each ``WorldSystem`` once with the
post-fold World, and persists a ``WorldCheckpoint`` to Redis.

Tests use real Redis and verify that the loop produces the
expected events deterministically.

See: ADR-018 — WorldIncremental + WorldSystem.
"""

from __future__ import annotations
from kntgraph.core.event import CorrelationContext

import pytest

from kntgraph.core.event import Event
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.runner.runner import Runner
from kntgraph.stream.event_log import EventLog

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Cyclic system — promotes spawned agents to idle.
# ---------------------------------------------------------------------------


def _ctx() -> CorrelationContext:
    return CorrelationContext.new(correlation_id=__import__("uuid").uuid4())


def promote_spawned_to_idle(world):
    """
    Cyclic system: find agents in "spawned" phase and emit
    an "agent.idle" lifecycle event for each.
    """
    out = []
    for agent_id, view in world.agents.items():
        if view.operational_phase == "spawned":
            out.append(
                Event.create(
                    event_type="agent.idle",
                    agent_id=agent_id,
                    event_class="lifecycle",
                    data={},
                    correlation=_ctx(),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Reactive system — validates received documents.
# ---------------------------------------------------------------------------


def validate_received_documents(world):
    """
    Reactive system: for every agent whose World has a
    ``document.received`` component AND no
    ``document.validated`` yet, emit the validated event.

    Inspects the World directly — does NOT receive the
    triggering event (v2.2 contract).
    """
    out = []
    for agent_id, view in world.agents.items():
        received = view.components.get("document.received")
        if received is None:
            continue
        if "document.validated" in view.components:
            continue
        out.append(
            Event.create(
                event_type="document.validated",
                agent_id=agent_id,
                event_class="domain",
                data={**received, "validated": True},
                causation_id=view.last_event_id,
                correlation=_ctx(),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Runner tests
# ---------------------------------------------------------------------------


class TestRunnerTick:
    async def test_tick_once_no_events(self, clean_redis):
        log = EventLog(clean_redis)
        runner = Runner(log, cyclic_systems=[])
        n_before = await log.stream_len("a-1")
        await runner.tick_once()
        n_after = await log.stream_len("a-1")
        assert n_after == n_before

    async def test_tick_once_promotes_spawned(self, clean_redis):
        log = EventLog(clean_redis)
        await log.append(
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            )
        )
        n_before = await log.stream_len("a-1")
        runner = Runner(log, cyclic_systems=[promote_spawned_to_idle])
        await runner.tick_once()
        events = await log.read("a-1")
        assert len(events) == n_before + 1
        types = [e.event_type for e in events]
        assert "agent.idle" in types

    async def test_tick_once_no_new_events_when_idle(self, clean_redis):
        """
        After the cyclic system promotes the agent to idle,
        a subsequent tick produces no events (the rule is
        already satisfied).
        """
        log = EventLog(clean_redis)
        await log.append(
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            )
        )
        runner = Runner(log, cyclic_systems=[promote_spawned_to_idle])
        # First tick: spawned → idle
        await runner.tick_once()
        n_after_first = await log.stream_len("a-1")
        # Second tick: no new events
        await runner.tick_once()
        n_after_second = await log.stream_len("a-1")
        assert n_after_first == n_after_second

    async def test_multiple_ticks(self, clean_redis):
        log = EventLog(clean_redis)
        await log.append(
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            )
        )
        runner = Runner(log, cyclic_systems=[promote_spawned_to_idle])
        # Idempotent: two ticks on the same world produce the same output.
        await runner.tick_once()
        n_after_first = await log.stream_len("a-1")
        await runner.tick_once()
        n_after_second = await log.stream_len("a-1")
        assert n_after_first == n_after_second

    async def test_multiple_systems(self, clean_redis):
        log = EventLog(clean_redis)
        await log.append(
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            )
        )

        def emit_something(world):
            for agent_id, view in world.agents.items():
                if view.operational_phase == "spawned":
                    return [
                        Event.create(
                            event_type="agent.idle",
                            agent_id=agent_id,
                            event_class="lifecycle",
                            data={},
                            correlation=_ctx(),
                        )
                    ]
            return []

        runner = Runner(
            log,
            cyclic_systems=[promote_spawned_to_idle, emit_something],
        )
        await runner.tick_once()
        # Both systems see the same World; only one should fire
        # (the second sees the agent as spawned still because the
        # first system's output isn't in the World yet — the
        # Runner folds once at the START of the tick).
        events = await log.read("a-1")
        idle_count = sum(1 for e in events if e.event_type == "agent.idle")
        assert idle_count == 1

    async def test_idempotent_replay(self, clean_redis):
        log = EventLog(clean_redis)
        await log.append(
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            )
        )
        await log.append(
            Event.create(
                event_type="agent.idle",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            )
        )
        runner = Runner(log, cyclic_systems=[promote_spawned_to_idle])
        # No new events: the agent is already idle, the
        # promote_spawned_to_idle system produces nothing.
        n_before = await log.stream_len("a-1")
        await runner.tick_once()
        n_after = await log.stream_len("a-1")
        assert n_after == n_before

    async def test_start_and_stop(self, clean_redis):
        log = EventLog(clean_redis)
        runner = Runner(log, cyclic_systems=[], tick_interval=0.1)
        await runner.start()
        await runner.stop()

    async def test_start_is_idempotent(self, clean_redis):
        log = EventLog(clean_redis)
        runner = Runner(log, cyclic_systems=[], tick_interval=0.1)
        await runner.start()
        await runner.start()  # no-op
        await runner.stop()


# ---------------------------------------------------------------------------
# ReactiveDispatcher tests (run alongside the Runner)
# ---------------------------------------------------------------------------


class TestRunnerReactive:
    async def test_reactive_dispatch(self, clean_redis):
        log = EventLog(clean_redis)
        await log.append(
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            )
        )
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"document_id": "NF-001"},
                correlation=_ctx(),
            )
        )
        dispatcher = ReactiveDispatcher(
            log,
            systems=[validate_received_documents],
            poll_interval=0.1,
            redis=clean_redis,
        )
        processed = await dispatcher.dispatch_once()
        assert processed >= 1
        events = await log.read("a-1")
        types = [e.event_type for e in events]
        assert "document.validated" in types

    async def test_reactive_filter(self, clean_redis):
        """
        Filter applies to incoming events. Events that fail
        the filter are still folded into the World (so the
        World stays consistent with the full stream) but
        they do NOT count toward ``processed``. The dispatcher
        only invokes systems when at least one event in the
        batch passed the filter.
        """
        log = EventLog(clean_redis)
        await log.append(
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            )
        )
        await log.append(
            Event.create(
                event_type="metric.observed",
                agent_id="a-1",
                event_class="domain",
                data={"v": 1},
                correlation=_ctx(),
            )
        )

        # Track invocations
        invocations: list[int] = []

        def sys(world):
            invocations.append(1)
            return []

        # Filter accepts only metric.observed — agent.spawned
        # is a lifecycle event (filtered).
        dispatcher = ReactiveDispatcher(
            log,
            systems=[sys],
            filter_fn=lambda e: e.event_type == "metric.observed",
            redis=clean_redis,
        )
        processed = await dispatcher.dispatch_once()
        # Only metric.observed passed the filter.
        assert processed == 1
        # The system was invoked once (at least one event
        # passed the filter).
        assert len(invocations) == 1

        # Now test that ALL events filtered → system not invoked.
        dispatcher2 = ReactiveDispatcher(
            log,
            systems=[sys],
            filter_fn=lambda e: e.event_type == "nonexistent",
            redis=clean_redis,
        )
        # Re-create the dispatcher fresh; the previous one
        # advanced its own checkpoint.
        processed = await dispatcher2.dispatch_once()
        assert processed == 0
        # The system was NOT invoked again (only the first
        # invocation).
        assert len(invocations) == 1

    async def test_reactive_idempotent(self, clean_redis):
        """
        Running the dispatcher twice on the same events does
        not duplicate the output. The second dispatch
        produces nothing (cursor advanced; no new events).
        """
        log = EventLog(clean_redis)
        await log.append(
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            )
        )
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"x": 1},
                correlation=_ctx(),
            )
        )
        dispatcher = ReactiveDispatcher(
            log,
            systems=[validate_received_documents],
            redis=clean_redis,
        )
        await dispatcher.dispatch_once()
        n_after_first = await log.stream_len("a-1")
        await dispatcher.dispatch_once()
        n_after_second = await log.stream_len("a-1")
        # Idempotent: no new events on second dispatch
        assert n_after_first == n_after_second


class TestRunnerWithReactive:
    async def test_cyclic_and_reactive_together(self, clean_redis):
        """
        End-to-end: a cyclic system kicks idle agents; a
        reactive system validates received documents. Both
        run via the EventLog; the World is always
        reconstructed by fold.
        """
        log = EventLog(clean_redis)
        await log.append(
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            )
        )
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"document_id": "NF-001"},
                correlation=_ctx(),
            )
        )

        runner = Runner(log, cyclic_systems=[promote_spawned_to_idle])
        dispatcher = ReactiveDispatcher(
            log,
            systems=[validate_received_documents],
            redis=clean_redis,
        )
        # Run a tick (idempotent: should only add the
        # "agent.idle").
        await runner.tick_once()
        # Dispatch reactive (adds document.validated).
        await dispatcher.dispatch_once()
        events = await log.read("a-1")
        types = sorted({e.event_type for e in events})
        assert "agent.spawned" in types
        assert "agent.idle" in types
        assert "document.received" in types
        assert "document.validated" in types
        # All four events present, no duplicates
        assert len(events) == 4
