# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the System contract (v2.2).

A system is a pure function ``World -> list[Event]``. Both
``WorldSystem`` (the canonical Protocol) and the legacy
``ReactiveSystem`` / ``CyclicSystem`` aliases share the same
signature. No I/O, no side effects.

Tests use the ``World.empty()`` / ``World.with_event(...)``
APIs to construct states — no EventLog required.

See: ADR-018 — WorldIncremental + WorldSystem.
"""

from __future__ import annotations

import uuid

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.system import (
    CyclicSystem,
    ReactiveSystem,
    WorldSystem,
)
from kntgraph.core.world import World


def _ctx() -> CorrelationContext:
    return CorrelationContext.new(correlation_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# Reactive system — v2.2 model: looks at the World, not the event
# ---------------------------------------------------------------------------


def make_validator_reactive_system() -> ReactiveSystem:
    """
    Returns a reactive system that, after each "document.received"
    event has been folded into the World, emits a
    "document.validated" event with the same payload enriched
    with a ``validated`` flag.

    The system inspects the World directly — looking at
    agent views whose ``components`` carry a
    ``document.received`` entry but no ``document.validated`` yet.
    """

    def _system(world: World) -> list[Event]:
        out: list[Event] = []
        for agent_id, view in world.agents.items():
            received = view.components.get("document.received")
            validated = view.components.get("document.validated")
            if received is None:
                continue
            if validated is not None:
                continue
            # The received entry was stored as a dict; carry
            # the last seen event_id forward as causation.
            out.append(
                Event.create(
                    event_type="document.validated",
                    agent_id=agent_id,
                    event_class="domain",
                    data={**received, "validated": True},
                    causation_id=view.last_event_id,
                    correlation=_ctx(),
                )
            )
        return out

    return _system


class TestReactiveSystem:
    def test_world_empty_produces_no_output(self):
        """An empty World has nothing to react to."""
        sys = make_validator_reactive_system()
        assert sys(World.empty()) == []

    def test_emits_validated_when_document_received(self):
        """
        After folding a "document.received" event into the World,
        the validator emits a "document.validated" event.
        """
        sys = make_validator_reactive_system()
        received = Event.create(
            event_type="document.received",
            agent_id="a-1",
            event_class="domain",
            data={"document_id": "NF-001", "received": True},
            correlation=_ctx(),
        )
        world = World.empty().with_event(received)
        out = sys(world)
        assert isinstance(out, list)
        assert len(out) == 1
        assert out[0].event_type == "document.validated"
        # causation_id is the str form (matches last_event_id)
        assert out[0].causation_id == str(received.event_id)
        assert out[0].data["validated"] is True
        assert out[0].data["document_id"] == "NF-001"

    def test_idempotent_when_already_validated(self):
        """
        Re-running the system on a World that already has the
        validated component produces no output (deterministic
        idempotence via the World's components).
        """
        sys = make_validator_reactive_system()
        received = Event.create(
            event_type="document.received",
            agent_id="a-1",
            event_class="domain",
            data={"document_id": "NF-001", "received": True},
            correlation=_ctx(),
        )
        validated = Event.create(
            event_type="document.validated",
            agent_id="a-1",
            event_class="domain",
            data={"document_id": "NF-001", "validated": True},
            correlation=_ctx(),
        )
        # World has BOTH received and validated
        world = World.fold([received, validated])
        out = sys(world)
        assert isinstance(out, list)
        assert out == []

    def test_system_is_pure_across_invocations(self):
        """
        Same World → same events out (deterministic event_id).
        """
        sys = make_validator_reactive_system()
        received = Event.create(
            event_type="document.received",
            agent_id="a-1",
            event_class="domain",
            data={"document_id": "NF-001", "received": True},
            correlation=_ctx(),
        )
        world = World.empty().with_event(received)
        a = sys(world)
        assert isinstance(a, list)
        b = sys(world)
        assert isinstance(b, list)
        # event_ids are deterministic given the same input
        assert a[0].event_id == b[0].event_id


# ---------------------------------------------------------------------------
# Cyclic system — same shape (world) -> list[Event]
# ---------------------------------------------------------------------------


def make_cyclic_idle_kick() -> CyclicSystem:
    """
    Cyclic system that finds agents in "spawned" phase and emits
    an "agent.idle" lifecycle event for each.
    """

    def _system(world: World) -> list[Event]:
        out: list[Event] = []
        for agent_id, view in world.agents.items():
            if view.operational_phase == "spawned":
                out.append(
                    Event.create(
                        event_type="agent.idle",
                        agent_id=agent_id,
                        event_class="lifecycle",
                        data={},
                        correlation=_ctx(),
                    )
                )
        return out

    return _system


class TestCyclicSystem:
    def test_emits_idle_for_spawned_agents(self):
        sys = make_cyclic_idle_kick()
        events = [
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            ),
            Event.create(
                event_type="agent.spawned",
                agent_id="a-2",
                event_class="lifecycle",
                correlation=_ctx(),
            ),
        ]
        w = World.fold(events)
        out = sys(w)
        assert isinstance(out, list)
        assert len(out) == 2
        assert {e.agent_id for e in out} == {"a-1", "a-2"}
        assert all(e.event_type == "agent.idle" for e in out)

    def test_ignores_already_idle_agents(self):
        sys = make_cyclic_idle_kick()
        events = [
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            ),
            Event.create(
                event_type="agent.idle",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            ),
        ]
        w = World.fold(events)
        out = sys(w)
        assert isinstance(out, list)
        assert out == []

    def test_empty_world_no_output(self):
        sys = make_cyclic_idle_kick()
        w = World.empty()
        assert sys(w) == []


# ---------------------------------------------------------------------------
# Chained systems — reactive + cyclic on the same World
# ---------------------------------------------------------------------------


class TestSystemChaining:
    def test_reactive_then_replay_folds_state(self):
        """
        End-to-end: a reactive system produces a new event;
        folding it back yields the new state.
        """
        sys = make_validator_reactive_system()
        received = Event.create(
            event_type="document.received",
            agent_id="a-1",
            event_class="domain",
            data={"document_id": "NF-001", "received": True},
            correlation=_ctx(),
        )
        world_with_received = World.empty().with_event(received)
        out_events = sys(world_with_received)
        assert isinstance(out_events, list)
        validated = out_events[0]

        # Replay: fold both events and check the World
        world = World.fold([received, validated], tick=2)
        view = world.agents["a-1"]
        assert view.domain_phase == "document.validated"

    def test_idempotent_replay(self):
        """
        Re-running a system on the SAME world it just produced
        yields no output (the rule is already satisfied).
        """
        sys = make_validator_reactive_system()
        received = Event.create(
            event_type="document.received",
            agent_id="a-1",
            event_class="domain",
            data={"document_id": "NF-001", "received": True},
            correlation=_ctx(),
        )
        out_events = sys(World.empty().with_event(received))
        assert isinstance(out_events, list)
        validated = out_events[0]
        # World now has the validated event folded in
        world_with_validated = World.fold([received, validated])
        # Re-running: the rule is already satisfied
        assert sys(world_with_validated) == []


# ---------------------------------------------------------------------------
# Protocol conformance — WorldSystem / ReactiveSystem / CyclicSystem
# ---------------------------------------------------------------------------


class TestProtocolAliases:
    def test_aliases_are_same_protocol(self):
        """``ReactiveSystem`` and ``CyclicSystem`` are the same
        Protocol as ``WorldSystem`` (v2.2 unification)."""
        assert ReactiveSystem is WorldSystem
        assert CyclicSystem is WorldSystem

    def test_one_arg_callable_satisfies_protocol(self):
        """A callable with the right shape satisfies the Protocol."""

        def my_system(world: World) -> list[Event]:
            return []

        # ``runtime_checkable`` lets us isinstance-check
        assert isinstance(my_system, WorldSystem)


# ---------------------------------------------------------------------------
# World inspection — systems read the World's agents dict directly
# ---------------------------------------------------------------------------


class TestWorldInspection:
    def test_agents_dict_after_fold(self):
        """Systems use ``world.agents`` (a Mapping) to iterate
        the post-fold state. The dict is keyed by agent_id;
        each value is an AgentView carrying components, phases,
        timestamps."""
        events = [
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            ),
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"document_id": "NF-001"},
                correlation=_ctx(),
            ),
        ]
        world = World.fold(events)
        assert "a-1" in world.agents
        view = world.agents["a-1"]
        assert view.operational_phase == "spawned"
        assert view.domain_phase == "document.received"
        # Components is a dict keyed by event_type (default projection)
        assert "document.received" in view.components


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
