# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Stress test and process execution telemetry benchmark for BusinessFSM + Tool Workers.

Exercises the full reactive state-machine pipeline against a real Redis instance:
  - EventLog (RedisEventLogAdapter)
  - ReactiveDispatcher
  - BusinessFSMConcordo (FSMSystem + FSMProjection)
  - ToolRouter
  - WorkerManager (ProcessPoolExecutor)

State Machine Topology per Process:
  - State 'created' --(order.submit)--> State 'processing'
    On entry to 'processing': emits 'tool.<tool_name>.requested'
  - State 'processing' --(tool.<tool_name>.completed)--> State 'approved' (terminal)
  - State 'processing' --(tool.<tool_name>.failed)--> State 'rejected' (terminal)

Calculates and prints telemetry:
  - Total process executions.
  - Process executions per second (throughput).
  - Peak state transitions per second (TPS).
  - Tool completions vs failures.
"""

from __future__ import annotations

import asyncio
import gc
import os
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest

from kntgraph.concordos.fsm import BusinessFSMConcordo, FSMConfig, FSMTransition
from kntgraph.core.event import (
    CorrelationContext,
    Event,
    correlation_middleware,
)
from kntgraph.core.result import Err, Ok, Result, ToolError
from kntgraph.core.world import DomainComponent, domain_component
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
# Domain Component & Tools
# ---------------------------------------------------------------------------


@domain_component("order.submit")
@dataclass(frozen=True, slots=True)
class StressOrderComponent(DomainComponent):
    """Domain component representing order state under stress."""

    stage: str = "created"
    amount: int = 100


@tool_worker(name="fast_success_tool", max_concurrency=20, retries=0)
class FastSuccessTool:
    """Fast completing tool (instant Ok)."""

    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Ok({"status": "processed", "state": state})


@tool_worker(name="fast_failure_tool", max_concurrency=20, retries=0)
class FastFailureTool:
    """Fast failing tool (instant Err)."""

    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Err(ToolError(kind="permanent", message="simulated fast failure"))


# ---------------------------------------------------------------------------
# FSM Configuration
# ---------------------------------------------------------------------------


def create_fsm_config(tool_name: str) -> FSMConfig:
    """Build an FSMConfig that uses the given tool on entry to 'processing'."""
    return FSMConfig(
        component_type=StressOrderComponent,
        state_field="stage",
        transitions={
            "created": {
                "order.submit": FSMTransition(to="processing"),
            },
            "processing": {
                f"tool.{tool_name}.completed": FSMTransition(to="approved"),
                f"tool.{tool_name}.failed": FSMTransition(to="rejected"),
            },
        },
        on_entry={
            "processing": f"tool.{tool_name}.requested",
        },
        terminal=frozenset({"approved", "rejected"}),
    )


# ---------------------------------------------------------------------------
# Driver & Telemetry Benchmark
# ---------------------------------------------------------------------------


async def run_fsm_telemetry_benchmark(
    redis,
    tool_name: str,
    tool_cls: type,
    num_processes: int = 100,
    max_wait_seconds: float = 10.0,
) -> dict[str, str | float | int]:
    """Run Business FSM for num_processes unique process executions and return telemetry."""
    event_log = EventLog(RedisEventLogAdapter(client=redis))
    tool_router = ToolRouter(redis)

    fsm_config = create_fsm_config(tool_name)
    concordo = BusinessFSMConcordo(fsm_config)

    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[],
        redis=redis,
        tool_router=tool_router,
        poll_interval=0.01,
        rediscovery_interval_seconds=0.1,
        heartbeat_interval_seconds=0.0,
    )
    concordo.install(dispatcher)

    worker_manager = WorkerManager(
        redis=redis,
        event_log=event_log,
        reaper_interval=0.1,
        reaper_idle_time=0.1,
    )
    worker_manager.register(tool_cls, acl=None)

    agent_ids = [f"proc-{tool_name}-{i}" for i in range(num_processes)]

    # Seed all process agents BEFORE starting the dispatcher & worker manager
    for agent_id in agent_ids:
        correlation_middleware.start(metadata={"stress": tool_name})
        await event_log.append(
            Event.create(
                event_type="agent.spawned",
                agent_id=agent_id,
                event_class="lifecycle",
                correlation=correlation_middleware.current(),
                data={"role": "fsm_stress"},
            )
        )
        await event_log.append(
            Event.create(
                event_type="order.submit",
                agent_id=agent_id,
                event_class="domain",
                correlation=correlation_middleware.current(),
                data={"stage": "created", "amount": 100},
            )
        )
        dispatcher.track_agent(agent_id)
        correlation_middleware.clear()

    start_time = time.monotonic()
    await dispatcher.start()
    await worker_manager.start()

    # Poll until all processes reach terminal state
    deadline = start_time + max_wait_seconds
    completed_processes = 0
    total_transitions = 0
    completions = 0
    failures = 0

    while time.monotonic() < deadline:
        completed_processes = 0
        total_transitions = 0
        completions = 0
        failures = 0

        for agent_id in agent_ids:
            log_events = await event_log.read(agent_id)
            by_type = Counter(e.event_type for e in log_events)
            transitions = by_type.get("fsm.transitioned", 0)
            comp = by_type.get(f"tool.{tool_name}.completed", 0)
            fail = by_type.get(f"tool.{tool_name}.failed", 0)

            total_transitions += transitions
            completions += comp
            failures += fail

            if (comp + fail) >= 1:
                completed_processes += 1

        if completed_processes >= num_processes:
            break

        await asyncio.sleep(0.05)

    debug_events = [e.event_type for e in await event_log.read(agent_ids[0])]
    print(f"\nDEBUG events for {agent_ids[0]}: {debug_events}")

    stop_time = time.monotonic()
    total_elapsed = stop_time - start_time

    await dispatcher.stop()
    await worker_manager.stop()

    processes_per_sec = completed_processes / total_elapsed if total_elapsed > 0 else 0
    tps = total_transitions / total_elapsed if total_elapsed > 0 else 0

    metrics: dict[str, str | float | int] = {
        "tool_name": tool_name,
        "duration_sec": round(total_elapsed, 3),
        "total_processes": num_processes,
        "completed_processes": completed_processes,
        "total_transitions": total_transitions,
        "completions": completions,
        "failures": failures,
        "process_throughput_sec": round(processes_per_sec, 2),
        "peak_tps": round(tps, 2),
    }

    assert completed_processes == num_processes, (
        f"Expected all {num_processes} processes to complete, got {completed_processes}."
    )

    stream_key = f"knt:tools:{tool_name}:queue"
    pending_info = await redis.xpending(stream_key, "fmh_tool_workers")
    pending_count = (
        pending_info.get("pending", 0)
        if isinstance(pending_info, dict)
        else (pending_info or 0)
    )
    assert pending_count == 0, f"PEL for {tool_name} has {pending_count} unacked messages."

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
    assert leaked == [], f"Found {len(leaked)} leaked tasks: {[t.get_name() for t in leaked]}"

    return metrics


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


@pytest.mark.stress
async def test_business_fsm_telemetry_fast_completion(redis_client) -> None:
    """Telemetry benchmark for fast tool completion."""
    metrics = await run_fsm_telemetry_benchmark(
        redis_client,
        tool_name="fast_success_tool",
        tool_cls=FastSuccessTool,
        num_processes=100,
    )
    print("\n--- BUSINESS FSM TELEMETRY (FAST COMPLETION) ---")
    for key, val in metrics.items():
        print(f"  {key}: {val}")


@pytest.mark.stress
async def test_business_fsm_telemetry_fast_failure(redis_client) -> None:
    """Telemetry benchmark for fast tool failure."""
    metrics = await run_fsm_telemetry_benchmark(
        redis_client,
        tool_name="fast_failure_tool",
        tool_cls=FastFailureTool,
        num_processes=100,
    )
    print("\n--- BUSINESS FSM TELEMETRY (FAST FAILURE) ---")
    for key, val in metrics.items():
        print(f"  {key}: {val}")


# ---------------------------------------------------------------------------
# CLI Execution
# ---------------------------------------------------------------------------


async def _run_benchmark_cli():
    import redis.asyncio as aioredis

    password = os.environ.get("KNT_REDIS_PASSWORD", "redispassword")
    client = aioredis.Redis(
        host="localhost",
        port=6379,
        password=password,
        db=15,
        decode_responses=False,
    )
    try:
        await client.ping()
    except Exception as e:
        print(f"Error connecting to Redis: {e}")
        return

    await client.flushdb()

    m_succ = await run_fsm_telemetry_benchmark(
        client,
        tool_name="fast_success_tool",
        tool_cls=FastSuccessTool,
        num_processes=100,
    )
    await client.flushdb()

    m_fail = await run_fsm_telemetry_benchmark(
        client,
        tool_name="fast_failure_tool",
        tool_cls=FastFailureTool,
        num_processes=100,
    )
    await client.flushdb()
    await client.aclose()

    print("\n=========================================================================")
    print("        BUSINESS FSM PROCESS EXECUTION TELEMETRY (REAL REDIS)           ")
    print("=========================================================================")
    print("Scenario 1: Tool Completing Fast (Success)")
    print(f"  - Process Executions Completed : {m_succ['completed_processes']} / {m_succ['total_processes']}")
    print(f"  - Process Throughput           : {m_succ['process_throughput_sec']} processes/sec")
    print(f"  - Peak FSM Transitions (TPS)   : {m_succ['peak_tps']} TPS")
    print(f"  - Total Tool Completions       : {m_succ['completions']}")
    print(f"  - Total Duration               : {m_succ['duration_sec']}s")
    print("-------------------------------------------------------------------------")
    print("Scenario 2: Tool Failing Fast (Error)")
    print(f"  - Process Executions Completed : {m_fail['completed_processes']} / {m_fail['total_processes']}")
    print(f"  - Process Throughput           : {m_fail['process_throughput_sec']} processes/sec")
    print(f"  - Peak FSM Transitions (TPS)   : {m_fail['peak_tps']} TPS")
    print(f"  - Total Tool Failures          : {m_fail['failures']}")
    print(f"  - Total Duration               : {m_fail['duration_sec']}s")
    print("=========================================================================\n")


if __name__ == "__main__":
    asyncio.run(_run_benchmark_cli())
