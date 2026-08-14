# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import fakeredis.aioredis
import pytest

from kntgraph.core.event import Event, CorrelationContext
from kntgraph.core.result import Err, Ok
from kntgraph.core.world import World
from kntgraph.infra.redis import RedisEventLogAdapter
from kntgraph.runner.runner import Runner
from kntgraph.stream.event_log import EventLog

pytestmark = pytest.mark.asyncio


def _make_log() -> EventLog:
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    storage = RedisEventLogAdapter(client=client)
    return EventLog(storage=storage)


def _dummy_event() -> Event:
    return Event(
        event_id=uuid4(),
        event_type="test",
        agent_id="test.agent",
        event_class="domain",
        data={},
        timestamp=datetime.now(timezone.utc),
        correlation=CorrelationContext(causation_id=uuid4(), correlation_id=uuid4()),
    )


class TestRunnerTickOnceBranches:
    async def test_empty_systems(self):
        log = _make_log()
        runner = Runner(log=log, cyclic_systems=[])
        assert runner.tick == 0
        tick = await runner.tick_once()
        assert tick == 1
        assert runner.tick == 1

    async def test_sync_and_async_systems(self):
        log = _make_log()

        def sync_sys(world: World) -> list[Event]:
            return [_dummy_event()]

        async def async_sys(world: World) -> list[Event]:
            return [_dummy_event()]

        runner = Runner(log=log, cyclic_systems=[sync_sys, async_sys])  # type: ignore
        tick = await runner.tick_once()
        assert tick == 1

    async def test_append_batch_error(self):
        log = _make_log()

        def sys(world: World) -> list[Event]:
            return [_dummy_event()]

        runner = Runner(log=log, cyclic_systems=[sys])  # type: ignore

        with patch.object(log, "append_batch", return_value=Err(RuntimeError("Simulated error"))):
            tick = await runner.tick_once()
            # If append_batch fails, _tick is not incremented
            assert tick == 0


class TestRunnerStartStopBranches:
    async def test_start_idempotency(self):
        log = _make_log()
        runner = Runner(log=log)
        assert runner._task is None
        await runner.start()
        task = runner._task
        assert task is not None
        # Calling start again should return early
        await runner.start()
        assert runner._task is task
        await runner.stop()

    async def test_stop_idempotency(self):
        log = _make_log()
        runner = Runner(log=log)
        await runner.stop()  # Should not error if not running
        assert runner._task is None

    async def test_loop_break(self):
        log = _make_log()
        runner = Runner(log=log, tick_interval=0.01)

        def stop_sys(world: World) -> list[Event]:
            # This simulates a system that requests a stop during the loop
            runner._running = False
            return []

        runner.add_cyclic_system(stop_sys)
        await runner.start()
        # Wait a bit for the loop to execute
        await asyncio.sleep(0.05)
        # We manually cancelled the running flag, so the task should naturally exit
        assert runner._task.done() if runner._task else True
