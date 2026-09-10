# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Behaviour tests for the FSM state-advance projection (ADR-069 §9.2 item 1).

The ``FSMProjection`` advances the configured ``DomainComponent``'s
``state_field`` from ``fsm.transitioned`` events, so a re-fold of
the EventLog reconstructs the same state without an in-memory
cache. These tests build a real ``World`` via the SUT builders and
run the projection against it.
"""

from __future__ import annotations

from dataclasses import dataclass

from kntgraph.concordos.fsm import FSMConfig, FSMProjection, FSMTransition
from kntgraph.core.event import Event
from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.world import DomainComponent
from kntgraph.testing import AgentViewBuilder, WorldBuilder


@dataclass(frozen=True, slots=True)
class InvoiceDomainComponent(DomainComponent):
    """A minimal invoice domain component for the FSM tests."""

    status: str = "draft"
    tax_regime: str = "lucro_real"


def _config() -> FSMConfig:
    return FSMConfig(
        component_type=InvoiceDomainComponent,
        state_field="status",
        transitions={
            "draft": {"invoice.submitted": FSMTransition(to="validating")},
        },
    )


def _event(agent_id: str, event_type: str, data: dict) -> Event:
    return Event.create(
        agent_id=agent_id,
        event_type=event_type,
        event_class="domain",
        data=data,
        correlation=CorrelationContext.new(),
    )


def _run_projection(
    config: FSMConfig,
    events: list[Event],
    *,
    base: InvoiceDomainComponent | None = None,
) -> InvoiceDomainComponent | None:
    """Build a World with one agent and run the projection."""
    builder = AgentViewBuilder("inv-1")
    if base is not None:
        builder = builder.with_component(base)
    view = builder.build()
    world = WorldBuilder().with_agent(view).build()
    projection = FSMProjection(config)
    new_world = projection(world, events)
    return new_world.get_agent("inv-1").get_component(InvoiceDomainComponent)


def test_projection_advances_state_field() -> None:
    """``fsm.transitioned`` advances the component's ``state_field``."""
    events = [
        _event(
            "inv-1",
            "fsm.transitioned",
            {"from": "draft", "to": "validating", "trigger": "invoice.submitted"},
        ),
    ]
    comp = _run_projection(
        _config(), events, base=InvoiceDomainComponent(status="draft")
    )
    assert comp is not None
    assert comp.status == "validating"
    # Other fields survive.
    assert comp.tax_regime == "lucro_real"


def test_projection_ignores_rejected() -> None:
    """``fsm.transition_rejected`` does NOT advance the state."""
    events = [
        _event(
            "inv-1",
            "fsm.transition_rejected",
            {"current_state": "draft", "trigger": "payment.received", "reason": "x"},
        ),
    ]
    comp = _run_projection(
        _config(), events, base=InvoiceDomainComponent(status="draft")
    )
    assert comp is not None
    assert comp.status == "draft"


def test_projection_returns_none_without_transition() -> None:
    """An agent with no ``fsm.transitioned`` event and no base
    component produces no component (no allocation)."""
    events = [
        _event("inv-1", "user.intent", {"message": "hi"}),
    ]
    comp = _run_projection(_config(), events)
    assert comp is None


def test_projection_preserves_base_without_transition() -> None:
    """An agent with a base component but no ``fsm.transitioned``
    event in the batch keeps the base component."""
    events = [
        _event("inv-1", "user.intent", {"message": "hi"}),
    ]
    comp = _run_projection(
        _config(), events, base=InvoiceDomainComponent(status="validating")
    )
    assert comp is not None
    assert comp.status == "validating"


def test_projection_advances_multiple_transitions() -> None:
    """Multiple ``fsm.transitioned`` events in one batch advance the
    state in order."""
    events = [
        _event(
            "inv-1",
            "fsm.transitioned",
            {"from": "draft", "to": "validating", "trigger": "invoice.submitted"},
        ),
        _event(
            "inv-1",
            "fsm.transitioned",
            {"from": "validating", "to": "issued", "trigger": "invoice.approved"},
        ),
    ]
    comp = _run_projection(
        _config(), events, base=InvoiceDomainComponent(status="draft")
    )
    assert comp is not None
    assert comp.status == "issued"
