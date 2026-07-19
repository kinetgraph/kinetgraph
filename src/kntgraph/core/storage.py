# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Archetype-keyed in-memory storage for ECS.

This is the working set the World holds. It is rebuilt from the
event log at every tick (fold of events). It is mutable INSIDE the
storage object, but the World facade presents an immutable view of
it: every World operation that would mutate the underlying storage
returns a new World with a fresh storage.

Layout:
    _archetypes: dict[ArchetypeId, dict[entity_id, dict[slot, component]]]
    _entity_archetype: dict[entity_id, ArchetypeId]

Operations:
    - add_entity / remove_entity: O(1)
    - move_entity (archetype change): O(1) amortized
    - query(*types): O(K) where K = archetypes matching the query

This is a pure in-memory implementation. Storage volume grows linearly
with the number of active agents in a tick. For very large fleets,
the World fold reads only the events relevant to the tick window
(see stream/projection.py).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Type

from immutables import Map

from ._typing import ComponentT
from .archetype import ArchetypeId


# Local alias kept for backward compatibility with
# downstream imports (`from kntgraph.core.storage
# import Component`). New code should import
# ``ComponentT`` from :mod:`kntgraph.core._typing`.
Component = ComponentT


class ArchetypeStorage:
    """
    In-memory archetype-keyed storage. Pure Python, no native deps.

    Mutations are in-place (we need fast O(1) for tick loops), but
    the World facade does NOT expose them directly — every change
    goes through `World.add/remove/move` which return a new World.
    """

    def __init__(self) -> None:
        self._archetypes: dict[ArchetypeId, dict[str, dict[str | type[Any], ComponentT]]] = {}
        self._entity_archetype: dict[str, ArchetypeId] = {}

    @property
    def num_entities(self) -> int:
        return len(self._entity_archetype)

    @property
    def num_archetypes(self) -> int:
        return len(self._archetypes)

    def has_entity(self, entity_id: str) -> bool:
        return entity_id in self._entity_archetype

    def get_archetype_of(self, entity_id: str) -> ArchetypeId | None:
        return self._entity_archetype.get(entity_id)

    def get_components(self, entity_id: str) -> dict[str | type[Any], ComponentT] | None:
        arch = self._entity_archetype.get(entity_id)
        if arch is None:
            return None
        table = self._archetypes.get(arch)
        if table is None:
            return None
        return table.get(entity_id)

    def archetype_ids(self) -> list[ArchetypeId]:
        return list(self._archetypes.keys())

    def entities_in(self, archetype_id: ArchetypeId) -> list[str]:
        return list(self._archetypes.get(archetype_id, {}).keys())

    def add_entity(
        self,
        entity_id: str,
        components: dict[str | type[Any], ComponentT],
    ) -> ArchetypeId:
        if entity_id in self._entity_archetype:
            raise KeyError(f"Entity {entity_id!r} already exists")

        archetype_id = self._derive_archetype(components)
        self._archetypes.setdefault(archetype_id, {})[entity_id] = components
        self._entity_archetype[entity_id] = archetype_id
        return archetype_id

    def remove_entity(self, entity_id: str) -> None:
        arch = self._entity_archetype.pop(entity_id, None)
        if arch is None:
            return
        table = self._archetypes.get(arch)
        if table is not None:
            table.pop(entity_id, None)
            if not table:
                del self._archetypes[arch]

    def move_entity(
        self,
        entity_id: str,
        new_components: dict[str | type[Any], ComponentT],
    ) -> tuple[ArchetypeId | None, ArchetypeId]:
        old_arch = self._entity_archetype.get(entity_id)
        new_arch = self._derive_archetype(new_components)

        if old_arch == new_arch:
            if old_arch is not None:
                self._archetypes[old_arch][entity_id] = new_components
            return old_arch, new_arch

        if old_arch is not None:
            old_table = self._archetypes.get(old_arch)
            if old_table is not None:
                old_table.pop(entity_id, None)
                if not old_table:
                    del self._archetypes[old_arch]

        self._archetypes.setdefault(new_arch, {})[entity_id] = new_components
        self._entity_archetype[entity_id] = new_arch
        return old_arch, new_arch

    def add_component(
        self,
        entity_id: str,
        name: str | type[Any],
        component: ComponentT,
    ) -> tuple[ArchetypeId | None, ArchetypeId]:
        current = self.get_components(entity_id)
        if current is None:
            raise KeyError(f"Entity {entity_id!r} not found")
        new_components: dict[str | type[Any], ComponentT] = {**current, name: component}
        return self.move_entity(entity_id, new_components)

    def remove_component(
        self,
        entity_id: str,
        name: str | type[Any],
    ) -> tuple[ArchetypeId | None, ArchetypeId]:
        current = self.get_components(entity_id)
        if current is None:
            raise KeyError(f"Entity {entity_id!r} not found")
        new_components = {k: v for k, v in current.items() if k != name}
        return self.move_entity(entity_id, new_components)

    def query(
        self, *component_types: Type[ComponentT]
    ) -> Iterator[tuple[str, dict[str | type[Any], ComponentT]]]:
        """
        Iterate entities that contain ALL given component types (AND).

        Cost: O(K) where K = archetypes whose component set is a
        superset of the queried types.
        """
        if not component_types:
            for arch_id, table in self._archetypes.items():
                for eid, comps in table.items():
                    yield eid, comps
            return

        for arch_id in self._archetypes:
            if all(t in arch_id.components for t in component_types):
                table = self._archetypes[arch_id]
                for eid, comps in table.items():
                    yield eid, comps

    def query_one(
        self, *component_types: Type[ComponentT]
    ) -> tuple[str, dict[str | type[Any], ComponentT]] | None:
        for eid, comps in self.query(*component_types):
            return eid, comps
        return None

    def count(self, *component_types: Type[ComponentT]) -> int:
        return sum(1 for _ in self.query(*component_types))

    def to_map(self) -> Map[str, Map[str | type[Any], ComponentT]]:
        result: dict[str, Map[str | type[Any], ComponentT]] = {}
        for table in self._archetypes.values():
            for eid, comps in table.items():
                result[eid] = Map(comps)
        return Map(result)

    def _derive_archetype(self, components: dict[str | type[Any], ComponentT]) -> ArchetypeId:
        types = frozenset(type(c) for c in components.values())
        return ArchetypeId(types)

    def clear(self) -> None:
        self._archetypes.clear()
        self._entity_archetype.clear()

    def clone_with_entity(
        self,
        entity_id: str,
        components: Mapping[str | type[Any], ComponentT],
    ) -> "ArchetypeStorage":
        """
        Return a NEW ArchetypeStorage containing every entity
        of `self` except `entity_id`, plus the supplied
        `entity_id` with the given `components`.

        This is the public API used by `World.with_event` to
        build a new storage without reaching into private
        attributes. Pure copy: the original storage is not
        mutated.
        """
        new = ArchetypeStorage()
        for arch_id, table in self._archetypes.items():
            for eid, comps in table.items():
                if eid == entity_id:
                    continue
                # `add_entity` is O(1) and validates the
                # archetype; cloning is structural.
                new.add_entity(eid, dict(comps))
        if components:
            new.add_entity(entity_id, dict(components))
        return new

    def __repr__(self) -> str:
        return (
            f"ArchetypeStorage(entities={self.num_entities}, "
            f"archetypes={self.num_archetypes})"
        )
