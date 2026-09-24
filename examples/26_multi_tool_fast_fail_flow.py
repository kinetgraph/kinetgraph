# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 26: Multi-Tool Parallel Execution with Fast Failure (ADR-069).

Demonstrates a workflow where an entry event ('process.started') triggers 3 parallel tool requests:
  1. 'tool.fast_success_tool.requested' (completes in ~0s with Ok)
  2. 'tool.fast_failure_tool.requested' (fails in ~0s with Err)
  3. 'tool.slow_10s_tool.requested'     (takes 10s to complete)

Behavior:
  - Upon receiving 'process.started', the reactive system emits all 3 tool request events.
  - Tool 1 (fast success) completes almost instantly.
  - Tool 2 (fast failure) fails almost instantly.
  - The workflow supervisor reacts immediately to 'tool.fast_failure_tool.failed' and emits 'process.failed'.
  - The process status transitions to 'failed' at ~0.1s, failing fast WITHOUT waiting for Tool 3 (10s) to finish.

Run with:
  KNT_REDIS_FAKE=1 uv run python examples/26_multi_tool_fast_fail_flow.py
Or with real Redis (port 6379):
  uv run python examples/26_multi_tool_fast_fail_flow.py
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, replace
from typing import Any

from kntgraph.core.event import Event, correlation_middleware
from kntgraph.core.result import Err, Ok, Result, ToolError
from kntgraph.core.world import DomainComponent, World, domain_component
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog
from kntgraph.tools import tool_worker
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter

# ---------------------------------------------------------------------------
# Domain Component & Projection
# ---------------------------------------------------------------------------


@domain_component("process.started")
@dataclass(frozen=True, slots=True)
class ProcessDomainComponent(DomainComponent):
    """Domain component representing process state across parallel tool executions."""

    status: str = "pending"  # "pending" | "processing" | "failed" | "completed"
    failed_reason: str = ""


class ProcessProjection:
    """Projects 'process.dispatched' and 'process.failed' events into ProcessDomainComponent."""

    def __call__(self, world: World, events: list[Event]) -> World:
        new_views = dict(world.views)
        changed = False

        for event in events:
            agent_id = event.agent_id
            view = new_views.get(agent_id)
            if view is None:
                continue

            comp = view.get_component(ProcessDomainComponent)
            if comp is None:
                continue

            if event.event_type == "process.dispatched" and comp.status != "processing":
                new_comp = ProcessDomainComponent(
                    status="processing",
                    failed_reason=comp.failed_reason,
                )
                new_components = dict(view.components)
                new_components[ProcessDomainComponent] = new_comp
                new_views[agent_id] = replace(view, components=new_components)
                changed = True

            elif event.event_type == "process.failed" and comp.status != "failed":
                reason = str(event.data.get("reason", "unknown"))
                new_comp = ProcessDomainComponent(
                    status="failed",
                    failed_reason=reason,
                )
                new_components = dict(view.components)
                new_components[ProcessDomainComponent] = new_comp
                new_views[agent_id] = replace(view, components=new_components)
                changed = True

        if not changed:
            return world

        new_storage = world.storage
        for agent_id, view in new_views.items():
            if world.views.get(agent_id) is not view:
                new_storage = new_storage.clone_with_entity(
                    agent_id, dict(view.components)
                )

        return World(tick=world.tick, storage=new_storage, views=new_views)


# ---------------------------------------------------------------------------
# Tool Worker Definitions
# ---------------------------------------------------------------------------


@tool_worker(name="fast_success_tool", max_concurrency=10, retries=0)
class FastSuccessTool:
    """Tool 1: Fast tool that completes successfully (~0s)."""

    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Ok({"status": "fast_success_completed"})


@tool_worker(name="fast_failure_tool", max_concurrency=10, retries=0)
class FastFailureTool:
    """Tool 2: Fast tool that fails instantly with Err (~0s)."""

    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Err(ToolError("Simulated fast tool failure"))


@tool_worker(name="slow_10s_tool", max_concurrency=10, retries=0)
class Slow10sTool:
    """Tool 3: Slow tool that takes 10 seconds to execute."""

    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        print("  ⏱️  [Slow10sTool] Starting 10-second background execution...")
        await asyncio.sleep(10.0)
        print("  ⏱️  [Slow10sTool] 10-second background execution finished!")
        return Ok({"status": "slow_10s_completed"})


# ---------------------------------------------------------------------------
# Reactive System (Orchestrator)
# ---------------------------------------------------------------------------


class MultiToolWorkflowSystem:
    """Reactive system that emits 3 parallel tool requests on 'process.started'
    and fails fast on the first tool failure."""

    def __call__(self, world: World) -> list[Event]:
        out: list[Event] = []
        for agent_id, view in world.query_agents(ProcessDomainComponent):
            comp = view.get_component(ProcessDomainComponent)
            if comp is None:
                continue

            last_type = view.domain_phase
            if last_type is None:
                continue

            # 1. Entry event 'process.started': emit 3 tool request events simultaneously
            if last_type == "process.started" and comp.status == "pending":
                corr = correlation_middleware.current()
                principal = "tenant-a.agent-1"
                out.extend(
                    [
                        Event.create(
                            event_type="tool.fast_success_tool.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "tool_1_fast_success"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="tool.fast_failure_tool.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "tool_2_fast_failure"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="tool.slow_10s_tool.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "tool_3_slow_10s"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="process.dispatched",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"status": "processing"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                    ]
                )

            # 2. Fast-fail reaction: if any tool fails, fail the workflow immediately
            elif last_type.endswith(".failed") and comp.status != "failed":
                corr = correlation_middleware.current()
                out.append(
                    Event.create(
                        event_type="process.failed",
                        agent_id=agent_id,
                        event_class="domain",
                        data={
                            "status": "failed",
                            "reason": f"Workflow failed fast due to tool failure: {last_type}",
                        },
                        correlation=corr,
                        producer_principal_id="tenant-a.agent-1",
                    )
                )

        return out


# ---------------------------------------------------------------------------
# Execution Driver
# ---------------------------------------------------------------------------


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

    event_log = EventLog(RedisEventLogAdapter(client=redis))
    tool_router = ToolRouter(redis)

    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[MultiToolWorkflowSystem()],
        projections=[ProcessProjection()],
        redis=redis,
        tool_router=tool_router,
        poll_interval=0.01,
        rediscovery_interval_seconds=0.1,
        heartbeat_interval_seconds=0.0,
    )

    worker_manager = WorkerManager(
        redis=redis,
        event_log=event_log,
        reaper_interval=0.1,
        reaper_idle_time=0.1,
    )

    worker_manager.register(FastSuccessTool, acl=None)
    worker_manager.register(FastFailureTool, acl=None)
    worker_manager.register(Slow10sTool, acl=None)

    agent_id = "proc-multi-tool-001"

    # Seed entry event
    print(
        f"\n🚀 [0.0s] Seeding entry event 'process.started' for agent '{agent_id}'..."
    )
    correlation_middleware.start(metadata={"flow": "3_tool_test"})
    await event_log.append(
        Event.create(
            event_type="agent.spawned",
            agent_id=agent_id,
            event_class="lifecycle",
            correlation=correlation_middleware.current(),
            data={"role": "multi_tool_agent"},
        )
    )
    await event_log.append(
        Event.create(
            event_type="process.started",
            agent_id=agent_id,
            event_class="domain",
            correlation=correlation_middleware.current(),
            data={"status": "pending"},
        )
    )
    dispatcher.track_agent(agent_id)
    correlation_middleware.clear()

    start_time = time.monotonic()
    await dispatcher.start()
    await worker_manager.start()

    print("\n⏳ Monitoring event log timeline...")
    failed_at: float | None = None

    for _ in range(40):  # Poll for up to 4s to demonstrate fast failure
        events = await event_log.read(agent_id)
        types = [e.event_type for e in events]
        elapsed = round(time.monotonic() - start_time, 2)

        if "process.failed" in types and failed_at is None:
            failed_at = elapsed
            fail_event = next(e for e in events if e.event_type == "process.failed")
            print(
                f"\n💥 [{elapsed}s] WORKFLOW FAILED FAST!"
                f"\n    Event: {fail_event.event_type}"
                f"\n    Reason: {fail_event.data.get('reason')}"
            )

        if failed_at is not None and elapsed >= (failed_at + 1.0):
            break

        await asyncio.sleep(0.1)

    all_events = await event_log.read(agent_id)
    print("\n📋 Timeline of emitted events:")
    for e in all_events:
        print(f"  - {e.event_type} (data={e.data})")

    await dispatcher.stop()
    await worker_manager.stop()
    await redis.aclose()

    print("\n=========================================================================")
    print(
        f"  SUCCESS: Workflow failed fast in {failed_at}s without waiting for 10s tool!"
    )
    print("=========================================================================\n")


if __name__ == "__main__":
    asyncio.run(main())
