# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

import asyncio

import fakeredis.aioredis
import pytest

from kntgraph.core.world import World
from kntgraph.infra.redis import RedisEventLogAdapter
from kntgraph.infra.world_checkpoint import WorldCheckpoint
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog

pytestmark = pytest.mark.asyncio


class _FakeWorldStore:
    async def load(self, agent_id: str) -> WorldCheckpoint:
        return WorldCheckpoint(world=World.empty(), last_stream_id="-")

    async def save(self, agent_id: str, checkpoint: WorldCheckpoint) -> None:
        pass


def _make_log() -> EventLog:
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    storage = RedisEventLogAdapter(client=client)
    return EventLog(storage=storage)


class TestReactiveDispatcherLoopBranches:
    async def test_loop_exits_cleanly(self):
        log = _make_log()
        store = _FakeWorldStore()
        dispatcher = ReactiveDispatcher(log=log, world_store=store, poll_interval=0.01)

        # Force a pre-existing error to ensure it gets cleared
        dispatcher._last_loop_error = "Some previous error"

        # Simula uma rodada do loop
        dispatcher._running = True

        async def _mock_dispatch_once():
            # Side-effect: end the loop after this dispatch
            dispatcher._running = False

        # Hook dispatch_once instead of systems because the reactive dispatcher
        # might not run systems if there are no pending agents.
        dispatcher.dispatch_once = _mock_dispatch_once  # type: ignore

        # Act
        await dispatcher._loop()

        # Assert
        assert dispatcher._last_loop_error is None
        assert not dispatcher._running
