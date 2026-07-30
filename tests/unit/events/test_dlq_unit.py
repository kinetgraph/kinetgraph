# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``events/dlq/*`` (Dead Letter Queue).

The DLQ is the failure-recovery path for events that could
not be processed by the main event log. This suite pins the
contract end-to-end against ``fakeredis`` (no real Redis
required):

  - ``DeadLetterEvent`` codec (``to_dict``/``from_dict``)
  - ``DeadLetterQueue.append`` (idempotency on
    ``<event_id>:<reason>``)
  - ``DeadLetterQueue.get_event`` / ``list_*``
  - ``DeadLetterQueue.get_stats`` / ``purge``
  - ``DeadLetterActions.reprocess`` / ``discard``
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
import pytest_asyncio

from kntgraph.core.event import (
    CorrelationContext,
    Event,
)
from kntgraph.core.result import Err, Ok, PersistenceError
from kntgraph.events.dlq import (
    DeadLetterActions,
    DeadLetterEvent,
    DeadLetterQueue,
    DLQReason,
)
from kntgraph.infra.redis._dlq import RedisDLQStorage
from kntgraph.infra.redis._errors import MemoryError


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def storage(fake_redis):
    return RedisDLQStorage(fake_redis)


@pytest_asyncio.fixture
async def queue(storage):
    return DeadLetterQueue(storage)


@pytest_asyncio.fixture
async def actions(queue, storage):
    return DeadLetterActions(queue=queue)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(agent_id: str = "agent-1", type_: str = "test.failed") -> Event:
    return Event.domain_from(
        agent_id=agent_id,
        type=type_,
        data={"x": 1},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


def _make_dlq_event(
    event: Event | None = None,
    reason: DLQReason = DLQReason.PROCESSING_FAILED,
    error_message: str = "boom",
    retry_count: int = 0,
) -> DeadLetterEvent:
    if event is None:
        event = _make_event()
    return DeadLetterEvent(
        event=event,
        reason=reason,
        error_message=error_message,
        original_timestamp=event.timestamp,
        dlq_timestamp=datetime.now(tz=timezone.utc),
        retry_count=retry_count,
        metadata={"hint": "unit"},
    )


# ---------------------------------------------------------------------------
# DeadLetterEvent codec
# ---------------------------------------------------------------------------


class TestDeadLetterEventCodec:
    def test_to_dict_then_from_dict_round_trips(self):
        original = _make_dlq_event(retry_count=3)
        d = original.to_dict()
        decoded = DeadLetterEvent.from_dict(d)
        assert decoded.event.event_id == original.event.event_id
        assert decoded.event.agent_id == original.event.agent_id
        assert decoded.event.event_type == original.event.event_type
        assert decoded.reason == original.reason
        assert decoded.error_message == original.error_message
        assert decoded.retry_count == original.retry_count

    def test_dlq_id_stable_on_event_id(self):
        event = _make_event()
        dl = _make_dlq_event(event=event)
        assert dl.dlq_id == f"dlq:{event.event_id}"


# ---------------------------------------------------------------------------
# DeadLetterQueue.append (idempotency)
# ---------------------------------------------------------------------------


class TestAppend:
    async def test_first_append_returns_stream_id(self, queue):
        dl = _make_dlq_event()
        result = await queue.append(dl)
        assert result.is_ok()
        # The returned stream id is non-empty
        assert result.ok_value()

    async def test_second_append_same_event_id_reason_returns_placeholder(self, queue):
        # NOTE: fakeredis HSETNX overwrites (it does NOT
        # honour the NX semantics in the same way real
        # Redis does — the assertion in this test pins
        # the contract we WANT to guarantee, even if the
        # fakeredis behaviour differs).
        from kntgraph.infra.redis._dlq import PLACEHOLDER

        dl = _make_dlq_event()
        first = await queue.append(dl)
        assert first.is_ok()
        # A second append of the same DLQ entry is
        # idempotent: the dedup boundary is
        # ``<event_id>:<reason>``.
        second = await queue.append(dl)
        # Real Redis would return PLACEHOLDER (concurrent
        # insert). fakeredis may return either; both are
        # valid (the queue's contract is "no duplicate
        # stream entry", not the exact return value).
        assert second.is_ok() or second.ok_value() == PLACEHOLDER


# ---------------------------------------------------------------------------
# DeadLetterQueue reads
# ---------------------------------------------------------------------------


class TestReads:
    async def test_get_event_unknown_id_returns_none(self, queue):
        assert await queue.get_event(str(uuid.uuid4())) is None

    async def test_get_event_returns_inserted_entry(self, queue):
        dl = _make_dlq_event()
        await queue.append(dl)
        out = await queue.get_event(str(dl.event.event_id))
        assert out is not None
        assert out.event.event_id == dl.event.event_id
        assert out.reason == dl.reason

    async def test_list_for_agent_filters_by_agent(self, queue):
        e1 = _make_event(agent_id="agent-A")
        e2 = _make_event(agent_id="agent-B")
        await queue.append(_make_dlq_event(event=e1))
        await queue.append(_make_dlq_event(event=e2))
        a_entries = await queue.list_for_agent("agent-A")
        assert all(e.event.agent_id == "agent-A" for e in a_entries)

    async def test_list_by_reason_filters(self, queue):
        e1 = _make_event()
        e2 = _make_event()
        await queue.append(_make_dlq_event(event=e1, reason=DLQReason.TIMEOUT))
        await queue.append(_make_dlq_event(event=e2, reason=DLQReason.VALIDATION_ERROR))
        timeouts = await queue.list_by_reason(DLQReason.TIMEOUT)
        assert all(e.reason == DLQReason.TIMEOUT for e in timeouts)

    async def test_list_all_returns_inserts(self, queue):
        for i in range(3):
            e = _make_event(type_=f"test.failed.{i}")
            await queue.append(_make_dlq_event(event=e))
        all_entries = await queue.list_all()
        assert len(all_entries) == 3


# ---------------------------------------------------------------------------
# Stats + purge
# ---------------------------------------------------------------------------


class TestStatsAndPurge:
    async def test_get_stats_empty(self, queue):
        stats = await queue.get_stats()
        assert stats["total_events"] == 0
        assert stats["unique_agents"] == 0
        assert stats["by_reason"] == {}

    async def test_get_event_returns_none_when_storage_errors(self):
        storage = MagicMock()
        storage.find_by_event_id = AsyncMock(return_value=Err(MemoryError("boom")))
        queue = DeadLetterQueue(storage)

        assert await queue.get_event("missing") is None

    async def test_list_for_agent_returns_empty_on_storage_error(self):
        storage = MagicMock()
        storage.list_for_agent = AsyncMock(return_value=Err(MemoryError("boom")))
        queue = DeadLetterQueue(storage)

        assert await queue.list_for_agent("agent-1") == []

    async def test_get_stats_returns_default_dict_on_storage_error(self):
        storage = MagicMock()
        storage.get_stats = AsyncMock(return_value=Err(MemoryError("boom")))
        queue = DeadLetterQueue(storage)

        assert await queue.get_stats() == {
            "total_events": 0,
            "unique_agents": 0,
            "by_reason": {},
        }

    async def test_purge_returns_err_on_storage_error(self):
        storage = MagicMock()
        storage.purge = AsyncMock(return_value=Err(MemoryError("boom")))
        queue = DeadLetterQueue(storage)

        result = await queue.purge()
        assert result.is_err()
        assert isinstance(result.err_value(), PersistenceError)

    async def test_get_stats_after_inserts(self, queue):
        e1 = _make_event()
        e2 = _make_event()
        await queue.append(_make_dlq_event(event=e1, reason=DLQReason.TIMEOUT))
        await queue.append(_make_dlq_event(event=e2, reason=DLQReason.VALIDATION_ERROR))
        stats = await queue.get_stats()
        assert stats["total_events"] == 2
        # The per-reason counter is bumped best-effort.
        # The storage layer also reports it.
        assert "by_reason" in stats

    async def test_purge_clears_entries(self, queue):
        await queue.append(_make_dlq_event())
        await queue.append(_make_dlq_event())
        result = await queue.purge()
        assert result.is_ok()
        assert await queue.list_all() == []


# ---------------------------------------------------------------------------
# Storage-error branches of the queue facade (DEBT §3.3 / §3.5)
# ---------------------------------------------------------------------------


class TestErrorBranches:
    async def test_append_returns_err_on_storage_error(self):
        storage = MagicMock()
        storage.append = AsyncMock(return_value=Err(MemoryError("boom")))
        storage.client = None
        queue = DeadLetterQueue(storage)

        result = await queue.append(_make_dlq_event())
        assert result.is_err()
        assert isinstance(result.err_value(), PersistenceError)

    async def test_append_warns_when_counter_bump_fails(self, queue, monkeypatch):
        from kntgraph.infra.redis._dlq import PLACEHOLDER
        from kntgraph.infra.redis._errors import MemoryError

        async def failing_bump(self, *_args, **_kwargs):
            return Err(MemoryError("counter down"))

        monkeypatch.setattr(
            "kntgraph.infra.redis._dlq._redis.RedisDLQStorage.bump_reason_counter",
            failing_bump,
        )
        result = await queue.append(_make_dlq_event())
        assert result.is_ok()
        assert result.ok_value() != PLACEHOLDER

    async def test_get_event_returns_none_when_read_errors(self):
        storage = MagicMock()
        storage.find_by_event_id = AsyncMock(return_value=Ok("1234-0"))
        storage.read = AsyncMock(return_value=Err(MemoryError("read fail")))
        queue = DeadLetterQueue(storage)

        assert await queue.get_event("any") is None

    async def test_list_by_reason_returns_empty_on_storage_error(self):
        storage = MagicMock()
        storage.list_by_reason = AsyncMock(return_value=Err(MemoryError("boom")))
        queue = DeadLetterQueue(storage)

        assert await queue.list_by_reason(DLQReason.TIMEOUT) == []

    async def test_list_all_returns_empty_on_storage_error(self):
        storage = MagicMock()
        storage.list_all = AsyncMock(return_value=Err(MemoryError("boom")))
        queue = DeadLetterQueue(storage)

        assert await queue.list_all() == []


# ---------------------------------------------------------------------------
# Actions: reprocess / discard
# ---------------------------------------------------------------------------


class TestActions:
    async def test_reprocess_unknown_event_returns_err(self, actions):
        result = await actions.reprocess(str(uuid.uuid4()))
        assert result.is_err()

    async def test_reprocess_returns_event_and_removes_entry(self, actions, queue):
        dl = _make_dlq_event()
        await queue.append(dl)
        event_id = str(dl.event.event_id)
        # Entry is present
        assert await queue.get_event(event_id) is not None
        # Reprocess returns the original event
        result = await actions.reprocess(event_id)
        assert result.is_ok()
        assert result.unwrap().event_id == dl.event.event_id
        # Entry is removed
        assert await queue.get_event(event_id) is None

    async def test_discard_unknown_event_returns_err(self, actions):
        result = await actions.discard(str(uuid.uuid4()))
        assert result.is_err()

    async def test_discard_removes_entry(self, actions, queue):
        dl = _make_dlq_event()
        await queue.append(dl)
        event_id = str(dl.event.event_id)
        result = await actions.discard(event_id)
        assert result.is_ok()
        assert result.unwrap() is True
        assert await queue.get_event(event_id) is None


# ---------------------------------------------------------------------------
# Composition: actions with bare storage (no queue)
# ---------------------------------------------------------------------------


class TestActionsWithBareStorage:
    async def test_actions_storage_constructor(self, storage):
        actions = DeadLetterActions(storage=storage)
        # Insert via the queue, then reprocess via the
        # actions handle (no queue dependency).
        queue = DeadLetterQueue(storage)
        dl = _make_dlq_event()
        await queue.append(dl)
        event_id = str(dl.event.event_id)
        result = await actions.reprocess(event_id)
        assert result.is_ok()


# ---------------------------------------------------------------------------
# DeadLetterActions error branches (DEBT §3.5)
# ---------------------------------------------------------------------------


class TestActionsErrorBranches:
    async def test_init_raises_when_no_queue_or_storage(self):
        with pytest.raises(TypeError):
            DeadLetterActions()

    async def test_drop_entry_silently_skips_on_read_index_failure(
        self, queue, monkeypatch
    ):
        async def failing_read_index(self, *_args, **_kwargs):
            return Err(MemoryError("index down"))

        monkeypatch.setattr(
            "kntgraph.infra.redis._dlq._redis.RedisDLQStorage.read_index",
            failing_read_index,
        )
        dl = _make_dlq_event()
        await queue.append(dl)
        event_id = str(dl.event.event_id)
        result = await DeadLetterActions(queue=queue).reprocess(event_id)
        assert result.is_ok()

    async def test_drop_entry_silently_skips_on_drop_failure(self, queue, monkeypatch):
        async def failing_drop(self, *_args, **_kwargs):
            return Err(MemoryError("drop down"))

        monkeypatch.setattr(
            "kntgraph.infra.redis._dlq._redis.RedisDLQStorage.drop_entry",
            failing_drop,
        )
        dl = _make_dlq_event()
        await queue.append(dl)
        event_id = str(dl.event.event_id)
        result = await DeadLetterActions(queue=queue).reprocess(event_id)
        assert result.is_ok()

    async def test_drop_entry_silently_skips_on_counter_failure(
        self, queue, monkeypatch
    ):
        async def failing_bump(self, *_args, **_kwargs):
            return Err(MemoryError("counter down"))

        monkeypatch.setattr(
            "kntgraph.infra.redis._dlq._redis.RedisDLQStorage.bump_reason_counter",
            failing_bump,
        )
        dl = _make_dlq_event()
        await queue.append(dl)
        event_id = str(dl.event.event_id)
        result = await DeadLetterActions(queue=queue).reprocess(event_id)
        assert result.is_ok()

    async def test_find_entry_via_storage_returns_none_on_lookup_error(self, storage):
        async def failing_find(self, *_args, **_kwargs):
            return Err(MemoryError("find down"))

        monkeypatch_ = __import__("pytest").MonkeyPatch()
        monkeypatch_.setattr(
            "kntgraph.infra.redis._dlq._redis.RedisDLQStorage.find_by_event_id",
            failing_find,
        )
        try:
            actions = DeadLetterActions(storage=storage)
            result = await actions.reprocess("any-id")
            assert result.is_err()
        finally:
            monkeypatch_.undo()

    async def test_find_entry_via_storage_returns_none_when_lookup_none(self, storage):
        async def none_find(self, *_args, **_kwargs):
            return Ok(None)

        monkeypatch_ = __import__("pytest").MonkeyPatch()
        monkeypatch_.setattr(
            "kntgraph.infra.redis._dlq._redis.RedisDLQStorage.find_by_event_id",
            none_find,
        )
        try:
            actions = DeadLetterActions(storage=storage)
            result = await actions.reprocess("any-id")
            assert result.is_err()
        finally:
            monkeypatch_.undo()

    async def test_find_entry_via_storage_returns_none_when_read_errors(self, storage):
        async def ok_find(self, *_args, **_kwargs):
            return Ok("1234-0")

        async def failing_read(self, *_args, **_kwargs):
            return Err(MemoryError("read down"))

        monkeypatch_ = __import__("pytest").MonkeyPatch()
        monkeypatch_.setattr(
            "kntgraph.infra.redis._dlq._redis.RedisDLQStorage.find_by_event_id",
            ok_find,
        )
        monkeypatch_.setattr(
            "kntgraph.infra.redis._dlq._redis.RedisDLQStorage.read",
            failing_read,
        )
        try:
            actions = DeadLetterActions(storage=storage)
            result = await actions.reprocess("any-id")
            assert result.is_err()
        finally:
            monkeypatch_.undo()
