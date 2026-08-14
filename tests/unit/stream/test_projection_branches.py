# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

import fakeredis.aioredis
import pytest
from uuid import uuid4
from datetime import datetime, timezone

from kntgraph.core.event import Event, CorrelationContext
from kntgraph.infra.redis import RedisEventLogAdapter
from kntgraph.stream.event_log import EventLog
from kntgraph.stream.projection import read_all_events, fold_world, fold_world_for_agent

pytestmark = pytest.mark.asyncio


def _make_log() -> EventLog:
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    storage = RedisEventLogAdapter(client=client)
    return EventLog(storage=storage)


def _dummy_event(agent_id: str) -> Event:
    return Event(
        event_id=uuid4(),
        event_type="test.event",
        agent_id=agent_id,
        event_class="domain",
        data={},
        timestamp=datetime.now(timezone.utc),
        correlation=CorrelationContext(causation_id=uuid4(), correlation_id=uuid4()),
    )


class TestProjectionBranches:
    async def test_read_all_empty(self):
        log = _make_log()
        events = await read_all_events(log)
        assert events == []

    async def test_read_all_with_events(self):
        log = _make_log()
        e1 = _dummy_event("a1")
        e2 = _dummy_event("a2")
        await log.append_batch([e1, e2])
        
        events = await read_all_events(log)
        assert len(events) == 2
        agent_ids = {e.agent_id for e in events}
        assert agent_ids == {"a1", "a2"}

    async def test_fold_world_empty_agents(self):
        log = _make_log()
        # Ensure it handles an empty list without fetching everything
        await log.append_batch([_dummy_event("a1")])
        world = await fold_world(log, agent_ids=[])
        # Nothing folded
        assert len(world.agents) == 0

    async def test_fold_world_with_agent_ids(self):
        log = _make_log()
        await log.append_batch([_dummy_event("a1"), _dummy_event("a2")])
        world = await fold_world(log, agent_ids=["a1"])
        assert "a1" in world.agents
        assert "a2" not in world.agents

    async def test_fold_world_none_agent_ids(self):
        log = _make_log()
        await log.append_batch([_dummy_event("a1"), _dummy_event("a2")])
        world = await fold_world(log, agent_ids=None)
        assert "a1" in world.agents
        assert "a2" in world.agents

    async def test_fold_world_for_agent(self):
        log = _make_log()
        await log.append_batch([_dummy_event("a1")])
        world = await fold_world_for_agent(log, "a1")
        assert "a1" in world.agents
