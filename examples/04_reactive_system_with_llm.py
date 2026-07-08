# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
04 — Calling an LLM from inside a reactive system.

Demonstrates how to bridge the `LiteLLMTool` with the
framework's event-sourced world:

  1. A reactive system `plan_after_validation` reacts to
     a domain event.
  2. The system calls a `PlannerRole` (which uses the
     LiteLLMTool under the hood).
  3. The result is emitted back as a `*.planned` domain
     event in the EventLog.
  4. The ReactiveDispatcher appends the new event.

The LLM call itself is a side effect, but the SYSTEM
remains pure: it takes (world, event) → list[Event]. The
tool call is wrapped in a system; the framework
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

from kntgraph.core.event import Event
from kntgraph.core.world import World
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog
from kntgraph.stream.projection import fold_world

from kntgraph.agents.config import LLMConfig, load_env
from kntgraph.agents.roles import PlannerRole, Plan
from kntgraph.agents.tools import LiteLLMTool


# A reactive system that calls an LLM role. The system
# signature is (World, Event) -> list[Event] — it is a pure
# function of its inputs, but the LLM call is a side effect
# that we model as "produce events based on external knowledge".
async def plan_after_validation(
    world: World, event: Event, planner: PlannerRole
) -> list[Event]:
    """
    Reacts to 'task.received' by asking the planner for a
    plan. Emits 'task.planned' on success or
    'task.planning_failed' on error.
    """
    if event.event_type != "task.received":
        return []
    result = await planner.plan(
        task=event.data.get("description", ""),
        context=event.data.get("context"),
        # qwen3.5 is a thinking model: skip reasoning so
        # the answer lands in `content` instead of
        # `reasoning`.
        think=False,
    )
    if result.is_err():
        return [
            Event.domain_from(
                agent_id=event.agent_id,
                type="task.planning_failed",
                data={"error": str(result.err_value())},
                causation_id=event.event_id,
            )
        ]
    plan: Plan = result.unwrap()
    return [
        Event.domain_from(
            agent_id=event.agent_id,
            type="task.planned",
            data={
                "goal": plan.goal,
                "steps": [s.model_dump() for s in plan.steps],
                "rationale": plan.rationale,
            },
            causation_id=event.event_id,
        )
    ]


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

    # Seed: a task received event
    task_id = "task-001"
    await log.append(
        Event.domain_from(
            agent_id=task_id,
            type="task.received",
            data={
                "description": "Migrar monolito para microsserviços",
                "context": "Time de 5 devs, 3 meses de prazo",
            },
        )
    )

    # Bind the role into the system via closure
    async def system(world: World, event: Event) -> list[Event]:
        return await plan_after_validation(world, event, planner)

    dispatcher = ReactiveDispatcher(log, systems=[system], poll_interval=0.5)
    n = await dispatcher.dispatch_once()
    print(f"dispatched {n} event(s)")

    # Inspect the resulting events
    world = await fold_world(log)
    view = world.agents[task_id]
    print(f"domain_phase: {view.domain_phase}")
    if view.domain_phase == "task.planned":
        steps = view.components.get("task.planned", {}).get("steps", [])
        print(f"plan steps: {len(steps)}")
        for s in steps:
            print(f"  - {s['name']}: {s['description']}")

    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
