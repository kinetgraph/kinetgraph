# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Integration tests for ``MetricsSink`` wiring into
``ReactiveDispatcher``.

The Protocol + no-op contract lives in
:mod:`tests.unit.runner.test_metrics_sink`. This file pins
the **wiring** -- the dispatcher actually calls the sink
at the right moments and with the right values:

  - the four ADR-075 Tier 4 read queries
    (``in_flight_tasks`` / ``stale_tasks`` /
    ``stuck_in_queue`` / ``dead_lettered_tasks``) push the
    size of the returned list;
  - the ``compensation_started`` event counter is
    incremented once per ``*.compensation_started`` event
    that flows through
    :func:`kntgraph.runner._systems_runner.append_system_outgoing`.

No Redis, no FalkorDB. The dispatcher's tick loop is not
exercised -- the methods are called directly so the test
stays in-process and the assertion is about wiring, not
about the dispatcher's recovery semantics (those are
covered by the existing observability suite).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.infra.world_checkpoint import WorldCheckpoint
from kntgraph.runner import MetricsSink
from kntgraph.runner.reactive import ReactiveDispatcher


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _RecordingSink:
    """A :class:`MetricsSink` that records every call.

    Used as the ``metrics_sink=`` argument to
    ``ReactiveDispatcher`` so the test can assert that the
    right method was called with the right argument.
    """

    in_flight_calls: list[int] = field(default_factory=list)
    stale_calls: list[int] = field(default_factory=list)
    stuck_calls: list[int] = field(default_factory=list)
    dlq_calls: list[int] = field(default_factory=list)
    compensation_calls: int = 0

    def record_in_flight(self, count: int) -> None:
        self.in_flight_calls.append(count)

    def record_stale(self, count: int) -> None:
        self.stale_calls.append(count)

    def record_stuck_in_queue(self, count: int) -> None:
        self.stuck_calls.append(count)

    def record_dead_lettered(self, count: int) -> None:
        self.dlq_calls.append(count)

    def incr_compensation_started(self) -> None:
        self.compensation_calls += 1


class _FakeWorldStore:
    """Minimal ``IncrementalWorldStore`` stand-in.

    The Tier 4 query tests use ``world_store=None`` to
    short-circuit (``_load_views`` returns ``{}``); this
    class is here for the compensation counter test, which
    does not exercise the world store directly but needs
    the attribute to exist.
    """

    async def load(self, agent_id: str) -> WorldCheckpoint:
        return WorldCheckpoint(world=World.empty(), last_stream_id="-")

    async def save(self, agent_id: str, checkpoint: WorldCheckpoint) -> None:
        return None


class _FakeEventLog:
    """Records every batch appended so the test can assert
    that the events made it to the log before the counter
    incremented (durability ordering).
    """

    def __init__(self) -> None:
        self.appended: list[Event] = []

    async def append_batch(self, events: list[Event]) -> Any:
        self.appended.extend(events)
        return ["ok"] * len(events)

    async def read_after_cursor(
        self, agent_id: str, cursor: str
    ) -> tuple[list[Event], str]:
        return [], cursor

    async def list_agents(self) -> list[str]:
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_dispatcher(*, sink: MetricsSink) -> ReactiveDispatcher:
    """Build a dispatcher with the metrics sink wired.

    A real ``_FakeWorldStore`` is provided because the
    dispatcher constructor rejects ``world_store=None``
    without ``redis=``. ``_load_views`` iterates
    ``_tracked_agents`` (empty in tests) and returns ``{}``;
    the Tier 4 queries then produce ``[]`` and push ``0``
    to the sink.
    """
    return ReactiveDispatcher(
        log=_FakeEventLog(),
        world_store=_FakeWorldStore(),
        metrics_sink=sink,
    )


# ---------------------------------------------------------------------------
# Tier 4 query wiring
# ---------------------------------------------------------------------------


async def test_in_flight_tasks_pushes_result_size_to_sink() -> None:
    sink = _RecordingSink()
    dispatcher = _build_dispatcher(sink=sink)
    result = await dispatcher.in_flight_tasks()
    # ``world_store=None`` ⇒ ``_load_views`` returns ``{}``
    # ⇒ ``_in_flight_tasks`` returns ``[]``.
    assert result == []
    assert sink.in_flight_calls == [0]


async def test_stale_tasks_pushes_result_size_to_sink() -> None:
    sink = _RecordingSink()
    dispatcher = _build_dispatcher(sink=sink)
    result = await dispatcher.stale_tasks()
    assert result == []
    assert sink.stale_calls == [0]


async def test_stuck_in_queue_pushes_result_size_to_sink() -> None:
    sink = _RecordingSink()
    dispatcher = _build_dispatcher(sink=sink)
    result = await dispatcher.stuck_in_queue()
    # ``world_store=None`` ⇒ ``stuck_in_queue`` short-circuits
    # to ``[]`` before reading the views.
    assert result == []
    assert sink.stuck_calls == [0]


async def test_dead_lettered_tasks_pushes_result_size_to_sink() -> None:
    sink = _RecordingSink()
    dispatcher = _build_dispatcher(sink=sink)
    result = await dispatcher.dead_lettered_tasks()
    # No DLQ wired ⇒ the query returns ``[]``.
    assert result == []
    assert sink.dlq_calls == [0]


# ---------------------------------------------------------------------------
# detect_and_recover pushes all four metrics
# ---------------------------------------------------------------------------


async def test_detect_and_recover_pushes_all_four_metrics() -> None:
    """``detect_and_recover`` calls the four Tier 4
    queries internally; each query pushes its metric, so
    the sink sees all four ``record_*`` calls with the
    matching sample value.
    """
    sink = _RecordingSink()
    dispatcher = _build_dispatcher(sink=sink)
    report = await dispatcher.detect_and_recover()
    assert report.in_flight_count == 0
    assert report.stale_count == 0
    assert report.stuck_in_queue_count == 0
    assert report.dead_lettered_count == 0
    # One call per Tier 4 query, in declaration order.
    assert sink.in_flight_calls == [0]
    assert sink.stale_calls == [0]
    assert sink.stuck_calls == [0]
    assert sink.dlq_calls == [0]


# ---------------------------------------------------------------------------
# Compensation counter
# ---------------------------------------------------------------------------


def _compensation_event(agent_id: str) -> Event:
    """Build a ``saga.<name>.<step>.compensation_started``
    event shaped like the saga projection emits
    (ADR-069 §11.18.2).
    """
    return Event.create(
        event_type="saga.fixture.step_a.compensation_started",
        agent_id=agent_id,
        event_class="domain",
        data={"step": "step_a", "saga": "fixture"},
        correlation=CorrelationContext.new(correlation_id=uuid4()),
    )


def _non_compensation_event(agent_id: str) -> Event:
    """A regular domain event with no compensation marker."""
    return Event.create(
        event_type="saga.fixture.step_a.step_completed",
        agent_id=agent_id,
        event_class="domain",
        data={"step": "step_a"},
        correlation=CorrelationContext.new(correlation_id=uuid4()),
    )


class _EmitCompensationSystem:
    """A system that emits the supplied events.

    Used to drive ``append_system_outgoing`` through a
    controlled outgoing batch without spinning the
    dispatcher's tick loop.
    """

    def __init__(self, *events: Event) -> None:
        self._events = list(events)

    def __call__(self, world: World) -> list[Event]:
        return list(self._events)


async def test_compensation_started_event_increments_counter() -> None:
    """A single ``*.compensation_started`` event in the
    outgoing batch triggers exactly one
    :meth:`MetricsSink.incr_compensation_started` call.

    The counter is incremented AFTER the events are
    appended to the EventLog (durability ordering) -- the
    test asserts the events landed first so the assertion
    also pins the order.
    """
    sink = _RecordingSink()
    dispatcher = _build_dispatcher(sink=sink)
    dispatcher._world_store = _FakeWorldStore()
    agent_id = "agent-comp"
    evt = _compensation_event(agent_id)
    dispatcher._systems = [_EmitCompensationSystem(evt)]

    from kntgraph.runner._systems_runner import append_system_outgoing

    await append_system_outgoing(
        dispatcher,
        world=World.empty(),
        agent_id=agent_id,
        return_events=False,
    )

    # The event made it to the log first.
    assert len(dispatcher._log.appended) == 1
    assert dispatcher._log.appended[0].event_type == evt.event_type
    # Then the counter advanced exactly once.
    assert sink.compensation_calls == 1


async def test_non_compensation_events_do_not_increment_counter() -> None:
    """The counter only fires on events whose type ends in
    ``.compensation_started``. A regular domain event in the
    outgoing batch is appended to the log but does NOT
    touch the counter.
    """
    sink = _RecordingSink()
    dispatcher = _build_dispatcher(sink=sink)
    dispatcher._world_store = _FakeWorldStore()
    agent_id = "agent-nocomp"
    dispatcher._systems = [_EmitCompensationSystem(_non_compensation_event(agent_id))]

    from kntgraph.runner._systems_runner import append_system_outgoing

    await append_system_outgoing(
        dispatcher,
        world=World.empty(),
        agent_id=agent_id,
        return_events=False,
    )

    assert len(dispatcher._log.appended) == 1
    assert sink.compensation_calls == 0


async def test_mixed_outgoing_batch_counts_only_compensation_started() -> None:
    """In a batch with N ``*.compensation_started`` events
    and M other events, the counter advances by N (not by
    ``N + M`` and not by 0).
    """
    sink = _RecordingSink()
    dispatcher = _build_dispatcher(sink=sink)
    dispatcher._world_store = _FakeWorldStore()
    agent_id = "agent-mix"
    events = [
        _compensation_event(agent_id),
        _non_compensation_event(agent_id),
        _compensation_event(agent_id),
        _non_compensation_event(agent_id),
        _compensation_event(agent_id),
    ]
    dispatcher._systems = [_EmitCompensationSystem(*events)]

    from kntgraph.runner._systems_runner import append_system_outgoing

    await append_system_outgoing(
        dispatcher,
        world=World.empty(),
        agent_id=agent_id,
        return_events=False,
    )

    assert len(dispatcher._log.appended) == 5
    assert sink.compensation_calls == 3


async def test_empty_outgoing_batch_does_not_touch_counter() -> None:
    """When the systems emit nothing, the EventLog is not
    touched and the counter is not touched either.
    """
    sink = _RecordingSink()
    dispatcher = _build_dispatcher(sink=sink)
    dispatcher._world_store = _FakeWorldStore()
    dispatcher._systems = [_EmitCompensationSystem()]  # emits nothing

    from kntgraph.runner._systems_runner import append_system_outgoing

    await append_system_outgoing(
        dispatcher,
        world=World.empty(),
        agent_id="agent-empty",
        return_events=False,
    )

    assert dispatcher._log.appended == []
    assert sink.compensation_calls == 0
