# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
End-to-end integration tests for the ``MetricsSink`` wiring
through the ``ReactiveDispatcher``.

The unit tests in
:mod:`tests.unit.runner.test_metrics_integration` pin the
sink's contract with a fake dispatcher. These tests
exercise the **full pipeline** against real Redis (the
``clean_redis`` fixture flushes the database before each
test):

    ``dispatch_once()``
        -> ``_systems_runner.append_system_outgoing``
        -> ``sink.incr_compensation_started()`` (per
           ``*.compensation_started`` event)
        -> ``sink.record_*(...)`` (when the operator calls
           the Tier 4 query methods on the dispatcher)

This is the missing coverage that proves the new
``metrics_sink=`` parameter is wired all the way through.
A unit test with a recording sink cannot prove the
production tick loop calls the sink at the right
moments; an integration test against a real Redis-backed
``EventLog`` + ``IncrementalWorldStore`` does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._world_checkpoint._redis import (
    RedisWorldCheckpointStorage,
)
from kntgraph.infra.world_checkpoint import IncrementalWorldStore
from kntgraph.runner import MetricsSink, NullMetricsSink
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog

if TYPE_CHECKING:
    # ``prometheus_client`` is the optional extra behind
    # ``kntgraph[metrics]``. The runtime import below is
    # guarded by ``pytest.importorskip``; the TYPE_CHECKING
    # block keeps pyright happy with the symbols the
    # final test uses.
    from prometheus_client import CollectorRegistry  # type: ignore[reportUnknownVariableType]


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> CorrelationContext:
    """Build a fresh ``CorrelationContext`` with a random flow id."""
    return CorrelationContext.new(correlation_id=uuid4())


@dataclass
class _RecordingSink:
    """A :class:`MetricsSink` that records every call.

    Used as the ``metrics_sink=`` argument to
    ``ReactiveDispatcher`` so the test can assert that
    the right method was called the right number of
    times during a real ``dispatch_once()`` tick.
    """

    in_flight_calls: list[int] = field(default_factory=list[int])
    stale_calls: list[int] = field(default_factory=list[int])
    stuck_calls: list[int] = field(default_factory=list[int])
    dlq_calls: list[int] = field(default_factory=list[int])
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


def _wire_dispatcher(
    clean_redis: Any,
    *,
    sink: MetricsSink,
) -> tuple[ReactiveDispatcher, EventLog]:
    """Build a Redis-backed ``ReactiveDispatcher`` with the
    ``MetricsSink`` wired. Returns the dispatcher and the
    EventLog (so the test can seed events).
    """
    log = EventLog(RedisEventLogAdapter(clean_redis))
    store = IncrementalWorldStore(RedisWorldCheckpointStorage(clean_redis))
    dispatcher = ReactiveDispatcher(
        log=log,
        world_store=store,
        metrics_sink=sink,
        poll_interval=0.1,
    )
    return dispatcher, log


def _seed_event(agent_id: str, event_type: str = "seed.received") -> Event:
    """Build a domain event for ``agent_id`` (not yet
    appended)."""
    return Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class="domain",
        data={"k": "v"},
        correlation=_ctx(),
    )


# ---------------------------------------------------------------------------
# Core: tick loop fires the compensation counter
# ---------------------------------------------------------------------------


async def test_dispatch_once_fires_compensation_counter_through_sink(
    clean_redis: Any,
) -> None:
    """When a system emits a ``*.compensation_started``
    event during ``dispatch_once()``, the dispatcher's
    sink receives one ``incr_compensation_started`` call.

    This is the end-to-end proof that the metric counter
    is wired through ``_systems_runner.append_system_outgoing``
    -- the same hook point the DLQ writer uses. A unit
    test with a fake ``MetricsSink`` proves the
    in-memory wiring; an integration test against real
    Redis proves the production tick loop drives it.
    """
    sink = _RecordingSink()
    dispatcher, log = _wire_dispatcher(clean_redis, sink=sink)
    agent_id = "a-comp-e2e"

    compensation_event = Event.create(
        event_type="saga.e2e.step_a.compensation_started",
        agent_id=agent_id,
        event_class="domain",
        data={"step": "step_a", "saga": "fixture"},
        correlation=_ctx(),
    )

    def _emit_compensation(world):
        return [compensation_event]

    dispatcher.add_system(_emit_compensation)
    dispatcher.track_agent(agent_id)

    # Seed: a domain event so the dispatcher picks the
    # agent up on the next tick.
    seed = _seed_event(agent_id)
    await log.append(seed)

    # Drive one dispatch tick.
    await dispatcher.dispatch_once()

    # The compensation counter advanced exactly once.
    # The four Tier 4 query counters are still empty (no
    # one called the query methods).
    assert sink.compensation_calls == 1
    assert sink.in_flight_calls == []
    assert sink.stale_calls == []
    assert sink.stuck_calls == []
    assert sink.dlq_calls == []


async def test_dispatch_once_does_not_fire_counter_when_no_compensation(
    clean_redis: Any,
) -> None:
    """When the system emits a regular domain event (not
    a compensation_started), the counter is NOT touched.
    The counter fires only on ``*.compensation_started``
    events (ADR-069 §11.18.2).
    """
    sink = _RecordingSink()
    dispatcher, log = _wire_dispatcher(clean_redis, sink=sink)
    agent_id = "a-no-comp"

    regular_event = Event.create(
        event_type="saga.e2e.step_a.step_completed",
        agent_id=agent_id,
        event_class="domain",
        data={"step": "step_a"},
        correlation=_ctx(),
    )

    def _emit_regular(world):
        return [regular_event]

    dispatcher.add_system(_emit_regular)
    dispatcher.track_agent(agent_id)

    seed = _seed_event(agent_id)
    await log.append(seed)
    await dispatcher.dispatch_once()

    assert sink.compensation_calls == 0


async def test_dispatch_once_counts_each_compensation_event(
    clean_redis: Any,
) -> None:
    """A batch of N ``*.compensation_started`` events
    increments the counter N times (not 1, not 0).
    """
    sink = _RecordingSink()
    dispatcher, log = _wire_dispatcher(clean_redis, sink=sink)
    agent_id = "a-multi-comp"

    events = [
        Event.create(
            event_type=f"saga.e2e.step_{i}.compensation_started",
            agent_id=agent_id,
            event_class="domain",
            data={"step": f"step_{i}"},
            correlation=_ctx(),
        )
        for i in range(3)
    ]

    def _emit_many(world):
        return events

    dispatcher.add_system(_emit_many)
    dispatcher.track_agent(agent_id)

    seed = _seed_event(agent_id)
    await log.append(seed)
    await dispatcher.dispatch_once()

    assert sink.compensation_calls == 3


# ---------------------------------------------------------------------------
# Tier 4 query methods after a real tick
# ---------------------------------------------------------------------------


async def test_in_flight_tasks_query_records_count_through_sink(
    clean_redis: Any,
) -> None:
    """The four Tier 4 query methods push their result
    sizes through the sink. With ``_world_store`` wired
    and the tick loop having run, calling
    ``in_flight_tasks`` on the dispatcher fires
    ``record_in_flight(N)`` where N is the size of the
    returned list.
    """
    sink = _RecordingSink()
    dispatcher, log = _wire_dispatcher(clean_redis, sink=sink)
    agent_id = "a-queries"

    def _noop(world):
        return []

    dispatcher.add_system(_noop)
    dispatcher.track_agent(agent_id)

    seed = _seed_event(agent_id)
    await log.append(seed)
    await dispatcher.dispatch_once()

    # Drive the four Tier 4 queries and verify each one
    # pushes to the sink.
    await dispatcher.in_flight_tasks()
    await dispatcher.stale_tasks()
    await dispatcher.stuck_in_queue()
    await dispatcher.dead_lettered_tasks()

    # Each query was invoked exactly once; the counts are
    # 0 because the world is empty (no tool requests in
    # flight, no DLQ, no stuck queues).
    assert sink.in_flight_calls == [0]
    assert sink.stale_calls == [0]
    assert sink.stuck_calls == [0]
    assert sink.dlq_calls == [0]
    # The compensation counter was untouched -- the
    # query methods do not fire it.
    assert sink.compensation_calls == 0


# ---------------------------------------------------------------------------
# NullMetricsSink through the real dispatcher
# ---------------------------------------------------------------------------


async def test_null_metrics_sink_does_not_break_dispatch_loop(
    clean_redis: Any,
) -> None:
    """When the dispatcher is constructed without a
    ``metrics_sink`` argument, the default
    :class:`NullMetricsSink` is installed. The tick loop
    still works (the no-op methods return ``None``
    without raising).
    """
    dispatcher, log = _wire_dispatcher(clean_redis, sink=NullMetricsSink())
    agent_id = "a-null-sink"

    def _noop(world):
        return []

    dispatcher.add_system(_noop)
    dispatcher.track_agent(agent_id)

    seed = _seed_event(agent_id)
    await log.append(seed)

    # Must not raise even though the sink is a no-op.
    processed = await dispatcher.dispatch_once()
    # The tick ran (returns the number of events processed;
    # for an idle dispatcher with a freshly seeded event,
    # this is at least 1).
    assert processed >= 1


# ---------------------------------------------------------------------------
# Prometheus end-to-end
# ---------------------------------------------------------------------------


async def test_prometheus_sink_records_values_through_dispatch_once(
    clean_redis: Any,
) -> None:
    """With ``prometheus_client`` installed and
    :class:`PrometheusMetricsSink` wired, the
    ``incr_compensation_started`` counter advances in the
    sink's private registry when a saga compensation
    event flows through the dispatcher.

    Skipped when ``prometheus_client`` is not installed
    -- the framework stays importable without the extra.
    """
    pytest.importorskip("prometheus_client")

    from prometheus_client import CollectorRegistry  # type: ignore[reportUnknownVariableType]

    from kntgraph.runner.metrics.prometheus import PrometheusMetricsSink

    registry: CollectorRegistry = CollectorRegistry()
    sink = PrometheusMetricsSink(registry=registry)
    dispatcher, log = _wire_dispatcher(clean_redis, sink=sink)
    agent_id = "a-prom-e2e"

    compensation_event = Event.create(
        event_type="saga.prom_e2e.step_a.compensation_started",
        agent_id=agent_id,
        event_class="domain",
        data={"step": "step_a"},
        correlation=_ctx(),
    )

    def _emit(world):
        return [compensation_event]

    dispatcher.add_system(_emit)
    dispatcher.track_agent(agent_id)

    seed = _seed_event(agent_id)
    await log.append(seed)
    await dispatcher.dispatch_once()

    # Read the counter value directly from the registry.
    counter_value = registry.get_sample_value("knt_saga_compensations_started_total")
    assert counter_value == 1.0
