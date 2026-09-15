# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for WorkflowSaga with 3 tools where step 2 fails due to timeout (ADR-069 §4).

Scenario:
  Saga has 3 sequential tool steps:
    1. 'reserve_payment' (tool='payment_reserver', compensate_tool='payment_releaser')
    2. 'lock_inventory' (tool='inventory_locker', compensate_tool='inventory_unlocker', timeout_ms=3000)
    3. 'schedule_shipping' (tool='shipping_scheduler', compensate_tool='shipping_canceller')

Flow:
  - Step 1 ('reserve_payment') completes successfully.
  - Step 2 ('lock_inventory') times out.
  - Step 3 ('schedule_shipping') is NEVER executed.
  - Saga enters compensation and dispatches Step 1's compensate_tool ('payment_releaser') to roll back Step 1.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from fakeredis.aioredis import FakeRedis

from kntgraph.concordos import ConcordoCatalog
from kntgraph.concordos.saga import (
    SagaConfig,
    SagaProgressComponent,
    SagaProjection,
    SagaStepConfig,
    SagaSystem,
    WorkflowSagaConcordo,
)
from kntgraph.core.event import Event, correlation_middleware
from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.result import Err, Ok, Result, ToolError
from kntgraph.core.world.components import ToolCallCompletion, ToolCallRequest
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog
from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system
from kntgraph.tools import tool_worker
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter

pytestmark = [pytest.mark.asyncio]

FIXED_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _three_tool_saga_config() -> SagaConfig:
    """Build SagaConfig with 3 tools where step 2 has timeout_ms=3000."""
    return SagaConfig(
        name="order_fulfillment",
        saga_timeout_ms=60_000,
        fail_when=None,
        steps=(
            SagaStepConfig(
                name="reserve_payment",
                tool_name="payment_reserver",
                compensate_tool="payment_releaser",
                timeout_ms=10_000,
            ),
            SagaStepConfig(
                name="lock_inventory",
                tool_name="inventory_locker",
                compensate_tool="inventory_unlocker",
                timeout_ms=3_000,
            ),
            SagaStepConfig(
                name="schedule_shipping",
                tool_name="shipping_scheduler",
                compensate_tool="shipping_canceller",
                timeout_ms=10_000,
            ),
        ),
    )


# ===========================================================================
# 1. Pure World / System Behavior Test
# ===========================================================================


async def test_saga_step2_timeout_prevents_step3_and_triggers_step1_compensation() -> None:
    """Pure World test asserting step 2 timeout skips step 3 and compensates step 1."""
    corr = CorrelationContext.new()
    config = _three_tool_saga_config()
    system = SagaSystem(config, now=lambda: FIXED_NOW)

    req_eid = str(uuid4())

    # Build agent view where Step 1 is completed and Step 2 receives timed_out completion
    view = (
        AgentViewBuilder("agent-3-tools")
        .with_component(
            SagaProgressComponent(
                saga_id="saga-3tool-001",
                saga_name="order_fulfillment",
                current_step="lock_inventory",
                direction="forward",
                step_order=("reserve_payment", "lock_inventory", "schedule_shipping"),
                step_states={
                    "reserve_payment": "completed",
                    "lock_inventory": "timed_out",
                    "schedule_shipping": "pending",
                },
                step_results={
                    "reserve_payment": {"reservation_id": "res-999"},
                    "lock_inventory": {},
                },
                compensate_stack=("reserve_payment", "lock_inventory"),
                started_at=FIXED_NOW,
            )
        )
        .with_tool_request(
            ToolCallRequest(
                request_event_id=req_eid,
                tool_name="inventory_locker",
                agent_id="agent-3-tools",
                params={},
                requested_at=FIXED_NOW,
                expires_at=FIXED_NOW + timedelta(seconds=300),
            )
        )
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                status="timed_out",
                error="inventory_lock_ttl_expired",
            ),
        )
        .with_trigger("tool.inventory_locker.timed_out")
        .build()
    )

    world = WorldBuilder().with_agent(view).build()
    out_events = run_system(system, world, correlation=corr)
    event_types = [e.event_type for e in out_events]

    # Verify: Step 3 (shipping_scheduler) is NEVER requested
    assert "tool.shipping_scheduler.requested" not in event_types, (
        "Step 3 tool was requested despite step 2 timing out!"
    )

    # Verify: Step 1 compensation tool (payment_releaser) IS requested
    assert "tool.payment_releaser.requested" in event_types, (
        "Step 1 compensation tool (payment_releaser) was NOT requested on step 2 timeout!"
    )

    # Verify data passed to payment_releaser contains step 1 results
    comp_event = next(e for e in out_events if e.event_type == "tool.payment_releaser.requested")
    assert comp_event.data.get("reservation_id") == "res-999"


# ===========================================================================
# 2. End-to-End ReactiveDispatcher Integration Test with Tool Workers
# ===========================================================================


@tool_worker(name="payment_reserver", max_concurrency=10, retries=0)
class PaymentReserverTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Ok({"reservation_id": "res-101", "amount": 250})


@tool_worker(name="payment_releaser", max_concurrency=10, retries=0)
class PaymentReleaserTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Ok({"status": "payment_released", "reservation_id": kwargs.get("reservation_id")})


@tool_worker(name="inventory_locker", max_concurrency=10, retries=0)
class InventoryLockerTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        # Simulate timeout failure in step 2
        return Err(ToolError("inventory_lock_timeout"))


@tool_worker(name="shipping_scheduler", max_concurrency=10, retries=0)
class ShippingSchedulerTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        pytest.fail("Step 3 (ShippingSchedulerTool) should NEVER be invoked when Step 2 times out!")


async def test_e2e_saga_3tools_step2_timeout_scenario() -> None:
    """End-to-end integration test with ReactiveDispatcher & Workers."""
    redis = FakeRedis(decode_responses=False)
    event_log = EventLog(RedisEventLogAdapter(client=redis))
    tool_router = ToolRouter(redis)

    saga_cfg = _three_tool_saga_config()
    concordo = WorkflowSagaConcordo(saga_cfg)

    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[],
        projections=[],
        redis=redis,
        tool_router=tool_router,
        poll_interval=0.01,
        rediscovery_interval_seconds=0.1,
        heartbeat_interval_seconds=0.0,
    )
    ConcordoCatalog(concordo).install_all(dispatcher)

    worker_manager = WorkerManager(
        redis=redis,
        event_log=event_log,
        reaper_interval=5.0,
        reaper_idle_time=15.0,
    )
    worker_manager.register(PaymentReserverTool, acl=None)
    worker_manager.register(PaymentReleaserTool, acl=None)
    worker_manager.register(InventoryLockerTool, acl=None)
    worker_manager.register(ShippingSchedulerTool, acl=None)

    agent_id = "agent-saga-3tools-timeout"

    correlation_middleware.start(metadata={"test": "saga_3tools_timeout"})
    corr = correlation_middleware.current()
    principal = "tenant-a.agent-1"

    await event_log.append(
        Event.create(
            event_type="agent.spawned",
            agent_id=agent_id,
            event_class="lifecycle",
            correlation=corr,
            data={"role": "saga_agent"},
            producer_principal_id=principal,
        )
    )
    await event_log.append(
        Event.create(
            event_type="saga.order_fulfillment.started",
            agent_id=agent_id,
            event_class="domain",
            correlation=corr,
            data={"saga_id": "saga-order-888"},
            producer_principal_id=principal,
        )
    )

    dispatcher.track_agent(agent_id)
    correlation_middleware.clear()

    await dispatcher.start()
    await worker_manager.start()

    compensated_event = None
    releaser_completed = None

    for _ in range(120):
        events = await event_log.read(agent_id)
        for e in events:
            if e.event_type == "tool.payment_releaser.completed":
                releaser_completed = e
            if e.event_type in ("saga.order_fulfillment.failed", "saga.order_fulfillment.compensated"):
                compensated_event = e

        if releaser_completed is not None:
            break
        await asyncio.sleep(0.05)

    all_events = await event_log.read(agent_id)
    await dispatcher.stop()
    await worker_manager.stop()
    event_types = [e.event_type for e in all_events]

    # Verify Step 1 requested & completed
    assert "tool.payment_reserver.requested" in event_types
    assert "tool.payment_reserver.completed" in event_types

    # Verify Step 2 requested & failed/timed out
    assert "tool.inventory_locker.requested" in event_types
    assert "tool.inventory_locker.failed" in event_types or "tool.inventory_locker.timed_out" in event_types

    # Verify Step 3 was NEVER requested
    assert "tool.shipping_scheduler.requested" not in event_types

    # Verify Step 1 compensation (payment_releaser) was requested & completed
    assert "tool.payment_releaser.requested" in event_types
    assert releaser_completed is not None
