# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
world.world -- The `World` class.

A pure snapshot of the system at tick T. The World is
the value produced by folding events; it is rebuilt
every tick by the runner. Direct mutation of the
World is not exposed; systems return new events and
the runner rebuilds the world from the augmented
stream.

The `storage` field is the in-memory working set of
the projection. It is rebuilt every fold. For very
large fleets the runner shards by tenant.

Why a class (and not a TypedDict / dataclass)?

  - Copy-on-write semantics: `with_event` and
    `with_tick` return new World instances; the
    caller cannot accidentally mutate the snapshot
    another caller holds.
  - Convenience methods: `agents`, `get_agent`,
    `query_agents`, `with_event`, `with_tick` —
    small surface, used by 5+ call-sites.
  - Storage composition: the World wraps an
    ``ArchetypeStorage`` (in `core.storage`) and
    exposes the agent views as a read-only mapping.

What is NOT in the World (v1.x → v2.0):

  - `status` field on AgentState  → replaced by
    LifecycleComponent in components, derived from
    the agent's last "lifecycle" event.
  - `pending_events` on AgentState → moved out;
    events are appended to the stream by the runner.
  - `outbox` on World              → removed; the
    runner holds the "events to append" buffer.
  - `repository.save_world/get_world` → removed;
    the World is rebuilt from the stream, never
    persisted as a whole.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Mapping, Optional, Type

from ..event import Event
from ..storage import ArchetypeStorage
from .projection import (
    Projection,
    _apply_event,
    project_default,
)
from .query import WorldQuery
from .view import AgentView


class World:
    """
    A pure snapshot of the system at tick T.

    The World is the value produced by folding events. It is rebuilt
    every tick by the runner. Direct mutation of the World is not
    exposed; systems return new events and the runner rebuilds the
    world from the augmented stream.

    The `storage` field is the in-memory working set of the
    projection. It is rebuilt every fold. For very large fleets the
    runner shards by tenant.
    """

    __slots__ = ("tick", "storage", "views")

    def __init__(
        self,
        tick: int,
        storage: ArchetypeStorage,
        views: dict[str, AgentView],
    ) -> None:
        self.tick = tick
        self.storage = storage
        self.views = views

    # ------------------------------------------------------------------ build

    @classmethod
    def empty(cls, tick: int = 0) -> "World":
        return cls(tick=tick, storage=ArchetypeStorage(), views={})

    @classmethod
    def fold(
        cls,
        events: Sequence[Event],
        *,
        up_to_tick: Optional[int] = None,
        projection: Optional[Projection] = None,
        tick: Optional[int] = None,
    ) -> "World":
        """
        Pure fold: events -> World.

        If `up_to_tick` is given, only events with tick <= up_to_tick
        are considered. If `tick` is given, it is the resulting
        world.tick (defaults to the max timestamp tick of the events,
        or 0 if no events).

        `projection` defaults to `project_default`.
        """
        if projection is None:
            projection = project_default

        views = projection(events)
        storage = ArchetypeStorage()
        for agent_id, view in views.items():
            if view.components:
                storage.add_entity(agent_id, dict(view.components))
        world_tick = tick if tick is not None else up_to_tick or 0
        return cls(tick=world_tick, storage=storage, views=views)

    # ------------------------------------------------------------------ view

    @property
    def agents(self) -> Mapping[str, AgentView]:
        """All agents at this tick. Returns ``self.views``
        directly; the contract is "do not mutate"
        (frozen dataclasses inside make this safe in
        practice).
        """
        return self.views

    def get_agent(self, agent_id: str) -> Optional[AgentView]:
        return self.views.get(agent_id)

    def query_agents(self, *component_types: Type) -> WorldQuery:
        return WorldQuery(self, *component_types)

    # ------------------------------------------------------------------ ops

    def with_event(self, event: Event) -> "World":
        """
        Returns a NEW world with the given event applied via the
        default projection. Used by tests and by the runner in
        single-event scenarios.

        For batch application, prefer `World.fold`.
        """
        prev = self.views.get(event.agent_id) or AgentView(agent_id=event.agent_id)
        view = _apply_event(prev, event)

        new_storage = self.storage.clone_with_entity(event.agent_id, view.components)

        new_views = dict(self.views)
        new_views[event.agent_id] = view
        return World(tick=self.tick + 1, storage=new_storage, views=new_views)

    def with_tick(self, tick: int) -> "World":
        return World(tick=tick, storage=self.storage, views=self.views)

    # ------------------------------------------------------------------ repr

    def __repr__(self) -> str:
        return (
            f"World(tick={self.tick}, agents={len(self.views)}, "
            f"archetypes={self.storage.num_archetypes})"
        )


__all__ = ["World"]
