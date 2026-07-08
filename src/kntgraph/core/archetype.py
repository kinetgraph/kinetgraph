# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Archetype primitives for the ECS.

An Archetype is a unique combination of Component types attached to an entity.
ArchetypeId is a hash-stable identifier derived from the canonical (module, qualname)
tuple of its component types, ensuring consistent identity across processes
and Python sessions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, FrozenSet, Type

from .component import ComponentMeta


@dataclass(frozen=True, slots=True)
class ArchetypeId:
    """
    Hash-stable identifier for an archetype (set of Component types).

    Internally keyed by (module, qualname) pairs, so two classes named
    the same way but in different modules produce different ids.
    """

    components: FrozenSet[Type[Any]]

    _cache: FrozenSet = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        keys = frozenset(
            sorted((c.__module__, c.__qualname__) for c in self.components)
        )
        object.__setattr__(self, "_cache", keys)

    def __hash__(self) -> int:
        return hash(self._cache)

    def __str__(self) -> str:
        names = sorted(c.__name__ for c in self.components)
        return "{" + ", ".join(names) + "}"

    def __repr__(self) -> str:
        return f"ArchetypeId({self})"

    def contains(self, *types: Type[Any]) -> bool:
        return all(t in self.components for t in types)

    def intersects(self, other: "ArchetypeId") -> bool:
        return bool(self.components & other.components)

    def metas(self) -> FrozenSet[ComponentMeta]:
        return frozenset(ComponentMeta.of(c) for c in self.components)

    @classmethod
    def of(cls, *types: Type[Any]) -> "ArchetypeId":
        return cls(frozenset(types))


def archetype_of(*types: Type[Any]) -> ArchetypeId:
    """Convenience: build an ArchetypeId from component types."""
    return ArchetypeId.of(*types)
