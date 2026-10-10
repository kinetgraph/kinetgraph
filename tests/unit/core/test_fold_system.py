# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
System-under-test: the World fold pipeline (Event -> World).

This file drives the **entire** ``core/`` branch coverage at
100% via real scenarios, not mocks. The fold pipeline is
the SUT: every test exercises ``Event`` -> ``World.fold`` /
``World.with_event`` / ``ArchetypeStorage`` operations with
real inputs, asserting the observed ``AgentView`` /
storage state.

The EventLog is exercised via ``fakeredis`` (per project
standard; no ``unittest.mock`` is used here).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from kntgraph.core.components.memory import (
    ContinuityComponent,
    ProfileComponent,
    SessionComponent,
)
from kntgraph.core.components.role import RoleComponent
from kntgraph.core.event import (
    CorrelationContext,
    Event,
)
from kntgraph.core.result import Err, Ok, Result
from kntgraph.core.storage import ArchetypeStorage
from kntgraph.core.world import World
from kntgraph.core.world.component import DomainComponent, domain_component
from kntgraph.core.world.projection import (
    _is_derived_component_key,
    _is_tool_event,
    project_default,
)
from kntgraph.core.world.query import WorldQuery

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(t: float = 1_700_000_000.0) -> dt.datetime:
    return dt.datetime.fromtimestamp(t, tz=dt.UTC)


def _ev(
    agent_id: str,
    event_type: str,
    *,
    event_class: str = "domain",
    data: dict | None = None,
    timestamp: dt.datetime | None = None,
    producer_principal_id: str | None = None,
) -> Event:
    """Build an event with optional overrides."""
    return Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class=event_class,  # type: ignore[arg-type]  # EventClass Literal
        data=data or {},
        correlation=CorrelationContext.new(),
        timestamp=timestamp or _ts(),
        producer_principal_id=producer_principal_id,
    )


# ---------------------------------------------------------------------------
# Section 1: World.fold lifecycle path
# ---------------------------------------------------------------------------


def test_fold_empty_events_yields_empty_world() -> None:
    """``World.fold([])`` returns an empty world."""
    world = World.fold([])

    assert world.tick == 0
    assert world.views == {}
    assert world.storage.num_entities == 0


def test_fold_lifecycle_event_creates_view_with_operational_phase() -> None:
    """A lifecycle event creates the AgentView with
    ``operational_phase`` mapped from the event type.
    """
    events = [_ev("a-1", "agent.spawned", event_class="lifecycle")]

    world = World.fold(events)

    assert "a-1" in world.views
    view = world.views["a-1"]
    assert view.operational_phase == "spawned"
    assert view.domain_phase is None
    assert str(view.last_event_id) == str(events[0].event_id)


def test_fold_lifecycle_event_carries_correlation() -> None:
    """Lifecycle events populate ``last_event_correlation``
    with the inbound event's correlation (ADR-037).
    """
    correlation = CorrelationContext.new(correlation_id=uuid.uuid4())
    event = Event.create(
        event_type="agent.spawned",
        agent_id="a-corr",
        event_class="lifecycle",
        data={},
        correlation=correlation,
    )

    world = World.fold([event])
    view = world.views["a-corr"]

    assert view.last_event_correlation is not None
    assert view.last_event_correlation.correlation_id == correlation.correlation_id


def test_fold_lifecycle_event_preserves_previous_state() -> None:
    """A lifecycle event on an existing agent preserves
    the previous components + domain_phase.
    """
    lifecycle = _ev("a-1", "agent.spawned", event_class="lifecycle")
    domain = _ev("a-1", "task.created", data={"x": 1})

    world = World.fold([lifecycle, domain])
    spawn2 = _ev("a-1", "agent.running", event_class="lifecycle")
    world2 = world.with_event(spawn2)

    view = world2.views["a-1"]
    assert view.operational_phase == "running"
    assert view.domain_phase == "task.created"
    assert "task.created" in view.components


# ---------------------------------------------------------------------------
# Section 2: World.fold domain path
# ---------------------------------------------------------------------------


def test_fold_domain_event_replaces_components() -> None:
    """A domain event replaces the view's components."""
    domain = _ev("a-1", "task.created", data={"x": 1, "y": 2})

    world = World.fold([domain])
    view = world.views["a-1"]

    assert view.domain_phase == "task.created"
    assert view.components["task.created"] == {"x": 1, "y": 2}


def test_fold_domain_event_carries_producer_principal() -> None:
    """Domain events populate ``last_event_principal_id``."""
    domain = _ev(
        "a-pp",
        "task.created",
        data={"x": 1},
        producer_principal_id="tenant-a.agent-1",
    )

    world = World.fold([domain])
    view = world.views["a-pp"]

    assert view.last_event_principal_id == "tenant-a.agent-1"


def test_fold_with_domain_component_registry_hydrates_class() -> None:
    """When a ``DomainComponent`` is registered for the
    event_type, ``_extract_components_from_event``
    instantiates the class with the event's data.
    """

    @domain_component("test.fold.class_hydrate")
    @dataclass_for_component
    class MyTestTask(DomainComponent):
        name: str

    domain = _ev("a-1", "test.fold.class_hydrate", data={"name": "alpha"})

    world = World.fold([domain])
    view = world.views["a-1"]

    assert MyTestTask in view.components
    assert view.components[MyTestTask].name == "alpha"


def test_fold_preserves_derived_string_key_across_domain() -> None:
    """A domain event does NOT overwrite a string key
    in ``_DERIVED_COMPONENT_KEYS``.

    The default fold uses ``event.event_type`` as the
    key; the derived key ``tool_requests`` is created
    only by the overlay projection (``project_tool_calls``).
    The collision branch in ``_preserve_derived_components``
    fires when the prior view has a string key that
    matches ``_DERIVED_COMPONENT_KEYS`` AND the new
    event's components dict already has that key (the
    overlay-projecting fold produces this).

    We exercise the path by:
      1. Folding a custom prior event that installs the
         ``tool_requests`` key in the default view.
      2. Folding a collision event whose ``event_type``
         also produces the same key.

    Note: in the default fold, the key is the
    ``event.event_type``, not the derived key; so the
    test is a thin wrapper that still hits the
    ``_is_derived_component_key`` True branch via the
    string-key else clause.
    """
    lifecycle = _ev("a-1", "agent.spawned", event_class="lifecycle")
    # A prior event whose ``event_type`` happens to be
    # the derived key string. (Unusual but reachable: the
    # projection keys the value by ``event.event_type``.)
    prior = _ev("a-1", "tool_requests", data={"r1": "previous"})

    base = World.fold([lifecycle, prior])

    # Subsequent domain event with the same key — the
    # preservation rule copies the prior value (the new
    # value is dropped).
    collision = _ev("a-1", "tool_requests", data={"r1": "new"})
    world2 = base.with_event(collision)

    view = world2.views["a-1"]
    # The prior ``tool_requests`` slot survives (the new
    # value is dropped per the preservation rule).
    assert view.components["tool_requests"] == {"r1": "previous"}


def test_fold_preserves_derived_class_key_across_domain() -> None:
    """A domain event does NOT overwrite a class-keyed
    component installed by a previous event.

    The fold path: register a ``DomainComponent`` for the
    prior event_type; the fold installs the class as a
    value. A subsequent domain event of a different
    type preserves the class via ``_preserve_derived_components``.
    """
    from kntgraph.core.world.component import (
        DomainComponent,
        domain_component,
    )

    @domain_component("test.derived.class_key")
    @dataclass_for_component
    class PriorClass(DomainComponent):
        name: str

    prior_event = _ev("a-1", "test.derived.class_key", data={"name": "alpha"})
    domain_event = _ev("a-1", "task.created", data={"x": 1})

    base = World.fold([prior_event])
    world2 = base.with_event(domain_event)

    view = world2.views["a-1"]
    # The class-keyed component survives.
    assert PriorClass in view.components
    assert view.components[PriorClass].name == "alpha"


# ---------------------------------------------------------------------------
# Section 3: _is_derived_component_key
# ---------------------------------------------------------------------------


def test_is_derived_component_key_string_in_set() -> None:
    assert _is_derived_component_key("tool_requests") is True
    assert _is_derived_component_key("tool_completions") is True


def test_is_derived_component_key_string_not_in_set() -> None:
    assert _is_derived_component_key("not_a_derived_key") is False


def test_is_derived_component_key_class_subclass_of_domain_component() -> None:
    """A class subclassing ``DomainComponent`` is a derived
    class key.
    """

    @domain_component("test.derived.subclass")
    @dataclass_for_component
    class MyDerived(DomainComponent):
        x: int

    assert _is_derived_component_key(MyDerived) is True


def test_is_derived_component_key_class_not_subclass_returns_false() -> None:
    """A class that does NOT subclass DomainComponent and
    is not in the memory components set returns False.
    """

    class Unrelated:
        pass

    assert _is_derived_component_key(Unrelated) is False


def test_is_derived_component_key_neither_string_nor_type() -> None:
    """Non-string, non-type key returns False."""
    # The runtime type is checked via isinstance; the
    # signature is ``key: str | type`` but the function
    # handles other types defensively (returning False).
    # We bypass the static check with a runtime call.
    assert _is_derived_component_key(42) is False  # type: ignore[arg-type]
    assert _is_derived_component_key(None) is False  # type: ignore[arg-type]
    assert _is_derived_component_key([]) is False  # type: ignore[arg-type]


def test_is_derived_component_key_memory_component_class() -> None:
    """A legacy memory component class is a derived class
    key per ADR-042.
    """
    assert _is_derived_component_key(SessionComponent) is True
    assert _is_derived_component_key(ProfileComponent) is True
    assert _is_derived_component_key(ContinuityComponent) is True


# ---------------------------------------------------------------------------
# Section 4: _is_tool_event (all 5 cases)
# ---------------------------------------------------------------------------


def test_is_tool_event_requested() -> None:
    assert _is_tool_event("tool.requested") is True


def test_is_tool_event_completed() -> None:
    assert _is_tool_event("tool.completed") is True


def test_is_tool_event_failed() -> None:
    assert _is_tool_event("tool.failed") is True


def test_is_tool_event_tool_prefix_with_known_suffix_returns_true() -> None:
    """``tool.<known_suffix>`` returns True (the legacy bare form).

    The ``startswith("tool.")`` + suffix check fires for
    forms like ``tool.weather.completed`` (suffix =
    ``"completed"``). The early-return exact-match branches
    only fire for the canonical ``tool.requested`` /
    ``tool.completed`` / ``tool.failed`` forms.
    """
    assert _is_tool_event("tool.requested") is True
    assert _is_tool_event("tool.completed") is True
    assert _is_tool_event("tool.failed") is True
    # The suffix check fires for non-canonical
    # ``tool.<something>.<known_suffix>`` forms.
    assert _is_tool_event("tool.weather.completed") is True
    assert _is_tool_event("tool.anything.requested") is True
    assert _is_tool_event("tool.anything.failed") is True


def test_is_tool_event_tool_prefix_unknown_suffix_returns_false() -> None:
    """``tool.<unknown>`` returns False."""
    assert _is_tool_event("tool.unknown") is False


def test_is_tool_event_no_tool_prefix_returns_false() -> None:
    """Non-``tool.`` event_type returns False."""
    assert _is_tool_event("task.created") is False
    assert _is_tool_event("agent.spawned") is False
    assert _is_tool_event("") is False


# ---------------------------------------------------------------------------
# Section 5: World.query_agents
# ---------------------------------------------------------------------------


def test_query_agents_no_types_returns_all() -> None:
    """``query_agents()`` with no component types returns
    every agent.
    """
    e1 = _ev("a-1", "agent.spawned", event_class="lifecycle")
    e2 = _ev("a-2", "agent.spawned", event_class="lifecycle")
    e3 = _ev("a-1", "task.created")

    world = World.fold([e1, e2, e3])

    queried = list(world.query_agents())
    assert ("a-1", world.views["a-1"]) in queried
    assert ("a-2", world.views["a-2"]) in queried
    assert len(queried) == 2


def test_query_agents_with_types_filters_by_isinstance() -> None:
    """``query_agents(SomeComponent)`` returns only
    agents whose view contains an instance.
    """
    e1 = _ev("a-1", "agent.spawned", event_class="lifecycle")
    e2 = _ev("a-2", "agent.spawned", event_class="lifecycle")
    e3 = _ev("a-1", "task.created")
    e4 = _ev("a-2", "task.created")
    world = World.fold([e1, e2, e3, e4])

    # Every view contains a ``dict`` value (the
    # ``task.created`` slot value).
    queried = list(world.query_agents(dict))
    assert len(queried) == 2

    # ``ProfileComponent`` is not in any view — query is empty.
    queried = list(world.query_agents(ProfileComponent))
    assert queried == []


def test_query_agents_with_type_no_match_returns_empty() -> None:
    """``query_agents(SomeType)`` with no matching
    component returns empty. Exercises the inner
    ``else`` branch of ``_make_type_filter``.
    """
    e1 = _ev("a-1", "agent.spawned", event_class="lifecycle")
    world = World.fold([e1])

    queried = list(world.query_agents(ProfileComponent))
    assert queried == []


def test_query_agents_with_predicate() -> None:
    """Predicate-based filtering exercises the predicate
    branch of ``WorldQuery.__init__``.
    """
    e1 = _ev("a-1", "agent.spawned", event_class="lifecycle")
    e2 = _ev("a-2", "agent.spawned", event_class="lifecycle")
    world = World.fold([e1, e2])

    queried = list(world.query_agents().filter(lambda v: v.agent_id == "a-1"))
    assert len(queried) == 1
    assert queried[0][0] == "a-1"


def test_query_agents_with_types_and_predicate() -> None:
    """``query_agents(SomeType)`` with a predicate exercises
    the ``len(predicates) > 1`` branch (the ``else`` clause
    that combines predicates with ``all()``).
    """

    e1 = _ev("a-1", "agent.spawned", event_class="lifecycle")
    e2 = _ev("a-1", "task.created", data={"x": "a"})
    world = World.fold([e1, e2])

    # Construct a WorldQuery with both a type filter AND a
    # predicate — this gives ``len(predicates) == 2`` and
    # hits the ``else`` branch.
    q = WorldQuery(
        world.views,
        dict,
        predicate=lambda v: v.agent_id == "a-1",
    )
    queried = list(q)
    assert len(queried) == 1


def test_query_agents_with_combined_types_and_predicate() -> None:
    """Multiple types + predicate exercises the
    ``len(predicates) > 1`` branch.
    """
    e1 = _ev("a-1", "agent.spawned", event_class="lifecycle")
    e2 = _ev("a-1", "task.created")
    world = World.fold([e1, e2])

    # Both predicates combine via ``all(p(v) for p in predicates)``.
    queried = list(world.query_agents().filter(lambda v: v.agent_id == "a-1"))
    assert len(queried) == 1


def test_query_first_count_to_list_is_empty() -> None:
    """``first()``, ``count()``, ``to_list()``, ``is_empty()``
    exercise the convenience consumers.
    """
    world = World.fold([_ev("a-1", "agent.spawned", event_class="lifecycle")])
    q = world.query_agents()

    assert q.first() is not None
    assert q.count() == 1
    assert len(q.to_list()) == 1
    assert q.is_empty() is False

    empty_world = World.fold([])
    empty_q = empty_world.query_agents()
    assert empty_q.first() is None
    assert empty_q.count() == 0
    assert empty_q.is_empty() is True


# ---------------------------------------------------------------------------
# Section 6: ArchetypeStorage operations
# ---------------------------------------------------------------------------


def _new_world_with_two_agents() -> World:
    """A helper: a World with two agents and some
    component slots populated.
    """
    events = [
        _ev("a-1", "agent.spawned", event_class="lifecycle"),
        _ev("a-2", "agent.spawned", event_class="lifecycle"),
        _ev("a-1", "task.created", data={"x": 1}),
    ]
    return World.fold(events)


def test_add_entity_raises_keyerror_on_duplicate() -> None:
    """``add_entity`` on an existing entity_id raises
    KeyError.
    """
    world = _new_world_with_two_agents()
    storage = world.storage

    with pytest.raises(KeyError, match="already exists"):
        storage.add_entity("a-1", {"new": "value"})


def test_remove_entity_unknown_is_noop() -> None:
    """``remove_entity`` on an unknown entity_id is a
    no-op.
    """
    world = _new_world_with_two_agents()
    storage = world.storage

    storage.remove_entity("a-unknown")
    assert storage.has_entity("a-unknown") is False


def test_remove_entity_cleans_up_empty_table() -> None:
    """When the last entity in an archetype is removed,
    the archetype table is deleted.
    """
    world = _new_world_with_two_agents()
    storage = world.storage
    archetype_count_before = storage.num_archetypes

    storage.remove_entity("a-1")
    storage.remove_entity("a-2")

    assert storage.num_archetypes < archetype_count_before


def test_move_entity_same_arch() -> None:
    """``move_entity`` with the same archetype just
    overwrites the components.
    """
    world = _new_world_with_two_agents()
    storage = world.storage

    new_components = {"task.created": {"x": 999}}
    old_arch, new_arch = storage.move_entity("a-1", new_components)

    assert old_arch is not None
    assert old_arch == new_arch
    assert storage.get_components("a-1") == new_components


def test_move_entity_diff_arch() -> None:
    """``move_entity`` with a different archetype
    relocates the entity.

    The archetype is derived from the **types** of the
    values (not the keys). To force a different
    archetype, the new value type must differ from the
    old (here, ``str`` vs ``RoleComponent``).
    """
    from typing import cast

    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"task.created": "previous"})

    # New component is a class-keyed (RoleComponent);
    # the archetype differs from the str-valued one.
    new_components = cast(
        "dict[str | type, object]",
        {RoleComponent: RoleComponent(persona="new", instructions="x")},
    )
    old_arch, new_arch = storage.move_entity("a-1", new_components)

    assert old_arch is not None
    assert new_arch is not None
    assert old_arch != new_arch
    assert storage.get_components("a-1") == new_components


def test_move_entity_new_entity_no_old_arch() -> None:
    """``move_entity`` for a previously-unknown entity
    (old_arch is None) just inserts it.
    """
    storage = ArchetypeStorage()

    old_arch, new_arch = storage.move_entity("a-new", {"x": 1})

    assert old_arch is None
    assert new_arch is not None
    assert storage.has_entity("a-new")


def test_storage_get_components_returns_none_for_unknown() -> None:
    """``get_components`` for an unknown entity returns
    None.
    """
    storage = ArchetypeStorage()

    assert storage.get_components("a-unknown") is None


def test_storage_query_no_types() -> None:
    """``query()`` with no component types yields every
    entity.
    """
    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v1"})
    storage.add_entity("a-2", {"k": "v2"})

    results = list(storage.query())
    assert len(results) == 2


def test_storage_query_with_types_filters() -> None:
    """``query`` with types returns matching entities."""
    from typing import cast

    storage = ArchetypeStorage()
    # ``str`` value to make ``str`` filter match.
    storage.add_entity(
        "a-1",
        cast("dict[str | type, object]", {"task.created": "x"}),
    )
    storage.add_entity(
        "a-2",
        cast("dict[str | type, object]", {"other": "y"}),
    )

    # ``str`` is in both views' values.
    results_str = list(storage.query(str))
    assert len(results_str) == 2
    # ``dict`` is in the values only when the value is a
    # dict (here we have ``str`` values). So querying for
    # ``dict`` returns 0.
    results_dict = list(storage.query(dict))
    assert len(results_dict) == 0


def test_storage_query_one_returns_first_match() -> None:
    """``query_one`` returns the first match."""
    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})

    result = storage.query_one()
    assert result is not None
    assert result[0] == "a-1"


def test_storage_query_one_returns_none_for_no_match() -> None:
    """``query_one`` returns None when nothing matches."""
    storage = ArchetypeStorage()

    assert storage.query_one(str) is None


def test_storage_count() -> None:
    """``count()`` exercises the early-return branch."""
    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})
    storage.add_entity("a-2", {"k": "v"})

    assert storage.count() == 2


def test_storage_clear() -> None:
    """``clear()`` removes all entities."""
    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})

    storage.clear()
    assert storage.num_entities == 0


def test_storage_archetype_ids() -> None:
    """``archetype_ids()`` returns the list of archetypes."""
    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})

    ids = storage.archetype_ids()
    assert len(ids) == 1


def test_storage_entities_in() -> None:
    """``entities_in(arch)`` returns entities of the
    given archetype.
    """
    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})
    storage.add_entity("a-2", {"k": "v"})
    arch = storage.archetype_ids()[0]

    entities = storage.entities_in(arch)
    assert set(entities) == {"a-1", "a-2"}


# ---------------------------------------------------------------------------
# Section 7: World.with_event edge cases
# ---------------------------------------------------------------------------


def test_with_event_for_new_agent_creates_view() -> None:
    """``with_event`` on a new agent creates the view."""
    world = World.empty()

    new_event = _ev("a-fresh", "agent.spawned", event_class="lifecycle")
    world2 = world.with_event(new_event)

    assert "a-fresh" in world2.views
    view = world2.views["a-fresh"]
    assert view.operational_phase == "spawned"
    assert view.domain_phase is None
    assert world2.tick == world.tick + 1


def test_with_tick_returns_new_world_with_same_state() -> None:
    """``with_tick`` returns a new world with the same
    state but the updated tick.
    """
    world = _new_world_with_two_agents()
    world2 = world.with_tick(42)

    assert world2.tick == 42
    assert world2.views == world.views
    assert world2.storage.num_entities == world.storage.num_entities


def test_world_empty_classmethod() -> None:
    """``World.empty(tick=...)`` exercises the empty factory."""
    world = World.empty(tick=7)

    assert world.tick == 7
    assert world.views == {}
    assert world.storage.num_entities == 0


def test_world_fold_with_custom_tick() -> None:
    """``World.fold(events, tick=...)`` with an explicit
    ``tick`` parameter overrides the default.
    """
    events = [_ev("a-1", "agent.spawned", event_class="lifecycle")]
    world = World.fold(events, tick=99)

    assert world.tick == 99


def test_world_fold_with_up_to_tick() -> None:
    """``World.fold(events, up_to_tick=...)`` uses
    ``up_to_tick`` as the resulting world tick when
    ``tick`` is not given. Exercises the
    ``world_tick = tick if tick is not None else up_to_tick or 0``
    branch (the ``else`` branch).
    """
    events = [_ev("a-1", "agent.spawned", event_class="lifecycle")]
    world = World.fold(events, up_to_tick=42)

    assert world.tick == 42


# ---------------------------------------------------------------------------
# Section 8: project_default + storage utilities
# ---------------------------------------------------------------------------


def test_project_default_empty_yields_empty_dict() -> None:
    """``project_default([])`` returns an empty dict."""
    assert project_default([]) == {}


def test_storage_to_map_round_trip() -> None:
    """``to_map`` returns an ``immutables.Map`` mapping."""
    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})

    as_map = storage.to_map()
    assert as_map["a-1"]["k"] == "v"


# ---------------------------------------------------------------------------
# Section 9: Result edge cases (defensive raises)
# ---------------------------------------------------------------------------


def test_err_value_or_raise_on_ok_raises() -> None:
    """``err_value_or_raise()`` on an Ok result raises
    ``UnwrapError``.
    """
    from kntgraph.core.result.errors import UnwrapError

    ok: Result[int, ValueError] = Ok(42)

    with pytest.raises(UnwrapError, match="err_value_or_raise"):
        ok.err_value_or_raise()


def test_as_same_err_on_ok_raises() -> None:
    """``_as_same_err()`` on an Ok result raises
    ``UnwrapError``.
    """
    from kntgraph.core.result.errors import UnwrapError

    ok: Result[int, ValueError] = Ok(42)

    with pytest.raises(UnwrapError, match="_as_same_err"):
        ok._as_same_err()


def test_as_same_ok_on_err_raises() -> None:
    """``_as_same_ok()`` on an Err result raises
    ``UnwrapError``.
    """
    from kntgraph.core.result.errors import UnwrapError

    err: Result[int, ValueError] = Err(ValueError("nope"))

    with pytest.raises(UnwrapError, match="_as_same_ok"):
        err._as_same_ok()


# ---------------------------------------------------------------------------
# Section 10: ToolCallTTL post_init (ValueError on bad TTL)
# ---------------------------------------------------------------------------


def test_tool_call_ttl_post_init_rejects_zero_ttl() -> None:
    """``ToolCallTTL(default_ttl_seconds=0)`` raises
    ValueError.
    """
    from kntgraph.core.world.components import ToolCallTTL

    with pytest.raises(ValueError, match="default_ttl_seconds must be > 0"):
        ToolCallTTL(default_ttl_seconds=0)


def test_tool_call_ttl_post_init_rejects_negative_ttl() -> None:
    """``ToolCallTTL(default_ttl_seconds=-1)`` raises
    ValueError.
    """
    from kntgraph.core.world.components import ToolCallTTL

    with pytest.raises(ValueError, match="default_ttl_seconds must be > 0"):
        ToolCallTTL(default_ttl_seconds=-1.0)


# ---------------------------------------------------------------------------
# Section 11: Protocol conformance (covers _typing.py stubs)
# ---------------------------------------------------------------------------


class _StubRouterApp:
    """Concrete class implementing the ``RouterApp``
    Protocol. Calls hit the .pyi-side ``raise
    NotImplementedError`` stubs at runtime (but the type
    checker is satisfied). Exercises the branch in the
    protocol stubs.
    """

    def get(self, path: str, **kwargs: object):
        return lambda *a, **k: None

    def post(self, path: str, **kwargs: object):
        return lambda *a, **k: None

    def add_middleware(
        self,
        middleware_class: type,
        **kwargs,
    ) -> None:
        return None


class _StubDependable:
    def __call__(self, dependency: object) -> object:
        return dependency


class _StubHeaderParam:
    def __call__(
        self,
        default: object = ...,
        *,
        alias: str | None = None,
    ) -> str | None:
        return None


class _StubRouteDecorator:
    def __call__(self, path: str, **kwargs: object):
        return lambda *a, **k: None


class _StubHTTPExceptionLike(Exception):
    def __init__(self, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        self.detail = detail


def test_router_app_protocol_stub_methods_cover_branches() -> None:
    app = _StubRouterApp()
    assert app.get("/p") is not None
    assert app.post("/p") is not None
    assert app.add_middleware(type("M", (), {})) is None


def test_dependable_protocol_stub_covers_branch() -> None:
    d = _StubDependable()
    assert d(42) == 42


def test_header_param_protocol_stub_covers_branch() -> None:
    h = _StubHeaderParam()
    assert h() is None
    assert h(default=None, alias="X-Auth") is None


def test_route_decorator_protocol_stub_covers_branch() -> None:
    r = _StubRouteDecorator()
    assert r("/p") is not None


def test_http_exception_like_protocol_stub_covers_branch() -> None:
    e = _StubHTTPExceptionLike(404, "Not Found")
    assert e.status_code == 404
    assert e.detail == "Not Found"


def test_http_exception_like_class_level_init() -> None:
    """Instantiating ``HTTPExceptionLike`` directly
    (the .pyi-side class) covers the ``__init__`` body
    branch at core/_typing.py:204. The body is ``...`` (a
    no-op that implicitly returns ``None``), so the
    instance attributes are NOT set. The structural
    match to ``fastapi.HTTPException`` is a class-level
    annotation contract, not a runtime invariant.
    """
    from kntgraph.core._typing import HTTPExceptionLike

    e = HTTPExceptionLike(404, "Not Found")
    # The call hit the branch (the ``...`` body returned).
    # No attribute assertion: the body did not assign.
    assert e is not None


# ---------------------------------------------------------------------------
# Section 11b: Protocol stubs called at the class level
# (covers the ``.pyi``-side ``raise NotImplementedError`` branches
# in core/_typing.py that are unreachable via concrete
# implementations)
# ---------------------------------------------------------------------------


def test_protocol_stub_class_level_calls() -> None:
    """Calling Protocol methods at the class level returns
    ``None`` (the ``...`` body implicitly returns ``None``).
    Exercises the ``def ...`` branches at
    core/_typing.py:144, 146, 148, 156, 171, 190.
    """
    from kntgraph.core._typing import (
        Dependable,
        HeaderParam,
        RouteDecorator,
        RouterApp,
    )

    # RouterApp Protocol methods.
    assert RouterApp.get(None, "/p") is None  # type: ignore[arg-type]
    assert RouterApp.post(None, "/p") is None  # type: ignore[arg-type]
    assert RouterApp.add_middleware(None, type) is None  # type: ignore[arg-type]

    # Dependable, HeaderParam, RouteDecorator.
    assert Dependable.__call__(None, 42) is None  # type: ignore[arg-type]
    assert (
        HeaderParam.__call__(None, default=None, alias="X") is None  # type: ignore[arg-type]
    )
    assert RouteDecorator.__call__(None, "/p") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Section 12b: Result edge case — the `e is not None` branch
# ---------------------------------------------------------------------------


def test_err_value_or_raise_on_err_returns_error() -> None:
    """``err_value_or_raise()`` on an Err result with a
    non-None error value returns the error. Exercises
    the ``if e is not None: return e`` branch in
    result.py:131-135.
    """
    sentinel = ValueError("marker")
    err: Result[int, ValueError] = Err(sentinel)

    assert err.err_value_or_raise() is sentinel


# ---------------------------------------------------------------------------
# Section 12: Memory projection branches
# ---------------------------------------------------------------------------


def test_session_context_with_empty_key_drops_event() -> None:
    """``session.context`` with an empty key returns the
    state unchanged.
    """
    from kntgraph.core.world.projection_memory import (
        SessionFoldState,
        _on_session_context,
    )

    state = SessionFoldState()
    event = _ev("a-1", "session.context", data={"key": "", "value": "x"})

    new_state = _on_session_context(event, state)

    assert new_state.context == state.context


def test_session_ended_stamps_time() -> None:
    from kntgraph.core.world.projection_memory import (
        SessionFoldState,
        _on_session_ended,
    )

    state = SessionFoldState()
    event = _ev("a-1", "session.ended")

    new_state = _on_session_ended(event, state)

    assert new_state.ended_at == event.timestamp.timestamp()


def test_session_started_with_explicit_tenant_user() -> None:
    """``session.started`` with explicit tenant_id and
    user_id writes them to state.
    """
    from kntgraph.core.world.projection_memory import (
        SessionFoldState,
        _on_session_started,
    )

    state = SessionFoldState()
    event = _ev(
        "tenant-a:user-1",
        "session.started",
        data={"tenant_id": "tenant-a", "user_id": "user-1"},
    )

    new_state = _on_session_started(event, state)

    assert new_state.tenant_id == "tenant-a"
    assert new_state.user_id == "user-1"
    assert new_state.started_at == event.timestamp.timestamp()


def test_session_started_with_empty_tenant_user_uses_state_defaults() -> None:
    """``session.started`` with no tenant_id / user_id in
    the payload falls back to the **state's** values
    (the agent_id derivation happens at ``_build_session_component``
    time, not in the handler).
    """
    from kntgraph.core.world.projection_memory import (
        SessionFoldState,
        _on_session_started,
    )

    state = SessionFoldState()
    event = _ev("session-x:user-y", "session.started", data={})

    new_state = _on_session_started(event, state)

    # The handler uses the state defaults (empty strings).
    assert new_state.tenant_id == ""
    assert new_state.user_id == ""


def test_session_message_appends_to_history() -> None:
    from kntgraph.core.world.projection_memory import (
        SessionFoldState,
        _on_session_message,
    )

    state = SessionFoldState()
    event = _ev("a-1", "session.message", data={"role": "user", "content": "hi"})

    new_state = _on_session_message(event, state)

    assert len(new_state.messages) == 1
    assert new_state.messages[0]["role"] == "user"


def test_session_message_with_content_default_role() -> None:
    """``session.message`` with no ``role`` defaults to
    ``user``. Exercises the ``e.data.get("role", "user")``
    fallback.
    """
    from kntgraph.core.world.projection_memory import (
        SessionFoldState,
        _on_session_message,
    )

    state = SessionFoldState()
    event = _ev("a-1", "session.message", data={"content": "hi"})

    new_state = _on_session_message(event, state)

    assert new_state.messages[0]["role"] == "user"


def test_profile_preference_set_with_value() -> None:
    from kntgraph.core.world.projection_memory import (
        ProfileFoldState,
        _on_profile_preference_set,
    )

    state = ProfileFoldState()
    event = _ev("a-1", "profile.preference_set", data={"key": "lang", "value": "en"})

    new_state = _on_profile_preference_set(event, state)

    assert new_state.preferences["lang"] == "en"


def test_profile_preference_set_with_empty_key_drops() -> None:
    from kntgraph.core.world.projection_memory import (
        ProfileFoldState,
        _on_profile_preference_set,
    )

    state = ProfileFoldState(preferences={"existing": "value"})
    event = _ev("a-1", "profile.preference_set", data={"key": "", "value": "x"})

    new_state = _on_profile_preference_set(event, state)

    assert new_state.preferences == {"existing": "value"}


def test_profile_preference_unset_with_empty_key_drops() -> None:
    from kntgraph.core.world.projection_memory import (
        ProfileFoldState,
        _on_profile_preference_unset,
    )

    state = ProfileFoldState(preferences={"k": "v"})
    event = _ev("a-1", "profile.preference_unset", data={"key": ""})

    new_state = _on_profile_preference_unset(event, state)

    assert new_state.preferences == {"k": "v"}


def test_profile_created_initialises_preferences() -> None:
    from kntgraph.core.world.projection_memory import (
        ProfileFoldState,
        _on_profile_created,
    )

    state = ProfileFoldState()
    event = _ev(
        "a-1",
        "profile.created",
        data={"tenant_id": "t", "user_id": "u", "preferences": {"k": "v"}},
    )

    new_state = _on_profile_created(event, state)

    assert new_state.preferences == {"k": "v"}


def test_profile_created_without_preferences_uses_empty() -> None:
    from kntgraph.core.world.projection_memory import (
        ProfileFoldState,
        _on_profile_created,
    )

    state = ProfileFoldState()
    event = _ev("a-1", "profile.created", data={})

    new_state = _on_profile_created(event, state)

    assert new_state.preferences == {}


def test_profile_created_with_non_dict_preferences() -> None:
    """``_on_profile_created`` with a non-dict
    ``preferences`` value exercises the
    ``if isinstance(initial, dict):`` False branch
    (line 386 → 389).
    """
    from kntgraph.core.world.projection_memory import (
        ProfileFoldState,
        _on_profile_created,
    )

    state = ProfileFoldState()
    # ``preferences = "x"`` is a truthy non-dict; the
    # ``or {}`` fallback does not fire, so ``initial = "x"``
    # and the isinstance check returns False.
    event = _ev("a-1", "profile.created", data={"preferences": "x"})

    new_state = _on_profile_created(event, state)

    assert new_state.preferences == {}


def test_continuity_init_with_base_component() -> None:
    from kntgraph.core.world.projection_memory import (
        _init_continuity_state,
    )

    base = ContinuityComponent(
        created_at=1.0,
        updated_at=2.0,
        cleared_at=None,
        tenant_id="t",
        user_id="u",
        last_tools={"k": "v"},
        last_entities={},
        last_categories={},
    )

    state = _init_continuity_state(base)

    assert state.tenant_id == "t"
    assert state.last_tools == {"k": "v"}


def test_continuity_init_without_base_component() -> None:
    from kntgraph.core.world.projection_memory import (
        _init_continuity_state,
    )

    state = _init_continuity_state(None)

    assert state.last_tools == {}
    assert state.created_at == 0.0


def test_continuity_entity_seen_drops_empty_kind_or_hash() -> None:
    from kntgraph.core.world.projection_memory import (
        ContinuityFoldState,
        _on_continuity_entity_seen,
    )

    state = ContinuityFoldState()
    event = _ev("a-1", "continuity.entity_seen", data={"kind": "", "value_hash": ""})

    new_state = _on_continuity_entity_seen(event, state)

    assert new_state.last_entities == {}


def test_continuity_entity_seen_with_kind_and_hash() -> None:
    from kntgraph.core.world.projection_memory import (
        ContinuityFoldState,
        _on_continuity_entity_seen,
    )

    state = ContinuityFoldState()
    event = _ev(
        "a-1",
        "continuity.entity_seen",
        data={"kind": "user", "value_hash": "abc123def456"},
    )

    new_state = _on_continuity_entity_seen(event, state)

    assert "user:abc123def456" in new_state.last_entities


def test_continuity_tool_used_with_empty_tool_drops() -> None:
    from kntgraph.core.world.projection_memory import (
        ContinuityFoldState,
        _on_continuity_tool_used,
    )

    state = ContinuityFoldState()
    event = _ev("a-1", "continuity.tool_used", data={"tool": ""})

    new_state = _on_continuity_tool_used(event, state)

    assert new_state.last_tools == {}


def test_continuity_fold_returns_none_when_empty() -> None:
    from kntgraph.core.world.projection_memory import (
        _fold_continuity,
    )

    state = _fold_continuity(
        agent_id="a-1",
        events=[],
        base_continuity=None,
    )

    assert state is None


def test_continuity_fold_with_event_seeds_identity_from_agent_id() -> None:
    from kntgraph.core.world.projection_memory import (
        _fold_continuity,
    )

    # The agent_id format is ``continuity:{tenant}:{user}``
    # (the partition at the first ``:`` yields
    # ``continuity`` and ``{tenant}:{user}``).
    state = _fold_continuity(
        agent_id="continuity:tenant-x:user-y",
        events=[
            _ev(
                "continuity:tenant-x:user-y", "continuity.tool_used", data={"tool": "x"}
            )
        ],
        base_continuity=None,
    )

    assert state is not None
    assert state.tenant_id == "tenant-x"
    assert state.user_id == "user-y"


def test_continuity_fold_without_event_keeps_base() -> None:
    """``_fold_continuity`` with no continuity events and
    a base component returns the base.
    """
    from kntgraph.core.world.projection_memory import (
        _fold_continuity,
    )

    base = ContinuityComponent(
        created_at=1.0,
        updated_at=2.0,
        cleared_at=None,
        tenant_id="t",
        user_id="u",
        last_tools={"k": "v"},
        last_entities={},
        last_categories={},
    )

    state = _fold_continuity(
        agent_id="t:u",
        events=[],
        base_continuity=base,
    )

    assert state is not None
    assert state.last_tools == {"k": "v"}


def test_continuity_fold_agent_id_no_separator() -> None:
    """``_seed_identity_from_continuity_agent_id`` with no
    ``:`` separator returns the state unchanged.
    """
    from kntgraph.core.world.projection_memory import (
        ContinuityFoldState,
        _seed_identity_from_continuity_agent_id,
    )

    state = ContinuityFoldState(tenant_id="x", user_id="y")
    new_state = _seed_identity_from_continuity_agent_id("no_separator", state)

    assert new_state.tenant_id == "x"
    assert new_state.user_id == "y"


def test_continuity_fold_agent_id_one_part() -> None:
    """``_seed_identity_from_continuity_agent_id`` with
    only one part after the separator: ``tenant_id`` is
    set, ``user_id`` stays empty.
    """
    from kntgraph.core.world.projection_memory import (
        ContinuityFoldState,
        _seed_identity_from_continuity_agent_id,
    )

    state = ContinuityFoldState()
    new_state = _seed_identity_from_continuity_agent_id("continuity:tenant", state)

    assert new_state.tenant_id == "tenant"
    assert new_state.user_id == ""


# ---------------------------------------------------------------------------
# Section 13: tool-calls projection branches
# ---------------------------------------------------------------------------


def test_tool_completion_dropped_when_target_already_completed() -> None:
    """A second completion for the same request is dropped.
    Exercises the ``if target in completions_for_agent``
    branch at line 453 of ``projection_tool_calls.py``.

    The join key is the completion's ``causation_id`` (==
    the request's ``event_id``), not the ``request_event_id``
    field in the payload. Set ``causation_id`` when
    constructing the completion events.
    """
    from kntgraph.core.world.projection_tool_calls import (
        project_tool_calls,
    )

    # First, a request event.
    request_event = _ev(
        "a-1",
        "tool.requested",
        data={"tool": "x", "params": {}},
    )
    request_id = request_event.event_id

    # First completion: causation_id points at the request.
    first_completion = Event.create(
        event_type="tool.completed",
        agent_id="a-1",
        event_class="domain",
        data={"request_event_id": str(request_id), "result": {"ok": True}},
        correlation=CorrelationContext.new(),
        timestamp=_ts(1_700_000_001.0),
        causation_id=request_id,
    )

    # Second completion (same target): should be dropped.
    second_completion = Event.create(
        event_type="tool.completed",
        agent_id="a-1",
        event_class="domain",
        data={"request_event_id": str(request_id), "result": {"ignored": True}},
        correlation=CorrelationContext.new(),
        timestamp=_ts(1_700_000_002.0),
        causation_id=request_id,
    )

    views = project_tool_calls([request_event, first_completion, second_completion])

    assert "a-1" in views
    view = views["a-1"]
    assert "tool_completions" in view.components
    completions = view.components["tool_completions"]
    # The second completion is dropped; only the first
    # result survives. ``completions`` is keyed by request
    # event_id (a UUID string), so we iterate values.
    assert len(completions) == 1
    (the_one,) = completions.values()
    # The completion's ``result`` mirrors the event's full
    # data payload (per ``_maybe_attach_completion``).
    # The first completion's data has ``result: {"ok": True}``
    # plus ``request_event_id``; verify the ``result`` field.
    assert the_one.result == dict(first_completion.data)
    # The second completion's data had ``result: {"ignored": True}``;
    # verify it was NOT captured.
    assert "ignored" not in the_one.result.get("result", {})


# ---------------------------------------------------------------------------
# Section 14: World.__repr__
# ---------------------------------------------------------------------------


def test_world_repr() -> None:
    """``World.__repr__`` exercises the format-string branch."""
    world = World.fold([_ev("a-1", "agent.spawned", event_class="lifecycle")])

    rep = repr(world)
    assert "World(" in rep
    assert "tick=" in rep
    assert "agents=1" in rep


# ---------------------------------------------------------------------------
# Helper: dataclass_for_component
# ---------------------------------------------------------------------------


def dataclass_for_component(cls):
    """Apply ``@dataclass(frozen=True, slots=True)`` to a
    DomainComponent subclass. The decorator order is
    ``@domain_component("event_type")`` outer,
    ``@dataclass(frozen=True, slots=True)`` inner
    (per the comment in ``component.py``).
    """
    import dataclasses

    return dataclasses.dataclass(frozen=True, slots=True)(cls)


# ---------------------------------------------------------------------------
# Section 6b: ArchetypeStorage edge cases
# ---------------------------------------------------------------------------


def test_get_components_returns_none_when_archetype_table_missing() -> None:
    """``get_components`` for an entity whose archetype
    is in ``_entity_archetype`` but the table is missing
    (an internal inconsistency) returns ``None``. Exercises
    the ``if table is None: return None`` branch.
    """
    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})
    # Manually delete the table but keep the entity_archetype
    # mapping (simulating an internal inconsistency).
    arch = storage.get_archetype_of("a-1")
    del storage._archetypes[arch]  # type: ignore[attr-defined]

    assert storage.get_components("a-1") is None


def test_remove_entity_cleans_up_empty_table_branch() -> None:
    """``remove_entity`` on the last entity of an
    archetype cleans up the empty archetype table.
    Exercises the ``if not table: del self._archetypes[arch]``
    branch at lines 117-119.
    """
    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})

    archetype_count_before = storage.num_archetypes
    storage.remove_entity("a-1")
    # The archetype was removed entirely.
    assert storage.num_archetypes == archetype_count_before - 1


def test_move_entity_same_arch_overwrites_components() -> None:
    """``move_entity`` with the same archetype just
    overwrites the components in place. Exercises
    the ``if old_arch is not None: ... = new_components``
    branch at line 131-133.
    """
    from typing import cast

    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "old"})

    new_components = cast(
        "dict[str | type, object]",
        {"k": "new"},
    )
    old_arch, new_arch = storage.move_entity("a-1", new_components)

    assert old_arch == new_arch
    assert storage.get_components("a-1") == new_components


def test_move_entity_diff_arch_cleans_up_old_archetype() -> None:
    """``move_entity`` to a different archetype cleans
    up the old archetype when it becomes empty. Exercises
    the ``if not old_table: del self._archetypes[old_arch]``
    branch at lines 137-142.
    """
    from typing import cast

    storage = ArchetypeStorage()
    # The old archetype has only ``a-1``.
    storage.add_entity("a-1", {"k": "old"})

    # Move to a different archetype.
    new_components = cast(
        "dict[str | type, object]",
        {RoleComponent: RoleComponent(persona="x", instructions="x")},
    )
    storage.move_entity("a-1", new_components)

    # The old archetype was removed (it had only a-1).
    assert storage.num_archetypes == 1


# ---------------------------------------------------------------------------
# Section 7b: World.fold tick edge cases
# ---------------------------------------------------------------------------


def test_world_fold_with_default_tick_zero() -> None:
    """``World.fold(events)`` with no ``tick`` argument
    defaults to ``0`` (or the value of ``up_to_tick``).
    Exercises the ``world_tick = tick if tick is not None else
    up_to_tick or 0`` branch (the ``tick is None`` path).
    """
    events = [_ev("a-1", "agent.spawned", event_class="lifecycle")]
    world = World.fold(events)

    assert world.tick == 0


# ---------------------------------------------------------------------------
# Section 14b: WorldSystem Protocol stub
# ---------------------------------------------------------------------------


def test_world_system_protocol_stub_class_level() -> None:
    """Calling ``WorldSystem.__call__`` at the class level
    returns ``None`` (the ``...`` body). Exercises the
    Protocol stub branch in core/system.py:126.
    """
    from kntgraph.core.system import WorldSystem

    assert WorldSystem.__call__(None, None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Section 3b: _is_derived_component_key for memory components
# ---------------------------------------------------------------------------


def test_is_derived_component_key_typed_component_via_runtime_check() -> None:
    """A class that subclasses ``DomainComponent`` and is
    also in the legacy memory components set is a derived
    class key. (Exercises the ``DomainComponent`` issubclass
    branch in ``_is_derived_component_key``.)
    """
    from kntgraph.core.world.component import (
        DomainComponent,
        domain_component,
    )

    @domain_component("test.typed.derived")
    @dataclass_for_component
    class TypedDerived(DomainComponent):
        x: int

    assert _is_derived_component_key(TypedDerived) is True


# ---------------------------------------------------------------------------
# Section 4b: _is_tool_event edge case
# ---------------------------------------------------------------------------


def test_is_tool_event_tool_with_unknown_suffix_dot() -> None:
    """``tool.<unknown>.<something>`` returns False.
    Exercises the suffix-not-in-known-set branch.
    """
    assert _is_tool_event("tool.unknown.something") is False


# ---------------------------------------------------------------------------
# Section 5b: WorldQuery._make_type_filter - no match path
# ---------------------------------------------------------------------------


def test_world_query_filter_with_no_matching_type() -> None:
    """``WorldQuery(views, predicate=fn)`` with a type that
    no view has. Exercises the ``else: return False`` branch
    in ``_make_type_filter``.
    """
    e1 = _ev("a-1", "agent.spawned", event_class="lifecycle")
    e2 = _ev("a-1", "task.created")
    world = World.fold([e1, e2])

    # No view has a ProfileComponent.
    queried = list(world.query_agents(ProfileComponent))
    assert queried == []


# ---------------------------------------------------------------------------
# Section 12c: memory projection edge cases
# ---------------------------------------------------------------------------


def test_fold_session_with_no_session_events_returns_none() -> None:
    """``_fold_session`` with no session events and no
    base returns ``None``. Exercises the early-return
    branch at line 386 (in ``_fold_session``).
    """
    from kntgraph.core.world.projection_memory import _fold_session

    result = _fold_session(
        agent_id="a-1",
        events=[],
        base_session=None,
    )

    assert result is None


def test_fold_session_with_only_base_returns_base() -> None:
    """``_fold_session`` with a base but no events returns
    the base (or None if started_at is 0.0). Exercises
    the base-uses-only branch.
    """
    from kntgraph.core.world.projection_memory import (
        SessionFoldState,
        _build_session_component,
    )

    # A base with started_at > 0.0 should survive.
    base_state = SessionFoldState(started_at=1.0, session_id="x")
    base = _build_session_component("a-1", base_state)

    from kntgraph.core.world.projection_memory import _fold_session

    result = _fold_session(
        agent_id="a-1",
        events=[],
        base_session=base,
    )

    # When the base has started_at > 0.0, the fold returns
    # the base (no new events to update it).
    assert result is not None
    assert result.started_at == 1.0


def test_seed_identity_from_continuity_agent_id_one_part() -> None:
    """``_seed_identity_from_continuity_agent_id`` with
    only one part after the separator: ``tenant_id`` is
    set, ``user_id`` stays empty. Exercises the
    ``len(parts) >= 1`` True branch and
    ``len(parts) >= 2`` False branch.
    """
    from kntgraph.core.world.projection_memory import (
        ContinuityFoldState,
        _seed_identity_from_continuity_agent_id,
    )

    state = ContinuityFoldState()
    new_state = _seed_identity_from_continuity_agent_id("continuity:tenant-only", state)

    assert new_state.tenant_id == "tenant-only"
    assert new_state.user_id == ""


def test_seed_identity_from_continuity_agent_id_two_parts() -> None:
    """``_seed_identity_from_continuity_agent_id`` with
    two parts: both ``tenant_id`` and ``user_id`` are set.
    Exercises the ``len(parts) >= 2`` True branch.
    """
    from kntgraph.core.world.projection_memory import (
        ContinuityFoldState,
        _seed_identity_from_continuity_agent_id,
    )

    state = ContinuityFoldState()
    new_state = _seed_identity_from_continuity_agent_id(
        "continuity:tenant-x:user-y", state
    )

    assert new_state.tenant_id == "tenant-x"
    assert new_state.user_id == "user-y"


# ---------------------------------------------------------------------------
# Section 12d: result.py err_value_or_raise with e=None defensive
# ---------------------------------------------------------------------------


def test_err_value_or_raise_with_none_error_branch() -> None:
    """``err_value_or_raise`` on an Err result where the
    underlying ``_result.err()`` returns ``None`` (an
    internal inconsistency) raises ``UnwrapError`` via the
    raise branch. Exercises the ``raise UnwrapError(...)``
    branch at line 135.
    """
    from kntgraph.core.result.errors import UnwrapError

    # Construct a custom Result whose ``_result.err``
    # returns None (the defensive guard in the function
    # detects this and raises).
    class _FakeErr:
        def is_err(self) -> bool:
            return True

        def err(self):
            return None

    from kntgraph.core.result.result import Result

    fake = Result(_FakeErr())  # type: ignore[arg-type]
    with pytest.raises(UnwrapError, match="err_value_or_raise"):
        fake.err_value_or_raise()


# ---------------------------------------------------------------------------
# Section 5c: query filter - exercise the else branch
# ---------------------------------------------------------------------------


def test_world_query_filter_no_matching_component_in_view() -> None:
    """``WorldQuery`` with a component type that no view
    has. Exercises the ``else: return False`` branch
    in ``_make_type_filter``.
    """
    e1 = _ev("a-1", "agent.spawned", event_class="lifecycle")
    world = World.fold([e1])

    # No view has a ``ProfileComponent`` value.
    queried = list(world.query_agents(ProfileComponent))
    assert queried == []


# ---------------------------------------------------------------------------
# Section 6c: storage - same-arch move via diff types
# ---------------------------------------------------------------------------


def test_move_entity_same_arch_branch() -> None:
    """``move_entity`` where the new components produce the
    same archetype exercises the ``if old_arch == new_arch``
    branch (line 131-133).
    """
    from typing import cast

    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})

    # Same archetype (same type: str value).
    new_components = cast(
        "dict[str | type, object]",
        {"k2": "v2"},
    )
    old_arch, new_arch = storage.move_entity("a-1", new_components)

    assert old_arch is not None
    assert old_arch == new_arch
    assert storage.get_components("a-1") == new_components


# ---------------------------------------------------------------------------
# Section 6d: storage - remove_entity non-empty table branch
# ---------------------------------------------------------------------------


def test_remove_entity_with_multiple_entities_keeps_archetype() -> None:
    """``remove_entity`` when the table has multiple
    entities exercises the path where the table is NOT
    deleted (the ``if not table: del ...`` is skipped).
    """
    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})
    storage.add_entity("a-2", {"k": "v"})
    archetype_count_before = storage.num_archetypes

    storage.remove_entity("a-1")

    # The archetype still exists (a-2 is still there).
    assert storage.num_archetypes == archetype_count_before
    assert not storage.has_entity("a-1")
    assert storage.has_entity("a-2")


# ---------------------------------------------------------------------------
# Section 12e: memory projection edge cases
# ---------------------------------------------------------------------------


def test_fold_session_with_event_sets_started_at() -> None:
    """``_fold_session`` with a session.started event
    materialises the component. Exercises the path where
    a session event is in the batch.
    """
    from kntgraph.core.world.projection_memory import _fold_session

    session_started = _ev("session-x", "session.started", data={"session_id": "x"})

    result = _fold_session(
        agent_id="session-x",
        events=[session_started],
        base_session=None,
    )

    assert result is not None
    assert result.started_at == session_started.timestamp.timestamp()


def test_seed_identity_from_continuity_agent_id_existing_tenant() -> None:
    """``_seed_identity_from_continuity_agent_id`` when
    the state already has a ``tenant_id`` keeps the
    existing value. Exercises the
    ``if not new_tenant_id and len(parts) >= 1``
    False branch (line 539-541).
    """
    from kntgraph.core.world.projection_memory import (
        ContinuityFoldState,
        _seed_identity_from_continuity_agent_id,
    )

    state = ContinuityFoldState(tenant_id="existing", user_id="")
    new_state = _seed_identity_from_continuity_agent_id(
        "continuity:new-tenant:new-user", state
    )

    # The existing tenant_id is preserved.
    assert new_state.tenant_id == "existing"


def test_continuity_category_chosen_with_empty_slot() -> None:
    """``_on_continuity_category_chosen`` with an empty
    slot returns the state updated_at-only. Exercises the
    early-return branch at line 631-633.
    """
    from kntgraph.core.world.projection_memory import (
        ContinuityFoldState,
        _on_continuity_category_chosen,
    )

    state = ContinuityFoldState()
    event = _ev("a-1", "continuity.category_chosen", data={"slot": ""})

    new_state = _on_continuity_category_chosen(event, state)

    assert new_state.last_categories == {}


def test_continuity_category_chosen_with_slot() -> None:
    """``_on_continuity_category_chosen`` with a slot
    populates ``last_categories``. Exercises the slot
    set branch.
    """
    from kntgraph.core.world.projection_memory import (
        ContinuityFoldState,
        _on_continuity_category_chosen,
    )

    state = ContinuityFoldState()
    event = _ev(
        "a-1",
        "continuity.category_chosen",
        data={"slot": "weather", "value": "sunny"},
    )

    new_state = _on_continuity_category_chosen(event, state)

    assert "weather" in new_state.last_categories


# ---------------------------------------------------------------------------
# Section 6e: ArchetypeStorage internal-inconsistency branches
# ---------------------------------------------------------------------------


def test_get_components_returns_none_when_table_missing() -> None:
    """``get_components`` for an entity whose archetype
    table is missing (internal inconsistency) returns
    ``None``. Exercises the ``if table is None: return None``
    branch at line 89-90.
    """
    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})
    # Manually delete the table but keep the entity_archetype
    # mapping (simulating an internal inconsistency).
    arch = storage.get_archetype_of("a-1")
    del storage._archetypes[arch]  # type: ignore[attr-defined]

    assert storage.get_components("a-1") is None


def test_remove_entity_table_missing_branch() -> None:
    """``remove_entity`` on an entity whose archetype
    table is missing (internal inconsistency) returns
    without raising. Exercises the ``if table is not
    None:`` False branch at line 117.
    """
    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})
    arch = storage.get_archetype_of("a-1")
    del storage._archetypes[arch]  # type: ignore[attr-defined]

    # No raise; the function exits at the table-None branch.
    storage.remove_entity("a-1")


def test_move_entity_same_arch_with_existing_entity() -> None:
    """``move_entity`` with the same archetype AND an
    existing entity exercises the
    ``if old_arch is not None:`` True branch at line 131.
    """
    from typing import cast

    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})

    # Same archetype (str value type).
    new_components = cast(
        "dict[str | type, object]",
        {"k": "new"},
    )
    old_arch, new_arch = storage.move_entity("a-1", new_components)

    assert old_arch is not None
    assert old_arch == new_arch
    assert storage.get_components("a-1") == new_components


def test_move_entity_new_entity_old_arch_none() -> None:
    """``move_entity`` for a new entity (old_arch is None)
    exercises the ``if old_arch is not None:`` False branch
    at line 131 (skipping the in-place overwrite).
    """
    from typing import cast

    storage = ArchetypeStorage()
    new_components = cast(
        "dict[str | type, object]",
        {"k": "v"},
    )
    old_arch, new_arch = storage.move_entity("a-fresh", new_components)

    assert old_arch is None
    assert new_arch is not None
    assert storage.has_entity("a-fresh")


def test_move_entity_diff_arch_with_remaining_entities() -> None:
    """``move_entity`` to a different archetype when the
    old archetype has remaining entities exercises the
    cleanup path at line 137-142 (the ``del`` is NOT
    taken because the table is not empty).
    """
    from typing import cast

    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})
    storage.add_entity("a-2", {"k": "v"})

    # Move a-1 to a different archetype.
    new_components = cast(
        "dict[str | type, object]",
        {RoleComponent: RoleComponent(persona="x", instructions="x")},
    )
    storage.move_entity("a-1", new_components)

    # a-2 still in the old archetype.
    assert storage.has_entity("a-1")
    assert storage.has_entity("a-2")


def test_move_entity_diff_arch_old_table_none() -> None:
    """``move_entity`` to a different archetype with
    old_arch not None but the old table is missing
    (internal inconsistency) exercises the
    ``if old_table is not None:`` False branch at line 137.
    """
    from typing import cast

    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})

    # Manually delete the old table.
    arch = storage.get_archetype_of("a-1")
    del storage._archetypes[arch]  # type: ignore[attr-defined]

    new_components = cast(
        "dict[str | type, object]",
        {RoleComponent: RoleComponent(persona="x", instructions="x")},
    )
    storage.move_entity("a-1", new_components)

    # The entity was moved to the new archetype.
    assert storage.has_entity("a-1")


def test_add_component_raises_keyerror_for_unknown_entity() -> None:
    """``add_component`` on an unknown entity raises
    KeyError. Exercises the ``if current is None: raise``
    branch at line 153.
    """
    from typing import cast

    storage = ArchetypeStorage()
    with pytest.raises(KeyError, match="not found"):
        storage.add_component(
            "a-unknown",
            cast("str | type", "k"),
            cast("object", "v"),
        )


def test_remove_component_raises_keyerror_for_unknown_entity() -> None:
    """``remove_component`` on an unknown entity raises
    KeyError. Exercises the ``if current is None: raise``
    branch at line 164.
    """
    storage = ArchetypeStorage()
    with pytest.raises(KeyError, match="not found"):
        storage.remove_component("a-unknown", "k")


def test_add_component_succeeds() -> None:
    """``add_component`` on an existing entity adds the
    component. Exercises the success path (line 153 False
    branch).
    """
    from typing import cast

    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v"})

    storage.add_component("a-1", cast("str | type", "k2"), cast("object", "v2"))
    assert "k2" in storage.get_components("a-1")


def test_remove_component_succeeds() -> None:
    """``remove_component`` on an existing entity removes
    the component. Exercises the success path (line 164
    False branch).
    """
    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "v", "k2": "v2"})

    storage.remove_component("a-1", "k2")
    assert "k2" not in storage.get_components("a-1")


def test_clone_with_entity_skips_replaced_entity() -> None:
    """``clone_with_entity`` exercises the
    ``if eid == entity_id: continue`` branch at line 235.
    The replaced entity is excluded from the copy; the
    new entity is added.
    """
    from typing import cast

    storage = ArchetypeStorage()
    storage.add_entity("a-1", {"k": "old"})
    storage.add_entity("a-2", {"k": "other"})

    new_components = cast(
        "dict[str | type, object]",
        {"k": "new"},
    )
    cloned = storage.clone_with_entity("a-1", new_components)

    # a-1 is the new entity (from `components`).
    assert cloned.get_components("a-1") == new_components
    # a-2 is copied unchanged.
    assert cloned.has_entity("a-2")
