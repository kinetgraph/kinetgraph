# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Behaviour tests for the BusinessFSM (ADR-069 §3).

These tests follow the project's behaviour-test convention:
they build a real ``World`` via the SUT builders in
``kntgraph.testing`` and call the system against it. No
mocks on ``ReactiveDispatcher``. They run with
``KNT_REDIS_FAKE=1``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from kntgraph.concordos.fsm import FSMConfig, FSMTransition, FSMSystem
from kntgraph.concordos.specs import ContinuityToolUsed
from kntgraph.core.components.memory import ContinuityComponent
from kntgraph.core.world import DomainComponent
from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system

FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class InvoiceDomainComponent(DomainComponent):
    """A minimal invoice domain component for the FSM tests."""

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


def test_fsm_allows_valid_transition() -> None:
    """A declared transition with a satisfied guard emits
    ``fsm.transitioned`` plus the on-entry event."""
    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="validating"))
        .with_trigger("invoice.approved")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
    types = [e.event_type for e in out]
    assert "fsm.transitioned" in types
    assert "invoice.issuance_confirmed" in types
    transitioned = next(e for e in out if e.event_type == "fsm.transitioned")
    assert transitioned.data["from"] == "validating"
    assert transitioned.data["to"] == "issued"


def test_fsm_rejects_terminal_state() -> None:
    """A transition attempt from a terminal state is rejected."""
    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="paid"))
        .with_trigger("invoice.submitted")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
    assert len(out) == 1
    assert out[0].event_type == "fsm.transition_rejected"
    assert out[0].data["reason"] == "terminal_state"


def test_fsm_rejects_undeclared_transition() -> None:
    """An event with no declared transition from the current
    state is rejected with ``transition_not_declared``."""
    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="draft"))
        .with_trigger("payment.received")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
    assert len(out) == 1
    assert out[0].event_type == "fsm.transition_rejected"
    assert out[0].data["reason"] == "transition_not_declared"


def test_fsm_guard_blocks_when_nfe_emitter_was_last() -> None:
    """The guard blocks when the nfe_emitter tool was the last
    tool used in the recent continuity window."""
    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="validating"))
        .with_component(
            ContinuityComponent(
                tenant_id="t-1",
                user_id="u-1",
                last_tools={"nfe_emitter": "2026-09-07T11:59:00Z"},
            )
        )
        .with_trigger("invoice.approved")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
    assert out[0].event_type == "fsm.transition_rejected"
    assert out[0].data["reason"] == "guard_failed"


def test_fsm_guard_allows_when_nfe_emitter_not_last() -> None:
    """The guard allows when the nfe_emitter tool is NOT in the
    recent continuity window."""
    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="validating"))
        .with_component(
            ContinuityComponent(
                tenant_id="t-1",
                user_id="u-1",
                last_tools={"other_tool": "2026-09-07T11:59:00Z"},
            )
        )
        .with_trigger("invoice.approved")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
    assert any(e.event_type == "fsm.transitioned" for e in out)


def test_fsm_emits_nothing_without_domain_event() -> None:
    """An agent with no domain event (no trigger) produces no
    FSM output."""
    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="draft"))
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
    assert out == []


def test_fsm_emits_nothing_for_agent_without_component() -> None:
    """An agent that does not carry the configured component is
    skipped entirely."""
    view = AgentViewBuilder("other-1").with_trigger("invoice.approved").build()
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
    assert out == []


def test_fsm_transition_without_guard() -> None:
    """A transition with no guard (``guard is None``) is allowed
    without evaluating any Specification."""
    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="draft"))
        .with_trigger("invoice.submitted")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
    assert any(e.event_type == "fsm.transitioned" for e in out)
    transitioned = next(e for e in out if e.event_type == "fsm.transitioned")
    assert transitioned.data["to"] == "validating"


def test_fsm_transition_without_on_entry() -> None:
    """A transition whose target state has no on-entry event emits
    only ``fsm.transitioned`` (the ``on_entry`` lookup misses)."""
    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="draft"))
        .with_trigger("invoice.submitted")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
    # ``validating`` has no on-entry event declared.
    assert [e.event_type for e in out] == ["fsm.transitioned"]


def test_fsm_events_for_agent_guards_missing_component() -> None:
    """The defensive ``component is None`` guard in
    ``_events_for_agent`` returns ``[]`` (exercised directly, since
    ``query_agents`` already filters by component type)."""
    view = AgentViewBuilder("inv-1").with_trigger("invoice.approved").build()
    system = FSMSystem(invoice_fsm, now=lambda: FIXED_NOW)
    world = WorldBuilder().with_agent(view).build()
    assert system._events_for_agent(view, world) == []
