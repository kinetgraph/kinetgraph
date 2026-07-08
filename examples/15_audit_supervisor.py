# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
15 — AuditAgent supervisor with reconciliation (ADR-010 pattern).

Demonstrates a **supervisor agent** that watches for
inconsistencies emitted by other agents and reconciles
them before downstream processing can resume.

The construction pattern is the same as example 14
(composed `agent_id` from a prefix + key tuple) but
adds a third concern:

  - **Watch**: subscribe to events from other agents'
    streams by registering them with the dispatcher.
  - **Reconcile**: the audit agent runs its own logic
    to fix the inconsistency (in this demo, it just
    emits an `audit.cleared` event — in production this
    would call back into the upstream system).
  - **Unblock**: emit a downstream-visible event that
    the affected agents can read to resume processing.

This is a simplified version of the v2.0 architecture
proposal's `SolutionPromoter` review flow (ADR-010 §2.6).
The full `KnowledgeConsolidator` uses FalkorDB; here
everything lives in the EventLog + Redis Streams.

Scenario
--------

  1. Sales creates order (qty=2) → logistics processes.
  2. Customer changes order to qty=3 → logistics emits
     `logistics.inconsistency_detected` and pauses.
  3. **Audit agent** observes the inconsistency, runs a
     reconciliation (in this demo, emits `audit.reviewing`).
  4. Audit agent emits `audit.cleared` (the inconsistency
     is resolved in production by a downstream policy
     decision — for the demo, we trust the audit and
     mark cleared).
  5. Logistics observes `audit.cleared` (cross-agent
     subscription) and resumes: state["paused"] = False,
     `last_qty_seen` updated to the latest value.
  6. Logistics emits `logistics.shipped` — the order is
     now consistent and shipped.

The agent classes (`BaseAgent`, `SalesAgent`,
`LogisticsAgent`, `AuditAgent`) are intentionally
duplicated from example 14 so this example stays
self-contained — in a real app they would live in a
shared `agents/` module.

Without Docker
--------------

Set `FMH_REDIS_FAKE=1`.

Run:

    docker run -d -p 6379:6379 --name fmh-redis redis
    python examples/15_audit_supervisor.py

    FMH_REDIS_FAKE=1 python examples/15_audit_supervisor.py
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
from kntgraph.runner.reactive import ReactiveDispatcher  # noqa: E402
from kntgraph.infra.redis._event_log import RedisEventLogAdapter  # noqa: E402
from kntgraph.stream.event_log import EventLog  # noqa: E402

from _lib.redis_or_fake import make_redis_client  # noqa: E402


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-1"
ORDER_ID = "order-001"


# ---------------------------------------------------------------------------
# Base agent (composed agent_id from prefix + key tuple)
# ---------------------------------------------------------------------------


@dataclass
class BaseAgent:
    """
    A composed agent built from `agent_id_prefix` + key tuple.

    Subclasses declare `agent_id_prefix` (str). The full
    `agent_id` is `prefix + ":" + ":".join(keys)`. Each
    agent has its own EventLog stream, isolated from
    others. The `state` dict is per-agent working memory.
    """

    agent_id: str
    state: dict = field(default_factory=dict)

    @classmethod
    def agent_id_for(cls, *keys: str) -> str:
        return cls.agent_id_prefix + ":".join(keys)

    def handle(self, world: World, event: Event) -> list[Event]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Sales agent
# ---------------------------------------------------------------------------


class SalesAgent(BaseAgent):
    """Handles `order.requested`; emits `order.created`."""

    agent_id_prefix = "sales:"

    def __init__(self, *, tenant_id: str, order_id: str) -> None:
        super().__init__(
            agent_id=self.agent_id_for(tenant_id, order_id),
        )
        self._order_id = order_id

    def handle(self, world: World, event: Event) -> list[Event]:
        if event.event_type != "order.requested":
            return []
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
# Logistics agent (with audit cross-subscribe)
# ---------------------------------------------------------------------------


@dataclass
class LogisticsAgent(BaseAgent):
    """
    Handles `order.created`. Tracks `last_qty_seen`. If a
    new `order.created` arrives with a different qty →
    emits `logistics.inconsistency_detected` and pauses.

    Also cross-subscribes to `audit.cleared` (emitted by
    AuditAgent): when paused and cleared, unpauses and
    emits `logistics.shipped`.
    """

    agent_id_prefix = "logistics:"

    def __init__(self, *, tenant_id: str, order_id: str) -> None:
        super().__init__(
            agent_id=self.agent_id_for(tenant_id, order_id),
            state={"last_qty_seen": None, "paused": False},
        )
        self._order_id = order_id
        self._shipped = False

    def handle(self, world: World, event: Event) -> list[Event]:
        # Cross-subscribe: audit.cleared unpauses us.
        if event.event_type == "audit.cleared":
            order_id = event.data.get("order_id")
            if order_id != self._order_id:
                return []
            if not self.state.get("paused"):
                return []
            cleared_qty = event.data.get("cleared_qty")
            self.state["paused"] = False
            self.state["last_qty_seen"] = cleared_qty
            print(
                f"  [logistics {self.agent_id}] cleared by audit "
                f"→ unpaused, last_qty_seen={cleared_qty}"
            )
            ship = Event.domain_from(
                agent_id=self.agent_id,
                type="logistics.shipped",
                data={
                    "order_id": order_id,
                    "qty": cleared_qty,
                },
                causation_id=event.event_id,
            )
            self._shipped = True
            return [ship]

        if event.event_type != "order.created":
            return []
        if self.state.get("paused"):
            return []
        qty = event.data.get("qty", 0)
        last_qty = self.state.get("last_qty_seen")
        if last_qty is not None and last_qty != qty:
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
# Audit agent (supervisor)
# ---------------------------------------------------------------------------


class AuditAgent(BaseAgent):
    """
    Supervisor that watches `logistics.inconsistency_detected`
    and reconciles.

    The dispatcher's stream isolation means each agent
    normally sees events only on its OWN stream. For the
    audit to cross-subscribe, we run `audit.handle` on
    every event regardless of agent_id, and `handle`
    filters by event_type. event_types are globally
    unique in the EventLog keyspace, so this works
    without a broker.
    """

    agent_id_prefix = "audit:"

    def __init__(self, *, tenant_id: str, order_id: str) -> None:
        super().__init__(
            agent_id=self.agent_id_for(tenant_id, order_id),
        )
        self._order_id = order_id
        self.state = {"reviewed": set(), "cleared": set()}

    def handle(self, world, event: Event) -> list[Event]:
        if event.event_type != "logistics.inconsistency_detected":
            return []
        order_id = event.data.get("order_id")
        if order_id != self._order_id:
            return []
        if order_id in self.state["reviewed"]:
            return []
        self.state["reviewed"].add(order_id)
        expected = event.data.get("expected_qty")
        observed = event.data.get("observed_qty")
        print(
            f"  [audit {self.agent_id}] reviewing inconsistency "
            f"order={order_id} expected={expected} observed={observed}"
        )
        reviewing = Event.domain_from(
            agent_id=self.agent_id,
            type="audit.reviewing",
            data={
                "order_id": order_id,
                "expected_qty": expected,
                "observed_qty": observed,
            },
            causation_id=event.event_id,
        )
        # In production, the audit would query the source
        # of truth and emit cleared only after validation.
        # Here we trust the audit immediately.
        cleared = Event.domain_from(
            agent_id=self.agent_id,
            type="audit.cleared",
            data={
                "order_id": order_id,
                "cleared_qty": observed,
                "review": "auto-approved",
            },
            causation_id=event.event_id,
        )
        self.state["cleared"].add(order_id)
        return [reviewing, cleared]


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def _banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


async def main() -> None:
    _banner("15 — AuditAgent supervisor with reconciliation")

    redis = make_redis_client()
    await redis.flushdb()
    log = EventLog(RedisEventLogAdapter(client=redis))

    sales_1 = SalesAgent(tenant_id=TENANT_ID, order_id=ORDER_ID)
    logistics_1 = LogisticsAgent(tenant_id=TENANT_ID, order_id=ORDER_ID)
    audit_1 = AuditAgent(tenant_id=TENANT_ID, order_id=ORDER_ID)

    # The dispatcher runs ALL three `handle` methods on
    # every event. Each agent filters by event_type and
    # by its own key. This is the cross-agent pattern: a
    # single dispatcher fans out to multiple specialised
    # agents without a broker.
    dispatcher = ReactiveDispatcher(
        log,
        systems=[
            sales_1.handle,
            logistics_1.handle,
            audit_1.handle,
        ],
        poll_interval=0.5,
    )
    dispatcher.track_agent(sales_1.agent_id)
    dispatcher.track_agent(logistics_1.agent_id)
    dispatcher.track_agent(audit_1.agent_id)

    # --- Step 1: customer places order, qty=2 -------------------------
    _banner("Step 1: order placed, qty=2")
    e1 = Event.domain_from(
        agent_id="external:cashier",
        type="order.requested",
        data={
            "tenant_id": TENANT_ID,
            "order_id": ORDER_ID,
            "qty": 2,
            "product": "ABC",
        },
    )
    await log.append(e1)
    n = await dispatcher.dispatch_once()
    print(f"  processed {n} event(s)")
    print(f"  logistics_1.state = {logistics_1.state}")
    print(f"  audit_1.state     = {audit_1.state}")

    # --- Step 2: customer changes order to qty=3 --------------------
    _banner("Step 2: order changed, qty=3")
    e2 = Event.domain_from(
        agent_id="external:cashier",
        type="order.requested",
        data={
            "tenant_id": TENANT_ID,
            "order_id": ORDER_ID,
            "qty": 3,
            "product": "ABC",
        },
    )
    await log.append(e2)
    n = await dispatcher.dispatch_once()
    print(f"  processed {n} event(s)")
    print(f"  logistics_1.state = {logistics_1.state}")
    print(f"  audit_1.state     = {audit_1.state}")

    # --- Step 3: run again to drive audit.cleared → logistics -------
    _banner("Step 3: audit cleared, logistics resumed + shipped")
    n = await dispatcher.dispatch_once()
    print(f"  processed {n} event(s)")
    print(f"  logistics_1.state = {logistics_1.state}")
    print(f"  logistics_1._shipped = {logistics_1._shipped}")
    print(f"  audit_1.state     = {audit_1.state}")

    # --- Final view -------------------------------------------------
    _banner("Final EventLog view")
    for label, aid in (
        ("sales_1", sales_1.agent_id),
        ("logistics_1", logistics_1.agent_id),
        ("audit_1", audit_1.agent_id),
    ):
        events = await log.read(aid)
        type_counts: dict[str, int] = {}
        for e in events:
            type_counts[e.event_type] = type_counts.get(e.event_type, 0) + 1
        types_str = ", ".join(f"{t}={c}" for t, c in sorted(type_counts.items()))
        print(f"  {label:14s} {aid:42s}  {types_str}")

    _banner("Causation chain (audit supervision)")
    audit_events = await log.read(audit_1.agent_id)
    for e in audit_events:
        cid = f"causation={e.causation_id}" if e.causation_id else "(root)"
        print(f"  {e.event_type:30s} {cid}")

    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
