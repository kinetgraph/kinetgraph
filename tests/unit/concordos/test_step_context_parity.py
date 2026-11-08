# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
#
"""
Parity tests for the canonical ``StepContext`` (DEBT.md §2.42).

The debt existed because five call sites built the carrier by
hand and disagreed about what it held. The FSM path populated
the memory tiers; two Saga paths passed ``None`` for all of
them. A Specification written for an FSM guard therefore
behaved differently when reused as a Saga skip condition.

These tests assert the property that closes the debt: the
same Specification, evaluated against the same agent view,
observes the same memory tiers and the same domain component
in the FSM cycle and in the Saga cycle.

The tests use the real ``WorldBuilder`` / ``AgentViewBuilder``
SUT builders (no mocks, no monkey-patches) and the real
``FSMSystem`` / ``SagaSystem`` under test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from kntgraph.concordos.base import Specification, StepContext
from kntgraph.concordos.fsm import FSMConfig, FSMSystem, FSMTransition
from kntgraph.concordos.saga import SagaConfig, SagaStepConfig, SagaSystem
from kntgraph.concordos.saga._components import SagaProgressComponent
from kntgraph.core.components.memory import (
    ContinuityComponent,
    ProfileComponent,
    SessionComponent,
)
from kntgraph.core.world.component import DomainComponent
from kntgraph.core.world.view import AgentView
from kntgraph.testing import AgentViewBuilder, WorldBuilder

FIXED_NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderDomainComponent(DomainComponent):
    """The agent's domain component: the FSM's state field and
    the Saga's business fact live on the same object."""

    status: str = "created"
    order_amount: int = 250


class Observation(Specification):
    """Records what the Specification saw, then answers a
    question about it.

    The observation is the assertion surface: the test
    compares what the FSM cycle saw with what the Saga cycle
    saw over the same view.
    """

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.tier: str | None = None
        self.last_tool: str | None = None
        self.status: str | None = None
        self.order_amount: int | None = None

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        """Record the observation and report whether the order
        was paid."""
        self.session_id = ctx.session.session_id if ctx.session else None
        self.tier = ctx.profile.tier if ctx.profile else None
        self.last_tool = (
            next(iter(ctx.continuity.last_tools), None) if ctx.continuity else None
        )
        order = ctx.get_component(OrderDomainComponent)
        self.status = order.status if order else None
        self.order_amount = order.order_amount if order else None
        return order is not None and order.status == "paid"


def _view(*, saga_started: bool = False) -> AgentView:
    """The agent view both cycles evaluate against: one domain
    component and all three memory tiers.

    With ``saga_started``, the view also carries a
    ``SagaProgressComponent`` and a ``saga.OrderSaga.started``
    trigger, which is what makes the Saga system walk its
    steps (and therefore evaluate ``skip_when``).
    """
    builder = (
        AgentViewBuilder("order-agent-1")
        .with_component(OrderDomainComponent(status="created", order_amount=250))
        .with_component(
            SessionComponent(session_id="sess-1", user_id="u-1", tenant_id="t-1")
        )
        .with_component(ProfileComponent(tenant_id="t-1", user_id="u-1", tier="vip"))
        .with_component(
            ContinuityComponent(
                tenant_id="t-1",
                user_id="u-1",
                last_tools={"reserve": "2026-11-05"},
            )
        )
    )
    if saga_started:
        builder = builder.with_component(
            SagaProgressComponent(
                saga_id="saga-1",
                saga_name="OrderSaga",
                current_step="",
                direction="forward",
                step_order=("reserve",),
                step_states={},
                step_results={},
                compensate_stack=(),
                started_at=FIXED_NOW,
            )
        ).with_trigger("saga.OrderSaga.started", data={})
        return builder.build()
    return builder.with_trigger("order.submit", data={"status": "created"}).build()


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------


def test_fsm_and_saga_cycles_observe_the_same_tiers() -> None:
    """The same Specification observes the same memory tiers
    and the same domain component in both execution cycles.

    Before DEBT.md §2.42 the Saga skip path passed ``None``
    for ``domain``, ``continuity`` and ``profile``, so the
    Saga cycle saw nothing where the FSM cycle saw the tiers.
    """
    view = _view()
    world = WorldBuilder().with_agent(view).build()

    fsm_spec = Observation()
    fsm_config = FSMConfig(
        component_type=OrderDomainComponent,
        state_field="status",
        transitions={
            "created": {
                "order.submit": FSMTransition(to="paid", guard=fsm_spec),
            },
        },
    )
    fsm = FSMSystem(fsm_config, now=lambda: FIXED_NOW)
    fsm(world)

    saga_spec = Observation()
    saga_config = SagaConfig(
        name="OrderSaga",
        steps=(
            SagaStepConfig(
                name="reserve",
                tool_name="reserve",
                skip_when=saga_spec,
            ),
        ),
    )
    saga = SagaSystem(saga_config, now=lambda: FIXED_NOW)
    saga(WorldBuilder().with_agent(_view(saga_started=True)).build())

    assert saga_spec.session_id == fsm_spec.session_id == "sess-1"
    assert saga_spec.tier == fsm_spec.tier == "vip"
    assert saga_spec.last_tool == fsm_spec.last_tool == "reserve"
    assert saga_spec.status == fsm_spec.status == "created"
    assert saga_spec.order_amount == fsm_spec.order_amount == 250


def test_saga_skip_condition_sees_the_domain_component() -> None:
    """A Saga ``skip_when`` that reads the domain component
    evaluates against real data, not against a ``None``.

    This is the concrete failure the debt described: the skip
    path built the carrier with ``domain=None``, so a
    Specification checking the domain component always saw
    nothing.
    """
    view = _view(saga_started=True)
    world = WorldBuilder().with_agent(view).build()

    saw_domain = Observation()
    saga_config = SagaConfig(
        name="OrderSaga",
        steps=(
            SagaStepConfig(name="reserve", tool_name="reserve", skip_when=saw_domain),
        ),
    )
    saga = SagaSystem(saga_config, now=lambda: FIXED_NOW)
    saga(world)

    assert saw_domain.status == "created"
    assert saw_domain.order_amount == 250


def test_saga_compensation_sees_the_memory_tiers() -> None:
    """A Saga ``compensate_when`` observes the memory tiers.

    The compensation path built the carrier with ``None`` for
    every tier, so a Specification deciding whether a step is
    compensable could not read the agent's profile.
    """
    view = _view()
    world = WorldBuilder().with_agent(view).build()

    compensation_spec = Observation()
    saga_config = SagaConfig(
        name="OrderSaga",
        steps=(
            SagaStepConfig(
                name="reserve",
                tool_name="reserve",
                compensate_tool="release",
                compensate_when=compensation_spec,
            ),
        ),
    )
    saga = SagaSystem(saga_config, now=lambda: FIXED_NOW)

    from kntgraph.concordos.base import ViewTrigger
    from kntgraph.core.event.correlation import CorrelationContext

    progress = SagaProgressComponent(
        saga_id="saga-1",
        saga_name="OrderSaga",
        current_step="reserve",
        direction="forward",
        step_order=("reserve",),
        step_states={"reserve": "completed"},
        step_results={},
        compensate_stack=("reserve",),
        started_at=FIXED_NOW,
    )
    trigger = ViewTrigger(
        agent_id=view.agent_id,
        event_type="tool.reserve.failed",
        event_id=None,
        data={},
        correlation=CorrelationContext(correlation_id=uuid4()),
    )
    saga._on_saga_timeout(world, view, progress, trigger)

    assert compensation_spec.tier == "vip"
    assert compensation_spec.status == "created"
