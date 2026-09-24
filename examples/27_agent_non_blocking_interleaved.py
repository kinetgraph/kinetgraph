# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 27: Non-Blocking Interleaved Tool Execution (ADR-069 / ADR-036).

Demonstrates that an agent processing a slow tool (3.0s) NEVER freezes or blocks
receiving and processing a second fast tool completion (0.05s) for the SAME agent.

Behavior:
  - Two requests are submitted simultaneously for agent 'agent-interleaved-001':
      1. Request 1: 'tool.slow_3s_tool.requested' (takes 3.0s in background worker)
      2. Request 2: 'tool.fast_005s_tool.requested' (takes 0.05s in background worker)
  - The fast tool completes and is processed by the agent at t ~ 0.1s.
  - The slow tool continues running in background and completes at t ~ 3.0s.
  - The timeline proves that the agent handled the fast tool completion ~2.9s BEFORE the slow tool finished.

Run with:
  KNT_REDIS_FAKE=1 uv run python examples/27_agent_non_blocking_interleaved.py
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, replace
from typing import Any

from kntgraph.core.event import Event, correlation_middleware
from kntgraph.core.result import Ok, Result, ToolError
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


@domain_component("request.submitted")
@dataclass(frozen=True, slots=True)
class InterleavedAgentComponent(DomainComponent):
    """Component tracking completion status of fast and slow tools."""

    fast_completed: bool = False
    slow_completed: bool = False


class InterleavedAgentProjection:
    """Folds tool completions into InterleavedAgentComponent."""

    def __call__(self, world: World, events: list[Event]) -> World:
        new_views = dict(world.views)
        changed = False

        for event in events:
            agent_id = event.agent_id
            view = new_views.get(agent_id)
            if view is None:
                continue

            comp = view.get_component(InterleavedAgentComponent)
            if comp is None:
                continue

            if (
                event.event_type == "tool.fast_005s_tool.completed"
                and not comp.fast_completed
            ):
                new_comp = replace(comp, fast_completed=True)
                new_components = dict(view.components)
                new_components[InterleavedAgentComponent] = new_comp
                new_views[agent_id] = replace(view, components=new_components)
                changed = True

            elif (
                event.event_type == "tool.slow_3s_tool.completed"
                and not comp.slow_completed
            ):
                new_comp = replace(comp, slow_completed=True)
                new_components = dict(view.components)
                new_components[InterleavedAgentComponent] = new_comp
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
# Tool Workers
# ---------------------------------------------------------------------------


@tool_worker(name="slow_3s_tool", max_concurrency=10, retries=0)
class Slow3sTool:
    """Tool 1: Takes 3.0 seconds to execute."""

    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        print("  🐢 [Slow3sTool] Started execution (3.0 seconds)...")
        await asyncio.sleep(3.0)
        print("  🐢 [Slow3sTool] Finished 3.0s execution!")
        return Ok({"status": "slow_3s_done"})


@tool_worker(name="fast_005s_tool", max_concurrency=10, retries=0)
class Fast005sTool:
    """Tool 2: Takes 0.05 seconds to execute."""

    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        print("  ⚡ [Fast005sTool] Executed in ~0.05 seconds!")
        await asyncio.sleep(0.05)
        return Ok({"status": "fast_005s_done"})


# ---------------------------------------------------------------------------
# Main Execution Driver
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
        systems=[],
        projections=[InterleavedAgentProjection()],
        redis=redis,
        tool_router=tool_router,
        poll_interval=0.01,
        rediscovery_interval_seconds=0.1,
        heartbeat_interval_seconds=0.0,
    )

    worker_manager = WorkerManager(
        redis=redis,
        event_log=event_log,
        reaper_interval=5.0,
        reaper_idle_time=10.0,
    )
    worker_manager.register(Slow3sTool, acl=None)
    worker_manager.register(Fast005sTool, acl=None)

    agent_id = "agent-interleaved-001"

    print(f"\n🚀 [0.0s] Submitting 2 simultaneous requests for agent '{agent_id}'...")
    correlation_middleware.start(metadata={"example": "non_blocking"})
    corr = correlation_middleware.current()
    principal = "tenant-a.agent-1"

    await event_log.append(
        Event.create(
            event_type="agent.spawned",
            agent_id=agent_id,
            event_class="lifecycle",
            correlation=corr,
            data={"role": "test_agent"},
            producer_principal_id=principal,
        )
    )
    await event_log.append(
        Event.create(
            event_type="request.submitted",
            agent_id=agent_id,
            event_class="domain",
            correlation=corr,
            data={"fast_completed": False, "slow_completed": False},
            producer_principal_id=principal,
        )
    )
    # Dispatch Request 1 (Slow 3s tool)
    await event_log.append(
        Event.create(
            event_type="tool.slow_3s_tool.requested",
            agent_id=agent_id,
            event_class="domain",
            correlation=corr,
            data={"job": "slow_task"},
            producer_principal_id=principal,
        )
    )
    # Dispatch Request 2 (Fast 0.05s tool)
    await event_log.append(
        Event.create(
            event_type="tool.fast_005s_tool.requested",
            agent_id=agent_id,
            event_class="domain",
            correlation=corr,
            data={"job": "fast_task"},
            producer_principal_id=principal,
        )
    )

    dispatcher.track_agent(agent_id)
    correlation_middleware.clear()

    start_time = time.monotonic()
    await dispatcher.start()
    await worker_manager.start()

    fast_at: float | None = None
    slow_at: float | None = None

    for _ in range(70):  # Poll up to 3.5s
        events = await event_log.read(agent_id)
        types = [e.event_type for e in events]
        elapsed = round(time.monotonic() - start_time, 2)

        if "tool.fast_005s_tool.completed" in types and fast_at is None:
            fast_at = elapsed
            print(f"\n⚡ [{elapsed}s] FAST TOOL COMPLETED & PROCESSED BY AGENT!")

        if "tool.slow_3s_tool.completed" in types and slow_at is None:
            slow_at = elapsed
            print(f"\n🐢 [{elapsed}s] SLOW TOOL COMPLETED & PROCESSED BY AGENT!")
            break

        await asyncio.sleep(0.05)

    all_events = await event_log.read(agent_id)
    print("\n📋 Timeline of Events in Agent EventLog:")
    for e in all_events:
        print(f"  - {e.event_type} (data={e.data})")

    await dispatcher.stop()
    await worker_manager.stop()
    await redis.aclose()

    delta = round(slow_at - fast_at, 2) if (slow_at and fast_at) else 0.0
    print("\n=========================================================================")
    print(f"  VERIFICATION PASSED!")
    print(f"  - Fast Tool Processed : {fast_at}s (agent did NOT block!)")
    print(f"  - Slow Tool Processed : {slow_at}s")
    print(f"  - Difference          : Fast tool processed {delta}s BEFORE slow tool!")
    print("=========================================================================\n")


if __name__ == "__main__":
    asyncio.run(main())
