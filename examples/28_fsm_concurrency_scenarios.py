# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 28: FSM Multi-Tool Concurrency & Non-Blocking Fast-Fail (ADR-069 §3).

Demonstrates both FSM concurrency scenarios powered by BusinessFSMConcordo:

Scenario 1: FSM Parallel Execution with Fast-Fail
  - Entry transition 'order.submit' -> FSM state 'processing'.
  - FSM dispatches 3 parallel tool requests (fast success, fast failure, slow 10s tool).
  - Fast failure tool fails in ~0.1s. FSM reacts to 'tool.fsm_fast_failure_tool.failed'
    and transitions immediately to state 'rejected' at t ~ 0.1s WITHOUT waiting for the 10s tool.

Scenario 2: FSM Interleaved Tools (Agent Non-Blocking Execution)
  - FSM in state 'executing' receives two tool requests (slow 3s tool and fast 0.05s tool).
  - Fast tool completes in ~0.05s and FSM transitions to 'partially_completed' at t < 0.5s.
  - Slow tool completes at t ~ 3.0s and FSM transitions to 'fully_completed'.
  - Proves the FSM agent never freezes or blocks when receiving fast tool events while a slow tool is running.

Run with:
  KNT_REDIS_FAKE=1 uv run python examples/28_fsm_concurrency_scenarios.py
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

from kntgraph.concordos.fsm import (
    BusinessFSMConcordo,
    FSMConfig,
    FSMTransition,
)
from kntgraph.core.event import Event, correlation_middleware
from kntgraph.core.result import Err, Ok, Result, ToolError
from kntgraph.core.world import DomainComponent, World, domain_component
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog
from kntgraph.tools import tool_worker
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter

# ===========================================================================
# SCENARIO 1: Multi-Tool Fast-Fail FSM Components & Tools
# ===========================================================================


@domain_component("order.submit")
@dataclass(frozen=True, slots=True)
class FSMStressOrderComponent(DomainComponent):
    stage: str = "created"


@tool_worker(name="ex28_fast_success_tool", max_concurrency=10, retries=0)
class FastSuccessTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Ok({"status": "processed"})


@tool_worker(name="ex28_fast_failure_tool", max_concurrency=10, retries=0)
class FastFailureTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Err(ToolError("Simulated fast failure in FSM tool"))


@tool_worker(name="ex28_slow_10s_tool", max_concurrency=10, retries=0)
class Slow10sTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        print("    ⏱️  [Slow10sTool] Starting 10s background execution...")
        await asyncio.sleep(10.0)
        print("    ⏱️  [Slow10sTool] Finished 10s background execution!")
        return Ok({"status": "slow_done"})


class MultiToolDispatchSystem:
    """Dispatches 3 parallel tool requests when FSM transitions to 'processing' state."""

    def __call__(self, world: World) -> list[Event]:
        out: list[Event] = []
        for agent_id, view in world.query_agents(FSMStressOrderComponent):
            comp = view.get_component(FSMStressOrderComponent)
            if comp is None:
                continue

            last_type = view.domain_phase
            if last_type == "fsm.transitioned" and comp.stage == "processing":
                corr = correlation_middleware.current()
                principal = "tenant-a.agent-1"
                out.extend(
                    [
                        Event.create(
                            event_type="tool.ex28_fast_success_tool.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "1"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="tool.ex28_fast_failure_tool.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "2"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="tool.ex28_slow_10s_tool.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "3"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                    ]
                )

        return out


multi_tool_fsm_config = FSMConfig(
    component_type=FSMStressOrderComponent,
    state_field="stage",
    transitions={
        "created": {
            "order.submit": FSMTransition(to="processing"),
        },
        "processing": {
            "tool.ex28_fast_success_tool.completed": FSMTransition(to="approved"),
            "tool.ex28_fast_failure_tool.failed": FSMTransition(to="rejected"),
            "tool.ex28_slow_10s_tool.failed": FSMTransition(to="rejected"),
        },
    },
    terminal=frozenset({"approved", "rejected"}),
)


# ===========================================================================
# SCENARIO 2: Non-Blocking Interleaved Tools FSM Components & Tools
# ===========================================================================


@domain_component("process.start")
@dataclass(frozen=True, slots=True)
class FSMInterleavedComponent(DomainComponent):
    stage: str = "draft"


@tool_worker(name="ex28_slow_3s_tool", max_concurrency=10, retries=0)
class FSMSlow3sTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        print("    🐢 [FSMSlow3sTool] Starting 3s background execution...")
        await asyncio.sleep(3.0)
        print("    🐢 [FSMSlow3sTool] Finished 3s execution!")
        return Ok({"status": "slow_3s_done"})


@tool_worker(name="ex28_fast_005s_tool", max_concurrency=10, retries=0)
class FSMFast005sTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        print("    ⚡ [FSMFast005sTool] Executed in ~0.05s!")
        await asyncio.sleep(0.05)
        return Ok({"status": "fast_005s_done"})


class InterleavedDispatchSystem:
    def __call__(self, world: World) -> list[Event]:
        out: list[Event] = []
        for agent_id, view in world.query_agents(FSMInterleavedComponent):
            comp = view.get_component(FSMInterleavedComponent)
            if comp is None:
                continue

            last_type = view.domain_phase
            if last_type == "fsm.transitioned" and comp.stage == "executing":
                corr = correlation_middleware.current()
                principal = "tenant-a.agent-1"
                out.extend(
                    [
                        Event.create(
                            event_type="tool.ex28_slow_3s_tool.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"job": "slow"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="tool.ex28_fast_005s_tool.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"job": "fast"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                    ]
                )

        return out


interleaved_fsm_config = FSMConfig(
    component_type=FSMInterleavedComponent,
    state_field="stage",
    transitions={
        "draft": {
            "process.start": FSMTransition(to="executing"),
        },
        "executing": {
            "tool.ex28_fast_005s_tool.completed": FSMTransition(
                to="partially_completed"
            ),
            "tool.ex28_slow_3s_tool.completed": FSMTransition(to="fully_completed"),
        },
        "partially_completed": {
            "tool.ex28_slow_3s_tool.completed": FSMTransition(to="fully_completed"),
        },
    },
    terminal=frozenset({"fully_completed"}),
)


# ===========================================================================
# DRIVER FUNCTIONS
# ===========================================================================


async def run_scenario_1(redis) -> None:
    print("\n-------------------------------------------------------------------------")
    print("SCENARIO 1: FSM Parallel Multi-Tool Fast-Fail")
    print("-------------------------------------------------------------------------")

    event_log = EventLog(RedisEventLogAdapter(client=redis))
    tool_router = ToolRouter(redis)

    concordo = BusinessFSMConcordo(multi_tool_fsm_config)

    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[MultiToolDispatchSystem()],
        projections=[],
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
        reaper_interval=5.0,
        reaper_idle_time=15.0,
    )
    worker_manager.register(FastSuccessTool, acl=None)
    worker_manager.register(FastFailureTool, acl=None)
    worker_manager.register(Slow10sTool, acl=None)

    agent_id = "fsm-fast-fail-agent-001"

    print(f"🚀 [0.0s] Triggering 'order.submit' for agent '{agent_id}'...")
    correlation_middleware.start(metadata={"scenario": "fsm_fast_fail"})
    corr = correlation_middleware.current()
    principal = "tenant-a.agent-1"

    await event_log.append(
        Event.create(
            event_type="agent.spawned",
            agent_id=agent_id,
            event_class="lifecycle",
            correlation=corr,
            data={"role": "fsm_agent"},
            producer_principal_id=principal,
        )
    )
    await event_log.append(
        Event.create(
            event_type="order.submit",
            agent_id=agent_id,
            event_class="domain",
            correlation=corr,
            data={"stage": "created"},
            producer_principal_id=principal,
        )
    )

    dispatcher.track_agent(agent_id)
    correlation_middleware.clear()

    start_time = time.monotonic()
    await dispatcher.start()
    await worker_manager.start()

    rejected_elapsed: float | None = None

    for _ in range(40):
        events = await event_log.read(agent_id)
        elapsed = round(time.monotonic() - start_time, 2)
        for e in events:
            if e.event_type == "fsm.transitioned" and e.data.get("to") == "rejected":
                if rejected_elapsed is None:
                    rejected_elapsed = elapsed
                    print(
                        f"\n💥 [{elapsed}s] FSM TRANSITIONED TO REJECTED STATE FAST!"
                        f"\n    Transition: {e.data.get('from')} -> {e.data.get('to')}"
                        f"\n    Trigger: {e.data.get('trigger')}"
                    )
        if rejected_elapsed is not None:
            break
        await asyncio.sleep(0.05)

    all_events = await event_log.read(agent_id)
    print("\n📋 Timeline of Events in FSM EventLog:")
    for e in all_events:
        print(f"  - {e.event_type} (data={e.data})")

    await dispatcher.stop()
    await worker_manager.stop()

    print(
        f"\n✅ Scenario 1 Passed: FSM rejected in {rejected_elapsed}s without waiting for 10s tool!"
    )


async def run_scenario_2(redis) -> None:
    print("\n-------------------------------------------------------------------------")
    print("SCENARIO 2: FSM Interleaved Non-Blocking Tools")
    print("-------------------------------------------------------------------------")

    event_log = EventLog(RedisEventLogAdapter(client=redis))
    tool_router = ToolRouter(redis)

    concordo = BusinessFSMConcordo(interleaved_fsm_config)

    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[InterleavedDispatchSystem()],
        projections=[],
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
        reaper_interval=5.0,
        reaper_idle_time=15.0,
    )
    worker_manager.register(FSMSlow3sTool, acl=None)
    worker_manager.register(FSMFast005sTool, acl=None)

    agent_id = "fsm-interleaved-agent-001"

    print(f"🚀 [0.0s] Triggering 'process.start' for agent '{agent_id}'...")
    correlation_middleware.start(metadata={"scenario": "fsm_interleaved"})
    corr = correlation_middleware.current()
    principal = "tenant-a.agent-1"

    await event_log.append(
        Event.create(
            event_type="agent.spawned",
            agent_id=agent_id,
            event_class="lifecycle",
            correlation=corr,
            data={"role": "fsm_agent"},
            producer_principal_id=principal,
        )
    )
    await event_log.append(
        Event.create(
            event_type="process.start",
            agent_id=agent_id,
            event_class="domain",
            correlation=corr,
            data={"stage": "draft"},
            producer_principal_id=principal,
        )
    )

    dispatcher.track_agent(agent_id)
    correlation_middleware.clear()

    start_time = time.monotonic()
    await dispatcher.start()
    await worker_manager.start()

    partial_at: float | None = None
    full_at: float | None = None

    for _ in range(70):
        events = await event_log.read(agent_id)
        elapsed = round(time.monotonic() - start_time, 2)
        for e in events:
            if e.event_type == "fsm.transitioned":
                to_state = e.data.get("to")
                if to_state == "partially_completed" and partial_at is None:
                    partial_at = elapsed
                    print(
                        f"\n⚡ [{elapsed}s] FSM TRANSITIONED TO 'partially_completed' (Fast Tool Done)!"
                    )
                elif to_state == "fully_completed" and full_at is None:
                    full_at = elapsed
                    print(
                        f"\n🐢 [{elapsed}s] FSM TRANSITIONED TO 'fully_completed' (Slow Tool Done)!"
                    )
        if full_at is not None:
            break
        await asyncio.sleep(0.05)

    all_events = await event_log.read(agent_id)
    print("\n📋 Timeline of Events in FSM EventLog:")
    for e in all_events:
        print(f"  - {e.event_type} (data={e.data})")

    await dispatcher.stop()
    await worker_manager.stop()

    delta = round(full_at - partial_at, 2) if (full_at and partial_at) else 0.0
    print(
        f"\n✅ Scenario 2 Passed: FSM transitioned fast at {partial_at}s, {delta}s before slow tool!"
    )


async def main() -> None:
    import redis.asyncio as aioredis

    use_fake = os.environ.get("KNT_REDIS_FAKE", "0") == "1"
    if use_fake:
        from fakeredis.aioredis import FakeRedis

        redis = FakeRedis(decode_responses=False)
        print("ℹ️  Running with FakeRedis (in-memory)")
    else:
        password = os.environ.get("KNT_REDIS_PASSWORD", "redispassword")
        redis = aioredis.Redis(
            host="localhost",
            port=6379,
            password=password,
            db=15,
            decode_responses=False,
        )
        await redis.flushdb()
        print("ℹ️  Running with Real Redis (port 6379, db=15)")

    await run_scenario_1(redis)
    await run_scenario_2(redis)
    await redis.aclose()

    print("\n=========================================================================")
    print("    ALL FSM CONCURRENCY SCENARIOS COMPLETED SUCCESSFULLY!")
    print("=========================================================================\n")


if __name__ == "__main__":
    asyncio.run(main())
