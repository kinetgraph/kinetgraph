# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Composable post-fold projections for the
:class:`ReactiveDispatcher`.

The default dispatcher fold (see
:func:`kntgraph.runner._folding.fold_with_filter`) runs
the base projection (last-event-wins), then the
memory-hydration projection (ADR-042 §6.1, ADR-059
§2.2), then the tool-call overlay (ADR-044 §2.3).
That pipeline is hard-coded and covers the common case
where every kinetgraph deployment wants memory
hydration and the tool overlay.

For deployments that need **custom projections** (for
example, an analytics projection that materialises
a ``CostComponent`` from ``cost.*`` events) the
framework exposes two primitives:

  - :class:`WorldProjection`: a pure function over a
    batch of events and the current World that returns
    a new World.
  - :class:`MemoryHydrationProjection`: a built-in
    :class:`WorldProjection` that wraps
    :func:`kntgraph.core.world.projection_memory.project_memory`
    for callers who prefer an object to a function.

The dispatcher accepts an optional ``projections``
list. Projections are applied **after the base fold
and before the tool overlay**, in the order they were
registered. Built-in projections (memory hydration,
tool overlay) always run regardless of the
``projections`` kwarg; the kwarg extends the pipeline,
it does not replace the built-ins.

The dispatcher's tool overlay is the LAST step on
purpose: it materialises the ``tool_requests`` /
``tool_completions`` slots on top of any custom
projection so ``ToolAwareSystem`` sees a consistent
view.

See :class:`ReactiveDispatcher` for the dispatcher-
side wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from kntgraph.core.event import Event
    from kntgraph.core.world import World


__all__ = ["WorldProjection", "MemoryHydrationProjection"]


@runtime_checkable
class WorldProjection(Protocol):
    """A pure post-fold projection over a batch of events.

    Implementations are called once per tick with the
    post-base-fold World and the batch of new events,
    and return a new World. Projections must be
    **pure** (no side effects, no Redis I/O, no logging
    outside the caller's own logger) so the dispatcher
    can replay ticks during recovery.

    The framework's built-in projections (memory
    hydration, tool overlay) live elsewhere; this
    protocol is the extension point for third-party
    projections.
    """

    def __call__(self, world: "World", events: list["Event"]) -> "World": ...


class MemoryHydrationProjection:
    """Built-in projection that materialises memory
    components on the World (ADR-042 §6.1).

    The projection wraps
    :func:`kntgraph.core.world.projection_memory.project_memory`
    so callers that prefer objects to bare functions can
    pass it to :class:`ReactiveDispatcher` via the
    ``projections=[...]`` kwarg:

    .. code-block:: python

        from kntgraph.runner.reactive import ReactiveDispatcher
        from kntgraph.runner.reactive_extensions import (
            MemoryHydrationProjection,
        )

        dispatcher = ReactiveDispatcher(
            log,
            projections=[MemoryHydrationProjection()],
        )

    The default dispatcher already runs memory
    hydration as part of its built-in pipeline; this
    class is the registration point for callers that
    want to compose their **own** projections with the
    built-in memory hydration.

    For callers that want to suppress the built-in
    memory hydration (rare; usually only justified in
    tests that exercise projection isolation) the
    same projection object can be skipped by passing
    ``projections=[]`` and replicating the projection
    manually; the framework does not currently expose
    a "disable built-ins" knob on purpose (it would
    be a footgun).
    """

    __slots__ = ("_project_memory_fn",)

    def __init__(self) -> None:
        # Imported lazily so importing this module does
        # not pull in the full memory-hydration chain
        # (the chain pulls the World view + component
        # modules; tests that only need the dispatcher
        # do not pay for it).
        from kntgraph.core.world.projection_memory import (
            project_memory,
        )

        self._project_memory_fn = project_memory

    def __call__(self, world: "World", events: list["Event"]) -> "World":
        """Apply the memory-hydration projection.

        Args:
            world: the post-base-fold World.
            events: the batch of new events the
                dispatcher just folded.

        Returns:
            A new World with
            ``SessionComponent`` / ``ProfileComponent`` /
            ``ContinuityComponent`` installed on the
            agents whose events touched a memory
            namespace. Agents with no memory event in
            the batch are passed through unchanged
            (same view object; ``is`` identity).
        """
        new_views = self._project_memory_fn(events, world.views)
        if not new_views:
            return world
        # Identity check: if ``project_memory`` returned
        # the same views dict (no agent touched), no
        # storage work runs either.
        changed: bool = False
        new_storage = world.storage
        for agent_id, projected_view in new_views.items():
            if world.views.get(agent_id) is projected_view:
                # Pass-through (no memory events for
                # this agent).
                continue
            new_storage = new_storage.clone_with_entity(
                agent_id,
                dict(projected_view.components),
            )
            changed = True
        if not changed:
            return world
        from kntgraph.core.world import World

        return World(tick=world.tick, storage=new_storage, views=new_views)
