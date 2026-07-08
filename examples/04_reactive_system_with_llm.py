# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
04 — Calling an LLM from inside a reactive system.

Demonstrates how to bridge the `LiteLLMTool` with the
framework's event-sourced world:

  1. A reactive system `plan_after_validation` reacts to a
     `task.received` event by inspecting the World.
  2. The system calls a `PlannerRole` (which uses the
     LiteLLMTool under the hood).
  3. The result is emitted back as a `*.planned` domain
     event in the EventLog.
  4. The ReactiveDispatcher appends the new event.

The LLM call itself is a side effect, but the SYSTEM
remains pure: it takes `World` → `list[Event]` (ADR-018).
The tool call is wrapped in a system; the framework
handles the I/O via the standard Tool / EventLog path.

Configuration is loaded from env / `.env`. Requires Redis
running on localhost:6379.

Run:
    docker run -d -p 6379:6379 --name fmh-redis redis
    python examples/04_reactive_system_with_llm.py
"""

from __future__ import annotations

import asyncio
import redis.asyncio as aioredis

from kntgraph.core.event import Event, correlation_middleware
from kntgraph.core.world import World
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog
from kntgraph.stream.projection import fold_world

from kntgraph.agents.config import LLMConfig, load_env
from kntgraph.agents.roles import PlannerRole, Plan
from kntgraph.agents.tools import LiteLLMTool


TASK_ID = "task-001"


# A reactive system that calls an LLM role. The system
# signature is `World -> list[Event]` (ADR-018) — it is a
# pure function of the World, but the LLM call is a side
# effect that we model as "produce events based on external
# knowledge". The system inspects the World's components
# (here: the seeded `task.received` payload stored on the
# `task.received` component) to decide what to do.
async def plan_after_validation(
    world: World, planner: PlannerRole
) -> list[Event]:
    """
    For every tracked agent that has a `task.received`
    component but no `task.planned` yet, ask the planner
    for a plan. Emits `task.planned` on success or
    `task.planning_failed` on error.

    The triggering `task.received` is reconstructed from
    the World component payload (immutable; ADR-018).
    """
    out: list[Event] = []
    for agent_id, view in world.agents.items():
        received = view.components.get("task.received")
        planned = view.components.get("task.planned")
        failed = view.components.get("task.planning_failed")
        if not received or planned or failed:
            continue
        # Reconstruct a synthetic `Event` envelope so the
        # role's downstream `Event.domain_from(...)` calls
        # can `continue_from` it for ADR-037 correlation.
        trigger = Event.domain_from(
            agent_id=agent_id,
            type="task.received",
            data=received,
            correlation=correlation_middleware.current(),
        )
        result = await planner.plan(
            task=received.get("description", ""),
            context=received.get("context"),
            # qwen3.5 is a thinking model: skip reasoning so
            # the answer lands in `content` instead of
            # `reasoning`.
            think=False,
        )
        if result.is_err():
            out.append(
                Event.domain_from(
                    agent_id=agent_id,
                    type="task.planning_failed",
                    data={"error": str(result.err_value())},
                    correlation=correlation_middleware.continue_from(trigger),
                )
            )
            continue
        plan: Plan = result.unwrap()
        out.append(
            Event.domain_from(
                agent_id=agent_id,
                type="task.planned",
                data={
                    "goal": plan.goal,
                    "steps": [s.model_dump() for s in plan.steps],
                    "rationale": plan.rationale,
                },
                correlation=correlation_middleware.continue_from(trigger),
            )
        )
    return out


async def main() -> None:
    load_env()
    cfg = LLMConfig.from_env()
    if cfg.default_model == "gpt-4o-mini":
        cfg = LLMConfig(
            default_model="ollama/qwen3.5:4b",
            rate_limit_rpm=cfg.rate_limit_rpm,
            cost_budget_per_hour_usd=cfg.cost_budget_per_hour_usd,
            timeout_s=60.0,
        )

    redis = aioredis.from_url("redis://localhost:6379")
    log = EventLog(RedisEventLogAdapter(client=redis))
    llm = LiteLLMTool(
        default_model=cfg.default_model,
        rate_limiter=cfg.rate_limiter(),
        cost_budget=cfg.cost_budget(),
        timeout_s=cfg.timeout_s,
    )
    planner = PlannerRole(llm=llm)

    dispatcher = ReactiveDispatcher(
        log,
        systems=[lambda world: plan_after_validation(world, planner)],
        poll_interval=0.5,
        redis=redis,
    )
    with correlation_middleware.scope(
        metadata={"example": "04", "task_id": TASK_ID}
    ):
        await log.append(
            Event.domain_from(
                agent_id=TASK_ID,
                type="task.received",
                data={
                    "description": "Migrar monolito para microsserviços",
                    "context": "Time de 5 devs, 3 meses de prazo",
                },
                correlation=correlation_middleware.current(),
            )
        )
        n = await dispatcher.dispatch_once()
    print(f"dispatched {n} event(s)")

    # Inspect the resulting events
    world = await fold_world(log)
    view = world.agents[TASK_ID]
    print(f"domain_phase: {view.domain_phase}")
    if view.domain_phase == "task.planned":
        steps = view.components.get("task.planned", {}).get("steps", [])
        print(f"plan steps: {len(steps)}")
        for s in steps:
            print(f"  - {s['name']}: {s['description']}")

    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
