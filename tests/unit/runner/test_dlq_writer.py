# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Behaviour tests for ``kntgraph.runner._dlq_writer``.

The writer is the bridge between ``*.dlq`` events on the
EventLog and the :class:`DeadLetterQueue` (ADR-069 §5.2).
The storage layer is already idempotent on
``<event_id>:<reason>``; these tests pin the writer's
contract on top of that:

  - ``*.dlq`` events are converted and pushed;
  - non-``*.dlq`` events are ignored;
  - idempotent re-runs do not double-write;
  - a missing DLQ (``dlq=None``) is a no-op;
  - storage errors are logged but do not abort the loop;
  - the ``reason`` from the event payload overrides the
    default.

A fake ``DeadLetterQueue`` records every ``append`` call
so the test asserts on the wire-format shape rather than
mocking out the storage layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.result import Err, Ok, PersistenceError
from kntgraph.events.dlq import DLQReason, DeadLetterEvent
from kntgraph.events.dlq.store import DeadLetterQueue
from kntgraph.runner._dlq_writer import (
    DEFAULT_DLQ_REASON,
    DLQ_EVENT_SUFFIX,
    PLACEHOLDER,
    append_dlq_events,
)


# All tests in this module are async; the global mark
# avoids per-test decorator noise (the project's
# ``asyncio_mode = "strict"`` rejects implicit marks).
pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _AppendRecord:
    """One call to :meth:`DeadLetterQueue.append`."""

    dl_event: DeadLetterEvent
    event_id: str
    reason: str
    error_message: str


@dataclass
class _FakeDeadLetterQueue:
    """A minimal :class:`DeadLetterQueue` stand-in.

    Records every ``append`` call so the test can assert
    on the wire-format shape. Implements ``get_event``
    so the writer's pre-check (added in the same iteration)
    can detect dedup hits without round-tripping through
    Redis.
    """

    records: list[_AppendRecord] = field(default_factory=list)
    # Maps ``<event_id>`` → ``_AppendRecord`` so
    # ``get_event`` returns the first record for an
    # event_id (matching the real ``DeadLetterQueue``
    # behaviour, which scans ``<event_id>:*`` and
    # returns the first match).
    by_event_id: dict[str, _AppendRecord] = field(default_factory=dict)
    # Set to True to make append fail with a
    # PersistenceError -- the writer must catch and
    # continue.
    fail_next: bool = False

    def _idem_key(self, event_id: str, reason: str) -> str:
        return f"{event_id}:{reason}"

    async def get_event(self, event_id: str) -> "DeadLetterEvent | None":
        """Return the first DLQ entry for ``event_id``,
        or ``None`` if no entry exists. Mirrors the real
        ``DeadLetterQueue.get_event`` contract.
        """
        record = self.by_event_id.get(event_id)
        return record.dl_event if record is not None else None

    async def append(
        self, dl_event: DeadLetterEvent
    ) -> "Any":  # Result[str, PersistenceError]
        if self.fail_next:
            self.fail_next = False
            return Err(PersistenceError("storage unavailable"))
        record = _AppendRecord(
            dl_event=dl_event,
            event_id=str(dl_event.event.event_id),
            reason=dl_event.reason.value,
            error_message=dl_event.error_message,
        )
        self.records.append(record)
        # First-write-wins for the by_event_id index
        # (the real storage does the same on dedup).
        self.by_event_id.setdefault(str(dl_event.event.event_id), record)
        return Ok(f"stream-{len(self.records)}")


def _dlq_event(
    agent_id: str = "agent-1",
    saga_name: str = "fixture",
    data: dict | None = None,
) -> Event:
    """Build a saga-emitted ``saga.<name>.dlq`` event."""
    return Event.create(
        event_type=f"saga.{saga_name}.dlq",
        agent_id=agent_id,
        event_class="domain",
        data=data
        or {
            "saga_id": "saga-abc",
            "stuck_step": "step_b",
            "step_states": {"step_a": "compensated"},
        },
        correlation=CorrelationContext.new(correlation_id=uuid4()),
    )


def _non_dlq_event(agent_id: str = "agent-1") -> Event:
    """A regular domain event that should NOT reach the DLQ."""
    return Event.create(
        event_type="saga.fixture.step_completed",
        agent_id=agent_id,
        event_class="domain",
        data={"step": "step_a"},
        correlation=CorrelationContext.new(correlation_id=uuid4()),
    )


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


async def test_constants_match_documented_shape() -> None:
    """``DLQ_EVENT_SUFFIX`` and ``DEFAULT_DLQ_REASON`` are
    the documented public values; tests on other modules
    depend on these being stable.

    Marked ``async`` so the file uses a single
    ``@pytest.mark.asyncio`` decorator form; the test body
    does not need an event loop but is degenerate enough
    that the consistency win outweighs the noise.
    """
    assert DLQ_EVENT_SUFFIX == ".dlq"
    assert DEFAULT_DLQ_REASON == DLQReason.PROCESSING_FAILED
    assert PLACEHOLDER == "PLACEHOLDER"


# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------


async def test_dlq_none_short_circuits() -> None:
    """When ``dlq`` is ``None``, the writer is a no-op.
    The saga stays side-effect-free (ADR-069 §5.2): the
    operator has not wired a DLQ, so the typed event
    stays on the EventLog alone.
    """
    outgoing = [_dlq_event()]
    result = await append_dlq_events(outgoing, dlq=None)
    assert result == []


async def test_empty_outgoing_returns_empty_list() -> None:
    """No events, no DLQ writes."""
    fake = _FakeDeadLetterQueue()
    result = await append_dlq_events([], fake)  # type: ignore[arg-type]
    assert result == []
    assert fake.records == []


async def test_non_dlq_events_are_ignored() -> None:
    """Events whose type does NOT end with ``.dlq`` are
    left alone by the writer.
    """
    fake = _FakeDeadLetterQueue()
    outgoing = [_non_dlq_event() for _ in range(3)]
    result = await append_dlq_events(outgoing, fake)  # type: ignore[arg-type]
    assert result == []
    assert fake.records == []


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_dlq_event_is_pushed_to_queue() -> None:
    """A single ``*.dlq`` event in the outgoing batch
    triggers exactly one :meth:`DeadLetterQueue.append`
    call with a properly built :class:`DeadLetterEvent`.
    """
    fake = _FakeDeadLetterQueue()
    event = _dlq_event()
    result = await append_dlq_events([event], fake)  # type: ignore[arg-type]
    assert result == ["stream-1"]
    assert len(fake.records) == 1
    rec = fake.records[0]
    assert rec.event_id == str(event.event_id)
    # Default reason: compensation failure → PROCESSING_FAILED.
    assert rec.reason == DLQReason.PROCESSING_FAILED.value
    # Error message fallback ("compensation failed") is
    # used because the saga's dlq event has no ``error``
    # key in its data payload.
    assert rec.error_message == "compensation failed"
    # The original Event is preserved on the DLQ entry.
    assert rec.dl_event.event.event_id == event.event_id
    assert rec.dl_event.event.event_type == event.event_type


async def test_mixed_batch_pushes_only_dlq_events() -> None:
    """In a batch of N events with K ``*.dlq`` events, the
    writer pushes exactly K entries (in batch order).
    """
    fake = _FakeDeadLetterQueue()
    dlq_a = _dlq_event(saga_name="alpha")
    non_dlq = _non_dlq_event()
    dlq_b = _dlq_event(saga_name="beta")
    non_dlq_2 = _non_dlq_event()
    outgoing = [dlq_a, non_dlq, dlq_b, non_dlq_2]

    result = await append_dlq_events(outgoing, fake)  # type: ignore[arg-type]
    assert result == ["stream-1", "stream-2"]
    assert [r.event_id for r in fake.records] == [
        str(dlq_a.event_id),
        str(dlq_b.event_id),
    ]


# ---------------------------------------------------------------------------
# Reason resolution
# ---------------------------------------------------------------------------


async def test_reason_from_data_overrides_default() -> None:
    """When the event's data payload has a recognised
    ``reason`` key, the writer uses it instead of the
    default. This lets verticals with richer failure
    vocabularies route to specific buckets.
    """
    fake = _FakeDeadLetterQueue()
    event = _dlq_event(
        data={
            "saga_id": "x",
            "reason": DLQReason.TIMEOUT.value,
            "error": "tool timed out after 5s",
        }
    )
    result = await append_dlq_events([event], fake)  # type: ignore[arg-type]
    assert result == ["stream-1"]
    assert fake.records[0].reason == DLQReason.TIMEOUT.value
    assert fake.records[0].error_message == "tool timed out after 5s"


async def test_unrecognised_reason_falls_back_to_default() -> None:
    """An unrecognised ``reason`` value is a forward-compat
    signal (the event's emitter may be a newer vertical),
    not a bug in the writer. The writer falls back to the
    default rather than crashing the loop.
    """
    fake = _FakeDeadLetterQueue()
    event = _dlq_event(data={"reason": "unknown_thing_from_future"})
    result = await append_dlq_events([event], fake)  # type: ignore[arg-type]
    assert result == ["stream-1"]
    assert fake.records[0].reason == DLQReason.PROCESSING_FAILED.value


async def test_explicit_default_reason_parameter_is_used() -> None:
    """The caller can pass a non-default ``default_reason``
    to override the writer-level default for a whole
    batch.
    """
    fake = _FakeDeadLetterQueue()
    event = _dlq_event()  # no ``reason`` in data
    result = await append_dlq_events(
        [event],
        fake,  # type: ignore[arg-type]
        default_reason=DLQReason.POISON_PILL,
    )
    assert result == ["stream-1"]
    assert fake.records[0].reason == DLQReason.POISON_PILL.value


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_idempotent_rerun_returns_empty_stream_ids() -> None:
    """Re-running the writer on the same ``*.dlq`` event
    is a no-op at the stream-id level. The fake simulates
    the storage's idempotency boundary by returning
    ``PLACEHOLDER`` for the second call; the writer
    filters it out of the returned ``stream_ids`` list.
    """
    fake = _FakeDeadLetterQueue()
    event = _dlq_event()

    first = await append_dlq_events([event], fake)  # type: ignore[arg-type]
    second = await append_dlq_events([event], fake)  # type: ignore[arg-type]

    # First call wrote the entry.
    assert first == ["stream-1"]
    assert len(fake.records) == 1
    # Second call was a dedup hit; the writer must NOT
    # surface the PLACEHOLDER in the returned list (the
    # caller does not care about "wrote vs already
    # there" for accounting).
    assert second == []
    # The fake's ``records`` list still only has one
    # entry -- the dedup hit did not double-write.
    assert len(fake.records) == 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def test_storage_error_does_not_abort_loop() -> None:
    """A storage error on event ``N`` is logged but does
    NOT block events ``N+1...``. The DLQ is
    best-effort; the EventLog is the source of truth and
    a future replay will retry the append.
    """
    fake = _FakeDeadLetterQueue()
    # The first event will fail; subsequent events
    # succeed.
    failing = _dlq_event(saga_name="failing")
    succeeding_a = _dlq_event(saga_name="ok_a")
    succeeding_b = _dlq_event(saga_name="ok_b")
    fake.fail_next = True

    outgoing = [failing, succeeding_a, succeeding_b]
    result = await append_dlq_events(outgoing, fake)  # type: ignore[arg-type]

    # The failing event was skipped; the other two
    # landed in the queue.
    assert result == ["stream-1", "stream-2"]
    assert len(fake.records) == 2
    assert fake.records[0].event_id == str(succeeding_a.event_id)
    assert fake.records[1].event_id == str(succeeding_b.event_id)


# ---------------------------------------------------------------------------
# Integration with the dispatcher's outgoing batch
# ---------------------------------------------------------------------------


async def test_dlq_writer_runs_from_append_system_outgoing() -> None:
    """The hook point in
    :func:`append_system_outgoing` calls the DLQ writer
    after the EventLog append. A ``saga.<name>.dlq``
    event emitted by a system lands in the DLQ on the
    same tick.
    """
    from kntgraph.core.world import World
    from kntgraph.runner._systems_runner import append_system_outgoing

    fake_dlq = _FakeDeadLetterQueue()

    @dataclass
    class _EventLog:
        appended: list[Event] = field(default_factory=list)

        async def append_batch(self, events: list[Event]) -> Any:
            self.appended.extend(events)
            return ["ok"] * len(events)

    @dataclass
    class _WorldStore:
        async def load(self, agent_id: str) -> Any:  # pragma: no cover
            from kntgraph.infra.world_checkpoint import WorldCheckpoint

            return WorldCheckpoint(world=World.empty(), last_stream_id="-")

        async def save(self, agent_id: str, checkpoint: Any) -> None:
            return None

    class _EmitDlqSystem:
        def __init__(self, *events: Event) -> None:
            self._events = list(events)

        def __call__(self, world: World) -> list[Event]:
            return list(self._events)

    @dataclass
    class _Dispatcher:
        # Bare-minimum surface for ``append_system_outgoing``.
        # ``_dlq`` is the only field the DLQ writer reads;
        # the rest are required by ``append_system_outgoing``
        # for the EventLog + cursor advancement paths.
        _log: Any
        _dlq: Optional[DeadLetterQueue]
        _systems: list
        _tool_router: Optional[Any] = None
        _metrics_sink: Any = None
        _tick_runners: set[tuple[str, str]] = field(default_factory=set)

    log = _EventLog()
    dispatcher = _Dispatcher(
        _log=log,
        _dlq=fake_dlq,
        _systems=[],
    )
    dlq_event = _dlq_event(saga_name="integration")
    dispatcher._systems = [_EmitDlqSystem(dlq_event)]

    await append_system_outgoing(
        dispatcher,  # type: ignore[arg-type]
        world=World.empty(),
        agent_id="agent-int",
        return_events=False,
    )

    # The event landed in the EventLog first (durability).
    assert len(log.appended) == 1
    # Then the writer pushed it to the DLQ.
    assert len(fake_dlq.records) == 1
    assert fake_dlq.records[0].event_id == str(dlq_event.event_id)


async def test_dlq_writer_skipped_when_dispatcher_has_no_dlq() -> None:
    """When the dispatcher was constructed without a DLQ
    (``_dlq=None``), the writer short-circuits and the
    saga's ``*.dlq`` event lands in the EventLog alone.
    The saga stays side-effect-free (ADR-069 §5.2).
    """
    from kntgraph.core.world import World
    from kntgraph.runner._systems_runner import append_system_outgoing

    @dataclass
    class _EventLog:
        appended: list[Event] = field(default_factory=list)

        async def append_batch(self, events: list[Event]) -> Any:
            self.appended.extend(events)
            return ["ok"] * len(events)

    class _EmitDlqSystem:
        def __call__(self, world: World) -> list[Event]:
            return [_dlq_event()]

    @dataclass
    class _Dispatcher:
        _log: Any
        _dlq: Optional[DeadLetterQueue]
        _systems: list
        _tool_router: Optional[Any] = None
        _metrics_sink: Any = None
        _tick_runners: set[tuple[str, str]] = field(default_factory=set)

    log = _EventLog()
    dispatcher = _Dispatcher(
        _log=log,
        _dlq=None,
        _systems=[_EmitDlqSystem()],
    )

    # Must not raise even though there is no DLQ wired.
    await append_system_outgoing(
        dispatcher,  # type: ignore[arg-type]
        world=World.empty(),
        agent_id="agent-no-dlq",
        return_events=False,
    )
    # The event is in the log; nothing was lost.
    assert len(log.appended) == 1
    assert log.appended[0].event_type.endswith(".dlq")
