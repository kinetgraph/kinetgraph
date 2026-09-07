# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Behaviour tests for ``EventLog.subscribe`` (ADR-068 Phase 1).

The subscribe primitive is the push-first wake-up transport:
a blocking multi-stream ``XREAD`` that returns
``(new_cursors, events)`` per call. The tests run against
**fakeredis** (``KNT_REDIS_FAKE`` unit-test convention) —
its ``xread`` is blocking-faithful: it holds the connection
until an entry arrives or the timeout elapses, which is the
production behaviour the consumers depend on.

Coverage contract (CONTRIBUTING.md §7.2 — happy path + one
failure mode per public function):

  - happy path: backlog read (no cursor), wake-up on a
    concurrent append (strictly-after-cursor), multi-agent
    fan-in on one connection, and the timeout arm
    (``({}, [])`` — the "notification is a hint" invariant).
  - failure mode: storage-level exception surfaces to the
    caller (the adapter is a thin I/O boundary; the
    orchestrator maps exceptions per the standard
    dispatch policy).

Cursor semantics pinned by these tests (ADR-068 §3.1):

  - agent present in ``cursors`` → exclusive read strictly
    after that cursor;
  - agent absent from ``cursors`` (map present or not) →
    full existing backlog plus new entries;
  - returned cursor for an untouched agent is absent from
    ``new_cursors`` (the caller keeps its own).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.stream.event_log import EventLog


pytestmark = pytest.mark.asyncio


@pytest.fixture
def corr() -> CorrelationContext:
    """One correlation per test (ADR-037 requires a non-None
    context at ``Event.create``)."""
    return CorrelationContext.new()


def _event(agent_id: str, payload: dict) -> Event:
    return Event.create(
        event_type="document.received",
        agent_id=agent_id,
        event_class="domain",
        data=payload,
        correlation=CorrelationContext.new(),
    )


@pytest.fixture
def log() -> EventLog:
    """Real EventLog over the real fakeredis client — the
    behaviour-test bar (kntgraph-testing §7.1)."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    return EventLog(storage=RedisEventLogAdapter(client=client))


class TestSubscribeBacklog:
    async def test_full_backlog_when_agent_absent_from_cursors(self, log):
        """No cursor entry for the agent → the whole existing
        backlog is returned (mirrors ``read_after_cursor
        (agent, '-')``), and the cursor advances to the last
        entry's stream id."""
        e1 = _event("agent-1", {"n": 1})
        e2 = _event("agent-1", {"n": 2})
        await log.append(e1)
        await log.append(e2)

        cursors, events = await log.subscribe(["agent-1"], block_ms=300)

        assert len(events) == 2
        assert events[0].data == {"n": 1}
        assert events[1].data == {"n": 2}
        assert set(cursors) == {"agent-1"}
        # The returned cursor is a live stream id — the next
        # subscribe from it must see nothing new.
        cursors2, events2 = await log.subscribe(
            ["agent-1"], cursors=cursors, block_ms=300
        )
        assert events2 == []
        assert cursors2 == {}

    async def test_cursors_none_means_full_backlog(self, log):
        """``cursors=None`` is the bootstrap form: read the
        agent's whole history."""
        e = _event("agent-1", {"n": 1})
        await log.append(e)

        cursors, events = await log.subscribe(["agent-1"], block_ms=300)

        # The cursor is the Redis stream id of the last entry
        # (not the Event's UUID); it must be a well-formed
        # ``<ms>-<seq>`` id and the read must have seen the
        # one event.
        assert len(events) == 1
        assert set(cursors) == {"agent-1"}
        ms, seq = cursors["agent-1"].split("-")
        assert ms.isdigit() and seq.isdigit()


class TestSubscribeWakeUp:
    async def test_wakes_on_concurrent_append_after_cursor(self, log):
        """The blocking read holds the connection and wakes
        when another task appends — the production idle path:
        zero round-trips while silent, immediate wake on
        arrival."""
        e1 = _event("agent-1", {"n": 1})
        await log.append(e1)
        cursors, _ = await log.subscribe(["agent-1"], block_ms=300)

        async def producer():
            # Wake first, append from the other task.
            await asyncio.sleep(0.05)
            await log.append(_event("agent-1", {"n": 2}))

        task = asyncio.create_task(producer())
        cursors2, events2 = await log.subscribe(
            ["agent-1"], cursors=cursors, block_ms=5000
        )
        await task

        assert [e.data for e in events2] == [{"n": 2}]
        assert set(cursors2) == {"agent-1"}

    async def test_wake_does_not_replay_untouched_agents(self, log):
        """Fan-in: a wake on agent-2 returns agent-2's events
        and advances only agent-2's cursor; agent-1's position
        is untouched (its cursor stays the caller's
        responsibility)."""
        await log.append(_event("agent-1", {"n": 1}))
        cursors, _ = await log.subscribe(["agent-1"], block_ms=300)

        async def producer():
            await asyncio.sleep(0.05)
            await log.append(_event("agent-2", {"n": 2}))

        task = asyncio.create_task(producer())
        cursors2, events2 = await log.subscribe(
            ["agent-1", "agent-2"],
            cursors={"agent-1": cursors["agent-1"], "agent-2": "-"},
            block_ms=5000,
        )
        await task

        assert [(e.agent_id, e.data) for e in events2] == [("agent-2", {"n": 2})]
        assert set(cursors2) == {"agent-2"}


class TestSubscribeTimeout:
    async def test_timeout_returns_empty_hint(self, log):
        """Silence for ``block_ms`` yields ``({}, [])`` — the
        consumer's fallback poll (``KNT_FALLBACK_POLL_INTERVAL``)
        closes the gap; the hint is a latency optimisation,
        never a correctness dependency (ADR-068 §3.1)."""
        await log.append(_event("agent-1", {"n": 1}))
        cursors, _ = await log.subscribe(["agent-1"], block_ms=300)

        cursors2, events2 = await log.subscribe(
            ["agent-1"], cursors=cursors, block_ms=250
        )

        assert cursors2 == {}
        assert events2 == []


class TestSubscribeFailureMode:
    async def test_storage_exception_propagates(self):
        """A storage-level failure is a crash signal for the
        consumer loop (which logs + backs off per the
        resilience policy), not a silent empty hint. The
        adapter is a thin I/O boundary — it does not swallow
        exceptions."""
        storage = RedisEventLogAdapter(client=AsyncMock())
        storage.client.xread = AsyncMock(side_effect=RuntimeError("redis down"))
        log = EventLog(storage=storage)

        with pytest.raises(RuntimeError, match="redis down"):
            await log.subscribe(["agent-1"], block_ms=300)
