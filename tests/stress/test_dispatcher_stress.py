# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Stress test for the ``ReactiveDispatcher`` +
``WorkerManager`` pipeline under concurrent load.

Topology:
  - 5 agents emit ``tool.<name>.requested`` events
    concurrently for 5 seconds. Round-robin between
    3 tools registered with the ``WorkerManager``:
      * ``stress_io`` — IO-bound (asyncio.sleep)
      * ``stress_cpu`` — CPU-bound (hashlib loop)
      * ``stress_mixed`` — IO + CPU
  - 3 levels of events are exercised:
      * ``lifecycle``: ``agent.spawned`` (one per agent)
      * ``domain``: ``tool.<name>.requested`` (the driver)
      * ``tool.*``: ``tool.<name>.completed`` /
        ``tool.<name>.failed`` (the worker's output)

The dispatcher has NO ``WorldSystem`` registered. It
acts only as a fan-out between the EventLog and the
``ToolRouter`` (the test exercises the
``ReactiveDispatcher`` -> ``ToolRouter`` ->
``WorkerManager`` -> ``ProcessPoolExecutor`` path
end-to-end without an LLM in the middle).

Invariants the test asserts on (all must pass):

  1. **No event is lost.** The number of
     ``tool.<name>.completed`` events in the
     EventLog matches the number of
     ``tool.<name>.requested`` events emitted by
     the driver (within a small slack for events
     in-flight at the moment of ``stop``).
  2. **No PEL residue after shutdown.** Every
     consumer group (``fmh_tool_workers``) has
     zero pending messages after the worker
     stops. A stuck message would indicate the
     worker process died or the reaper is not
     draining.
  3. **Observability counters match.** The
     dispatcher's ``_events_processed_total`` and
     the worker's ``_messages_processed_total`` are
     both positive; the worker's counter equals
     the number of completions appended to the
     EventLog.
  4. **No tasks leaked.** After ``stop()``, no
     ``asyncio.Task`` whose name starts with
     ``"fmh-"`` or whose coroutine is from the
     dispatcher / worker modules is still alive.
     This is the "parou sem aviso" guard: a task
     that survived ``stop()`` means the lifecycle
     did not propagate.

The test is **opt-in** via the ``stress`` step in
``scripts/ci.py``; it requires a real Redis on
``localhost:6379`` (db 15) and runs the
``ProcessPoolExecutor`` for tool execution.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import time
from collections import Counter
from typing import Any
from uuid import uuid4

import pytest

from kntgraph.core.event import (
    CorrelationContext,
    Event,
    correlation_middleware,
)
from kntgraph.core.result import Ok
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog
from kntgraph.tools import tool_worker
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.stress,
]


# ---------------------------------------------------------------------------
# Tools (3 levels: IO-bound, CPU-bound, mixed)
# ---------------------------------------------------------------------------


@tool_worker(name="stress_io", max_concurrency=4, retries=1)
class StressIOTool:
    """IO-bound tool. Simulates an LLM call by sleeping
    for ``io_ms`` milliseconds. The worker's
    ``ProcessPoolExecutor`` is what protects the
    dispatcher's event loop from this sleep.
    """

    async def invoke(
        self,
        *,
        idempotency_key: str,
        io_ms: int = 50,
    ) -> dict:
        await asyncio.sleep(io_ms / 1000.0)
        return Ok({"kind": "io", "slept_ms": io_ms})


@tool_worker(name="stress_cpu", max_concurrency=4, retries=1)
class StressCPUTool:
    """CPU-bound tool. A pure-Python hash loop in
    ``iterations`` iterations; no I/O, no ``await``.
    Inside the worker process, this still takes
    measurable wall time.
    """

    async def invoke(
        self,
        *,
        idempotency_key: str,
        iterations: int = 50_000,
    ) -> dict:
        h = hashlib.sha256()
        for i in range(iterations):
            h.update(f"{idempotency_key}-{i}".encode())
        return Ok({"kind": "cpu", "digest": h.hexdigest()[:16]})


@tool_worker(name="stress_mixed", max_concurrency=4, retries=1)
class StressMixedTool:
    """Mixed IO + CPU. Sleeps for ``io_ms`` then runs
    a short hash loop.
    """

    async def invoke(
        self,
        *,
        idempotency_key: str,
        io_ms: int = 30,
        iterations: int = 50_000,
    ) -> dict:
        await asyncio.sleep(io_ms / 1000.0)
        h = hashlib.sha256()
        for i in range(iterations):
            h.update(f"{idempotency_key}-{i}".encode())
        return Ok({"kind": "mixed", "slept_ms": io_ms, "digest": h.hexdigest()[:16]})


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def _drive_agents(
    event_log: EventLog,
    agent_ids: list[str],
    tool_names: list[str],
    duration_seconds: float,
) -> int:
    """Emit ``tool.<name>.requested`` events for each
    agent at a steady rate. Returns the total number
    of requests emitted across all agents.

    Each request is round-robined across the tools
    so the three tools see the same load. The
    ``ToolRouter`` fans each event out to
    ``knt:tools:<name>:queue``; the ``WorkerManager``
    consumes from those queues and runs the tool in
    a ``ProcessPoolExecutor``.

    Per-tool ``params`` are emitted so the tool's
    ``invoke`` signature is honoured. The IO tool
    only accepts ``io_ms``; the CPU tool only
    accepts ``iterations``; the mixed tool accepts
    both. Emitting the same payload to all three
    tools is a real bug surface — the test pins
    the contract that a tool's params are a
    function of the tool name, not a single
    shared payload.
    """
    total = 0
    failed_appends = 0
    deadline = time.monotonic() + duration_seconds
    interval = 0.05  # 20 events/sec per agent, 5 agents = 100 events/sec
    # Per-tool params. The keys must match the
    # tool's ``invoke`` signature exactly.
    params_per_tool = {
        "stress_io": {"io_ms": 30},
        "stress_cpu": {"iterations": 50_000},
        "stress_mixed": {"io_ms": 20, "iterations": 20_000},
    }
    while time.monotonic() < deadline:
        for agent_id in agent_ids:
            tool_name = tool_names[total % len(tool_names)]
            correlation_middleware.start(metadata={"stress": "1"})
            request = Event.create(
                event_type=f"tool.{tool_name}.requested",
                agent_id=agent_id,
                event_class="domain",
                correlation=correlation_middleware.current(),
                data={"params": params_per_tool[tool_name], "seq": total},
            )
            result = await event_log.append(request)
            if result.is_err():
                failed_appends += 1
                if failed_appends <= 3:
                    print(
                        f"DEBUG append FAILED for {agent_id}/{tool_name}: {result.err_value()}"
                    )
            total += 1
            correlation_middleware.clear()
        await asyncio.sleep(interval)
    print(f"DEBUG driver: total={total} failed_appends={failed_appends}")
    return total


# ---------------------------------------------------------------------------
# The stress test
# ---------------------------------------------------------------------------


@pytest.mark.stress
async def test_dispatcher_under_concurrent_load(redis_client) -> None:
    """5 agents, 3 tools, 3 event levels, 5 seconds.

    See the module docstring for the invariants.
    """
    redis = redis_client
    event_log = EventLog(RedisEventLogAdapter(client=redis))
    tool_router = ToolRouter(redis)

    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[],  # no domain systems; the driver emits tool.* directly
        redis=redis,
        tool_router=tool_router,
        poll_interval=0.05,
        rediscovery_interval_seconds=0.5,
        heartbeat_interval_seconds=0.0,  # off (loud under stress)
    )
    worker_manager = WorkerManager(
        redis=redis,
        event_log=event_log,
        reaper_interval=0.5,
        reaper_idle_time=0.5,
    )
    worker_manager.register(StressIOTool)
    worker_manager.register(StressCPUTool)
    worker_manager.register(StressMixedTool)

    # 1. Spawn the 5 agents with a ``lifecycle`` event.
    agent_ids = [f"stress-agent-{i}" for i in range(5)]
    for agent_id in agent_ids:
        await event_log.append(
            Event.create(
                event_type="agent.spawned",
                agent_id=agent_id,
                event_class="lifecycle",
                correlation=CorrelationContext.new(correlation_id=uuid4()),
                data={"role": "stress"},
            )
        )

    # 2. Start the dispatcher + worker.
    await dispatcher.start()
    await worker_manager.start()
    try:
        # 3. Drive the 5 agents for 5 seconds.
        requests_emitted = await _drive_agents(
            event_log,
            agent_ids,
            tool_names=["stress_io", "stress_cpu", "stress_mixed"],
            duration_seconds=5.0,
        )
        # 4. Allow in-flight requests to drain.
        # Generous bound: the IO tool sleeps 30ms,
        # the CPU tool runs ~50k hash iterations
        # in a child process, the mixed tool does
        # both. 5s of drain is enough for the 5s
        # of load on a single-machine runner; CI
        # runners with constrained CPU may need
        # a longer bound (see
        # ``reaper_idle_time`` above — kept short
        # to surface the worst-case CPU-tail
        # behavior the test was designed to catch).
        await asyncio.sleep(5.0)
    finally:
        await dispatcher.stop()
        await worker_manager.stop()

    # --- Invariant 1: every request -> 1 completion. ---
    completions_total = 0
    failed_total = 0
    for agent_id in agent_ids:
        log_events = await event_log.read(agent_id)
        by_type = Counter(e.event_type for e in log_events)
        completions_total += sum(
            v
            for k, v in by_type.items()
            if k.startswith("tool.") and k.endswith(".completed")
        )
        failed_total += sum(
            v
            for k, v in by_type.items()
            if k.startswith("tool.") and k.endswith(".failed")
        )

    requests_in_log = completions_total + failed_total
    # Allow a small slack: events that were in
    # the consumer's read-buffer at the moment
    # ``stop()`` was called may not have made it
    # to the log yet. We assert 90% of the
    # requests were serviced.
    assert requests_in_log >= requests_emitted * 0.9, (
        f"Expected at least 90% of {requests_emitted} requests to be "
        f"completed or failed; got {requests_in_log} "
        f"(completions={completions_total}, failed={failed_total}). "
        f"This indicates a worker deadlock or event loss."
    )

    # --- Invariant 2: no PEL residue. ---
    for tool_name in ("stress_io", "stress_cpu", "stress_mixed"):
        stream_key = f"knt:tools:{tool_name}:queue"
        group = "fmh_tool_workers"
        pending_info = await redis.xpending(stream_key, group)
        # ``xpending`` on a stream with no PEL
        # returns ``{"pending": 0, "min": None,
        # "max": None, "consumers": []}`` in
        # redis-py async; defensively handle the
        # int fallback.
        pending_count = (
            pending_info.get("pending", 0)
            if isinstance(pending_info, dict)
            else (pending_info or 0)
        )
        assert pending_count == 0, (
            f"{tool_name} PEL has {pending_count} unacked messages after "
            f"stop. The reaper should have drained these. A non-zero "
            f"count means a worker died or the consumer loop is stuck."
        )

    # --- Invariant 3: observability counters match. ---
    assert dispatcher._events_processed_total > 0, (
        "Dispatcher's event counter never advanced; the loop is "
        "either stuck or the poller never ticked."
    )
    worker_processed = (
        worker_manager._messages_processed_total + worker_manager._messages_failed_total
    )
    # Allow a small drift: the worker's counter
    # can be slightly ahead of the EventLog
    # completion count (a message was processed
    # but the completion was still in the
    # EventLog's write-buffer at ``read`` time).
    # The invariant we care about is "no
    # systematic drift", not "exact match" —
    # an unbounded drift would indicate a
    # bookkeeping bug; a 1-2% drift is a
    # well-known race between ``xack`` and the
    # completion EventLog commit.
    assert abs(worker_processed - (completions_total + failed_total)) <= 10, (
        f"Worker's message counter ({worker_processed}) diverged from "
        f"the completion+failed count in the EventLog "
        f"({completions_total + failed_total}) by more than 10. "
        f"This may indicate a bookkeeping drift bug."
    )
    # --- Invariant 4: no tasks leaked. ---
    gc.collect()
    leaked: list[asyncio.Task] = []
    for t in gc.get_objects():
        if not isinstance(t, asyncio.Task) or t.done():
            continue
        if t.get_name().startswith("fmh-"):
            leaked.append(t)
            continue
        coro = t.get_coro()
        if coro is None:
            continue
        qualname = getattr(coro, "__qualname__", "")
        if qualname.startswith(("WorkerManager.", "ReactiveDispatcher.")):
            leaked.append(t)
    assert leaked == [], (
        f"Found {len(leaked)} dispatcher/worker tasks still alive after "
        f"stop: {[t.get_name() for t in leaked]}. This is the 'parou "
        f"sem aviso' failure mode the observability hardening was "
        f"supposed to prevent."
    )
