# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
world.query -- The `WorldQuery` class (unified query).

A lazy iterable over the World's `views` dict. A
`WorldQuery` is constructed in one of three ways:

  1. From `World.query_agents(*component_types)`:
     filters by component-type membership. Each
     component type must be present (as an
     ``isinstance`` match) in the agent's
     ``components`` mapping.

  2. From `WorldQuery(world, predicate=...)`:
     filters by an arbitrary predicate over the
     ``AgentView``.

  3. From `query.filter(predicate)`:
     AND-composes a new predicate with the
     existing one and returns a new
     ``WorldQuery``.

The class unifies what used to be `WorldQuery` +
`FilteredWorldQuery` into a single type: the
constructor accepts both forms, and `filter()`
returns the same class. There is no public
type distinction between "component-filtered"
and "predicate-filtered" — both are just
``WorldQuery`` instances with a different
predicate.

`first()`, `count()`, `to_list()`, `is_empty()`
are convenience consumers over the lazy iterator.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Optional, Type, Union

from .view import AgentView

if TYPE_CHECKING:
    from .world import World


# Inputs accepted by ``WorldQuery.__init__``: either a
# ``World`` (with a ``.views`` attribute) or a plain
# mapping of ``agent_id -> AgentView``. The Union keeps
# both calling conventions statically typed without
# ``Any``.
WorldOrViews = Union["World", Mapping[str, AgentView]]


class WorldQuery:
    """
    Lazy query over the World's archetype storage.

    Filters by component type membership (all given types must be
    present) and supports an arbitrary predicate over the AgentView.
    """

    __slots__ = ("_views", "_predicate")

    def __init__(
        self,
        world_or_views: WorldOrViews,
        *component_types: Type,
        predicate: Optional[Callable[[AgentView], bool]] = None,
    ) -> None:
        """
        Build a WorldQuery.

        Two calling conventions:

          - ``WorldQuery(world, ComponentA, ComponentB)``
            -- component-type filter.
          - ``WorldQuery(views, predicate=fn)``
            -- arbitrary predicate over views.

        Internally both produce a single ``_predicate``
        callable that the iterator applies per agent.
        The view mapping is stored as ``_views``; the
        query is lazy and does not copy.
        """
        # ``isinstance(..., Mapping)`` narrows the Union for
        # pyright; without it, the ``.views`` attribute
        # access is flagged as possibly missing on a
        # plain ``Mapping[str, AgentView]``.
        if isinstance(world_or_views, Mapping):
            views: Mapping[str, AgentView] = world_or_views
        else:
            views = world_or_views.views
        self._views: Mapping[str, AgentView] = views
        predicates: list[Callable[[AgentView], bool]] = []
        if component_types:
            predicates.append(self._make_type_filter(component_types))
        if predicate is not None:
            predicates.append(predicate)
        if len(predicates) == 0:
            self._predicate: Callable[[AgentView], bool] = lambda v: True
        elif len(predicates) == 1:
            self._predicate = predicates[0]
        else:

            def combined(v: AgentView) -> bool:
                return all(p(v) for p in predicates)

            self._predicate = combined

    @staticmethod
    def _make_type_filter(
        types: tuple[Type, ...],
    ) -> Callable[[AgentView], bool]:
        def _filter(view: AgentView) -> bool:
            for t in types:
                for comp in view.components.values():
                    if isinstance(comp, t):
                        break
                else:
                    return False
            return True

        return _filter

    def __iter__(self) -> Iterator[tuple[str, AgentView]]:
        for agent_id, view in self._views.items():
            if self._predicate(view):
                yield agent_id, view

    def filter(
        self,
        predicate: Callable[[AgentView], bool],
    ) -> "WorldQuery":
        """Return a new WorldQuery with the predicate
        AND-composed with the existing one."""
        old = self._predicate
        return WorldQuery(
            self._views,
            predicate=lambda v: old(v) and predicate(v),
        )

    def first(self) -> Optional[tuple[str, AgentView]]:
        return next(iter(self), None)

    def count(self) -> int:
        return sum(1 for _ in self)

    def to_list(self) -> list[tuple[str, AgentView]]:
        return list(self)

    def is_empty(self) -> bool:
        return next(iter(self), None) is None


__all__ = ["WorldQuery"]
