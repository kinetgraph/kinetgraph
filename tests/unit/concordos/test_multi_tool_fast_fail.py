# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration test for multi-tool parallel execution with fast-fail behavior (ADR-069).

Validates that an entry event triggering 3 parallel tool requests (fast success, fast failure,
slow 10s execution) fails fast on the first tool failure (at ~0.1s) without waiting for the
10-second tool to complete.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

import pytest

from kntgraph.core.event import Event, correlation_middleware
from kntgraph.core.result import Err, Ok, Result, ToolError
from kntgraph.core.world import DomainComponent, World, domain_component
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog
from kntgraph.tools import tool_worker
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter

pytestmark = [pytest.mark.asyncio]


@domain_component("process.started")
@dataclass(frozen=True, slots=True)
class FastFailOrderComponent(DomainComponent):
    status: str = "pending"
    failed_reason: str = ""


class FastFailOrderProjection:
    def __call__(self, world: World, events: list[Event]) -> World:
        new_views = dict(world.views)
        changed = False

        for event in events:
            agent_id = event.agent_id
            view = new_views.get(agent_id)
            if view is None:
                continue

            comp = view.get_component(FastFailOrderComponent)
            if comp is None:
                continue

            if event.event_type == "process.dispatched" and comp.status != "processing":
                new_comp = FastFailOrderComponent(
                    status="processing",
                    failed_reason=comp.failed_reason,
                )
                new_components = dict(view.components)
                new_components[FastFailOrderComponent] = new_comp
                new_views[agent_id] = replace(view, components=new_components)
                changed = True

            elif event.event_type == "process.failed" and comp.status != "failed":
                reason = str(event.data.get("reason", "unknown"))
                new_comp = FastFailOrderComponent(
                    status="failed",
                    failed_reason=reason,
                )
                new_components = dict(view.components)
                new_components[FastFailOrderComponent] = new_comp
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


@tool_worker(name="fast_fail_tool_1_success", max_concurrency=10, retries=0)
class FastFailToolSuccess:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Ok({"status": "success"})


@tool_worker(name="fast_fail_tool_2_failure", max_concurrency=10, retries=0)
class FastFailToolFailure:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Err(ToolError("Simulated fast failure"))


@tool_worker(name="fast_fail_tool_3_slow", max_concurrency=10, retries=0)
class FastFailToolSlow:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        await asyncio.sleep(5.0)
        return Ok({"status": "slow_done"})


class FastFailOrchestratorSystem:
    def __call__(self, world: World) -> list[Event]:
        out: list[Event] = []
        for agent_id, view in world.query_agents(FastFailOrderComponent):
            comp = view.get_component(FastFailOrderComponent)
            if comp is None:
                continue

            last_type = view.domain_phase
            if last_type is None:
                continue

            if last_type == "process.started" and comp.status == "pending":
                corr = correlation_middleware.current()
                principal = "tenant-a.agent-1"
                out.extend(
                    [
                        Event.create(
                            event_type="tool.fast_fail_tool_1_success.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "1"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="tool.fast_fail_tool_2_failure.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "2"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="tool.fast_fail_tool_3_slow.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "3"},
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

            elif last_type.endswith(".failed") and comp.status != "failed":
                corr = correlation_middleware.current()
                out.append(
                    Event.create(
                        event_type="process.failed",
                        agent_id=agent_id,
                        event_class="domain",
                        data={
                            "status": "failed",
                            "reason": f"Workflow failed fast due to: {last_type}",
                        },
                        correlation=corr,
                        producer_principal_id="tenant-a.agent-1",
                    )
                )

        return out


async def test_multi_tool_fast_fail_pipeline() -> None:
    """Verify 3 parallel tools (1 success, 1 failure, 1 slow) fail fast immediately on failure."""
    from fakeredis.aioredis import FakeRedis

    redis_client = FakeRedis(decode_responses=False)
    event_log = EventLog(RedisEventLogAdapter(client=redis_client))
    tool_router = ToolRouter(redis_client)

    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[FastFailOrchestratorSystem()],
        projections=[FastFailOrderProjection()],
        redis=redis_client,
        tool_router=tool_router,
        poll_interval=0.01,
        rediscovery_interval_seconds=0.1,
        heartbeat_interval_seconds=0.0,
    )

    worker_manager = WorkerManager(
        redis=redis_client,
        event_log=event_log,
        reaper_interval=0.1,
        reaper_idle_time=0.1,
    )
    worker_manager.register(FastFailToolSuccess, acl=None)
    worker_manager.register(FastFailToolFailure, acl=None)
    worker_manager.register(FastFailToolSlow, acl=None)

    agent_id = "test-agent-fast-fail-001"

    correlation_middleware.start(metadata={"test": "fast_fail"})
    await event_log.append(
        Event.create(
            event_type="agent.spawned",
            agent_id=agent_id,
            event_class="lifecycle",
            correlation=correlation_middleware.current(),
            data={"role": "test_agent"},
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

    await dispatcher.start()
    await worker_manager.start()

    # Wait for process.failed to appear
    failed_event = None
    for _ in range(40):
        events = await event_log.read(agent_id)
        types = [e.event_type for e in events]
        if "process.failed" in types:
            failed_event = next(e for e in events if e.event_type == "process.failed")
            break
        await asyncio.sleep(0.05)

    await dispatcher.stop()
    await worker_manager.stop()

    assert failed_event is not None, "Expected process.failed event to be emitted fast."
    assert "fast_fail_tool_2_failure" in str(failed_event.data.get("reason"))
