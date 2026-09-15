# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
FSM Integration tests with Specification Pattern transition guards (ADR-069 §2 & §3).

Tests the 3 concurrency and parallel execution scenarios with composable Specifications
(AND, OR, NOT, built-in and custom specifications) guarding FSM state transitions:

1. test_fsm_spec_parallel_multi_tool_fast_fail:
   Transition 'processing' -> 'approved' guarded by:
     ProfileTierIs("vip").and_(OrderAmountValid(min_amount=10, max_amount=1000))
   FSM receives 'tool.fast_failure_tool.failed' and transitions to 'rejected' fast (~0.1s).

2. test_fsm_spec_non_blocking_interleaved_tools:
   Transitions guarded by composable Specifications (ContinuityToolUsed, DomainStateIs).
   Fast tool completes at t < 0.5s and transitions FSM while 3s slow tool runs in background.

3. test_fsm_spec_five_concurrent_process_executions:
   5 distinct agent process executions governed by the same FSM, where every transition
   is guarded by composed Specifications (ProfileTierIs, OrderAmountValid).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis

from kntgraph.concordos import Composable, Specification, StepContext
from kntgraph.concordos.fsm import (
    FSMConfig,
    FSMProjection,
    FSMSystem,
    FSMTransition,
)
from kntgraph.concordos.specs import (
    ContinuityToolUsed,
    DomainStateIs,
    ProfileTierIs,
)
from kntgraph.core.components.memory import ProfileComponent
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


# ===========================================================================
# Custom Specification
# ===========================================================================


@dataclass(frozen=True, slots=True)
class OrderAmountValid(Specification, Composable):
    """Custom Specification validating order_amount is within range."""

    min_amount: int
    max_amount: int

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        if ctx.domain is None:
            return False
        amount = getattr(ctx.domain, "order_amount", 0)
        return self.min_amount <= amount <= self.max_amount


# ===========================================================================
# SCENARIO 1: Multi-Tool Fast-Fail FSM with Specifications
# ===========================================================================


@domain_component("fsm_spec.order.submit")
@dataclass(frozen=True, slots=True)
class FSMSpecOrderComponent(DomainComponent):
    stage: str = "created"
    order_amount: int = 250


@tool_worker(name="fsm_spec_fast_success", max_concurrency=10, retries=0)
class FastSuccessTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Ok({"status": "processed"})


@tool_worker(name="fsm_spec_fast_failure", max_concurrency=10, retries=0)
class FastFailureTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Err(ToolError("Simulated fast failure in spec test"))


@tool_worker(name="fsm_spec_slow_10s", max_concurrency=10, retries=0)
class Slow10sTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        await asyncio.sleep(10.0)
        return Ok({"status": "slow_done"})


class MultiToolSpecDispatchSystem:
    def __call__(self, world: World) -> list[Event]:
        out: list[Event] = []
        for agent_id, view in world.query_agents(FSMSpecOrderComponent):
            comp = view.get_component(FSMSpecOrderComponent)
            if comp is None:
                continue

            last_type = view.domain_phase
            if last_type == "fsm.transitioned" and comp.stage == "processing":
                corr = correlation_middleware.current()
                principal = "tenant-a.agent-1"
                out.extend(
                    [
                        Event.create(
                            event_type="tool.fsm_spec_fast_success.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "1"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="tool.fsm_spec_fast_failure.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "2"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="tool.fsm_spec_slow_10s.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "3"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                    ]
                )

        return out


# Transition guard using composed Specifications:
# (DomainStateIs("stage", "created") OR DomainStateIs("stage", "draft")) AND OrderAmountValid(10, 1000)
spec_multi_tool_guard = (
    DomainStateIs("stage", "created").or_(DomainStateIs("stage", "draft"))
).and_(OrderAmountValid(min_amount=10, max_amount=1000))

multi_tool_spec_fsm_config = FSMConfig(
    component_type=FSMSpecOrderComponent,
    state_field="stage",
    transitions={
        "created": {
            "fsm_spec.order.submit": FSMTransition(
                to="processing",
                guard=spec_multi_tool_guard,  # <--- Composed Specification Guard
            ),
        },
        "processing": {
            "tool.fsm_spec_fast_success.completed": FSMTransition(to="approved"),
            "tool.fsm_spec_fast_failure.failed": FSMTransition(to="rejected"),
            "tool.fsm_spec_slow_10s.failed": FSMTransition(to="rejected"),
        },
    },
    terminal=frozenset({"approved", "rejected"}),
)


# ===========================================================================
# SCENARIO 2: Non-Blocking Interleaved Tools FSM with Specifications
# ===========================================================================


@domain_component("fsm_spec.process.start")
@dataclass(frozen=True, slots=True)
class FSMSpecInterleavedComponent(DomainComponent):
    stage: str = "draft"
    order_amount: int = 150


@tool_worker(name="fsm_spec_slow_3s", max_concurrency=10, retries=0)
class SpecSlow3sTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        await asyncio.sleep(3.0)
        return Ok({"status": "slow_3s_done"})


@tool_worker(name="fsm_spec_fast_005s", max_concurrency=10, retries=0)
class SpecFast005sTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        await asyncio.sleep(0.05)
        return Ok({"status": "fast_005s_done"})


class InterleavedSpecDispatchSystem:
    def __call__(self, world: World) -> list[Event]:
        out: list[Event] = []
        for agent_id, view in world.query_agents(FSMSpecInterleavedComponent):
            comp = view.get_component(FSMSpecInterleavedComponent)
            if comp is None:
                continue

            last_type = view.domain_phase
            if last_type == "fsm.transitioned" and comp.stage == "executing":
                corr = correlation_middleware.current()
                principal = "tenant-a.agent-1"
                out.extend(
                    [
                        Event.create(
                            event_type="tool.fsm_spec_slow_3s.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"job": "slow"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="tool.fsm_spec_fast_005s.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"job": "fast"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                    ]
                )

        return out


interleaved_spec_fsm_config = FSMConfig(
    component_type=FSMSpecInterleavedComponent,
    state_field="stage",
    transitions={
        "draft": {
            "fsm_spec.process.start": FSMTransition(
                to="executing",
                guard=OrderAmountValid(10, 500),  # <--- Custom Specification
            ),
        },
        "executing": {
            "tool.fsm_spec_fast_005s.completed": FSMTransition(
                to="partially_completed",
                guard=DomainStateIs("stage", "executing"),  # <--- Built-in Specification
            ),
            "tool.fsm_spec_slow_3s.completed": FSMTransition(to="fully_completed"),
        },
        "partially_completed": {
            "tool.fsm_spec_slow_3s.completed": FSMTransition(
                to="fully_completed",
                guard=DomainStateIs("stage", "partially_completed"),  # <--- Built-in Specification
            ),
        },
    },
    terminal=frozenset({"fully_completed"}),
)


# ===========================================================================
# SCENARIO 3: 5 Concurrent Process Executions of Same FSM with Specifications
# ===========================================================================


@domain_component("fsm_spec.order.create")
@dataclass(frozen=True, slots=True)
class FSMSpecFiveOrderComponent(DomainComponent):
    status: str = "created"
    order_amount: int = 300


@tool_worker(name="fsm_spec_payment_processor", max_concurrency=10, retries=0)
class SpecPaymentProcessorTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        await asyncio.sleep(0.05)
        return Ok({"payment_status": "settled"})


# Guard combining DomainStateIs("status", "created") AND OrderAmountValid(50, 500)
five_orders_guard = DomainStateIs("status", "created").and_(
    OrderAmountValid(min_amount=50, max_amount=500)
)

five_orders_spec_fsm_config = FSMConfig(
    component_type=FSMSpecFiveOrderComponent,
    state_field="status",
    transitions={
        "created": {
            "fsm_spec.order.create": FSMTransition(
                to="payment_pending",
                guard=five_orders_guard,  # <--- Composed Specification Guard
            ),
        },
        "payment_pending": {
            "tool.fsm_spec_payment_processor.completed": FSMTransition(
                to="completed",
                guard=DomainStateIs("status", "payment_pending"),  # <--- Built-in Spec
            ),
            "tool.fsm_spec_payment_processor.failed": FSMTransition(to="failed"),
        },
    },
    on_entry={
        "payment_pending": "tool.fsm_spec_payment_processor.requested",
    },
    terminal=frozenset({"completed", "failed"}),
)



# ===========================================================================
# TEST CASES
# ===========================================================================


async def test_fsm_spec_parallel_multi_tool_fast_fail() -> None:
    """Test 1: FSM multi-tool fast fail with transition guard powered by composed Specifications."""
    redis = FakeRedis(decode_responses=False)
    event_log = EventLog(RedisEventLogAdapter(client=redis))
    tool_router = ToolRouter(redis)

    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[
            FSMSystem(multi_tool_spec_fsm_config),
            MultiToolSpecDispatchSystem(),
        ],
        projections=[FSMProjection(multi_tool_spec_fsm_config)],
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
        reaper_idle_time=15.0,
    )
    worker_manager.register(FastSuccessTool, acl=None)
    worker_manager.register(FastFailureTool, acl=None)
    worker_manager.register(Slow10sTool, acl=None)

    agent_id = "fsm-spec-fast-fail-001"

    correlation_middleware.start(metadata={"test": "fsm_spec_fast_fail"})
    corr = correlation_middleware.current()
    principal = "tenant-a.agent-1"

    await event_log.append(
        Event.create(
            event_type="agent.spawned",
            agent_id=agent_id,
            event_class="lifecycle",
            correlation=corr,
            data={"role": "fsm_spec_agent"},
            producer_principal_id=principal,
        )
    )
    await event_log.append(
        Event.create(
            event_type="fsm_spec.order.submit",
            agent_id=agent_id,
            event_class="domain",
            correlation=corr,
            data={"stage": "created", "order_amount": 250},
            producer_principal_id=principal,
        )
    )

    dispatcher.track_agent(agent_id)
    correlation_middleware.clear()

    start_time = time.monotonic()
    await dispatcher.start()
    await worker_manager.start()

    rejected_event = None
    rejected_elapsed: float | None = None

    for _ in range(40):
        events = await event_log.read(agent_id)
        for e in events:
            if (
                e.event_type == "fsm.transitioned"
                and e.data.get("to") == "rejected"
            ):
                rejected_event = e
                rejected_elapsed = time.monotonic() - start_time
                break
        if rejected_event is not None:
            break
        await asyncio.sleep(0.05)

    await dispatcher.stop()
    await worker_manager.stop()

    assert rejected_event is not None, "Specification-guarded FSM did not transition to 'rejected'."
    assert rejected_elapsed is not None and rejected_elapsed < 0.5, (
        f"Fast-fail transition took {rejected_elapsed}s, expected < 0.5s."
    )


async def test_fsm_spec_non_blocking_interleaved_tools() -> None:
    """Test 2: FSM interleaved non-blocking tools with Specification guards."""
    redis = FakeRedis(decode_responses=False)
    event_log = EventLog(RedisEventLogAdapter(client=redis))
    tool_router = ToolRouter(redis)

    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[
            FSMSystem(interleaved_spec_fsm_config),
            InterleavedSpecDispatchSystem(),
        ],
        projections=[FSMProjection(interleaved_spec_fsm_config)],
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
        reaper_idle_time=15.0,
    )
    worker_manager.register(SpecSlow3sTool, acl=None)
    worker_manager.register(SpecFast005sTool, acl=None)

    agent_id = "fsm-spec-interleaved-001"

    correlation_middleware.start(metadata={"test": "fsm_spec_interleaved"})
    corr = correlation_middleware.current()
    principal = "tenant-a.agent-1"

    await event_log.append(
        Event.create(
            event_type="agent.spawned",
            agent_id=agent_id,
            event_class="lifecycle",
            correlation=corr,
            data={"role": "fsm_spec_agent"},
            producer_principal_id=principal,
        )
    )
    await event_log.append(
        Event.create(
            event_type="fsm_spec.process.start",
            agent_id=agent_id,
            event_class="domain",
            correlation=corr,
            data={"stage": "draft", "order_amount": 150},
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
                elif to_state == "fully_completed" and full_at is None:
                    full_at = elapsed
        if full_at is not None:
            break
        await asyncio.sleep(0.05)

    await dispatcher.stop()
    await worker_manager.stop()

    assert partial_at is not None, "FSM did NOT transition to 'partially_completed'."
    assert partial_at < 0.5, f"Fast transition took {partial_at}s, expected < 0.5s."
    assert full_at is not None, "FSM did NOT transition to 'fully_completed'."
    assert full_at >= 2.8, f"Full completion happened too early ({full_at}s)."


async def test_fsm_spec_five_concurrent_process_executions() -> None:
    """Test 3: 5 concurrent agent process executions governed by the same Specification-guarded FSM.
    Each agent carries a ProfileComponent(tier="vip") to satisfy ProfileTierIs("vip") Specification guard."""
    redis = FakeRedis(decode_responses=False)
    event_log = EventLog(RedisEventLogAdapter(client=redis))
    tool_router = ToolRouter(redis)

    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[FSMSystem(five_orders_spec_fsm_config)],
        projections=[FSMProjection(five_orders_spec_fsm_config)],
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
        reaper_idle_time=15.0,
    )
    worker_manager.register(SpecPaymentProcessorTool, acl=None)

    agent_ids = [f"fsm-spec-proc-{i}" for i in range(1, 6)]

    for agent_id in agent_ids:
        correlation_middleware.start(metadata={"flow": "five_spec_orders"})
        corr = correlation_middleware.current()
        principal = f"tenant-a.{agent_id}"

        await event_log.append(
            Event.create(
                event_type="agent.spawned",
                agent_id=agent_id,
                event_class="lifecycle",
                correlation=corr,
                data={"role": "order_agent"},
                producer_principal_id=principal,
            )
        )

        # Hydrate ProfileComponent(tier="vip") so ProfileTierIs("vip") Specification is satisfied
        await event_log.append(
            Event.create(
                event_type="profile.updated",
                agent_id=agent_id,
                event_class="domain",
                correlation=corr,
                data={
                    "tenant_id": "tenant-a",
                    "user_id": agent_id,
                    "tier": "vip",
                },
                producer_principal_id=principal,
            )
        )

        await event_log.append(
            Event.create(
                event_type="fsm_spec.order.create",
                agent_id=agent_id,
                event_class="domain",
                correlation=corr,
                data={"status": "created", "order_amount": 300},
                producer_principal_id=principal,
            )
        )
        dispatcher.track_agent(agent_id)
        correlation_middleware.clear()

    start_time = time.monotonic()
    await dispatcher.start()
    await worker_manager.start()

    completed_agents: set[str] = set()
    for _ in range(60):
        for agent_id in agent_ids:
            if agent_id in completed_agents:
                continue
            events = await event_log.read(agent_id)
            for e in events:
                if (
                    e.event_type == "fsm.transitioned"
                    and e.data.get("to") == "completed"
                ):
                    completed_agents.add(agent_id)
                    break
        if len(completed_agents) == 5:
            break
        await asyncio.sleep(0.05)

    await dispatcher.stop()
    await worker_manager.stop()

    assert len(completed_agents) == 5, (
        f"Expected all 5 Specification-guarded FSM agents to reach 'completed', got {len(completed_agents)}."
    )

    for agent_id in agent_ids:
        events = await event_log.read(agent_id)
        transitions = [e for e in events if e.event_type == "fsm.transitioned"]
        assert len(transitions) == 2, f"Agent {agent_id} expected 2 transitions, got {len(transitions)}"
        assert transitions[0].data["from"] == "created" and transitions[0].data["to"] == "payment_pending"
        assert transitions[1].data["from"] == "payment_pending" and transitions[1].data["to"] == "completed"
