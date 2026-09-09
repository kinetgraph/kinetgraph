# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 23: BusinessFSM — a pure state machine over a DomainComponent (ADR-069 §3).

This example demonstrates the **BusinessFSM Concordo** (C-01):
a pure reactive system that declares the lifecycle of a
business object as an explicit state machine over a
``DomainComponent``. It reacts to domain events, validates
transitions, and emits ``fsm.transitioned`` or
``fsm.transition_rejected``. No I/O, no tool calls.

The example models an **invoice lifecycle**:

```
draft ──invoice.submitted──► validating
validating ──invoice.approved──► issued
validating ──invoice.rejected──► draft
issued ──payment.received──► paid        (terminal)
issued ──invoice.cancelled──► cancelled  (terminal)
```

The ``invoice.approved`` transition is **guarded**: it only
fires when the ``nfe_emitter`` tool was NOT the last tool
used in the recent continuity window (a guard expressed as a
composable ``Specification``). This prevents re-emitting an
NF-e immediately after a previous emission that has not yet
aged out of the window.

## What the example shows

  1. **Declaring an FSM** — ``FSMConfig`` with states,
     transitions, optional guards, on-entry events, and
     terminal states.
  2. **Guards as Specifications** — a transition is allowed
     only when its ``guard`` is satisfied; otherwise
     ``fsm.transition_rejected`` with ``reason="guard_failed"``.
  3. **On-entry events** — entering a state can emit a
     follow-up domain event (e.g. ``invoice.issuance_confirmed``).
  4. **Terminal states** — no transition is allowed from a
     terminal state (``reason="terminal_state"``).
  5. **Undeclared transitions** — an event with no declared
     transition from the current state is rejected
     (``reason="transition_not_declared"``).
  6. **State advance** — the ``FSMProjection`` advances the
     ``DomainComponent``'s ``state_field`` from
     ``fsm.transitioned``, preserving the other fields
     (ADR-069 §9.2 item 1).

## Run with

    KNT_REDIS_FAKE=1 uv run python examples/23_business_fsm.py

The example is pure: it builds a ``World`` via the SUT
builders in ``kntgraph.testing`` and calls the ``FSMSystem``
against it. No Redis, no dispatcher, no external model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from kntgraph.concordos.fsm import FSMConfig, FSMProjection, FSMTransition, FSMSystem
from kntgraph.concordos.specs import ContinuityToolUsed
from kntgraph.core.components.memory import ContinuityComponent
from kntgraph.core.world import DomainComponent
from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system

FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class InvoiceDomainComponent(DomainComponent):
    """The business object whose lifecycle the FSM guards.

    ``status`` is the ``state_field`` the FSM reads. The
    component is a plain ``DomainComponent`` (ADR-059); the
    FSM does not introduce a new component for state.
    """

    status: str = "draft"
    tax_regime: str = "lucro_real"


# The invoice lifecycle from ADR-069 §3.6.
invoice_fsm = FSMConfig(
    component_type=InvoiceDomainComponent,
    state_field="status",
    transitions={
        "draft": {
            "invoice.submitted": FSMTransition(to="validating"),
        },
        "validating": {
            "invoice.approved": FSMTransition(
                to="issued",
                guard=ContinuityToolUsed("nfe_emitter").not_(),
            ),
            "invoice.rejected": FSMTransition(to="draft"),
        },
        "issued": {
            "payment.received": FSMTransition(to="paid"),
            "invoice.cancelled": FSMTransition(to="cancelled"),
        },
    },
    on_entry={
        "issued": "invoice.issuance_confirmed",
        "paid": "invoice.payment_confirmed",
    },
    terminal=frozenset({"paid", "cancelled"}),
)


def _banner(msg: str) -> None:
    print("\n" + "=" * 72)
    print(msg)
    print("=" * 72)


def _run(
    agent_id: str,
    *,
    status: str,
    trigger: str,
    last_tools: dict[str, str] | None = None,
) -> list[str]:
    """Build a World with one agent and run the FSM against it.

    Returns the emitted event types.
    """
    builder = (
        AgentViewBuilder(agent_id)
        .with_component(InvoiceDomainComponent(status=status))
        .with_trigger(trigger)
    )
    if last_tools is not None:
        builder = builder.with_component(
            ContinuityComponent(
                tenant_id="t-1",
                user_id="u-1",
                last_tools=last_tools,
            )
        )
    view = builder.build()
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
    return [e.event_type for e in out]


def main() -> None:
    print(
        "=== BusinessFSM — pure state machine over a DomainComponent (ADR-069 §3) ==="
    )

    # ------------------------------------------------------------------
    # 1. Valid transition, no guard.
    # ------------------------------------------------------------------
    _banner("1. draft --invoice.submitted--> validating (no guard)")
    types = _run("inv-1", status="draft", trigger="invoice.submitted")
    print(f"  events: {types}")
    assert "fsm.transitioned" in types
    assert "fsm.transition_rejected" not in types

    # ------------------------------------------------------------------
    # 2. Guarded transition, guard satisfied → allowed.
    # ------------------------------------------------------------------
    _banner("2. validating --invoice.approved--> issued (guard satisfied)")
    types = _run(
        "inv-2",
        status="validating",
        trigger="invoice.approved",
        last_tools={"other_tool": "2026-09-07T11:59:00Z"},
    )
    print(f"  events: {types}")
    assert "fsm.transitioned" in types
    assert "invoice.issuance_confirmed" in types  # on-entry event

    # ------------------------------------------------------------------
    # 3. Guarded transition, guard fails → rejected.
    # ------------------------------------------------------------------
    _banner("3. validating --invoice.approved--> BLOCKED (guard failed)")
    types = _run(
        "inv-3",
        status="validating",
        trigger="invoice.approved",
        last_tools={"nfe_emitter": "2026-09-07T11:59:00Z"},
    )
    print(f"  events: {types}")
    assert "fsm.transition_rejected" in types
    assert "fsm.transitioned" not in types

    # ------------------------------------------------------------------
    # 4. Undeclared transition → rejected.
    # ------------------------------------------------------------------
    _banner("4. draft --payment.received--> BLOCKED (transition not declared)")
    types = _run("inv-4", status="draft", trigger="payment.received")
    print(f"  events: {types}")
    assert "fsm.transition_rejected" in types

    # ------------------------------------------------------------------
    # 5. Terminal state → rejected.
    # ------------------------------------------------------------------
    _banner("5. paid --invoice.submitted--> BLOCKED (terminal state)")
    types = _run("inv-5", status="paid", trigger="invoice.submitted")
    print(f"  events: {types}")
    assert "fsm.transition_rejected" in types

    # ------------------------------------------------------------------
    # 6. No domain event → no output.
    # ------------------------------------------------------------------
    _banner("6. no trigger → no output")
    view = (
        AgentViewBuilder("inv-6")
        .with_component(InvoiceDomainComponent(status="draft"))
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
    print(f"  events: {[e.event_type for e in out]}")
    assert out == []

    # ------------------------------------------------------------------
    # 7. FSMProjection advances the state_field (ADR-069 §9.2 item 1).
    # ------------------------------------------------------------------
    _banner("7. FSMProjection advances state_field from fsm.transitioned")
    from kntgraph.core.event import Event
    from kntgraph.core.event.correlation import CorrelationContext

    transitioned = Event.create(
        agent_id="inv-7",
        event_type="fsm.transitioned",
        event_class="domain",
        data={
            "from": "draft",
            "to": "validating",
            "trigger": "invoice.submitted",
            "trigger_event_id": "00000000-0000-0000-0000-000000000001",
        },
        correlation=CorrelationContext.new(),
    )
    view = (
        AgentViewBuilder("inv-7")
        .with_component(InvoiceDomainComponent(status="draft"))
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    new_world = FSMProjection(invoice_fsm)(world, [transitioned])
    advanced = new_world.get_agent("inv-7").get_component(InvoiceDomainComponent)
    print(f"  state_field after projection: {advanced.status}")
    assert advanced.status == "validating"
    assert advanced.tax_regime == "lucro_real"  # other fields survive

    print("\nAll FSM scenarios passed.")


if __name__ == "__main__":
    main()
