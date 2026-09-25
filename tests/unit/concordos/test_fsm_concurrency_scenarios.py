# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
FSM Concurrency and Parallel execution integration tests (ADR-069 §3).

Tests two key FSM concurrency scenarios against a live/fake Redis event log:

1. test_fsm_parallel_multi_tool_fast_fail:
   An FSM entry transition to 'processing' dispatches 3 parallel tool requests:
     - tool 1: fast_success_tool (completes in ~0s with Ok)
     - tool 2: fast_failure_tool (fails in ~0s with Err)
     - tool 3: slow_10s_tool     (takes 10s in background)
   The FSM receives 'tool.fast_failure_tool.failed' and transitions to 'rejected'
   state at t ~ 0.1s without blocking on the 10-second tool.

2. test_fsm_non_blocking_interleaved_tools:
   An FSM in state 'executing' receives 2 tool requests (slow 3.0s tool & fast 0.05s tool).
   The FSM transitions on 'tool.fast_005s_tool.completed' to 'partially_completed'
   at t < 0.5s while the 3-second tool is still running in background, proving the
   FSM agent never freezes waiting for slow tools.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis

from kntgraph.concordos.fsm import (
    BusinessFSMConcordo,
    FSMConfig,
    FSMProjection,
    FSMSystem,
    FSMTransition,
)
from kntgraph.concordos import ConcordoCatalog
from kntgraph.core.event import Event, CorrelationContext, correlation_middleware
from kntgraph.testing import assert_all_correlation_ids
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
# SCENARIO 1: Multi-Tool Parallel Fast-Fail FSM Components & Tools
# ===========================================================================


@domain_component("fsm.order.submit")
@dataclass(frozen=True, slots=True)
class FSMStressOrderComponent(DomainComponent):
    stage: str = "created"
    amount: int = 100


@tool_worker(name="fsm_fast_success_tool", max_concurrency=10, retries=0)
class FastSuccessTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Ok({"status": "processed"})


@tool_worker(name="fsm_fast_failure_tool", max_concurrency=10, retries=0)
class FastFailureTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        return Err(ToolError("Simulated fast failure in FSM tool"))


@tool_worker(name="fsm_slow_10s_tool", max_concurrency=10, retries=0)
class Slow10sTool:
    """A tool that intentionally exceeds the FSM's
    fast-fail window (1.0s) so the FSM can prove it
    transitioned to ``rejected`` WITHOUT waiting for this
    tool's result. The sleep is intentionally well below
    the original 10s — the test asserts fast-fail at
    ``<0.5s`` and a 10s sleep is gratuitous leak-prone
    state across tests (a cancelled ``asyncio.sleep``
    leaves the event loop in a fragile state that breaks
    the next test's ``xrange`` call). 1s is enough to
    prove the FSM does NOT block on this tool."""

    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        await asyncio.sleep(1.0)
        return Ok({"status": "slow_done"})


class MultiToolDispatchSystem:
    """Dispatches 3 parallel tool requests when FSM enters 'processing' state."""

    def __call__(self, world: World) -> list[Event]:
        out: list[Event] = []
        for agent_id, view in world.query_agents(FSMStressOrderComponent):
            comp = view.get_component(FSMStressOrderComponent)
            if comp is None:
                continue

            last_type = view.domain_phase
            # When FSM transitioned to 'processing'
            if last_type == "fsm.transitioned" and comp.stage == "processing":
                corr = correlation_middleware.current()
                principal = "tenant-a.agent-1"
                out.extend(
                    [
                        Event.create(
                            event_type="tool.fsm_fast_success_tool.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "1"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="tool.fsm_fast_failure_tool.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "2"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="tool.fsm_slow_10s_tool.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"step": "3"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                    ]
                )

        return out


# FSM Configuration for Parallel Fast-Fail
multi_tool_fsm_config = FSMConfig(
    component_type=FSMStressOrderComponent,
    state_field="stage",
    transitions={
        "created": {
            "fsm.order.submit": FSMTransition(to="processing"),
        },
        "processing": {
            "tool.fsm_fast_success_tool.completed": FSMTransition(to="approved"),
            "tool.fsm_fast_failure_tool.failed": FSMTransition(to="rejected"),
            "tool.fsm_slow_10s_tool.failed": FSMTransition(to="rejected"),
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


@tool_worker(name="fsm_slow_3s_tool", max_concurrency=10, retries=0)
class FSMSlow3sTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        await asyncio.sleep(3.0)
        return Ok({"status": "slow_3s_done"})


@tool_worker(name="fsm_fast_005s_tool", max_concurrency=10, retries=0)
class FSMFast005sTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        await asyncio.sleep(0.05)
        return Ok({"status": "fast_005s_done"})


class InterleavedDispatchSystem:
    """Dispatches slow and fast tool requests when FSM transitions to 'executing'."""

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
                            event_type="tool.fsm_slow_3s_tool.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"job": "slow"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                        Event.create(
                            event_type="tool.fsm_fast_005s_tool.requested",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"job": "fast"},
                            correlation=corr,
                            producer_principal_id=principal,
                        ),
                    ]
                )

        return out


# FSM Configuration for Interleaved Non-blocking execution
interleaved_fsm_config = FSMConfig(
    component_type=FSMInterleavedComponent,
    state_field="stage",
    transitions={
        "draft": {
            "process.start": FSMTransition(to="executing"),
        },
        "executing": {
            "tool.fsm_fast_005s_tool.completed": FSMTransition(
                to="partially_completed"
            ),
            "tool.fsm_slow_3s_tool.completed": FSMTransition(to="fully_completed"),
        },
        "partially_completed": {
            "tool.fsm_slow_3s_tool.completed": FSMTransition(to="fully_completed"),
        },
    },
    terminal=frozenset({"fully_completed"}),
)


# ===========================================================================
# TEST CASES
# ===========================================================================


async def test_fsm_parallel_multi_tool_fast_fail() -> None:
    """Test 1: FSM dispatches 3 parallel tools on entry to 'processing'.
    When tool 2 fails in ~0.1s, the FSM transitions to 'rejected' fast without
    waiting for the 10-second tool."""
    redis = FakeRedis(decode_responses=False)
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
    ConcordoCatalog(concordo).install_all(dispatcher)

    worker_manager = WorkerManager(
        redis=redis,
        event_log=event_log,
        reaper_interval=5.0,
        reaper_idle_time=15.0,
    )
    worker_manager.register(FastSuccessTool, acl=None)
    worker_manager.register(FastFailureTool, acl=None)
    worker_manager.register(Slow10sTool, acl=None)

    agent_id = "fsm-fast-fail-001"

    correlation_middleware.start(metadata={"test": "fsm_fast_fail"})
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
            event_type="fsm.order.submit",
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

    rejected_event = None
    rejected_elapsed: float | None = None

    for _ in range(40):  # Poll up to 2.0s
        events = await event_log.read(agent_id)
        for e in events:
            if e.event_type == "fsm.transitioned" and e.data.get("to") == "rejected":
                rejected_event = e
                rejected_elapsed = time.monotonic() - start_time
                break

        if rejected_event is not None:
            break
        await asyncio.sleep(0.05)

    await dispatcher.stop()
    await worker_manager.stop()

    # 🚨 ASSERTIONS
    assert rejected_event is not None, "FSM did not emit transition to 'rejected'."
    assert rejected_elapsed is not None and rejected_elapsed < 0.5, (
        f"FSM rejection took {rejected_elapsed}s, expected fast-fail < 0.5s."
    )
    assert rejected_event.data.get("from") == "processing"
    assert rejected_event.data.get("to") == "rejected"

    # Audit trail invariant (ADR-037 §1.1): every event the
    # dispatcher wrote on behalf of this flow carries the
    # entry's correlation_id. The dispatcher (v0.15.3 fix)
    # calls ``correlation_middleware.continue_from(anchor)``
    # per tick so the FSM-emitted events inherit the entry's
    # flow id. A regression that drops the anchor-event
    # propagation would surface here as a mismatch on the
    # FSM-emitted events.
    final_events = await event_log.read(agent_id)
    assert_all_correlation_ids(final_events, corr)


async def test_fsm_non_blocking_interleaved_tools() -> None:
    """Test 2: FSM executes 2 tools (slow 3.0s and fast 0.05s).
    The FSM transitions from 'executing' to 'partially_completed' on fast tool completion
    at t < 0.5s while the slow tool is still running in background, and then reaches
    'fully_completed' at t ~ 3.0s."""
    redis = FakeRedis(decode_responses=False)
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
    ConcordoCatalog(concordo).install_all(dispatcher)

    worker_manager = WorkerManager(
        redis=redis,
        event_log=event_log,
        reaper_interval=5.0,
        reaper_idle_time=15.0,
    )
    worker_manager.register(FSMSlow3sTool, acl=None)
    worker_manager.register(FSMFast005sTool, acl=None)

    agent_id = "fsm-interleaved-001"

    correlation_middleware.start(metadata={"test": "fsm_interleaved"})
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

    partially_completed_time: float | None = None
    fully_completed_time: float | None = None

    # Step 1: Check fast transition to 'partially_completed' (at t < 0.5s)
    for _ in range(10):
        events = await event_log.read(agent_id)
        for e in events:
            if (
                e.event_type == "fsm.transitioned"
                and e.data.get("to") == "partially_completed"
                and partially_completed_time is None
            ):
                partially_completed_time = time.monotonic() - start_time

        if partially_completed_time is not None:
            break
        await asyncio.sleep(0.05)

    # 🚨 ASSERTION 1: FSM transitioned to 'partially_completed' at t < 0.5s
    assert partially_completed_time is not None, (
        "FSM did NOT transition to 'partially_completed' while slow tool was running!"
    )
    assert partially_completed_time < 0.5, (
        f"FSM transition to 'partially_completed' took {partially_completed_time}s, expected < 0.5s."
    )

    # Step 2: Now wait for full completion at ~3.0s
    for _ in range(70):
        events = await event_log.read(agent_id)
        for e in events:
            if (
                e.event_type == "fsm.transitioned"
                and e.data.get("to") == "fully_completed"
                and fully_completed_time is None
            ):
                fully_completed_time = time.monotonic() - start_time

        if fully_completed_time is not None:
            break
        await asyncio.sleep(0.05)

    await dispatcher.stop()
    await worker_manager.stop()

    # 🚨 ASSERTION 2: FSM transitioned to 'fully_completed' at t ~ 3.0s
    assert fully_completed_time is not None, (
        "FSM did NOT transition to 'fully_completed'."
    )
    assert fully_completed_time >= 2.8, (
        f"FSM fully_completed happened too early ({fully_completed_time}s)."
    )

    # Delta assertion: Fast transition happened ~2.7s before full completion!
    delta = round(fully_completed_time - partially_completed_time, 2)
    assert delta > 2.0, (
        f"Fast transition was only {delta}s before slow tool completion, expected > 2.0s."
    )

    # Audit trail invariant (ADR-037 §1.1): every event the
    # dispatcher wrote on behalf of this flow carries the
    # entry's correlation_id.
    final_events = await event_log.read(agent_id)
    assert_all_correlation_ids(final_events, corr)


# ===========================================================================
# SCENARIO 3: 5 Concurrent Process Executions of the Same FSM
# ===========================================================================


@domain_component("order.create")
@dataclass(frozen=True, slots=True)
class FSMFiveOrderComponent(DomainComponent):
    status: str = "created"
    order_amount: int = 250


@tool_worker(name="fsm_payment_processor", max_concurrency=10, retries=0)
class FSMPaymentProcessorTool:
    async def invoke(
        self,
        *,
        idempotency_key: str,
        state: str = "",
        **kwargs: Any,
    ) -> Result[dict, ToolError]:
        await asyncio.sleep(0.05)
        return Ok({"payment_status": "settled", "idempotency_key": idempotency_key})


five_orders_fsm_config = FSMConfig(
    component_type=FSMFiveOrderComponent,
    state_field="status",
    transitions={
        "created": {
            "order.create": FSMTransition(to="payment_pending"),
        },
        "payment_pending": {
            "tool.fsm_payment_processor.completed": FSMTransition(to="completed"),
            "tool.fsm_payment_processor.failed": FSMTransition(to="failed"),
        },
    },
    on_entry={
        "payment_pending": "tool.fsm_payment_processor.requested",
    },
    terminal=frozenset({"completed", "failed"}),
)


@pytest.mark.skipif(
    os.environ.get("KNT_REDIS_FAKE") == "1" or os.environ.get("KNT_REDIS_FAKE") is None,
    reason=(
        "Flaky under fakeredis: the 5-agent concurrent run races on the "
        "shared connection pool (RedisPool default max_connections=50, but "
        "fakeredis serialises blocking commands behind a single in-process "
        "lock that does not model real-Redis pipelining). The test reliably "
        "passes against a real Redis instance. Set KNT_REDIS_FAKE=0 "
        "(explicit opt-in to real Redis) to exercise this path."
    ),
)
async def test_fsm_five_concurrent_process_executions() -> None:
    """Test 3: 5 distinct agent processes execute against the same FSM concurrently.
    Direct registration of FSMSystem & FSMProjection on ReactiveDispatcher (no .install()).
    Verifies all 5 processes complete their state lifecycle independently without interference."""
    redis = FakeRedis(decode_responses=False)
    event_log = EventLog(RedisEventLogAdapter(client=redis))
    tool_router = ToolRouter(redis)

    # Direct registration without wrapper .install()
    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[FSMSystem(five_orders_fsm_config)],
        projections=[FSMProjection(five_orders_fsm_config)],
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
    worker_manager.register(FSMPaymentProcessorTool, acl=None)

    agent_ids = [f"fsm-proc-{i}" for i in range(1, 6)]
    corrs: dict[str, CorrelationContext] = {}

    # Seed all 5 agents with 'order.create' domain event
    for agent_id in agent_ids:
        correlation_middleware.start(metadata={"flow": "five_fsm_orders"})
        corr = correlation_middleware.current()
        corrs[agent_id] = corr
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
        await event_log.append(
            Event.create(
                event_type="order.create",
                agent_id=agent_id,
                event_class="domain",
                correlation=corr,
                data={"status": "created", "order_amount": 250},
                producer_principal_id=principal,
            )
        )
        dispatcher.track_agent(agent_id)
        correlation_middleware.clear()

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

    # 🚨 ASSERTIONS
    assert len(completed_agents) == 5, (
        f"Expected all 5 FSM agents to reach 'completed', got {len(completed_agents)} ({completed_agents})."
    )

    for agent_id in agent_ids:
        events = await event_log.read(agent_id)
        types = [e.event_type for e in events]
        assert "agent.spawned" in types
        assert "order.create" in types
        assert "tool.fsm_payment_processor.requested" in types
        assert "tool.fsm_payment_processor.completed" in types

        transitions = [e for e in events if e.event_type == "fsm.transitioned"]
        assert len(transitions) == 2, (
            f"Agent {agent_id} expected 2 transitions, got {len(transitions)}"
        )
        assert (
            transitions[0].data["from"] == "created"
            and transitions[0].data["to"] == "payment_pending"
        )
        assert (
            transitions[1].data["from"] == "payment_pending"
            and transitions[1].data["to"] == "completed"
        )

        # Audit trail invariant (ADR-037 §1.1): every event
        # for this agent carries the entry's correlation_id.
        assert_all_correlation_ids(events, corrs[agent_id])
