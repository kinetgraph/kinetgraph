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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kntgraph.core.event import Event

from .reactive_tool_projection import (
    _has_tool_events,
    _overlay_tool_projection,
)

if TYPE_CHECKING:
    from kntgraph.core.world import World
    from kntgraph.runner.reactive import ReactiveDispatcher


__all__ = ["fold_with_filter", "fold_with_systems"]


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

    After the base fold, if the batch contains any
    ``tool.*`` event (``tool.requested``,
    ``tool.<name>.<suffix>``, ``tool.completed``,
    ``tool.failed``) the ``overlay_tool_calls``
    projection is applied on top of the post-fold
    World so systems that use ``ToolAwareSystem``
    see the materialised ``tool_requests`` and
    ``tool_completions`` slots (ADR-036 §2.3).

    The overlay is base-projection-free: it reuses
    the views the incremental ``with_event`` loop
    already produced, so the cost is one extra pass
    over the batch (no second fold).

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
