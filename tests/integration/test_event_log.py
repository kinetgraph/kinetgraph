# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for EventLog (real Redis).

The EventLog is the only source of truth for events. The tests
cover the append/read cycle, idempotency, and the per-agent layout.
"""

from __future__ import annotations
from kntgraph.core.event import CorrelationContext

import pytest

from kntgraph.core.event import Event
from kntgraph.infra.redis._event_log._adapter import RedisEventLogAdapter
from kntgraph.stream.event_log import (
    AGENT_STREAM_KEY,
    EVENT_ID_INDEX,
    EventLog,
)

pytestmark = pytest.mark.asyncio


def _ctx() -> CorrelationContext:
    return CorrelationContext.new(correlation_id=__import__("uuid").uuid4())


class TestEventLogAppend:
    async def test_append_creates_stream(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        e = Event.create(
            event_type="agent.spawned",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        result = await log.append(e)
        assert result.is_ok()
        stream_id = result.unwrap()
        assert stream_id  # non-empty

    async def test_append_creates_idempotency_index(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        e = Event.create(
            event_type="x", agent_id="a-1", event_class="lifecycle", correlation=_ctx()
        )
        await log.append(e)
        # The eventid index key must exist
        key = EVENT_ID_INDEX.format(event_id=str(e.event_id))
        assert await clean_redis.get(key) is not None


class TestEventLogIdempotency:
    async def test_re_appending_same_event_is_noop(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        e = Event.create(
            event_type="x", agent_id="a-1", event_class="lifecycle", correlation=_ctx()
        )
        r1 = await log.append(e)
        r2 = await log.append(e)
        assert r1.is_ok() and r2.is_ok()
        assert r1.unwrap() == r2.unwrap()
        # Stream must have exactly ONE entry
        assert await log.stream_len("a-1") == 1

    async def test_replay_of_same_system_does_not_duplicate(self, clean_redis):
        """
        Simulates a system being re-applied. The system's events
        have deterministic ids (because causation_id is fixed by
        the system), so re-appending is a no-op.
        """
        log = EventLog(RedisEventLogAdapter(clean_redis))
        received = Event.create(
            event_type="document.received",
            agent_id="a-1",
            event_class="domain",
            data={"document_id": "NF-001"},
            correlation=_ctx(),
        )
        validated_1 = Event.create(
            event_type="document.validated",
            agent_id="a-1",
            event_class="domain",
            data={"document_id": "NF-001"},
            causation_id=received.event_id,
            correlation=_ctx(),
        )
        validated_2 = Event.create(
            event_type="document.validated",
            agent_id="a-1",
            event_class="domain",
            data={"document_id": "NF-001"},
            causation_id=received.event_id,
            correlation=_ctx(),
        )
        assert validated_1.event_id == validated_2.event_id
        await log.append(received)
        await log.append(validated_1)
        await log.append(validated_2)  # same id → no-op
        assert await log.stream_len("a-1") == 2


class TestEventLogRead:
    async def test_read_returns_empty_for_unknown_agent(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        events = await log.read("nonexistent")
        assert events == []

    async def test_read_returns_appended_events(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        e1 = Event.create(
            event_type="agent.spawned",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        e2 = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="domain",
            data={"k": 1},
            correlation=_ctx(),
        )
        await log.append(e1)
        await log.append(e2)
        events = await log.read("a-1")
        assert len(events) == 2
        assert events[0].event_type == "agent.spawned"
        assert events[1].event_type == "x"

    async def test_read_latest(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        for i in range(5):
            await log.append(
                Event.create(
                    event_type="x",
                    agent_id="a-1",
                    event_class="lifecycle",
                    data={"i": i},
                    correlation=_ctx(),
                )
            )
        latest = await log.read_latest("a-1", n=2)
        assert len(latest) == 2
        # most recent first
        assert latest[0].data["i"] == 4
        assert latest[1].data["i"] == 3


class TestEventLogIsolation:
    async def test_per_agent_streams_are_isolated(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        e_a = Event.create(
            event_type="x", agent_id="a", event_class="lifecycle", correlation=_ctx()
        )
        e_b = Event.create(
            event_type="x", agent_id="b", event_class="lifecycle", correlation=_ctx()
        )
        await log.append(e_a)
        await log.append(e_b)
        assert await log.stream_len("a") == 1
        assert await log.stream_len("b") == 1
        # Streams are different keys
        key_a = AGENT_STREAM_KEY.format(agent_id="a")
        key_b = AGENT_STREAM_KEY.format(agent_id="b")
        assert await clean_redis.exists(key_a)
        assert await clean_redis.exists(key_b)


class TestEventLogListAgents:
    async def test_list_agents(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        for aid in ("a", "b", "c"):
            await log.append(
                Event.create(
                    event_type="x",
                    agent_id=aid,
                    event_class="lifecycle",
                    correlation=_ctx(),
                )
            )
        ids = await log.list_agents()
        assert set(ids) == {"a", "b", "c"}

    async def test_list_agents_empty(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        ids = await log.list_agents()
        assert ids == []


class TestEventLogBatch:
    async def test_append_batch(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        events = [
            Event.create(
                event_type="x",
                agent_id="a-1",
                event_class="lifecycle",
                data={"i": i},
                correlation=_ctx(),
            )
            for i in range(5)
        ]
        result = await log.append_batch(events)
        assert result.is_ok()
        assert len(result.unwrap()) == 5
        assert await log.stream_len("a-1") == 5

    async def test_append_batch_with_duplicates(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        e = Event.create(
            event_type="x", agent_id="a-1", event_class="lifecycle", correlation=_ctx()
        )
        result = await log.append_batch([e, e, e])
        assert result.is_ok()
        assert await log.stream_len("a-1") == 1


class TestEventLogDelete:
    async def test_delete_agent_stream(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        await log.append(
            Event.create(
                event_type="x",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            )
        )
        await log.delete_agent_stream("a-1")
        assert await log.stream_len("a-1") == 0
