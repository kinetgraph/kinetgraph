# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
14 — Multi-agent cooperation via composed agents (Sales + Logistics).

Demonstrates the construction pattern for agents built on
**composed `agent_id`s**: each agent class declares an
`agent_id_prefix`, derives its full id from a key tuple
(e.g. `(tenant_id, order_id)`), and registers its own
reactive systems. Multiple agents share the same EventLog
but each reads only its own stream.

This is the FMH-flavoured answer to the v2.0 architecture
proposal's "agent as first-class entity" (§"Agent types").
The framework gives you the substrate; this example shows
how to compose it.

Scenario
--------

  Two agents collaborate on an order workflow:

    Sales agent (agent_id = "sales:<tenant>:<order>")
      - Handles `order.requested` events
      - Emits `order.created` with the order payload

    Logistics agent (agent_id = "logistics:<tenant>:<order>")
      - Handles `order.created` events
      - Emits `logistics.processing`
      - Tracks its own state (last_qty_seen)
      - Detects inconsistency when the next `order.created`
        carries a different qty than what it is processing
      - Pauses on inconsistency (does NOT emit
        `logistics.shipped`)

Steps
-----

  1. Customer places an order for 2 units of product ABC.
     Sales agent creates `order.requested` → emits
     `order.created {qty: 2}`.

  2. Logistics agent observes, starts processing.
     Emits `logistics.processing {qty: 2, stage: "packing"}`.

  3. Customer places a SECOND order for the same address
     (3 units of product XYZ). Sales agent emits
     `order.created {qty: 3}` for a new order id.

  4. Logistics agent is STILL processing the FIRST order
     (qty=2). It sees a new `order.created` for a DIFFERENT
     order (qty=3) — but the dispatcher's stream isolation
     means this event is on the OTHER order's stream, not
     its own. So the agent's `last_qty_seen` stays at 2,
     and the inconsistency branch does NOT fire.

  5. The SAME customer (a different action) emits
     `order.requested` AGAIN for the FIRST order, this
     time with qty=3. Sales agent emits
     `order.created {qty: 3}` for the SAME order id
     (causation tracks the chain).

  6. Logistics agent observes qty=3 vs its expected qty=2 →
     emits `logistics.inconsistency_detected {expected: 2,
     observed: 3}` and pauses.

This example shows:
  - How to compose agents with `agent_id_prefix` (each
    agent owns a slice of the EventLog keyspace).
  - How to maintain per-agent state (the `last_qty_seen`
    instance variable).
  - How to detect and respond to state inconsistency
    without a separate audit agent (the audit agent is
    example 15).

Without Docker
--------------

Set `FMH_REDIS_FAKE=1` for in-process Redis.

Run:

    docker run -d -p 6379:6379 --name fmh-redis redis
    python examples/14_sales_logistics.py

    # In-process Redis:
    FMH_REDIS_FAKE=1 python examples/14_sales_logistics.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field

from kntgraph.core.world.world import World

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from kntgraph.core.event import Event  # noqa: E402
from kntgraph.core.result import Ok, Result, ToolError  # noqa: E402
from kntgraph.runner.reactive import ReactiveDispatcher  # noqa: E402
from kntgraph.infra.redis._event_log import RedisEventLogAdapter  # noqa: E402
from kntgraph.stream.event_log import EventLog  # noqa: E402
from kntgraph.agents.tools.protocol import (  # noqa: E402
    Tool,
    ToolRegistry,
)

from _lib.redis_or_fake import make_redis_client  # noqa: E402


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-1"
ORDER_1 = "order-001"
ORDER_2 = "order-002"


# ---------------------------------------------------------------------------
# Base agent
# ---------------------------------------------------------------------------


@dataclass
class Agent:
    """
    A composed agent built from `agent_id_prefix` + key tuple.

    Subclasses declare `agent_id_prefix` (str) and override
    `agent_id_for(*keys)` to compose the full id. The
    full id is the per-agent Redis Streams key — each agent
    has its own stream, isolated from the others.

    `handle(world, event)` is the entry point registered
    with the `ReactiveDispatcher`. Subclasses override it
    to react to events they care about.
    """

    agent_id: str
    state: dict = field(default_factory=dict)

    @classmethod
    def agent_id_for(cls, *keys: str) -> str:
        return cls.agent_id_prefix + ":".join(keys)

    def handle(self, world: World, event: Event) -> list[Event]:
        """
        Default dispatcher entry point. Subclasses
        override to filter by event_type.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Sales agent
# ---------------------------------------------------------------------------


class SalesAgent(Agent):
    """
    Handles `order.requested` events.

    Emits `order.created` on the agent's own stream
    (`sales:<tenant>:<order>`). The agent_id is composed
    from the (tenant, order) key tuple, so each order has
    its own dedicated stream.
    """

    agent_id_prefix = "sales:"

    def __init__(self, *, tenant_id: str, order_id: str) -> None:
        super().__init__(
            agent_id=self.agent_id_for(tenant_id, order_id),
        )
        # Cached so `handle` can filter events without
        # recomputing the id each call.
        self._order_id = order_id

    @classmethod
    def for_order(cls, tenant_id: str, order_id: str) -> "SalesAgent":
        return cls(tenant_id=tenant_id, order_id=order_id)

    def handle(self, world: World, event: Event) -> list[Event]:
        if event.event_type != "order.requested":
            return []
        # The sales agent only reacts to orders for its own
        # key (order_id). If a different `order.requested`
        # event lands on the EventLog for a different order,
        # the other sales agent (a different `agent_id`)
        # picks it up — this is exactly what stream isolation
        # buys us.
        if event.data.get("order_id") != self._order_id:
            return []
        qty = event.data.get("qty", 0)
        product = event.data.get("product", "?")
        print(
            f"  [sales {self.agent_id}] received order.requested "
            f"qty={qty} product={product}"
        )
        return [
            Event.domain_from(
                agent_id=self.agent_id,
                type="order.created",
                data={
                    "tenant_id": event.data.get("tenant_id"),
                    "order_id": event.data.get("order_id"),
                    "qty": qty,
                    "product": product,
                },
                causation_id=event.event_id,
            )
        ]


# ---------------------------------------------------------------------------
# Logistics agent
# ---------------------------------------------------------------------------


@dataclass
class LogisticsAgent(Agent):
    """
    Handles `order.created` events on its own stream.

    Tracks `state["last_qty_seen"]`. If a NEW `order.created`
    arrives with a different qty than what it is currently
    processing, emits `logistics.inconsistency_detected`
    and marks itself as `state["paused"] = True`. Will not
    emit `logistics.shipped` while paused.

    The `last_qty_seen` instance variable is the agent's
    local memory. The framework's `World` is also
    available for queries that need a fold over the
    EventLog; for per-agent working state, an instance
    attribute is the simpler choice.
    """

    agent_id_prefix = "logistics:"

    @classmethod
    def for_order(cls, tenant_id: str, order_id: str) -> "LogisticsAgent":
        return cls(
            agent_id=cls.agent_id_for(tenant_id, order_id),
            state={"last_qty_seen": None, "paused": False},
        )

    def handle(self, world, event: Event) -> list[Event]:
        if event.event_type != "order.created":
            return []
        if self.state.get("paused"):
            return []
        qty = event.data.get("qty", 0)
        last_qty = self.state.get("last_qty_seen")
        if last_qty is not None and last_qty != qty:
            # Inconsistency: same order, different qty.
            self.state["paused"] = True
            print(
                f"  [logistics {self.agent_id}] INCONSISTENCY: "
                f"expected qty={last_qty}, observed qty={qty} → paused"
            )
            return [
                Event.domain_from(
                    agent_id=self.agent_id,
                    type="logistics.inconsistency_detected",
                    data={
                        "order_id": event.data.get("order_id"),
                        "expected_qty": last_qty,
                        "observed_qty": qty,
                    },
                    causation_id=event.event_id,
                )
            ]
        # First order, or same qty — process normally.
        self.state["last_qty_seen"] = qty
        print(f"  [logistics {self.agent_id}] processing order qty={qty}")
        return [
            Event.domain_from(
                agent_id=self.agent_id,
                type="logistics.processing",
                data={
                    "order_id": event.data.get("order_id"),
                    "qty": qty,
                    "stage": "packing",
                },
                causation_id=event.event_id,
            )
        ]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class _EchoTool(Tool):
    def __init__(self, *, name: str, description: str, input_schema: dict) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema

    async def invoke(
        self, *, idempotency_key: str, **kwargs
    ) -> Result[dict, ToolError]:
        return Ok(
            {
                "tool": self.name,
                "idempotency_key": idempotency_key,
                "args": dict(kwargs),
                "status": "ok",
            }
        )


def _build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    # No tools needed for this demo — the agents do all the
    # work via events. Tools would only be needed if an
    # agent delegates to an external capability (e.g.
    # `tools.crm.create_order`).
    return registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


async def main() -> None:
    _banner("14 — Composed agents: Sales + Logistics with inconsistency")

    redis = make_redis_client()
    await redis.flushdb()
    log = EventLog(RedisEventLogAdapter(client=redis))

    # Build the two agents for two orders.
    sales_1 = SalesAgent.for_order(TENANT_ID, ORDER_1)
    sales_2 = SalesAgent.for_order(TENANT_ID, ORDER_2)
    logistics_1 = LogisticsAgent.for_order(TENANT_ID, ORDER_1)
    # Note: NO logistics agent for ORDER_2 in this demo —
    # the second order is not yet picked up by logistics.
    # The dispatcher still processes its events through
    # the sales agent (because `track_agent(sales_2)` is
    # registered) — see scenario 3 below for what happens.

    dispatcher = ReactiveDispatcher(
        log,
        systems=[
            sales_1.handle,
            sales_2.handle,
            logistics_1.handle,
        ],
        poll_interval=0.5,
    )
    dispatcher.track_agent(sales_1.agent_id)
    dispatcher.track_agent(sales_2.agent_id)
    dispatcher.track_agent(logistics_1.agent_id)

    # --- Scenario 1: first order, 2 units -----------------------------
    _banner("Scenario 1: first order, 2 units")
    e1 = Event.domain_from(
        agent_id="external:cashier",
        type="order.requested",
        data={
            "tenant_id": TENANT_ID,
            "order_id": ORDER_1,
            "qty": 2,
            "product": "ABC",
        },
    )
    await log.append(e1)
    n = await dispatcher.dispatch_once()
    print(f"  dispatch_once processed {n} event(s)")
    print(f"  logistics_1.state = {logistics_1.state}")

    # --- Scenario 2: customer places SECOND order -----------------------
    _banner("Scenario 2: second order (different order_id), 3 units")
    e2 = Event.domain_from(
        agent_id="external:cashier",
        type="order.requested",
        data={
            "tenant_id": TENANT_ID,
            "order_id": ORDER_2,
            "qty": 3,
            "product": "XYZ",
        },
    )
    await log.append(e2)
    n = await dispatcher.dispatch_once()
    print(f"  dispatch_once processed {n} event(s)")
    # logistics_1 has its OWN stream — it does NOT see
    # events from sales_2 / order_2. Its last_qty_seen
    # stays at 2. The second order is silent on its end
    # (no logistics agent registered for ORDER_2).

    # --- Scenario 3: customer CHANGES the first order -----------------
    _banner("Scenario 3: customer changes first order to 3 units")
    e3 = Event.domain_from(
        agent_id="external:cashier",
        type="order.requested",
        data={
            "tenant_id": TENANT_ID,
            "order_id": ORDER_1,
            "qty": 3,
            "product": "ABC",  # same product, different qty
        },
    )
    await log.append(e3)
    n = await dispatcher.dispatch_once()
    print(f"  dispatch_once processed {n} event(s)")
    # Sales_1 fires order.created {qty: 3} (deterministic
    # event_id, so dedupe kicks in for prior orders with
    # the same data — but qty changed, so new event_id).
    # Logistics_1 observes qty=3 vs last_qty=2 → INCONSISTENCY.

    print(f"  logistics_1.state = {logistics_1.state}")

    # --- Scenario 4: try to ship — logistics is paused ----------------
    _banner("Scenario 4: shipping request while logistics is paused")
    # Even if some downstream system emits a shipping request,
    # logistics_1 won't process it because state["paused"] is True.
    e4 = Event.domain_from(
        agent_id="external:scheduler",
        type="shipping.requested",
        data={"order_id": ORDER_1},
    )
    await log.append(e4)
    n = await dispatcher.dispatch_once()
    print(f"  dispatch_once processed {n} event(s)")
    print("  (no system reacts to shipping.requested — paused)")

    # --- Final view ----------------------------------------------------
    _banner("Final EventLog view (per-agent stream isolation)")
    for label, aid in (
        ("sales_1 (order_001)", sales_1.agent_id),
        ("sales_2 (order_002)", sales_2.agent_id),
        ("logistics_1 (order_001)", logistics_1.agent_id),
    ):
        events = await log.read(aid)
        type_counts: dict[str, int] = {}
        for e in events:
            type_counts[e.event_type] = type_counts.get(e.event_type, 0) + 1
        types_str = ", ".join(f"{t}={c}" for t, c in sorted(type_counts.items()))
        print(f"  {label:32s}  {types_str}")

    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
