# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Behaviour tests for the SUT builders in ``kntgraph.testing.world_builder``.

These tests exercise the builders against the real ``World`` /
``AgentView`` types (no mocks) and verify that a ``WorldSystem``
reads the assembled state exactly as it would in production.
"""

from __future__ import annotations

from dataclasses import dataclass

from kntgraph.core.components.memory import ContinuityComponent
from kntgraph.core.event import Event
from kntgraph.core.world import DomainComponent, World
from kntgraph.core.world.components import ToolCallCompletion
from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system


@dataclass(frozen=True, slots=True)
class InvoiceDomainComponent(DomainComponent):
    """A minimal domain component for the FSM-style tests."""

    status: str = "draft"


def test_agent_view_builder_sets_trigger_surface() -> None:
    """``with_trigger`` sets ``domain_phase`` and ``last_event_id``
    together, and installs the data under the event_type key."""
    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="validating"))
        .with_trigger("invoice.approved", data={"nfe_required": True})
        .build()
    )
    assert view.agent_id == "inv-1"
    assert view.domain_phase == "invoice.approved"
    assert view.last_event_id is not None
    assert view.components["invoice.approved"] == {"nfe_required": True}
    assert view.get_component(InvoiceDomainComponent).status == "validating"


def test_agent_view_builder_installs_tool_completions() -> None:
    """``with_tool_completion`` populates the ``tool_completions``
    slot keyed by ``request_event_id`` (ADR-034)."""
    completion = ToolCallCompletion(
        request_event_id="req-1",
        status="completed",
        result={"nfe_required": False},
    )
    view = (
        AgentViewBuilder("agent-1")
        .with_trigger("tool.sefaz_validator.completed")
        .with_tool_completion("req-1", completion)
        .build()
    )
    slot = view.components["tool_completions"]
    assert slot["req-1"] is completion


def test_world_builder_keeps_storage_in_sync() -> None:
    """``WorldBuilder`` populates the ``ArchetypeStorage`` so
    ``query_agents`` and ``get_agent`` behave as in production."""
    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="validating"))
        .with_trigger("invoice.approved")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    assert world.get_agent("inv-1") is view
    # query_agents matches by component type membership.
    (agent_id, found) = world.query_agents(InvoiceDomainComponent).first()
    assert agent_id == "inv-1"
    assert found is view


def test_run_system_invokes_with_correlation_scope() -> None:
    """``run_system`` calls the system inside a correlation scope so
    ``Event.create`` (which requires a non-None correlation, ADR-037)
    does not raise."""

    class EmittingSystem:
        """A minimal WorldSystem that emits one event per agent.

        It reads the current correlation from the middleware
        (ADR-037) and passes it to ``Event.create`` — the same
        pattern the FSM/Saga systems use.
        """

        def __call__(self, world: World) -> list[Event]:
            from kntgraph.core.event.correlation import (
                correlation_middleware,
            )

            out: list[Event] = []
            for agent_id, view in world.views.items():
                out.append(
                    Event.create(
                        event_type="fsm.transitioned",
                        agent_id=agent_id,
                        event_class="domain",
                        data={"from": "draft", "to": "validating"},
                        correlation=correlation_middleware.current(),
                    )
                )
            return out

    view = AgentViewBuilder("inv-1").with_trigger("invoice.submitted").build()
    world = WorldBuilder().with_agent(view).build()
    events = run_system(EmittingSystem(), world)
    assert len(events) == 1
    assert events[0].event_type == "fsm.transitioned"
    assert events[0].correlation is not None


def test_world_builder_multiple_agents() -> None:
    """``WorldBuilder`` supports more than one agent."""
    a = AgentViewBuilder("a-1").with_trigger("invoice.approved").build()
    b = (
        AgentViewBuilder("a-2")
        .with_component(
            ContinuityComponent(
                tenant_id="t-1",
                user_id="u-1",
                last_tools={"nfe_emitter": "t"},
            )
        )
        .build()
    )
    world = WorldBuilder().with_agent(a).with_agent(b).build()
    assert set(world.views) == {"a-1", "a-2"}
    assert world.get_agent("a-2").get_component(ContinuityComponent) is not None


def test_agent_view_builder_with_last_event_id_override() -> None:
    """``with_last_event_id`` overrides the auto-generated id."""
    view = (
        AgentViewBuilder("inv-1")
        .with_trigger("invoice.approved")
        .with_last_event_id("custom-id")
        .build()
    )
    assert view.last_event_id == "custom-id"


def test_world_builder_skips_agent_without_components() -> None:
    """An agent view with no components is still added to the
    views dict, but contributes nothing to the storage."""
    view = AgentViewBuilder("empty-1").build()
    world = WorldBuilder().with_agent(view).build()
    assert world.get_agent("empty-1") is view
    assert world.storage.num_entities == 0


def test_run_system_awaits_async_system() -> None:
    """``run_system`` awaits a system that returns an awaitable."""

    class AsyncSystem:
        """A WorldSystem whose ``__call__`` is async."""

        async def __call__(self, world: World) -> list[Event]:
            return []

    view = AgentViewBuilder("inv-1").with_trigger("invoice.approved").build()
    world = WorldBuilder().with_agent(view).build()
    assert run_system(AsyncSystem(), world) == []
