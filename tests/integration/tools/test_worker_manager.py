# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for the Tool Worker Pattern (WorkerManager).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.result import Ok, Err, Result
from kntgraph.stream.event_log.store import EventLog
from kntgraph.tools.worker import tool_worker

# The WorkerManager will be implemented in kntgraph.tools.manager
# from kntgraph.tools.manager import WorkerManager


# We define the tool at the module level so it can be pickled by multiprocessing
@tool_worker(name="math_doubler", max_concurrency=2, retries=1)
class MathDoublerTool:
    async def invoke(self, *, idempotency_key: str, number: int) -> Result[dict, str]:
        if number < 0:
            return Err("Negative numbers not allowed")
        return Ok({"result": number * 2})


# For simulating a poison pill (crashes hard)
@tool_worker(name="poison_pill", max_concurrency=1, retries=2)
class PoisonPillTool:
    async def invoke(self, *, idempotency_key: str) -> Result[dict, str]:
        # Simulates a hard crash (e.g. OOM or unhandled sys.exit)
        # We use a dirty trick to kill the worker process to test DLQ
        import os
        import signal

        os.kill(os.getpid(), signal.SIGKILL)
        return Ok({})


pytestmark = pytest.mark.asyncio


async def test_worker_manager_happy_path(clean_redis):
    """
    Test that a registered tool processes a requested event from the global
    queue and outputs a completed event to the agent's event log.
    """
    from kntgraph.tools.manager import WorkerManager
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter

    agent_id = f"a-{uuid.uuid4()}"
    adapter = RedisEventLogAdapter(clean_redis)
    log = EventLog(adapter)

    manager = WorkerManager(clean_redis, event_log=log)
    manager.register(MathDoublerTool)

    # 2. Push a tool.requested event directly to the global tool queue
    # In production, the Router/Dispatcher does this Fan-Out.
    request_event = Event.create(
        event_type="tool.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "math_doubler", "params": {"number": 21}},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )

    await clean_redis.xadd(
        "fmh:tools:math_doubler:queue", {"payload": json.dumps(request_event.to_dict())}
    )

    # 3. Start the manager (runs in background)
    await manager.start()

    try:
        # Wait for the worker to process the message and emit the completed event
        # We poll the agent's event log
        success = False
        for _ in range(20):
            events = await log.read(agent_id)
            for e in events:
                if e.event_type == "tool.math_doubler.completed":
                    assert e.data["result"] == 42
                    assert e.causation_id == request_event.event_id
                    success = True
                    break
            if success:
                break
            await asyncio.sleep(0.1)

        assert success, "Worker did not process the event and output to EventLog"
    finally:
        await manager.stop()


async def test_worker_manager_railway_error(clean_redis):
    """
    Test that if a tool returns an Err(str), the manager translates it
    into a tool.failed event in the agent's log.
    """
    from kntgraph.tools.manager import WorkerManager
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter

    agent_id = f"a-{uuid.uuid4()}"
    adapter = RedisEventLogAdapter(clean_redis)
    log = EventLog(adapter)

    manager = WorkerManager(clean_redis, event_log=log)
    manager.register(MathDoublerTool)

    request_event = Event.create(
        event_type="tool.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "math_doubler", "params": {"number": -5}},  # triggers error,
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )

    await clean_redis.xadd(
        "fmh:tools:math_doubler:queue", {"payload": json.dumps(request_event.to_dict())}
    )

    await manager.start()
    try:
        success = False
        for _ in range(20):
            events = await log.read(agent_id)
            for e in events:
                if e.event_type == "tool.math_doubler.failed":
                    assert e.data["error"] == "Negative numbers not allowed"
                    success = True
                    break
            if success:
                break
            await asyncio.sleep(0.1)

        assert success, "Worker did not translate Err to tool.failed"
    finally:
        await manager.stop()


async def test_worker_manager_dlq_on_hard_crash(clean_redis):
    """
    Test that if a worker crashes repeatedly (exceeding retries),
    the XAUTOCLAIM reaper detects the poison pill and forces a tool.failed
    event into the agent's log, unblocking the agent.
    """
    from kntgraph.tools.manager import WorkerManager
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter

    agent_id = f"a-{uuid.uuid4()}"
    adapter = RedisEventLogAdapter(clean_redis)
    log = EventLog(adapter)

    manager = WorkerManager(
        clean_redis,
        event_log=log,
        reaper_interval=0.5,  # run reaper frequently for test
        reaper_idle_time=1.0,  # consider pending for 1s as crashed
    )
    manager.register(PoisonPillTool)

    request_event = Event.create(
        event_type="tool.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "poison_pill", "params": {}},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )

    await clean_redis.xadd(
        "fmh:tools:poison_pill:queue", {"payload": json.dumps(request_event.to_dict())}
    )

    await manager.start()

    try:
        # The tool crashes on invoke.
        # The reaper will run, wait 1s, claim it, and push it back.
        # It retries up to 'retries=2' times.
        # Then it DLQs and emits tool.poison_pill.failed.

        success = False
        # We wait up to 10 seconds because it has to fail, wait 1s, fail, wait 1s, fail.
        for _ in range(100):
            events = await log.read(agent_id)
            for e in events:
                if e.event_type == "tool.poison_pill.failed":
                    assert "Max retries exceeded" in e.data["error"]
                    success = True
                    break
            if success:
                break
            await asyncio.sleep(0.1)

        assert success, "WorkerManager did not recover from poison pill via DLQ"
    finally:
        await manager.stop()
