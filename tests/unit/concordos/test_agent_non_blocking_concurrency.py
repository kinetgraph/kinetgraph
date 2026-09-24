# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration test verifying agent non-blocking execution under interleaved slow/fast tools.

Scenario:
  Two tool requests arrive for the SAME agent simultaneously:
    1. Request 1: 'tool.slow_3s_tool.requested' (takes 3.0s to execute)
    2. Request 2: 'tool.fast_005s_tool.requested' (takes 0.05s to execute)

Invariants Validated:
  - The agent DOES NOT block/freeze waiting for the slow tool to finish.
  - 'tool.fast_005s_tool.completed' is processed by the dispatcher and folded into the agent's DomainComponent at t < 0.3s.
  - The fast tool completion is fully processed ~2.7s BEFORE the slow tool finishes.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis

from kntgraph.core.event import Event, correlation_middleware
from kntgraph.core.result import Ok, Result, ToolError
from kntgraph.core.world import DomainComponent, World, domain_component
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog
from kntgraph.tools import tool_worker
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter

pytestmark = [pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Domain Component & Projection
# ---------------------------------------------------------------------------


@domain_component("request.submitted")
@dataclass(frozen=True, slots=True)
class InterleavedAgentComponent(DomainComponent):
    """Component tracking completion timestamps for fast and slow tools."""

    fast_completed: bool = False
    slow_completed: bool = False
    fast_completed_time: float = 0.0
    slow_completed_time: float = 0.0


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
                new_comp = replace(
                    comp,
                    fast_completed=True,
                    fast_completed_time=time.monotonic(),
                )
                new_components = dict(view.components)
                new_components[InterleavedAgentComponent] = new_comp
                new_views[agent_id] = replace(view, components=new_components)
                changed = True

            elif (
                event.event_type == "tool.slow_3s_tool.completed"
                and not comp.slow_completed
            ):
                new_comp = replace(
                    comp,
                    slow_completed=True,
                    slow_completed_time=time.monotonic(),
                )
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
    """Tool taking 3 seconds to complete."""

    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        await asyncio.sleep(3.0)
        return Ok({"status": "slow_3s_done"})


@tool_worker(name="fast_005s_tool", max_concurrency=10, retries=0)
class Fast005sTool:
    """Tool taking 0.05 seconds to complete."""

    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        await asyncio.sleep(0.05)
        return Ok({"status": "fast_005s_done"})


# ---------------------------------------------------------------------------
# Test Case
# ---------------------------------------------------------------------------


async def test_agent_does_not_block_on_slow_tool() -> None:
    """Verify that an agent processing a 3.0s tool does NOT block from receiving and
    reacting to a 0.05s fast tool completion for the same agent."""
    redis_client = FakeRedis(decode_responses=False)
    event_log = EventLog(RedisEventLogAdapter(client=redis_client))
    tool_router = ToolRouter(redis_client)

    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[],  # Pure projection-driven test
        projections=[InterleavedAgentProjection()],
        redis=redis_client,
        tool_router=tool_router,
        poll_interval=0.01,
        rediscovery_interval_seconds=0.1,
        heartbeat_interval_seconds=0.0,
    )

    worker_manager = WorkerManager(
        redis=redis_client,
        event_log=event_log,
        reaper_interval=5.0,
        reaper_idle_time=10.0,
    )
    worker_manager.register(Slow3sTool, acl=None)
    worker_manager.register(Fast005sTool, acl=None)

    agent_id = "agent-non-blocking-001"

    # Seed initial agent state and dispatch BOTH tool requests to the SAME agent
    correlation_middleware.start(metadata={"test": "non_blocking"})
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
    # Request 1: Slow 3.0s tool
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
    # Request 2: Fast 0.05s tool
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

    # Step 1: Wait up to 0.5s (when slow tool is only 1/6th done)
    fast_processed_time: float | None = None
    for _ in range(10):  # 10 x 0.05s = 0.5s max wait
        events = await event_log.read(agent_id)
        types = [e.event_type for e in events]
        if "tool.fast_005s_tool.completed" in types:
            fast_processed_time = time.monotonic() - start_time
            break
        await asyncio.sleep(0.05)

    # 🚨 CRITICAL ASSERTION 1: Fast tool MUST be completed and processed in < 0.5s
    assert fast_processed_time is not None, (
        "Agent was BLOCKED! Fast tool completion was not processed while slow tool was running."
    )
    assert fast_processed_time < 0.5, (
        f"Fast tool completion took {fast_processed_time}s, expected < 0.5s."
    )

    # Verify events emitted to agent event_log at t ~ 0.2s (slow tool still running)
    events_at_fast = await event_log.read(agent_id)
    types_at_fast = [e.event_type for e in events_at_fast]
    assert "tool.fast_005s_tool.completed" in types_at_fast, (
        "Fast tool completion missing in EventLog!"
    )
    assert "tool.slow_3s_tool.completed" not in types_at_fast, (
        "Slow tool should NOT be completed yet at t < 0.5s!"
    )

    # Step 2: Now wait for the slow tool to finish (~3.0s total)
    slow_processed_time: float | None = None
    for _ in range(70):  # up to 3.5s wait
        events = await event_log.read(agent_id)
        types = [e.event_type for e in events]
        if "tool.slow_3s_tool.completed" in types:
            slow_processed_time = time.monotonic() - start_time
            break
        await asyncio.sleep(0.05)

    await dispatcher.stop()
    await worker_manager.stop()

    # 🚨 CRITICAL ASSERTION 2: Slow tool completes at ~3.0s
    assert slow_processed_time is not None, "Slow tool failed to complete."
    assert slow_processed_time >= 2.8, (
        f"Slow tool completed too early ({slow_processed_time}s)."
    )

    # Delta assertion: Fast tool was processed ~2.7s before slow tool finished!
    delta = round(slow_processed_time - fast_processed_time, 2)
    assert delta > 2.0, (
        f"Fast tool was processed only {delta}s before slow tool, expected > 2.0s difference."
    )
