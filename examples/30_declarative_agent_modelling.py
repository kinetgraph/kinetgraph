# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
#
"""
Example 30: Declarative agent modelling — one definition, two execution cycles.

This example shows the **new modelling style** the framework
settled on while closing DEBT.md §2.42:

  1. An agent is **declared as data** — one frozen dataclass
     that names the persona, the capability policy, the state
     machine and the workflow.
  2. The runtime is **derived** from that declaration, never
     hand-wired at each call site.
  3. Every business rule is a **Specification** that reads the
     same carrier in both execution cycles. A guard written for
     the FSM works unchanged as a Saga skip condition, because
     the carrier is built in exactly one place
     (``build_step_context``).

The example models an **order fulfilment agent**:

```
reactive cycle (zero-token governance)
  draft ──order.submitted──► validating
  validating ──order.approved──► approved   (guard: amount within limit)
  approved ──order.cancelled──► cancelled   (terminal)

deliberative cycle (tool orchestration)
  saga.OrderFulfilment
    reserve  → reserve_inventory
    charge   → charge_payment     (skipped when the order is free)
    ship     → create_shipment     (compensated by cancel_shipment)
```

The two cycles share the ``OrderDomainComponent`` and the three
memory tiers. A ``Specification`` reads them the same way in
either cycle, which is the property DEBT.md §2.42 tracked and
this example demonstrates.

## What the example shows

  1. **One declaration per agent** — the persona, the tool
     allow-list, the FSM and the Saga declared together.
  2. **The derived runtime** — ``AgentBundle`` carries the
     live systems and projections; the declaration keeps no
     copy of them.
  3. **Type-indexed domain access** — a Specification reads
     ``ctx.get_component(OrderDomainComponent)`` and gets
     exactly that class, never an arbitrary domain component.
  4. **Memory tiers in both cycles** — the guard and the skip
     condition observe the same session, continuity and profile.
  5. **Guard portability** — one Specification, used as an FSM
     guard and as a Saga skip condition, evaluated against the
     same view.

## Run with

    KNT_REDIS_FAKE=1 uv run python examples/30_declarative_agent_modelling.py

The example is pure: it builds a ``World`` via the SUT builders
in ``kntgraph.testing`` and calls the real systems against it.
No Redis, no dispatcher, no external model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from kntgraph.agents import AgentDefinition, RoleSpec
from kntgraph.concordos.base import Specification, StepContext
from kntgraph.concordos.fsm import (
    BusinessFSMConcordo,
    FSMConfig,
    FSMTransition,
)
from kntgraph.concordos.saga import SagaConfig, SagaStepConfig, WorkflowSagaConcordo
from kntgraph.concordos.saga._components import SagaProgressComponent
from kntgraph.core.components.memory import (
    ContinuityComponent,
    ProfileComponent,
    SessionComponent,
)
from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.world import DomainComponent
from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system

FIXED_NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The agent's business object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderDomainComponent(DomainComponent):
    """The order's business state.

    ``status`` drives the FSM; ``amount`` drives the business
    rules. Both live on one component so a Specification can
    read them together, and the carrier addresses the component
    by its concrete class.
    """

    status: str = "draft"
    amount: float = 0.0
    order_id: str = ""


# ---------------------------------------------------------------------------
# Business rules as Specifications
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AmountWithinLimit(Specification):
    """True when the order amount is within the accepted range.

    The Specification reads its component by concrete class
    through ``ctx.get_component``. It carries no reference to
    the agent, the FSM or the Saga, which is what makes it
    portable between the two execution cycles.
    """

    min_amount: float
    max_amount: float

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        order = ctx.get_component(OrderDomainComponent)
        if order is None:
            return False
        return self.min_amount <= order.amount <= self.max_amount


@dataclass(frozen=True, slots=True)
class IsHighValueCustomer(Specification):
    """True when the agent's profile tier is ``vip``.

    The profile is a memory tier; the carrier hydrates it from
    the view, so this Specification works unchanged as an FSM
    guard and as a Saga skip condition.
    """

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return ctx.profile is not None and ctx.profile.tier == "vip"


@dataclass(frozen=True, slots=True)
class OrderIsFree(Specification):
    """True when the order costs nothing.

    The Saga uses this to skip the payment step: a free order
    has nothing to charge.
    """

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        order = ctx.get_component(OrderDomainComponent)
        return order is not None and order.amount == 0.0


# ---------------------------------------------------------------------------
# The agent declaration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The order fulfilment agent
# ---------------------------------------------------------------------------

ORDER_FSM = FSMConfig(
    component_type=OrderDomainComponent,
    state_field="status",
    transitions={
        "draft": {
            "order.submitted": FSMTransition(to="validating"),
        },
        "validating": {
            "order.approved": FSMTransition(
                to="approved",
                guard=AmountWithinLimit(1.0, 100_000.0),
            ),
            "order.rejected": FSMTransition(to="draft"),
        },
        "approved": {
            "order.cancelled": FSMTransition(to="cancelled"),
        },
    },
    on_entry={"approved": "order.approval_confirmed"},
    terminal=frozenset({"cancelled"}),
)

ORDER_SAGA = SagaConfig(
    name="OrderFulfilment",
    steps=(
        SagaStepConfig(
            name="reserve",
            tool_name="reserve_inventory",
            skip_when=IsHighValueCustomer(),
            compensate_tool="release_inventory",
        ),
        SagaStepConfig(
            name="charge",
            tool_name="charge_payment",
            skip_when=OrderIsFree(),
            compensate_tool="refund_payment",
        ),
        SagaStepConfig(
            name="ship",
            tool_name="create_shipment",
            compensate_tool="cancel_shipment",
        ),
    ),
)

ORDER_AGENT = AgentDefinition(
    name="order-fulfilment",
    persona=RoleSpec(
        persona="fulfilment",
        allowed_tools=("reserve_inventory", "charge_payment", "create_shipment"),
    ),
    role_event_type="role.assigned",
    fsm=BusinessFSMConcordo(config=ORDER_FSM),
    sagas=(WorkflowSagaConcordo(config=ORDER_SAGA),),
    domain_types=(OrderDomainComponent,),
)


# ---------------------------------------------------------------------------
# The agent's view
# ---------------------------------------------------------------------------


def _view(
    agent_id: str,
    *,
    status: str,
    amount: float,
    tier: str = "standard",
    trigger: str | None = None,
    saga_started: bool = False,
) -> object:
    """The agent's view: its domain component and three memory tiers.

    With ``saga_started``, the view also carries the saga's
    progress component and a ``saga.<name>.started`` trigger —
    what makes the saga walk its steps.
    """
    builder = (
        AgentViewBuilder(agent_id)
        .with_component(OrderDomainComponent(status=status, amount=amount))
        .with_component(
            SessionComponent(session_id="sess-1", user_id="u-1", tenant_id="t-1")
        )
        .with_component(ProfileComponent(tenant_id="t-1", user_id="u-1", tier=tier))
        .with_component(
            ContinuityComponent(tenant_id="t-1", user_id="u-1", last_tools={})
        )
    )
    if saga_started:
        builder = builder.with_component(
            SagaProgressComponent(
                saga_id="saga-1",
                saga_name=ORDER_SAGA.name,
                current_step="",
                direction="forward",
                step_order=tuple(s.name for s in ORDER_SAGA.steps),
                step_states={},
                step_results={},
                compensate_stack=(),
                started_at=FIXED_NOW,
            )
        ).with_trigger(f"saga.{ORDER_SAGA.name}.started", data={})
        return builder.build()
    if trigger is not None:
        builder = builder.with_trigger(trigger)
    return builder.build()


def _banner(msg: str) -> None:
    print("\n" + "=" * 72)
    print(msg)
    print("=" * 72)


# ---------------------------------------------------------------------------
# The demonstration
# ---------------------------------------------------------------------------


def main() -> None:
    print("=" * 72)
    print("Declarative agent modelling — one definition, two execution cycles")
    print("=" * 72)

    bundle = ORDER_AGENT.bundle()

    # ------------------------------------------------------------------
    # 1. The declaration derives the runtime.
    # ------------------------------------------------------------------
    _banner("1. The declaration derives the runtime")
    print(f"  agent:       {ORDER_AGENT.name}")
    print(f"  persona:     {ORDER_AGENT.persona.persona}")
    print(f"  tools:       {ORDER_AGENT.persona.allowed_tools}")
    print(f"  domain:      {[t.__name__ for t in ORDER_AGENT.domain_types]}")
    print(f"  systems:     {[type(s).__name__ for s in bundle.systems]}")
    print(f"  projections: {[type(p).__name__ for p in bundle.projections]}")
    # The FSM contributes one system; the saga contributes the
    # orchestrator and its timeout watchdog.
    assert [type(s).__name__ for s in bundle.systems] == [
        "FSMSystem",
        "SagaSystem",
        "SagaTimeoutSystem",
    ]
    assert [type(p).__name__ for p in bundle.projections] == [
        "FSMProjection",
        "SagaProjection",
        "RoleProjection",
    ]

    # ------------------------------------------------------------------
    # 2. The reactive cycle: the FSM guards the transition.
    # ------------------------------------------------------------------
    _banner("2. Reactive cycle — the guard reads the domain component")
    fsm_system = next(s for s in bundle.systems if type(s).__name__ == "FSMSystem")
    view = _view("order-1", status="validating", amount=250.0, trigger="order.approved")
    world = WorldBuilder().with_agent(view).build()
    out = run_system(fsm_system, world, correlation=CorrelationContext.new())
    print(f"  events: {[e.event_type for e in out]}")
    assert "fsm.transitioned" in [e.event_type for e in out]
    assert "order.approval_confirmed" in [e.event_type for e in out]

    # ------------------------------------------------------------------
    # 3. The same guard, an amount outside the range.
    # ------------------------------------------------------------------
    _banner("3. Reactive cycle — the guard rejects an out-of-range order")
    view = _view(
        "order-2", status="validating", amount=500_000.0, trigger="order.approved"
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(fsm_system, world, correlation=CorrelationContext.new())
    print(f"  events: {[e.event_type for e in out]}")
    assert "fsm.transition_rejected" in [e.event_type for e in out]

    # ------------------------------------------------------------------
    # 4. Guard portability: the same Specification in both cycles.
    # ------------------------------------------------------------------
    _banner("4. One Specification, two cycles — same view, same observation")
    guard = AmountWithinLimit(1.0, 100_000.0)
    view = _view("order-3", status="validating", amount=250.0)
    world = WorldBuilder().with_agent(view).build()
    from kntgraph.concordos import build_step_context

    ctx = build_step_context(view, world, FIXED_NOW)
    print(f"  component read by class: {ctx.get_component(OrderDomainComponent)}")
    print(f"  guard satisfied in the carrier: {guard.is_satisfied_by(ctx)}")
    assert ctx.get_component(OrderDomainComponent) is not None
    assert guard.is_satisfied_by(ctx) is True

    # ------------------------------------------------------------------
    # 5. The deliberative cycle: the saga skips a step for a free order.
    # ------------------------------------------------------------------
    _banner("5. Deliberative cycle — the saga skips the charge step")
    saga_system = next(s for s in bundle.systems if type(s).__name__ == "SagaSystem")
    view = _view(
        "order-4",
        status="approved",
        amount=0.0,
        tier="standard",
        saga_started=True,
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(saga_system, world, correlation=CorrelationContext.new())
    types = [e.event_type for e in out]
    print(f"  events: {types}")
    # The free order skips ``charge``; ``reserve`` runs.
    assert "saga.OrderFulfilment.step_started" in types
    assert not any("charge" in t for t in types)

    # ------------------------------------------------------------------
    # 6. The same Specification, the other cycle.
    # ------------------------------------------------------------------
    _banner("6. The skip condition sees the same memory tiers")
    vip_guard = IsHighValueCustomer()
    view = _view("order-5", status="approved", amount=50.0, tier="vip")
    world = WorldBuilder().with_agent(view).build()
    ctx = build_step_context(view, world, FIXED_NOW)
    print(f"  profile tier: {ctx.profile.tier if ctx.profile else None}")
    print(f"  session:      {ctx.session.session_id if ctx.session else None}")
    print(f"  vip guard satisfied: {vip_guard.is_satisfied_by(ctx)}")
    assert vip_guard.is_satisfied_by(ctx) is True
    assert ctx.session is not None

    print("\n" + "=" * 72)
    print("All assertions passed.")
    print("=" * 72)


if __name__ == "__main__":
    main()
