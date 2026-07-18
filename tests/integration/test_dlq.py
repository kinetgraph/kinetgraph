# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for the DeadLetterQueue (real Redis).

The DLQ parks events that a system could not process. Tests cover:
  - Append with various DLQReason values
  - Idempotency on (event_id, reason)
  - Read paths (by event_id, by agent, by reason, all)
  - Reprocess (re-emit to caller)
  - Discard (poison pill)
  - Stats
  - Purge
"""

from __future__ import annotations
from kntgraph.infra.redis._event_log import RedisEventLogAdapter

import uuid

import pytest

from kntgraph.core.event import Event, CorrelationContext
from kntgraph.events.dlq import (
    DLQ_AGENT_INDEX,
    DLQ_EVENT_INDEX,
    DLQ_REASON_INDEX,
    DLQ_STREAM_KEY,
    DLQReason,
    DeadLetterActions as _ActualDeadLetterActions,
    DeadLetterEvent,
)
from kntgraph.events.dlq.store import DeadLetterQueue
from kntgraph.infra.redis._dlq import RedisDLQStorage
from typing import Any


class DeadLetterActions(_ActualDeadLetterActions):
    def __init__(self, redis_client: Any) -> None:
        storage = RedisDLQStorage(client=redis_client)
        queue = DeadLetterQueue(storage)
        super().__init__(queue=queue)

    async def append(self, *args: Any, **kwargs: Any) -> Any:
        assert self._queue is not None
        return await self._queue.append(*args, **kwargs)

    async def get_event(self, *args: Any, **kwargs: Any) -> Any:
        assert self._queue is not None
        return await self._queue.get_event(*args, **kwargs)

    async def list_for_agent(self, *args: Any, **kwargs: Any) -> Any:
        assert self._queue is not None
        return await self._queue.list_for_agent(*args, **kwargs)

    async def list_by_reason(self, *args: Any, **kwargs: Any) -> Any:
        assert self._queue is not None
        return await self._queue.list_by_reason(*args, **kwargs)

    async def list_all(self, *args: Any, **kwargs: Any) -> Any:
        assert self._queue is not None
        return await self._queue.list_all(*args, **kwargs)

    async def get_stats(self, *args: Any, **kwargs: Any) -> Any:
        assert self._queue is not None
        return await self._queue.get_stats(*args, **kwargs)

    async def purge(self, *args: Any, **kwargs: Any) -> Any:
        assert self._queue is not None
        return await self._queue.purge(*args, **kwargs)


pytestmark = pytest.mark.asyncio


def make_dl_event(
    agent_id: str = "a-1",
    event_type: str = "document.received",
    event_class: str = "domain",
    reason: DLQReason = DLQReason.PROCESSING_FAILED,
    error_message: str = "boom",
    retry_count: int = 0,
    *,
    unique: int = 0,
) -> DeadLetterEvent:
    """
    Builds a DeadLetterEvent. The `unique` keyword arg forces
    a different `event_id` (and thus a different stream entry)
    across calls. Default `unique=0` is shared with no-arg calls.
    """
    e = Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class=event_class,  # type: ignore[arg-type]
        data={"k": 1, "_unique": unique},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return DeadLetterEvent(
        event=e,
        reason=reason,
        error_message=error_message,
        original_timestamp=now,
        dlq_timestamp=now,
        retry_count=retry_count,
    )


class TestDLQAppend:
    async def test_append_creates_stream(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        dle = make_dl_event()
        result = await dlq.append(dle)
        assert result.is_ok()
        assert await clean_redis.exists(DLQ_STREAM_KEY)

    async def test_append_updates_reason_counter(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        await dlq.append(make_dl_event(reason=DLQReason.TIMEOUT, unique=1))
        await dlq.append(make_dl_event(reason=DLQReason.TIMEOUT, unique=2))
        await dlq.append(make_dl_event(reason=DLQReason.VALIDATION_ERROR, unique=3))
        reasons = await clean_redis.hgetall(DLQ_REASON_INDEX)
        decoded = {
            (k.decode() if isinstance(k, bytes) else k): int(v)
            for k, v in reasons.items()
        }
        assert decoded.get("timeout") == 2
        assert decoded.get("validation_error") == 1

    async def test_append_updates_agent_index(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        await dlq.append(make_dl_event(agent_id="a-1", unique=1))
        await clean_redis.hget(DLQ_AGENT_INDEX, "a-1")
        # The index exists and points to a stream id
        assert await clean_redis.hexists(DLQ_AGENT_INDEX, "a-1")


class TestDLQIdempotency:
    async def test_same_event_same_reason_idempotent(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        dle = make_dl_event()
        r1 = await dlq.append(dle)
        r2 = await dlq.append(dle)
        assert r1.unwrap() == r2.unwrap()

    async def test_same_event_different_reasons_are_separate(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        e = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        dle_timeout = DeadLetterEvent(
            event=e,
            reason=DLQReason.TIMEOUT,
            error_message="t",
            original_timestamp=now,
            dlq_timestamp=now,
        )
        dle_validation = DeadLetterEvent(
            event=e,
            reason=DLQReason.VALIDATION_ERROR,
            error_message="v",
            original_timestamp=now,
            dlq_timestamp=now,
        )
        r1 = await dlq.append(dle_timeout)
        r2 = await dlq.append(dle_validation)
        # Different reasons → different stream entries
        assert r1.unwrap() != r2.unwrap()


class TestDLQRead:
    async def test_get_event(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        dle = make_dl_event()
        await dlq.append(dle)
        retrieved = await dlq.get_event(str(dle.event.event_id))
        assert retrieved is not None
        assert retrieved.event.event_id == dle.event.event_id
        assert retrieved.reason == dle.reason

    async def test_get_event_unknown(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        retrieved = await dlq.get_event("00000000-0000-0000-0000-000000000000")
        assert retrieved is None

    async def test_list_for_agent(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        await dlq.append(
            make_dl_event(agent_id="a-1", reason=DLQReason.TIMEOUT, unique=1)
        )
        await dlq.append(
            make_dl_event(agent_id="a-1", reason=DLQReason.POISON_PILL, unique=2)
        )
        await dlq.append(
            make_dl_event(agent_id="a-2", reason=DLQReason.TIMEOUT, unique=3)
        )
        entries = await dlq.list_for_agent("a-1")
        assert len(entries) == 2
        for e in entries:
            assert e.event.agent_id == "a-1"

    async def test_list_by_reason(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        await dlq.append(make_dl_event(reason=DLQReason.TIMEOUT, unique=1))
        await dlq.append(make_dl_event(reason=DLQReason.TIMEOUT, unique=2))
        await dlq.append(make_dl_event(reason=DLQReason.POISON_PILL, unique=3))
        timeouts = await dlq.list_by_reason(DLQReason.TIMEOUT)
        assert len(timeouts) == 2
        for e in timeouts:
            assert e.reason == DLQReason.TIMEOUT

    async def test_list_all(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        for i in range(5):
            await dlq.append(make_dl_event(unique=i))
        all_entries = await dlq.list_all()
        assert len(all_entries) == 5


class TestDLQReprocess:
    async def test_reprocess_returns_event(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        dle = make_dl_event()
        await dlq.append(dle)
        result = await dlq.reprocess(str(dle.event.event_id))
        assert result.is_ok()
        event = result.unwrap()
        assert event.event_id == dle.event.event_id
        # The entry is removed
        assert await dlq.get_event(str(dle.event.event_id)) is None

    async def test_reprocess_unknown_fails(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        result = await dlq.reprocess("00000000-0000-0000-0000-000000000000")
        assert result.is_err()

    async def test_reprocess_decrements_reason_counter(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        dle = make_dl_event(reason=DLQReason.TIMEOUT)
        await dlq.append(dle)
        reasons_before = await clean_redis.hgetall(DLQ_REASON_INDEX)
        before = int(reasons_before.get(b"timeout", 0))
        await dlq.reprocess(str(dle.event.event_id))
        reasons_after = await clean_redis.hgetall(DLQ_REASON_INDEX)
        after = int(reasons_after.get(b"timeout", 0))
        assert after == before - 1


class TestDLQDiscard:
    async def test_discard_removes_entry(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        dle = make_dl_event()
        await dlq.append(dle)
        result = await dlq.discard(str(dle.event.event_id))
        assert result.is_ok()
        assert await dlq.get_event(str(dle.event.event_id)) is None

    async def test_discard_unknown_fails(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        result = await dlq.discard("00000000-0000-0000-0000-000000000000")
        assert result.is_err()


class TestDLQStats:
    async def test_stats_empty(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        stats = await dlq.get_stats()
        assert stats["total_events"] == 0
        assert stats["by_reason"] == {}

    async def test_stats_with_entries(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        await dlq.append(make_dl_event(reason=DLQReason.TIMEOUT, unique=1))
        await dlq.append(make_dl_event(reason=DLQReason.TIMEOUT, unique=2))
        await dlq.append(make_dl_event(reason=DLQReason.POISON_PILL, unique=3))
        stats = await dlq.get_stats()
        assert stats["total_events"] == 3
        assert stats["by_reason"]["timeout"] == 2
        assert stats["by_reason"]["poison_pill"] == 1


class TestDLQPurge:
    async def test_purge_clears_all(self, clean_redis):
        dlq = DeadLetterActions(clean_redis)
        for i in range(5):
            await dlq.append(make_dl_event(unique=i))
        result = await dlq.purge()
        assert result.is_ok()
        assert result.unwrap() == 5
        # All indexes gone
        assert not await clean_redis.exists(DLQ_STREAM_KEY)
        assert not await clean_redis.exists(DLQ_AGENT_INDEX)
        assert not await clean_redis.exists(DLQ_EVENT_INDEX)
        assert not await clean_redis.exists(DLQ_REASON_INDEX)


class TestDLQEndToEnd:
    """
    End-to-end: a system fails on an event → DLQ → reprocess →
    EventLog absorbs the re-emitted event idempotently.
    """

    async def test_dlq_to_eventlog_idempotent_cycle(self, clean_redis):
        from kntgraph.stream.event_log import EventLog

        dlq = DeadLetterActions(clean_redis)
        log = EventLog(RedisEventLogAdapter(clean_redis))

        dle = make_dl_event()
        await dlq.append(dle)

        # Reprocess: get the original event back
        result = await dlq.reprocess(str(dle.event.event_id))
        assert result.is_ok()
        recovered = result.unwrap()

        # Re-append to the EventLog
        r1 = await log.append(recovered)
        assert r1.is_ok()
        # Re-append again — idempotent
        r2 = await log.append(recovered)
        assert r2.is_ok()
        # Same stream id
        assert r1.unwrap() == r2.unwrap()
