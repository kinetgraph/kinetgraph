# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for the Tool Worker Pattern (WorkerManager).

The pool is force-built with the ``spawn`` start method
(see WorkerManager.start and ``_worker_invocation.py``)
to dodge the fork+threading+openssl deadlock in
container runtimes; the three happy-path / railway /
DLQ tests below all exercise that mode as a side effect.
The introspection guard at the top of each test turns
the implicit exercise into an explicit one — a future
regression that drops ``mp_context=self._mp_context``
will fail here before the other assertions run.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.result import Err, Ok, Result
from kntgraph.stream.event_log.store import EventLog
from kntgraph.tools.executors import PoolDeprecationWarning
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


# A **sync** invoke tool — the only shape that needs a process
# boundary. Defined at module level so ``spawn`` can pickle it.
# ADR-078 §12.4: registering it without a factory requires the
# operator to pass ``legacy_pool=True``.
@tool_worker(name="sync_doubler", max_concurrency=1, retries=1)
class SyncDoublerTool:
    def invoke(self, *, idempotency_key: str, number: int) -> Result[dict, str]:
        if number < 0:
            return Err("Negative numbers not allowed")
        return Ok({"result": number * 3})


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
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter
    from kntgraph.tools.manager import WorkerManager

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
        "knt:tools:math_doubler:queue", {"payload": json.dumps(request_event.to_dict())}
    )

    # 3. Start the manager (runs in background)
    await manager.start()
    # ADR-078 §4.4: no internal pool by default. The tool is
    # async, so it runs in-proc via ``AsyncInProcFactory`` and
    # needs no process boundary. The spawn-start-method guard
    # below moved to
    # ``test_worker_manager_legacy_pool_uses_spawn``.
    assert manager._pool is None
    assert manager._mp_context is None

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


async def test_worker_manager_legacy_pool_uses_spawn(clean_redis):
    """ADR-078 §4.4: the deprecated internal pool, when the
    operator opts in with ``legacy_pool=True``, must still use
    the ``spawn`` start method.

    ``fork`` corrupts the child's ``threading._RLock`` /
    ``select`` state after the parent imported ``threading`` +
    ``ssl`` + ``cryptography`` + ``redis.asyncio``, which stalls
    ``xreadgroup`` forever in container runtimes (ADR-054 lines
    269-273). If a future refactor drops ``mp_context=``, fail
    loudly here rather than hanging the consume loop.
    """
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter
    from kntgraph.tools.manager import WorkerManager

    log = EventLog(RedisEventLogAdapter(clean_redis))
    manager = WorkerManager(clean_redis, event_log=log, legacy_pool=True)
    manager.register(MathDoublerTool, acl=None)

    with pytest.warns(PoolDeprecationWarning):
        await manager.start()
    try:
        assert manager._pool is not None
        assert manager._mp_context is not None
        assert manager._mp_context.get_start_method() == "spawn"
    finally:
        await manager.stop()


async def test_worker_manager_legacy_pool_runs_sync_tool(clean_redis):
    """ADR-078 §12.4: the ``legacy_pool=True`` escape hatch must
    actually run a sync tool end-to-end.

    The registration gate exempts sync tools when the operator
    opts in, and ``dispatch_to_tool`` then hands
    ``default_factory_for`` the Manager's ``_pool``. This test
    pins the whole chain — register → start → consume → spawn
    worker → ``tool.<name>.completed`` — so a future refactor
    that leaves the flag exempt-but-unreachable fails here
    instead of in production.
    """
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter
    from kntgraph.tools.manager import WorkerManager

    log = EventLog(RedisEventLogAdapter(clean_redis))
    manager = WorkerManager(
        clean_redis,
        event_log=log,
        legacy_pool=True,
        max_pool_workers=1,
    )
    # No ``executor_factory``: the sync tool relies on the
    # deprecated internal pool for the rest of the migration window.
    manager.register(SyncDoublerTool, acl=None)

    agent_id = f"a-{uuid.uuid4()}"
    request_event = Event.create(
        event_type="tool.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "sync_doubler", "params": {"number": 14}},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )
    await clean_redis.xadd(
        "knt:tools:sync_doubler:queue", {"payload": json.dumps(request_event.to_dict())}
    )

    with pytest.warns(PoolDeprecationWarning):
        await manager.start()
    try:
        success = False
        for _ in range(60):
            events = await log.read(agent_id)
            for e in events:
                if e.event_type == "tool.sync_doubler.completed":
                    assert e.data["result"] == 42
                    assert e.causation_id == request_event.event_id
                    success = True
                    break
            if success:
                break
            await asyncio.sleep(0.1)

        assert success, "legacy pool did not run the sync tool"
    finally:
        await manager.stop()


async def test_worker_manager_railway_error(clean_redis):
    """
    Test that if a tool returns an Err(str), the manager translates it
    into a tool.failed event in the agent's log.
    """
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter
    from kntgraph.tools.manager import WorkerManager

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
        "knt:tools:math_doubler:queue", {"payload": json.dumps(request_event.to_dict())}
    )

    await manager.start()
    # ADR-078 §4.4: async tool, no internal pool is created.
    assert manager._pool is None
    assert manager._mp_context is None
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
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter
    from kntgraph.tools.manager import WorkerManager

    agent_id = f"a-{uuid.uuid4()}"
    adapter = RedisEventLogAdapter(clean_redis)
    log = EventLog(adapter)

    manager = WorkerManager(
        clean_redis,
        event_log=log,
        reaper_interval=0.5,  # run reaper frequently for test
        reaper_idle_time=1.0,  # consider pending for 1s as crashed
    )
    # A tool that kills its own process MUST declare a process
    # pool (ADR-078 §4.4). Under the default ``AsyncInProcFactory``
    # the tool runs on the Manager's event loop, so the SIGKILL
    # would take down the Manager itself and no reaper would ever
    # run. Declaring the pool is exactly the contract a
    # crash-prone tool has to honour.
    from concurrent.futures import ProcessPoolExecutor

    from kntgraph.tools.executors import ProcessPoolFactory

    pool = ProcessPoolExecutor(max_workers=1)
    manager.register(
        PoisonPillTool,
        acl=None,
        executor_factory=ProcessPoolFactory(pool, concurrency=1),
    )

    request_event = Event.create(
        event_type="tool.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "poison_pill", "params": {}},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )

    await clean_redis.xadd(
        "knt:tools:poison_pill:queue", {"payload": json.dumps(request_event.to_dict())}
    )

    await manager.start()

    try:
        # The tool kills its pool worker on invoke. The Manager
        # survives, the reaper runs, waits 1s, claims the message
        # via XAUTOCLAIM, and retries up to 'retries=2' times.
        # Then it escalates to tool.poison_pill.failed.

        success = False
        # We wait up to 10 seconds because it has to fail, wait 1s, fail, wait 1s, fail.
        for _ in range(100):
            events = await log.read(agent_id)
            for e in events:
                if e.event_type == "tool.poison_pill.failed":
                    # The framework escalates once the PEL
                    # delivery count exceeds ``retries=2``.
                    assert "Retry budget exhausted" in e.data["error"]
                    assert e.data["error_class"] == "permanent"
                    success = True
                    break
            if success:
                break
            await asyncio.sleep(0.1)

        assert success, "WorkerManager did not recover from poison pill via DLQ"
    finally:
        await manager.stop()
        pool.shutdown(wait=False, cancel_futures=True)
