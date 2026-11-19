# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pickle
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import DomainComponent, World, domain_component
from kntgraph.core.world.components import ToolCallCompletion, ToolCallRequest
from kntgraph.core.world.projection_tool_calls import project_tool_calls
from kntgraph.core.world.view import AgentView
from kntgraph.core.world.world import _apply_event
from kntgraph.testing.world_builder import AgentViewBuilder


@domain_component("test.component.loaded")
@dataclass(frozen=True)
class MockComponent(DomainComponent):
    value: int


@dataclass(frozen=True)
class SiblingMockComponent(DomainComponent):
    """A second component sharing ``MockComponent``'s base.

    Used to reproduce the ambiguity of a super-type lookup: both
    classes derive from ``DomainComponent``.
    """

    value: int


@pytest.fixture(autouse=True)
def _ensure_mock_component_registered() -> None:
    """Re-register ``MockComponent`` before every test.

    The ``@domain_component`` decorator runs at module-import
    time, but other test files in this suite (``test_domain_component_registry``)
    have an autouse fixture that calls ``reset_domain_registry()``
    on setup/teardown. If that file runs first, the registry is
    cleared and the imported ``MockComponent`` mapping is lost --
    the fold then cannot hydrate the typed component and three
    tests in this file fail. Re-applying the decorator before
    every test is idempotent (the ``is`` check inside ``domain_component``
    sees the same class object) and makes this file self-contained.
    """
    domain_component("test.component.loaded")(MockComponent)
    yield


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


def test_agent_view_get_component_does_not_match_a_subclass():
    """A base type never resolves to a subclass.

    Regression. The fallback matched by ``isinstance``, so asking for
    a base class returned whichever component the bag happened to
    hold first. The result depended on the bag's insertion order --
    and the framework's core invariant is ``World = fold(events)``:
    the same events must fold into the same view, every time, on
    every machine.

    The match is now by exact class. The bag is keyed by class, so
    the lookup asks the bag the question the bag can answer.
    """
    first = MockComponent(value=1)
    second = SiblingMockComponent(value=2)
    view = AgentView(
        agent_id="a-1",
        components={MockComponent: first, SiblingMockComponent: second},
    )

    assert view.get_component(DomainComponent) is None


def test_agent_view_get_component_is_independent_of_insertion_order():
    """The same components in a different order give the same answer."""
    first = MockComponent(value=1)
    second = SiblingMockComponent(value=2)
    forward = AgentView(
        agent_id="a-1",
        components={MockComponent: first, SiblingMockComponent: second},
    )
    reversed_ = AgentView(
        agent_id="a-1",
        components={SiblingMockComponent: second, MockComponent: first},
    )

    assert forward.get_component(DomainComponent) is reversed_.get_component(
        DomainComponent
    )


def test_agent_view_get_component_prefers_the_exact_class():
    """The exact class key wins even when a sibling shares its base.

    The bag holds one value per class key, so the exact key is
    unambiguous by construction.
    """
    exact = MockComponent(value=1)
    sibling = SiblingMockComponent(value=2)
    view = AgentView(
        agent_id="a-1",
        components={MockComponent: exact, SiblingMockComponent: sibling},
    )

    assert view.get_component(MockComponent) is exact


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


# ---------------------------------------------------------------------------
# Ownership rule regression tests (ADR-067): a domain event writes only the
# component keys it produced itself; every other derived key is untouchable.
# ---------------------------------------------------------------------------


def test_registered_class_key_last_event_wins():
    """Two events of the same registered event_type on the same
    agent: the LAST one wins for the typed class key. This is
    the class-key mirror of ``test_domain_replaces_components``
    and the regression pin for the First-Event-Wins freeze found
    in the soldi/backoffice issue (``new_components.update(preserved)``
    used to let the stale component overwrite the fresh one)."""
    first = Event.create(
        event_type="test.component.loaded",
        agent_id="a-1",
        event_class="domain",
        data={"value": 1},
        correlation=CorrelationContext.new(correlation_id="corr-1"),
    )
    second = Event.create(
        event_type="test.component.loaded",
        agent_id="a-1",
        event_class="domain",
        data={"value": 2},
        correlation=CorrelationContext.new(correlation_id="corr-2"),
    )

    world = World.empty().with_event(first).with_event(second)

    comp = world.get_agent("a-1").get_component(MockComponent)
    assert comp is not None
    assert comp.value == 2


def test_registered_class_key_survives_unrelated_domain_event():
    """A registered typed component survives a subsequent domain
    event of a DIFFERENT event_type (the derived-preservation
    contract), and the unrelated event installs its own slot."""
    first = _event("a-1", "test.component.loaded")
    second = _event("a-1", "some.other.event")

    world = World.empty().with_event(first).with_event(second)
    view = world.get_agent("a-1")

    comp = view.get_component(MockComponent)
    assert comp is not None
    assert comp.value == 1
    assert view.components["some.other.event"]["value"] == 1


def test_overlay_owned_string_key_never_overwritten_by_domain_event():
    """A domain event whose ``event_type`` is literally
    ``"tool_requests"`` must NOT clobber the overlay-owned slot;
    the overlay re-derives it from the events after the fold.
    The collision is surfaced as a WARNING log (A'), not as an
    error — the fold still succeeds."""
    existing = AgentView(
        agent_id="a-1",
        components={"tool_requests": {"req-1": "in-flight"}},
    )
    event = _event("a-1", "tool_requests")

    updated = _apply_event(existing, event)

    assert updated.components["tool_requests"] == {"req-1": "in-flight"}


def test_memory_class_key_survives_domain_event():
    """The legacy memory components (ADR-042) are never re-derived
    by a domain event (they are not in the ``@domain_component``
    registry), so a subsequent domain event preserves them."""
    from kntgraph.core.components.memory import ProfileComponent

    profile = ProfileComponent(
        tenant_id="t-1",
        user_id="u-1",
        preferences={"lang": "pt-BR"},
        tier="standard",
        created_at=1.0,
        updated_at=1.0,
    )
    existing = AgentView(
        agent_id="profile:t-1:u-1",
        components={ProfileComponent: profile},
    )
    event = _event("profile:t-1:u-1", "user.intent")

    updated = _apply_event(existing, event)

    assert updated.components[ProfileComponent] is profile
    assert updated.components["user.intent"]["value"] == 1


def test_tool_event_does_not_write_overlay_slot_directly():
    """A regular ``tool.*`` event does not carry an overlay slot in
    its payload — the ``tool_requests`` slot is materialised by
    the overlay projection AFTER the fold (ADR-044), never by
    ``_apply_event`` itself. The fold result therefore has no
    ``tool_requests`` key; the overlay owns the write."""
    event = _event("a-1", "tool.chat_llm.requested")

    world = World.empty().with_event(event)

    assert "tool_requests" not in world.get_agent("a-1").components


# ---------------------------------------------------------------------------
# Typed tool-call accessors on AgentView (``view.tool_requests`` /
# ``view.tool_completions``)
# ---------------------------------------------------------------------------


class TestAgentViewToolAccessors:
    """Typed read accessors over the derived ``tool_requests`` and
    ``tool_completions`` slots on :class:`AgentView`."""

    @staticmethod
    def _sample_request(req_id: str = "req-1") -> ToolCallRequest:
        now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
        return ToolCallRequest(
            request_event_id=req_id,
            tool_name="weather_api",
            agent_id="a-1",
            params={"city": "Sao Paulo"},
            requested_at=now,
            expires_at=now + timedelta(seconds=300),
        )

    @staticmethod
    def _sample_completion(req_id: str = "req-1") -> ToolCallCompletion:
        return ToolCallCompletion(
            request_event_id=req_id,
            status="completed",
            result={"temp_c": 24},
            completed_at=datetime(2026, 11, 19, 12, 0, 1, tzinfo=UTC),
            latency_ms=1000.0,
        )

    def test_absent_slots_return_empty_mappings(self) -> None:
        """A view without tool slots yields empty typed mappings."""
        view = AgentView(agent_id="a-1")
        assert view.tool_requests == {}
        assert view.tool_completions == {}

    def test_real_fold_exposes_typed_requests_and_completions(self) -> None:
        """Folding real ``tool.<name>.*`` events via ``project_tool_calls``
        populates both typed properties with no ``Any`` escape."""
        corr = CorrelationContext.new()
        req_event = Event.create(
            event_type="tool.weather_api.requested",
            agent_id="a-1",
            event_class="domain",
            data={"city": "Curitiba"},
            correlation=corr,
        )
        comp_event = Event.create(
            event_type="tool.weather_api.completed",
            agent_id="a-1",
            event_class="domain",
            data={"temp_c": 18},
            causation_id=req_event.event_id,
            correlation=corr,
        )

        # Request-only fold: request visible, completion empty.
        pending_world = World.fold([req_event], projection=project_tool_calls)
        pending_view = pending_world.get_agent("a-1")
        assert pending_view is not None
        pending_reqs: Mapping[str, ToolCallRequest] = pending_view.tool_requests
        assert list(pending_reqs.keys()) == [str(req_event.event_id)]
        assert pending_reqs[str(req_event.event_id)].tool_name == "weather_api"
        assert pending_reqs[str(req_event.event_id)].params == {"city": "Curitiba"}
        assert pending_view.tool_completions == {}

        # Request + completion fold: both typed properties populated.
        done_world = World.fold([req_event, comp_event], projection=project_tool_calls)
        done_view = done_world.get_agent("a-1")
        assert done_view is not None
        reqs: Mapping[str, ToolCallRequest] = done_view.tool_requests
        comps: Mapping[str, ToolCallCompletion] = done_view.tool_completions
        req = reqs[str(req_event.event_id)]
        comp = comps[str(req_event.event_id)]
        assert req.request_event_id == str(req_event.event_id)
        assert comp.status == "completed"
        assert comp.result == {"temp_c": 18}

    def test_agent_view_builder_round_trip(self) -> None:
        """``AgentViewBuilder`` tool helpers surface through the typed
        properties identically."""
        req = self._sample_request("req-b")
        comp = self._sample_completion("req-b")

        view = (
            AgentViewBuilder("a-1")
            .with_tool_request(req)
            .with_tool_completion("req-b", comp)
            .build()
        )
        assert view.tool_requests == {"req-b": req}
        assert view.tool_completions == {"req-b": comp}

    def test_non_mapping_slots_return_empty(self) -> None:
        """Malformed non-Mapping slot values degrade to empty mappings."""
        view = AgentView(
            agent_id="a-1",
            components={
                "tool_requests": ["not", "a", "mapping"],
                "tool_completions": 42,
            },
        )
        assert view.tool_requests == {}
        assert view.tool_completions == {}

    def test_tampered_slot_entries_are_filtered_preserving_valid_ones(self) -> None:
        """Non-str keys and wrong-typed values are skipped while valid
        entries survive untouched."""
        req = self._sample_request("req-valid")
        comp = self._sample_completion("req-valid")

        view = AgentView(
            agent_id="a-1",
            components={
                "tool_requests": {
                    "req-valid": req,
                    "req-bad-str": "not-a-request",
                    "req-wrong-cls": comp,
                    123: req,
                },
                "tool_completions": {
                    "req-valid": comp,
                    "comp-bad-int": 99,
                    "comp-wrong-cls": req,
                    456: comp,
                },
            },
        )
        assert view.tool_requests == {"req-valid": req}
        assert view.tool_completions == {"req-valid": comp}

    def test_returned_mapping_does_not_alias_underlying_slot(self) -> None:
        """Mutating a copy or returned dict cannot corrupt ``view.components``."""
        req = self._sample_request("req-1")
        backing = {"req-1": req}
        view = AgentView(agent_id="a-1", components={"tool_requests": backing})

        exposed = view.tool_requests
        assert exposed is not backing
        assert exposed == {"req-1": req}

    def test_pickle_round_trip_preserves_typed_accessors(self) -> None:
        """Adding properties does not change ``AgentView`` slots; pickled
        checkpoints round-trip cleanly."""
        req = self._sample_request("req-p")
        comp = self._sample_completion("req-p")
        view = AgentView(
            agent_id="a-1",
            components={
                "tool_requests": {"req-p": req},
                "tool_completions": {"req-p": comp},
            },
        )

        restored = pickle.loads(pickle.dumps(view))
        assert restored.tool_requests == {"req-p": req}
        assert restored.tool_completions == {"req-p": comp}
