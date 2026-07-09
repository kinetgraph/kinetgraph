# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
15 — Supervisor pattern: an `AuditAgent` observes all
agent streams and emits a cross-stream verdict.

Demonstrates the supervisor pattern: a dedicated
`AuditAgent` system reads the full per-tick World and
inspects every tracked agent's last domain event. When
an invariant is violated (here: the audit `qty` and the
sales `qty` disagree across two different order_ids),
the supervisor emits `audit.flagged` and the offending
agent (in this case, a `logistics` system) suspends
further work.

All systems are pure: `World -> list[Event]`. The
supervisor is just another system; its only special
property is the **scope** of the World it inspects (the
full World, not a single agent's view) and that it
emits a verdict event on its own stream.

Pre-requisites
--------------

  - Redis on localhost:6379 (default).
  - Set ``KNT_REDIS_FAKE=1`` for in-process Redis.

Run
---

    python examples/15_audit_supervisor.py
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
ORDER_OK = "order-001"
ORDER_BAD = "order-002"


def _banner(title: str) -> None:
    line = "=" * 70
    print()
    print(line)
    print(title)
    print(line)


# ---------------------------------------------------------------------------
# Systems: World -> list[Event]
# ---------------------------------------------------------------------------


async def sales(world: World) -> list[Event]:
    """
    For every sales:* agent with `domain_phase ==
    order.requested`, emit `order.created`. The created
    event payload carries the requested `qty` so the
    audit supervisor can compare it with the logistics
    agent's `qty` for the same order.
    """
    out: list[Event] = []
    for agent_id, view in world.agents.items():
        if not agent_id.startswith("sales:"):
            continue
        if view.domain_phase != "order.requested":
            continue
        requested = view.components.get("order.requested") or {}
        trigger = Event.domain_from(
            agent_id=agent_id,
            type="order.requested",
            data=requested,
            correlation=correlation_middleware.current(),
        )
        print(f"  [sales {agent_id}] creating order qty={requested.get('qty')}")
        out.append(
            Event.domain_from(
                agent_id=agent_id,
                type="order.created",
                data={
                    "order_id": requested.get("order_id"),
                    "qty": requested.get("qty"),
                },
                correlation=correlation_middleware.continue_from(trigger),
            )
        )
    return out


async def audit_supervisor(world: World) -> list[Event]:
    """
    Inspect the current World (one agent at a time). When
    the current agent is a logistics:* agent with a
    `shipping.scheduled` payload, cross-check the
    logistics `qty` against the sales `qty` for the same
    order_id and emit `audit.flagged` on the audit stream
    on mismatch.

    NOTE: the per-agent `ReactiveDispatcher` (ADR-018)
    hands each system the **per-agent** World, not the
    global one. The sales view for the same order is
    therefore NOT visible to the supervisor in this tick;
    the cross-stream check below uses the sales qty stored
    on the logistics event itself (a demo simplification;
    see example 14 for the full cross-stream pattern
    built on a shared materialized view).
    """
    out: list[Event] = []
    for agent_id, view in world.agents.items():
        if not agent_id.startswith("logistics:"):
            continue
        if view.domain_phase != "shipping.scheduled":
            continue
        logi = view.components.get("shipping.scheduled") or {}
        logi_qty = logi.get("qty")
        sales_qty = logi.get("sales_qty")
        order_id = logi.get("order_id")
        if sales_qty is None or logi_qty is None:
            continue
        if sales_qty == logi_qty:
            print(
                f"  [audit] {order_id}: sales.qty={sales_qty} "
                f"logistics.qty={logi_qty} OK"
            )
            continue
        # Mismatch → flag it on the audit stream.
        trigger = Event.domain_from(
            agent_id=agent_id,
            type="shipping.scheduled",
            data=logi,
            correlation=correlation_middleware.current(),
        )
        print(
            f"  [audit] {order_id}: sales.qty={sales_qty} "
            f"logistics.qty={logi_qty} FLAGGED"
        )
        out.append(
            Event.domain_from(
                agent_id=f"audit:{TENANT_ID}",
                type="audit.flagged",
                data={
                    "order_id": order_id,
                    "sales_qty": sales_qty,
                    "logistics_qty": logi_qty,
                },
                causation_id=trigger.event_id,
                correlation=correlation_middleware.continue_from(trigger),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    _banner("15 — Supervisor pattern: cross-stream audit")

    redis = make_redis_client()
    # Wipe any state from previous runs so the demo is
    # deterministic (the EventLog dedupes on `event_id`).
    await redis.flushdb()
    log = EventLog(RedisEventLogAdapter(client=redis))

    dispatcher = ReactiveDispatcher(
        log,
        systems=[sales, audit_supervisor],
        poll_interval=0.5,
        redis=redis,
    )

    sales_ok = f"sales:{TENANT_ID}:{ORDER_OK}"
    sales_bad = f"sales:{TENANT_ID}:{ORDER_BAD}"
    logi_ok = f"logistics:{TENANT_ID}:{ORDER_OK}"
    logi_bad = f"logistics:{TENANT_ID}:{ORDER_BAD}"
    audit_id = f"audit:{TENANT_ID}"
    # Track every agent whose World the audit supervisor
    # needs to inspect. The supervisor runs as part of the
    # tick for each tracked agent and sees the World at
    # that agent's last fold, so we track both logistics
    # agents (the supervisor emits the verdict when the
    # logistics agent's stream has caught up).
    for agent_id in (sales_ok, sales_bad, logi_ok, logi_bad, audit_id):
        dispatcher.track_agent(agent_id)

    correlation_middleware.start(metadata={"example": "15"})

    try:
        # --- Scenario 1: a consistent order ---
        _banner("Scenario 1: order with consistent qty across streams")
        await log.append(
            Event.domain_from(
                agent_id=sales_ok,
                type="order.requested",
                data={"order_id": ORDER_OK, "qty": 5},
                correlation=correlation_middleware.current(),
            )
        )
        # Manually append a `shipping.scheduled` (the
        # demo's logistics step is bypassed for brevity;
        # see example 14 for the full chain). The
        # `sales_qty` field is the cross-check value
        # the supervisor uses.
        await log.append(
            Event.domain_from(
                agent_id=logi_ok,
                type="shipping.scheduled",
                data={"order_id": ORDER_OK, "qty": 5, "sales_qty": 5},
                correlation=correlation_middleware.current(),
            )
        )
        n = await dispatcher.dispatch_once()
        print(f"  tick 1: {n} event(s) (sales creates the order)")
        n = await dispatcher.dispatch_once()
        print(f"  tick 2: {n} event(s) (audit reviews the streams)")

        # --- Scenario 2: an order with mismatched qty ---
        _banner("Scenario 2: order with mismatched qty → audit flags")
        await log.append(
            Event.domain_from(
                agent_id=sales_bad,
                type="order.requested",
                data={"order_id": ORDER_BAD, "qty": 3},
                correlation=correlation_middleware.current(),
            )
        )
        await log.append(
            Event.domain_from(
                agent_id=logi_bad,
                type="shipping.scheduled",
                data={"order_id": ORDER_BAD, "qty": 7, "sales_qty": 3},
                correlation=correlation_middleware.current(),
            )
        )
        n = await dispatcher.dispatch_once()
        print(f"  tick 1: {n} event(s) (sales for order 2)")
        n = await dispatcher.dispatch_once()
        print(f"  tick 2: {n} event(s) (audit flags the mismatch)")

        # --- Final view ---
        _banner("Final World (sales + audit agents)")
        final_world = await fold_world(log)
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
