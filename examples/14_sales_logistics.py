# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
14 — Per-stream isolation via composed `agent_id`s.

Demonstrates that each `agent_id` corresponds to its own
Redis Streams key: agents that share a `tenant_id` but
differ on the `order_id` end up with distinct streams,
distinct checkpoints, and distinct fold-views of the
World. The dispatcher fans events out across the
per-agent streams and each agent's `World -> list[Event]`
system sees ONLY its own events.

The demo seeds `order.requested` for two different
orders belonging to the same tenant. A `sales` system
reacts to the request and emits `order.created`. A
`logistics` system (registered for the same agent_id
prefix) reacts to `order.created` and emits
`shipping.scheduled`. The systems are pure: they inspect
the World's per-agent `AgentView` and emit based on the
agent's current state (ADR-018).

Scenario 3 exercises the cross-stream isolation: a
change to order 1 does NOT leak into order 2's World,
even though both agents are registered with the same
dispatcher and the EventLog is shared.

Pre-requisites
--------------

  - Redis on localhost:6379 (default).
  - Set ``FMH_REDIS_FAKE=1`` for in-process Redis.

Run
---

    python examples/14_sales_logistics.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from kntgraph.core.event import Event, correlation_middleware
from kntgraph.core.world import World
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog
from kntgraph.stream.projection import fold_world

sys.path.insert(0, str(Path(__file__).parent))
from _lib.redis_or_fake import make_redis_client  # noqa: E402


TENANT_ID = "tenant-1"
ORDER_1 = "order-001"
ORDER_2 = "order-002"


def _banner(title: str) -> None:
    line = "=" * 70
    print()
    print(line)
    print(title)
    print(line)


# ---------------------------------------------------------------------------
# Agent-id composition
# ---------------------------------------------------------------------------


def sales_agent_id(tenant_id: str, order_id: str) -> str:
    return f"sales:{tenant_id}:{order_id}"


def logistics_agent_id(tenant_id: str, order_id: str) -> str:
    return f"logistics:{tenant_id}:{order_id}"


# ---------------------------------------------------------------------------
# Systems: World -> list[Event]
# ---------------------------------------------------------------------------


async def sales(world: World) -> list[Event]:
    """
    For every tracked sales agent in the World whose
    `domain_phase` is `order.requested`, emit
    `order.created`.
    """
    out: list[Event] = []
    for agent_id, view in world.agents.items():
        if not agent_id.startswith("sales:"):
            continue
        if view.domain_phase != "order.requested":
            continue
        requested = view.components.get("order.requested") or {}
        # Synthetic trigger for ADR-037 correlation.
        trigger = Event.domain_from(
            agent_id=agent_id,
            type="order.requested",
            data=requested,
            correlation=correlation_middleware.current(),
        )
        print(
            f"  [sales {agent_id}] received order.requested "
            f"qty={requested.get('qty')} product={requested.get('product')}"
        )
        out.append(
            Event.domain_from(
                agent_id=agent_id,
                type="order.created",
                data={
                    "order_id": requested.get("order_id"),
                    "qty": requested.get("qty"),
                    "product": requested.get("product"),
                },
                correlation=correlation_middleware.continue_from(trigger),
            )
        )
    return out


async def logistics(world: World) -> list[Event]:
    """
    For every tracked logistics agent in the World whose
    `domain_phase` is `order.created`, emit
    `shipping.scheduled`.
    """
    out: list[Event] = []
    for agent_id, view in world.agents.items():
        if not agent_id.startswith("logistics:"):
            continue
        if view.domain_phase != "order.created":
            continue
        created = view.components.get("order.created") or {}
        # Synthetic trigger for ADR-037 correlation.
        trigger = Event.domain_from(
            agent_id=agent_id,
            type="order.created",
            data=created,
            correlation=correlation_middleware.current(),
        )
        print(
            f"  [logistics {agent_id}] scheduling shipping "
            f"for order {created.get('order_id')}"
        )
        out.append(
            Event.domain_from(
                agent_id=agent_id,
                type="shipping.scheduled",
                data={
                    "order_id": created.get("order_id"),
                    "carrier": "Correios",
                    "eta_days": 5,
                },
                causation_id=created.get("order_id"),
                correlation=correlation_middleware.continue_from(trigger),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    _banner("14 — Per-stream isolation via composed agent_ids")

    redis = make_redis_client()
    log = EventLog(RedisEventLogAdapter(client=redis))

    dispatcher = ReactiveDispatcher(
        log,
        systems=[sales, logistics],
        poll_interval=0.5,
        redis=redis,
    )

    s1 = sales_agent_id(TENANT_ID, ORDER_1)
    s2 = sales_agent_id(TENANT_ID, ORDER_2)
    l1 = logistics_agent_id(TENANT_ID, ORDER_1)
    l2 = logistics_agent_id(TENANT_ID, ORDER_2)
    for agent_id in (s1, s2, l1, l2):
        dispatcher.track_agent(agent_id)

    correlation_middleware.start(metadata={"example": "14"})

    try:
        # --- Scenario 1: first order ---
        _banner("Scenario 1: first order, 2 units")
        await log.append(
            Event.domain_from(
                agent_id=s1,
                type="order.requested",
                data={"order_id": ORDER_1, "qty": 2, "product": "widget"},
                correlation=correlation_middleware.current(),
            )
        )
        n = await dispatcher.dispatch_once()
        print(f"  tick 1: {n} event(s) (sales creates the order)")
        n = await dispatcher.dispatch_once()
        print(f"  tick 2: {n} event(s) (logistics schedules shipping)")

        # --- Scenario 2: second order ---
        _banner("Scenario 2: second order, 3 units")
        await log.append(
            Event.domain_from(
                agent_id=s2,
                type="order.requested",
                data={"order_id": ORDER_2, "qty": 3, "product": "gadget"},
                correlation=correlation_middleware.current(),
            )
        )
        n = await dispatcher.dispatch_once()
        print(f"  tick 1: {n} event(s) (sales for order 2)")
        n = await dispatcher.dispatch_once()
        print(f"  tick 2: {n} event(s) (logistics for order 2)")

        # --- Scenario 3: change order 1; verify order 2 is isolated ---
        _banner("Scenario 3: change order 1 → order 2's World is unaffected")
        await log.append(
            Event.domain_from(
                agent_id=s1,
                type="order.requested",
                data={"order_id": ORDER_1, "qty": 5, "product": "widget"},
                correlation=correlation_middleware.current(),
            )
        )
        await dispatcher.dispatch_once()
        final_world = await fold_world(log)
        v1 = final_world.agents.get(s1)
        v2 = final_world.agents.get(s2)
        # ``view.components`` keeps the LATEST domain event
        # payload. After the change, order 1's last domain
        # event is the new `order.created`; order 2 still
        # reflects its own last domain event.
        v1_qty = v1.components.get("order.created", {}).get("qty")
        v2_qty = v2.components.get("order.created", {}).get("qty")
        print(f"  {s1}: phase={v1.domain_phase!r} qty={v1_qty}")
        print(f"  {s2}: phase={v2.domain_phase!r} qty={v2_qty}")
        assert v1_qty == 5, "order 1 should reflect the change"
        assert v2_qty == 3, "order 2 must NOT see the change to order 1"

        # --- Final view ---
        _banner("Final World (4 agents, isolated streams)")
        for agent_id in sorted(final_world.agents):
            v = final_world.agents[agent_id]
            print(
                f"  {agent_id}: phase={v.domain_phase!r} "
                f"components={list(v.components.keys())}"
            )
    finally:
        correlation_middleware.clear()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
