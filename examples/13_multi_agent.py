# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
13 — Cooperation between independent systems on the same agent.

Demonstrates the canonical multi-step reactive flow: an
agent appends events to the EventLog, and **two
independent** ``World -> list[Event]`` systems cooperate
on the same World to drive the flow to completion. The
systems are pure functions of the World and know nothing
about each other; the dispatcher just runs them in order
on the post-fold World (ADR-018).

In a real deployment, the two systems below would live in
two different *services* (the `requester` bot and the
`approver` bot). The demo keeps them in one process to
stay runnable; the contract is identical:

  - The `requester` system emits `invoice.requested`.
  - The `approver` system reacts to `invoice.requested`
    whose `valor` exceeds a threshold by emitting
    `invoice.approved`.
  - The `requester` system reacts to `invoice.approved`
    (where `causation_id` points to one of its own
    requests) by emitting the terminal `invoice.issued`.

All three steps are pure:

    async def system(world: World) -> list[Event]

The systems inspect the World's per-agent `AgentView`
(``domain_phase`` + ``components[event_type]``) and emit
based on the agent's current state. No system ever
receives the triggering `Event` directly — that is the
ADR-018 contract, and it is what makes the systems
replayable, testable in isolation, and parallelisable.

Pre-requisites
--------------

  - Redis on localhost:6379 (default).

Run
---

    python examples/13_multi_agent.py
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


AGENT_ID = "agent-nfe-bot"  # one agent, multiple systems
APPROVAL_THRESHOLD = 10_000.0  # BRL


def _banner(title: str) -> None:
    line = "=" * 70
    print()
    print(line)
    print(title)
    print(line)


# ---------------------------------------------------------------------------
# Systems: World -> list[Event]
# ---------------------------------------------------------------------------


async def requester(world: World) -> list[Event]:
    """
    Reads ``world.agents[AGENT_ID]``. Emits:

      - ``invoice.requested`` when the agent's domain_phase
        is ``None`` (no events yet). This seeds the flow.

    The function is the *producer* side; the seed is run
    here rather than in ``main`` so the example exercises
    "system as producer" (a real `requester` bot would
    receive the user's request from an HTTP layer and
    append ``invoice.requested`` via the same system path).
    """
    view = world.agents.get(AGENT_ID)
    if view is None or view.domain_phase is None:
        # Seed: emit the first request. The demo reuses a
        # fixed payload so re-runs are idempotent (the
        # EventLog dedupes on `event_id`).
        return [
            Event.domain_from(
                agent_id=AGENT_ID,
                type="invoice.requested",
                data={
                    "cnpj": "12.345.678.0001-90",
                    "valor": 1500.50,
                },
                correlation=correlation_middleware.current(),
            )
        ]
    if view.domain_phase == "invoice.approved":
        approved = view.components.get("invoice.approved") or {}
        request_id = approved.get("request_id")
        # Synthetic trigger for ADR-037 correlation.
        trigger = Event.domain_from(
            agent_id=AGENT_ID,
            type="invoice.approved",
            data=approved,
            correlation=correlation_middleware.current(),
        )
        return [
            Event.domain_from(
                agent_id=AGENT_ID,
                type="invoice.issued",
                data={
                    "request_id": request_id,
                    "protocol": f"INV-{request_id}-SIMULATED",
                },
                causation_id=request_id,
                correlation=correlation_middleware.continue_from(trigger),
            )
        ]
    return []


async def approver(world: World) -> list[Event]:
    """
    Reads ``world.agents[AGENT_ID]``. Emits
    ``invoice.approved`` when the agent's domain_phase is
    ``invoice.requested`` and the requested `valor`
    exceeds the threshold.
    """
    view = world.agents.get(AGENT_ID)
    if view is None or view.domain_phase != "invoice.requested":
        return []
    requested = view.components.get("invoice.requested") or {}
    valor = float(requested.get("valor", 0.0))
    if valor <= APPROVAL_THRESHOLD:
        return []
    # Synthetic trigger for ADR-037 correlation.
    trigger = Event.domain_from(
        agent_id=AGENT_ID,
        type="invoice.requested",
        data=requested,
        correlation=correlation_middleware.current(),
    )
    return [
        Event.domain_from(
            agent_id=AGENT_ID,
            type="invoice.approved",
            data={
                "request_id": trigger.event_id,
                "approver": "approver-bot",
                "valor": valor,
                "threshold": APPROVAL_THRESHOLD,
            },
            causation_id=trigger.event_id,
            correlation=correlation_middleware.continue_from(trigger),
        )
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    _banner("13 — Cooperation between independent systems")

    redis = make_redis_client()
    log = EventLog(RedisEventLogAdapter(client=redis))

    dispatcher = ReactiveDispatcher(
        log,
        systems=[approver, requester],
        poll_interval=0.5,
        redis=redis,
    )
    dispatcher.track_agent(AGENT_ID)

    # Open a single correlation context for the whole demo
    # (ADR-037). All events emitted in the demo inherit
    # this `correlation_id`; the systems derive the
    # per-trigger context via `continue_from(trigger)`.
    correlation_middleware.start(metadata={"example": "13"})

    try:
        # --- Scenario 1: low-value invoice, no approval ---
        _banner("[1] Low-value invoice (R$ 1500): no approval needed")
        # The system itself emits the seed; one dispatch
        # tick suffices to drive the flow to its terminal
        # state (no approval needed → approver skips →
        # requester sees no `invoice.approved` → no further
        # event).
        n = await dispatcher.dispatch_once()
        print(f"  tick 1: dispatcher emitted {n} event(s)")

        # --- Scenario 2: high-value invoice, A→B→A ---
        _banner("[2] High-value invoice (R$ 15000): approval flow")
        # Reset the World for the new flow by appending a
        # second seed (the agent's World now contains both
        # flows; the requester system reacts to the LATEST
        # `domain_phase` only, which is the new request).
        await log.append(
            Event.domain_from(
                agent_id=AGENT_ID,
                type="invoice.requested",
                data={
                    "cnpj": "11.222.333.0001-44",
                    "valor": 15000.0,
                },
                correlation=correlation_middleware.current(),
            )
        )
        n = await dispatcher.dispatch_once()
        print(f"  tick 1 (approver runs): {n} event(s)")

        n = await dispatcher.dispatch_once()
        print(f"  tick 2 (requester reacts to approval): {n} event(s)")

        # --- Final view ---
        _banner("Final World (single agent, two flows)")
        final_world = await fold_world(log)
        view = final_world.agents.get(AGENT_ID)
        if view is not None:
            print(
                f"  {AGENT_ID}: phase={view.domain_phase!r} "
                f"components={list(view.components.keys())}"
            )
    finally:
        correlation_middleware.clear()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
