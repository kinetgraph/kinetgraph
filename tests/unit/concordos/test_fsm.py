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

from dataclasses import dataclass, replace
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
    assert system._events_for_agent(view, world, new_events=None) == []


# ---------------------------------------------------------------------------
# ADR-074 cursor integration
# ---------------------------------------------------------------------------


class TestFSMCursor:
    """Tests for the FSM's integration with the framework
    cursor primitive (ADR-074).

    The FSM reads ``view.cursors["FSMSystem"]``. When the
    cursor matches ``view.last_event_id``, the FSM has
    already processed everything the view has and emits
    no events (replay-safety + idle-tick fast path).

    The dispatcher advances the cursor after every tick
    the FSM emitted events; these tests construct the
    cursor manually via ``dataclasses.replace``.
    """

    def test_cursor_key_is_fsm_system(self) -> None:
        """The cursor key is the explicit ``__cursor_key__``
        ClassVar, not the class name. The dispatcher uses
        ``_system_name(system)`` which prefers the
        override.
        """
        assert FSMSystem.__cursor_key__ == "FSMSystem"

    def test_no_cursor_processes_normally(self) -> None:
        """When the cursor is absent (first tick), the
        FSM processes the view as today.
        """
        view = (
            AgentViewBuilder("inv-1")
            .with_component(InvoiceDomainComponent(status="draft"))
            .with_trigger("invoice.submitted")
            .build()
        )
        world = WorldBuilder().with_agent(view).build()
        out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
        assert any(e.event_type == "fsm.transitioned" for e in out)

    def test_cursor_matching_last_event_id_emits_nothing(self) -> None:
        """When ``view.cursors["FSMSystem"]`` matches
        ``view.last_event_id``, the FSM has already
        processed this view and emits no events (replay
        safety).
        """
        view = (
            AgentViewBuilder("inv-1")
            .with_component(InvoiceDomainComponent(status="draft"))
            .with_trigger("invoice.submitted")
            .build()
        )
        # Pre-set the cursor to the trigger's event_id.
        view_with_cursor = replace(
            view,
            cursors={"FSMSystem": str(view.last_event_id)},
        )
        world = WorldBuilder().with_agent(view_with_cursor).build()
        out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
        assert out == []

    def test_cursor_different_from_last_event_id_processes_normally(self) -> None:
        """When the cursor is set but DOES NOT match
        ``view.last_event_id`` (new events since the last
        processed tick), the FSM processes the view as
        today.
        """
        view = (
            AgentViewBuilder("inv-1")
            .with_component(InvoiceDomainComponent(status="draft"))
            .with_trigger("invoice.submitted")
            .build()
        )
        # Set the cursor to a stale event_id (not the
        # current ``last_event_id``).
        view_with_cursor = replace(
            view,
            cursors={"FSMSystem": "00000000-0000-0000-0000-000000000000"},
        )
        world = WorldBuilder().with_agent(view_with_cursor).build()
        out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)
        assert any(e.event_type == "fsm.transitioned" for e in out)

    def test_replay_after_first_run_emits_nothing(self) -> None:
        """End-to-end replay safety: run the FSM once on
        a world, simulate the dispatcher advancing the
        cursor, run the FSM again on the same world —
        the second run emits nothing.
        """
        view = (
            AgentViewBuilder("inv-1")
            .with_component(InvoiceDomainComponent(status="draft"))
            .with_trigger("invoice.submitted")
            .build()
        )
        world = WorldBuilder().with_agent(view).build()
        system = FSMSystem(invoice_fsm, now=lambda: FIXED_NOW)

        # First run: cursor absent, FSM processes.
        first = run_system(system, world)
        assert any(e.event_type == "fsm.transitioned" for e in first)

        # Simulate the dispatcher advancing the cursor
        # for the agent whose view the FSM processed.
        last_event_id = str(view.last_event_id)
        view_with_cursor = replace(
            view,
            cursors={"FSMSystem": last_event_id},
        )
        world_after = WorldBuilder().with_agent(view_with_cursor).build()

        # Second run (replay): cursor matches, FSM emits
        # nothing.
        second = run_system(system, world_after)
        assert second == []

    def test_cursor_per_agent_isolation(self) -> None:
        """Each agent has its own cursor entry. The FSM
        does not advance a cursor on one agent when the
        view of another agent matches.
        """
        # Agent "a-1": cursor matches → skip.
        # Agent "a-2": cursor absent → process.
        view_a = (
            AgentViewBuilder("a-1")
            .with_component(InvoiceDomainComponent(status="draft"))
            .with_trigger("invoice.submitted")
            .build()
        )
        view_a = replace(view_a, cursors={"FSMSystem": str(view_a.last_event_id)})

        view_b = (
            AgentViewBuilder("a-2")
            .with_component(InvoiceDomainComponent(status="draft"))
            .with_trigger("invoice.submitted")
            .build()
        )

        world = WorldBuilder().with_agent(view_a).with_agent(view_b).build()
        out = run_system(FSMSystem(invoice_fsm, now=lambda: FIXED_NOW), world)

        # Only one ``fsm.transitioned`` (for a-2).
        transitioned = [e for e in out if e.event_type == "fsm.transitioned"]
        assert len(transitioned) == 1
        assert transitioned[0].agent_id == "a-2"


class TestFSMMultiEventTick:
    """Tests for multi-event tick processing.

    The dispatcher passes ``new_events`` (the events it
    just folded into the world) to every system. The FSM
    iterates them with cascading local state, emitting a
    transition per event that matches a declared rule.
    """

    def _make_event(self, agent_id: str, event_type: str) -> "Event":
        from uuid import uuid4
        from kntgraph.core.event import CorrelationContext, Event

        return Event.domain_from(
            agent_id=agent_id,
            type=event_type,
            data={},
            correlation=CorrelationContext.new(correlation_id=uuid4()),
        )

    def test_multi_event_cascade_in_same_tick(self) -> None:
        """Three events land in the same tick:
        ``draft → validating → issued``. The FSM emits
        two transitions, one per event, with cascading
        local state.
        """
        e1 = self._make_event("inv-1", "invoice.submitted")
        e2 = self._make_event("inv-1", "invoice.approved")
        e3 = self._make_event("inv-1", "invoice.approved_bypass")

        view = (
            AgentViewBuilder("inv-1")
            .with_component(InvoiceDomainComponent(status="draft"))
            .with_trigger("invoice.approved_bypass", data={"k": "v"})
            .build()
        )
        world = WorldBuilder().with_agent(view).build()
        out = run_system(
            FSMSystem(invoice_fsm, now=lambda: FIXED_NOW),
            world,
            new_events=[e1, e2, e3],
        )

        transitioned = [e for e in out if e.event_type == "fsm.transitioned"]
        # ``invoice.submitted`` transitions draft → validating.
        # ``invoice.approved`` from validating → issued (guard:
        # ContinuityToolUsed("nfe_emitter").not_() — satisfied
        # since no ContinuityComponent on the view).
        # ``invoice.approved_bypass`` from issued → ??? (no
        # transition defined → rejected).
        assert len(transitioned) == 2
        assert transitioned[0].data["from"] == "draft"
        assert transitioned[0].data["to"] == "validating"
        assert transitioned[0].data["trigger_event_id"] == str(e1.event_id)
        assert transitioned[1].data["from"] == "validating"
        assert transitioned[1].data["to"] == "issued"
        assert transitioned[1].data["trigger_event_id"] == str(e2.event_id)

        rejected = [e for e in out if e.event_type == "fsm.transition_rejected"]
        assert len(rejected) == 1
        assert rejected[0].data["reason"] == "transition_not_declared"
        assert rejected[0].data["trigger"] == "invoice.approved_bypass"
        # The rejected event's causation is the input event
        # (e3); FSM tracks this for downstream tracing.
        assert rejected[0].causation_id == e3.event_id

    def test_filters_own_emitted_events_in_next_tick(self) -> None:
        """In tick T, the FSM emits ``fsm.transitioned``.
        In tick T+1, the FSM sees its own emitted events
        in ``new_events``. It must filter them out so it
        does not emit ``fsm.transition_rejected`` for
        its own output (the framework invariant: events
        are valid after persistence, so the FSM sees
        them in T+1 — but it must not re-process them).
        """
        # Tick T+1 scenario: the world has processed all
        # events through ``issued`` (terminal-ish state
        # with on_entry already emitted).
        view = (
            AgentViewBuilder("inv-1")
            .with_component(InvoiceDomainComponent(status="issued"))
            .with_trigger(
                "fsm.transitioned",
                data={
                    "from": "draft",
                    "to": "issued",
                    "trigger": "invoice.submitted",
                    "trigger_event_id": "x",
                },
            )
            .build()
        )
        world = WorldBuilder().with_agent(view).build()

        # The dispatcher passes the FSM's own emitted
        # event from tick T as ``new_events``.
        own_emitted = self._make_event("inv-1", "fsm.transitioned")
        out = run_system(
            FSMSystem(invoice_fsm, now=lambda: FIXED_NOW),
            world,
            new_events=[own_emitted],
        )

        # Filtered: no ``fsm.transition_rejected`` noise.
        assert out == []

    def test_filters_on_entry_events_in_next_tick(self) -> None:
        """``on_entry`` events emitted in tick T are
        filtered out in tick T+1 — same logic as
        ``fsm.transitioned``.
        """
        view = (
            AgentViewBuilder("inv-1")
            .with_component(InvoiceDomainComponent(status="issued"))
            .with_trigger("invoice.issuance_confirmed", data={"state": "issued"})
            .build()
        )
        world = WorldBuilder().with_agent(view).build()

        own_on_entry = self._make_event("inv-1", "invoice.issuance_confirmed")
        out = run_system(
            FSMSystem(invoice_fsm, now=lambda: FIXED_NOW),
            world,
            new_events=[own_on_entry],
        )
        assert out == []
