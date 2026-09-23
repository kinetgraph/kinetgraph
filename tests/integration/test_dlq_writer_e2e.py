# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
End-to-end integration tests for the DLQ writer through
the ``ReactiveDispatcher``.

The unit tests in :mod:`tests.unit.runner.test_dlq_writer`
pin the writer's contract with a fake ``DeadLetterQueue``.
These tests exercise the **full pipeline** against real
Redis (the ``clean_redis`` fixture flushes the database
before each test):

    system emits ``*.dlq`` event
        -> ``_systems_runner.append_system_outgoing``
        -> ``_dlq_writer.append_dlq_events``
        -> ``DeadLetterQueue.append``
        -> Redis ``XADD knt:dlq:events``

This is the missing coverage that closes ADR-069 §5.2's
"saga stays side-effect-free" invariant: the side effect
is observable in the Redis DLQ only because the writer
exists, and the writer is wired only inside the
dispatcher's tick loop. A unit test of the writer alone
cannot prove that the wiring is intact.

The tests deliberately avoid driving a real saga (no
``SagaSystem`` setup). The writer's contract is
event-shape-driven (suffix ``.dlq``), not saga-driven;
exercising the wiring without saga semantics keeps the
tests focused on what the writer actually does.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.events.dlq import DLQReason
from kntgraph.events.dlq.store import DeadLetterQueue
from kntgraph.infra.redis._dlq import RedisDLQStorage
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._world_checkpoint._redis import (
    RedisWorldCheckpointStorage,
)
from kntgraph.infra.world_checkpoint import IncrementalWorldStore
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> CorrelationContext:
    """Build a fresh ``CorrelationContext`` with a random flow id."""
    return CorrelationContext.new(correlation_id=uuid4())


def _wire_dispatcher(
    clean_redis: Any,
) -> tuple[ReactiveDispatcher, EventLog, DeadLetterQueue]:
    """Build a Redis-backed ``ReactiveDispatcher`` with the
    EventLog + DLQ already wired.

    Returns the dispatcher, the EventLog (so the test can
    seed events), and the DLQ (so the test can read back).
    Tests that want ``dlq=None`` (the "operator chose not
    to wire a DLQ" path) build the dispatcher directly
    instead of going through this helper.
    """
    log = EventLog(RedisEventLogAdapter(clean_redis))
    store = IncrementalWorldStore(RedisWorldCheckpointStorage(clean_redis))
    dlq = DeadLetterQueue(RedisDLQStorage(client=clean_redis))
    dispatcher = ReactiveDispatcher(
        log=log,
        world_store=store,
        dlq=dlq,
        poll_interval=0.1,
    )
    return dispatcher, log, dlq


def _seed_domain_event(
    log: EventLog,
    agent_id: str,
    event_type: str = "fixture.seed",
    data: dict[str, Any] | None = None,
) -> Event:
    """Append a domain event to the EventLog for ``agent_id``
    so the dispatcher's bootstrap discovers the agent.
    """
    event = Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class="domain",
        data=data or {},
        correlation=_ctx(),
    )
    return event


# ---------------------------------------------------------------------------
# Core: writer wiring through the real dispatcher
# ---------------------------------------------------------------------------


async def test_saga_dlq_event_lands_in_redis_dlq_through_dispatcher(
    clean_redis: Any,
) -> None:
    """The full pipeline:

        system emits ``saga.test.dlq`` event
        -> ``append_system_outgoing`` persists to EventLog
        -> ``_dlq_writer.append_dlq_events`` scans the batch
        -> ``DeadLetterQueue.append`` writes to Redis

    End-to-end proof that ADR-069 §5.2's
    "saga stays side-effect-free" invariant actually closes:
    the effect is observable in the Redis DLQ because the
    writer exists inside the dispatcher's tick loop.
    """
    dispatcher, log, dlq = _wire_dispatcher(clean_redis)
    agent_id = "a-dlq-e2e"

    # Build the saga-emitted ``*.dlq`` event shape.
    saga_dlq_event = Event.create(
        event_type="saga.test.dlq",
        agent_id=agent_id,
        event_class="domain",
        data={
            "saga_id": "saga-abc",
            "stuck_step": "step_a",
            "step_states": {"step_a": "compensating"},
        },
        correlation=_ctx(),
    )

    # System emits the dlq event in response to the seed.
    def _emit_dlq(world):
        return [saga_dlq_event]

    dispatcher.add_system(_emit_dlq)
    dispatcher.track_agent(agent_id)

    # Seed: a domain event so the dispatcher picks the
    # agent up on the next tick.
    seed = _seed_domain_event(log, agent_id, event_type="seed.received")
    await log.append(seed)

    # Drive one dispatch tick.
    await dispatcher.dispatch_once()

    # The DLQ writer pushed the event to the Redis DLQ.
    entries = await dlq.list_all(count=10)
    assert len(entries) == 1, f"expected 1 DLQ entry, got {len(entries)}: {entries!r}"
    entry = entries[0]
    # The original event is preserved on the DLQ entry.
    assert entry.event.event_id == saga_dlq_event.event_id
    assert entry.event.event_type == saga_dlq_event.event_type
    assert entry.event.agent_id == agent_id
    # Default reason (compensation failure -> PROCESSING_FAILED).
    assert entry.reason == DLQReason.PROCESSING_FAILED
    # The writer's fallback error message.
    assert entry.error_message == "compensation failed"
    # The saga's data payload is preserved as metadata.
    assert entry.metadata.get("saga_id") == "saga-abc"
    assert entry.metadata.get("stuck_step") == "step_a"


async def test_non_dlq_events_in_batch_do_not_touch_dlq(
    clean_redis: Any,
) -> None:
    """When the outgoing batch contains regular domain
    events alongside ``*.dlq`` events, only the DLQ events
    land in the Redis DLQ. The other events stay on the
    EventLog alone.
    """
    dispatcher, log, dlq = _wire_dispatcher(clean_redis)
    agent_id = "a-mixed"

    saga_dlq_event = Event.create(
        event_type="saga.mixed.dlq",
        agent_id=agent_id,
        event_class="domain",
        data={"saga_id": "x"},
        correlation=_ctx(),
    )
    non_dlq_event = Event.create(
        event_type="saga.mixed.step_completed",
        agent_id=agent_id,
        event_class="domain",
        data={"step": "a"},
        correlation=_ctx(),
    )

    def _emit_mixed(world):
        return [saga_dlq_event, non_dlq_event]

    dispatcher.add_system(_emit_mixed)
    dispatcher.track_agent(agent_id)

    seed = _seed_domain_event(log, agent_id)
    await log.append(seed)
    await dispatcher.dispatch_once()

    # Only the *.dlq event lands in the DLQ.
    entries = await dlq.list_all(count=10)
    assert len(entries) == 1
    assert entries[0].event.event_id == saga_dlq_event.event_id

    # Both events are in the EventLog (the writer does NOT
    # remove them from the log; the log is the source of
    # truth).
    all_events = await log.read(agent_id)
    event_ids = {e.event_id for e in all_events}
    assert saga_dlq_event.event_id in event_ids
    assert non_dlq_event.event_id in event_ids


async def test_explicit_reason_in_event_data_overrides_default(
    clean_redis: Any,
) -> None:
    """When the ``*.dlq`` event carries an explicit
    ``reason`` in its data payload, the writer routes the
    entry to that bucket instead of the default
    ``PROCESSING_FAILED``.
    """
    dispatcher, log, dlq = _wire_dispatcher(clean_redis)
    agent_id = "a-reason"

    timed_out_event = Event.create(
        event_type="saga.timeout_explicit.dlq",
        agent_id=agent_id,
        event_class="domain",
        data={
            "saga_id": "x",
            "reason": DLQReason.TIMEOUT.value,
            "error": "step_b timed out after 5s",
        },
        correlation=_ctx(),
    )

    def _emit(world):
        return [timed_out_event]

    dispatcher.add_system(_emit)
    dispatcher.track_agent(agent_id)

    seed = _seed_domain_event(log, agent_id)
    await log.append(seed)
    await dispatcher.dispatch_once()

    entries = await dlq.list_all(count=10)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.reason == DLQReason.TIMEOUT
    assert entry.error_message == "step_b timed out after 5s"


async def test_dispatcher_without_dlq_keeps_event_on_eventlog(
    clean_redis: Any,
) -> None:
    """When the dispatcher is constructed without a DLQ
    (``dlq=None``), the writer short-circuits. The saga's
    ``*.dlq`` event lands in the EventLog but NOT in any
    DLQ storage. The saga stays side-effect-free
    (ADR-069 §5.2) -- the operator chose not to wire a
    DLQ.
    """
    log = EventLog(RedisEventLogAdapter(clean_redis))
    store = IncrementalWorldStore(RedisWorldCheckpointStorage(clean_redis))
    dispatcher = ReactiveDispatcher(
        log=log,
        world_store=store,
        dlq=None,  # explicit opt-out
        poll_interval=0.1,
    )

    agent_id = "a-no-dlq"
    saga_dlq_event = Event.create(
        event_type="saga.no_dlq.dlq",
        agent_id=agent_id,
        event_class="domain",
        data={"saga_id": "x"},
        correlation=_ctx(),
    )

    def _emit(world):
        return [saga_dlq_event]

    dispatcher.add_system(_emit)
    dispatcher.track_agent(agent_id)

    seed = _seed_domain_event(log, agent_id)
    await log.append(seed)

    # Must not raise even though there is no DLQ wired.
    await dispatcher.dispatch_once()

    # The event IS in the EventLog (durability preserved).
    all_events = await log.read(agent_id)
    assert any(e.event_id == saga_dlq_event.event_id for e in all_events)

    # But the Redis DLQ stream is untouched.
    from kntgraph.events.dlq.values import DLQ_STREAM_KEY

    stream_len = await clean_redis.xlen(DLQ_STREAM_KEY)
    assert stream_len == 0


async def test_idempotent_rerun_of_same_batch_does_not_double_write(
    clean_redis: Any,
) -> None:
    """The DLQ storage is idempotent on
    ``<event_id>:<reason>``; the writer inherits that
    property. Re-running the writer on the same
    ``*.dlq`` event (e.g. a manual replay) does not
    create a duplicate entry.

    This test simulates a "dispatcher restart that
    replays the same batch" by directly invoking
    ``append_dlq_events`` twice with the same event --
    the same code path ``append_system_outgoing`` uses.
    """
    from kntgraph.runner._dlq_writer import append_dlq_events

    dispatcher, log, dlq = _wire_dispatcher(clean_redis)

    agent_id = "a-idem"
    dlq_event = Event.create(
        event_type="saga.idempotent.dlq",
        agent_id=agent_id,
        event_class="domain",
        data={"saga_id": "x"},
        correlation=_ctx(),
    )

    # First writer call (simulates the dispatcher's tick).
    first = await append_dlq_events([dlq_event], dispatcher._dlq)
    # Second writer call (simulates a replay).
    second = await append_dlq_events([dlq_event], dispatcher._dlq)

    # First call wrote the entry; the writer returns the
    # stream_id assigned by the storage.
    assert len(first) == 1
    # Second call was a dedup hit; the writer filters the
    # PLACEHOLDER out of the return list.
    assert second == []

    # The DLQ stream has exactly ONE entry -- the dedup
    # hit did not create a duplicate.
    entries = await dlq.list_all(count=10)
    assert len(entries) == 1
    assert entries[0].event.event_id == dlq_event.event_id


async def test_unknown_reason_in_event_data_falls_back_to_default(
    clean_redis: Any,
) -> None:
    """An unrecognised ``reason`` value is treated as a
    forward-compat signal (the emitter may be a newer
    vertical), not a bug. The writer falls back to the
    default reason rather than crashing the dispatch loop.
    """
    dispatcher, log, dlq = _wire_dispatcher(clean_redis)
    agent_id = "a-unknown-reason"

    future_event = Event.create(
        event_type="saga.future.dlq",
        agent_id=agent_id,
        event_class="domain",
        data={
            "saga_id": "x",
            "reason": "reason_from_future_v3",
        },
        correlation=_ctx(),
    )

    def _emit(world):
        return [future_event]

    dispatcher.add_system(_emit)
    dispatcher.track_agent(agent_id)

    seed = _seed_domain_event(log, agent_id)
    await log.append(seed)
    await dispatcher.dispatch_once()

    entries = await dlq.list_all(count=10)
    assert len(entries) == 1
    # Fallback to PROCESSING_FAILED -- the writer does
    # not crash on unknown reasons.
    assert entries[0].reason == DLQReason.PROCESSING_FAILED
