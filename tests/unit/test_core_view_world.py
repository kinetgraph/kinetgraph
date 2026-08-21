# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World, DomainComponent, domain_component
from kntgraph.core.world.view import AgentView
from kntgraph.core.world.world import _apply_event
from dataclasses import dataclass


@domain_component("test.component.loaded")
@dataclass(frozen=True)
class MockComponent(DomainComponent):
    value: int


def _event(agent_id: str, event_type: str, *, event_class: str = "domain") -> Event:
    return Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class=event_class,
        data={"value": 1},
        correlation=CorrelationContext.new(correlation_id="corr-1"),
    )


def test_agent_view_properties_expose_lifecycle_state():
    view = AgentView(agent_id="a-1", operational_phase="terminated")
    assert view.is_terminated is True
    assert view.is_running is False

    running_view = AgentView(agent_id="a-2", operational_phase="running")
    assert running_view.is_running is True
    assert running_view.is_terminated is False


def test_world_with_event_updates_existing_agent_view():
    world = World.empty()
    first = world.with_event(_event("a-1", "agent.spawned", event_class="lifecycle"))
    second = first.with_event(_event("a-1", "document.received"))

    assert second.get_agent("a-1") is not None
    assert second.get_agent("a-1").domain_phase == "document.received"
    assert second.get_agent("a-1").operational_phase == "spawned"
    assert second.get_agent("a-1").components["document.received"]["value"] == 1


def test_world_with_tick_returns_copy_with_new_tick():
    world = World.empty(tick=2)
    updated = world.with_tick(7)

    assert updated.tick == 7
    assert updated is not world
    assert updated.views == world.views


def test_apply_event_preserves_derived_components_for_domain_events():
    existing = AgentView(
        agent_id="a-1",
        components={
            "prior": {"value": 0},
            "tool_requests": {"id": "req-1"},
        },
    )
    event = _event("a-1", "next.step")

    updated = _apply_event(existing, event)

    assert updated.components["next.step"]["value"] == 1
    assert updated.components["tool_requests"] == {"id": "req-1"}
    assert "prior" not in updated.components


def test_apply_event_preserves_custom_components_for_domain_events():
    comp = MockComponent(value=99)
    existing = AgentView(
        agent_id="a-1",
        components={
            MockComponent: comp,
        },
    )
    event = _event("a-1", "next.step")

    updated = _apply_event(existing, event)

    assert updated.get_component(MockComponent) is comp


def test_apply_event_tracks_last_event_metadata():
    view = AgentView(agent_id="a-1")
    event = Event.create(
        event_type="agent.running",
        agent_id="a-1",
        event_class="lifecycle",
        data={"value": 2},
        correlation=CorrelationContext.new(correlation_id="corr-2"),
    )

    updated = _apply_event(view, event)

    assert updated.last_event_id == str(event.event_id)
    assert updated.last_event_at == event.timestamp
    assert updated.domain_phase is None
    assert updated.operational_phase == "running"


def test_agent_view_get_component_fast_path():
    comp = MockComponent(value=42)
    view = AgentView(agent_id="a-1", components={MockComponent: comp})
    assert view.get_component(MockComponent) is comp


def test_agent_view_get_component_legacy_fallback():
    comp = MockComponent(value=10)
    view = AgentView(agent_id="a-1", components={"legacy.key": comp})
    assert view.get_component(MockComponent) is comp


def test_agent_view_get_component_not_found():
    view = AgentView(agent_id="a-1", components={"other": 123})
    assert view.get_component(MockComponent) is None


def test_world_fold_with_custom_domain_component_projection():
    # Since MockComponent is auto-registered with event_type="test.component.loaded",
    # the default projection will automatically hydrate it! We don't need custom_proj.

    events = [
        _event("a-1", "test.component.loaded"),
        _event("a-1", "some.other.event"),
    ]

    world = World.fold(events)

    view = world.get_agent("a-1")
    assert view is not None

    comp = view.get_component(MockComponent)
    assert comp is not None
    assert comp.value == 1
