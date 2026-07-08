# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Component metadata for the ECS.

A "Component" in FMH is any immutable value object. It can be:

    - @dataclass(slots=True, frozen=True)
    - Pydantic BaseModel with `model_config = ConfigDict(frozen=True)`
    - NamedTuple
    - plain Mapping[str, Any] with structural typing

The core does NOT impose Pydantic. The only thing the core needs is a
canonical identity of the component TYPE (used for archetype keying and
for distinguishing components from each other in storage and queries).

Component identity = (module, qualname) — same convention as ArchetypeId,
hash-stable across processes and Python sessions.

A component INSTANCE is a plain immutable value. Systems fold events into
the world; the world's state is composed of those values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Any,
    FrozenSet,
    Type,
    runtime_checkable,
    Protocol,
)


@runtime_checkable
class ComponentInstance(Protocol):
    """
    Marker protocol for component instances.

    Anything hashable and immutable qualifies. This protocol exists for
    documentation and for runtime checks in storage; the core does not
    require components to subclass it.
    """

    __slots__: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ComponentMeta:
    """
    Canonical identity of a component class.

    Same construction as ArchetypeId, but per-class. Used for storage
    keying, query signatures, and structural deduplication.
    """

    module: str
    qualname: str

    @classmethod
    def of(cls, t: Type[Any]) -> "ComponentMeta":
        return cls(t.__module__, t.__qualname__)

    def __str__(self) -> str:
        return f"{self.module}.{self.qualname}"


def component_meta(t: Type[Any]) -> ComponentMeta:
    """Convenience: returns ComponentMeta.of(t)."""
    return ComponentMeta.of(t)


def archetype_id(component_types: FrozenSet[Type[Any]]) -> FrozenSet[ComponentMeta]:
    """
    Returns the archetype key (a frozen set of ComponentMeta) for a set
    of component types. Two entities with the same archetype id share
    the same set of component types.
    """
    return frozenset(ComponentMeta.of(t) for t in component_types)
