# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
World folding helpers for the reactive dispatcher.

The two passes that drive the per-tick fold live here so
the dispatcher's tick body stays flat. The first pass
(``fold_with_filter``) folds new events from the EventLog
into the World; the second pass (``fold_with_systems``)
re-folds the World with the events the systems emitted in
the same tick so the tool-call overlay's completion-driven
eviction rule can remove orphan requests synchronously
(ADR-045 Slot GC).

The functions are module-level rather than methods so
this module can be unit-tested in isolation (no dispatcher
instance, no Redis, no systems). The dispatcher passes
``self`` so the functions can read its ``_filter`` and
``_tool_ttls`` configuration.

The fold composes three orthogonal projections in order:
``default fold`` -> ``project_memory`` -> ``overlay tool``
(ADR-042 §6.1, ADR-059 §2.2, ADR-044 §2.3). The base fold
is incremental (one ``with_event`` per event); the two
derived projections are base-projection-free — they reuse
the views the base fold produced and overlay their derived
components on top. The cost is one extra pass per derived
projection; no second fold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, cast

from kntgraph.core.event import Event
from kntgraph.core.world import World

from .reactive_tool_projection import (
    _has_tool_events,
    _overlay_tool_projection,
)

if TYPE_CHECKING:
    from kntgraph.runner.reactive import ReactiveDispatcher


__all__ = ["fold_with_filter", "fold_with_systems"]


def _project_memory_into_world(
    world: "World",
    new_events: list[Event],
) -> "World":
    """Compose the memory-hydration projection onto the
    World (ADR-042 §6.1, ADR-059 §2.2).

    The projection is ``project_memory`` (a pure function
    over events and views) merged back into the
    World with storage sync. Each agent whose view was
    updated by ``project_memory`` has its components
    cloned into the World's storage via
    ``clone_with_entity`` so a subsequent replay
    (which rebuilds the World from storage) preserves
    the memory components.

    Fast path: ``project_memory`` returns a dict that
    passes agents with no memory events through
    unchanged (``_project_memory_for_agent`` returns
    ``None`` when the batch had no memory event and
    the base view has no memory component). When the
    returned dict is identity-equal to the input views,
    no storage work runs either.
    """
    from kntgraph.core.world.projection_memory import project_memory

    new_views = project_memory(new_events, world.views)
    if not new_views:
        return world
    new_storage = world.storage
    changed: bool = False
    for agent_id, projected_view in new_views.items():
        if world.views.get(agent_id) is projected_view:
            # Pass-through (no memory events for this
            # agent; ``project_memory`` returned the
            # base view unchanged).
            continue
        new_storage = new_storage.clone_with_entity(
            agent_id,
            cast(
                "Mapping[str | type[Any], Any]",
                dict(projected_view.components),
            ),
        )
        changed = True
    if not changed:
        return world
    return World(tick=world.tick, storage=new_storage, views=new_views)


def fold_with_filter(
    dispatcher: "ReactiveDispatcher",
    world: "World",
    new_events: list[Event],
) -> tuple["World", int]:
    """Fold every new event into the World and count
    the ones that survive ``dispatcher._filter`` (i.e.
    should be surfaced to systems).

    Folding happens regardless of the filter result so
    the World stays consistent with the full stream
    history; skipping a fold would desync it.

    After the base fold, the memory-hydration projection
    (ADR-042 §6.1, ADR-059 §2.2) populates
    ``SessionComponent``, ``ProfileComponent``, and
    ``ContinuityComponent`` on the agents whose events
    include a memory namespace; agents whose events do
    not include one are passed through unchanged.

    Then, if the batch contains any ``tool.*`` event
    (``tool.requested``, ``tool.<name>.<suffix>``,
    ``tool.completed``, ``tool.failed``) the
    ``overlay_tool_calls`` projection is applied on top
    of the post-fold World so systems that use
    ``ToolAwareSystem`` see the materialised
    ``tool_requests`` and ``tool_completions`` slots
    (ADR-036 §2.3, ADR-044 §2.3).

    The two derived projections are
    **base-projection-free**: they reuse the views the
    incremental ``with_event`` loop already produced,
    so the cost is one extra pass per projection (no
    second fold).

    The overlay is configured with the dispatcher's
    ``tool_ttls`` (ADR-045); the overlay sets
    ``expires_at`` on each new request. The TTL
    itself is **enforced** by the
    :class:`ToolCallTTLSweeperSystem` (registered
    with the dispatcher; emits
    ``tool.<name>.failed`` events for stale
    requests). The orphan-request eviction is
    handled by :func:`fold_with_systems`, a second
    overlay pass over the system-emitted events
    (DEBT §2.21 follow-up; closes the memory leak
    documented in ADR-045).
    """
    new_event_count = 0
    for event in new_events:
        world = world.with_event(event)
        if dispatcher._filter is not None and not dispatcher._filter(event):
            continue
        new_event_count += 1
    world = _project_memory_into_world(world, new_events)
    # ADR-042 §6.1 follow-up: caller-supplied
    # projections run after the built-in memory
    # hydration and before the tool overlay. Each
    # projection receives the World returned by the
    # previous projection (compose-in-order). The
    # list is empty by default; the legacy behaviour
    # (built-in memory hydration + tool overlay
    # only) is preserved when no projections are
    # registered. See
    # :mod:`kntgraph.runner.reactive_extensions`.
    for projection in dispatcher._projections:
        world = projection(world, new_events)
    if new_event_count > 0 and _has_tool_events(new_events):
        world = _overlay_tool_projection(
            world,
            new_events,
            tool_ttls=dispatcher._tool_ttls,
            post_systems=False,
        )
    return world, new_event_count


def fold_with_systems(
    dispatcher: "ReactiveDispatcher",
    world: "World",
    system_events: list[Event],
) -> "World":
    """Re-fold the World with the events emitted by the
    systems in the same tick (ADR-045 Slot GC; DEBT
    §2.21 follow-up).

    The :class:`ToolCallTTLSweeperSystem` emits
    ``tool.<name>.failed`` events for stale requests.
    The first overlay pass (in :func:`fold_with_filter`)
    did not see these events because they did not exist
    yet (the systems run AFTER the overlay). Without this
    second pass, the stale request stays in the
    ``tool_requests`` slot forever (the completion-driven
    eviction rule in ``overlay_tool_calls`` only fires
    when the matching ``failed`` event lands in a next
    tick's batch, but the sweeper emits it in the CURRENT
    tick and it is never folded into the slot until then).

    The second pass folds the system events into the
    World and re-applies the overlay with
    ``new_events=system_events`` so the completion-driven
    eviction rule (``request in existing_completions`` ->
    ``pop``) removes the orphan request in the SAME tick.
    The overlay is pure, so the second pass is
    deterministic and idempotent (the stale request is
    gone in the new World; a stale ``tool.<name>.failed``
    is itself the completion the rule looks for).

    No-op when ``system_events`` contains no ``tool.*``
    event: the ``_has_tool_events`` pre-check is the same
    fast path as in :func:`fold_with_filter` (ADR-044
    §2.4 "no allocation for non-tool batches"). A non-tool
    batch pays zero for this second pass; the same World
    object is returned.
    """
    if not _has_tool_events(system_events):
        return world
    new_world = world
    for event in system_events:
        new_world = new_world.with_event(event)
    return _overlay_tool_projection(
        new_world,
        system_events,
        tool_ttls=dispatcher._tool_ttls,
        post_systems=True,
    )
