# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tool-call projection helpers for the reactive dispatcher.

The dispatcher (``runner.reactive.ReactiveDispatcher``) needs
two helpers that wrap the generic
``core.world.projection_tool_calls.overlay_tool_calls`` for
runner-specific concerns:

  - ``_has_tool_events``: cheap pre-check that lets the
    dispatcher skip the projection pass entirely when no
    tool event is in the batch.
  - ``_overlay_tool_projection``: applies the projection
    to the World (mutating ``storage`` and ``views`` for
    affected agents) and returns either a new World with
    the slots installed or the original World (no
    allocation) when nothing changed.

These are runner-specific: they consume the post-fold
``World`` and return a ``World`` (the generic
``overlay_tool_calls`` works on ``dict[str, AgentView]``
and returns the same shape). The dispatcher is the
only caller.

Extracted from ``reactive.py`` to keep that file under
the §3.1 500-L guideline. The helpers depend on
``core.world`` (the World value object) and on
``core.world.projection_tool_calls.overlay_tool_calls``
(the generic projection). They do not depend on
``ReactiveDispatcher`` state.
"""

from __future__ import annotations

from kntgraph.core.event import Event
from kntgraph.core.world import World
from kntgraph.core.world.projection_tool_calls import (
    overlay_tool_calls,
)
from kntgraph.core.world.view import AgentView


def _has_tool_events(events: list[Event]) -> bool:
    """Cheap pre-check: any ``tool.*`` event in the batch?

    Used to skip the second fold pass when no tool
    event is present, so non-tool batches pay zero
    cost for the projection.
    """
    for e in events:
        if e.event_type == "tool.requested" or (
            e.event_type.startswith("tool.")
            and (
                e.event_type.endswith(".completed") or e.event_type.endswith(".failed")
            )
        ):
            return True
    return False


def _overlay_tool_projection(world: "World", new_events: list[Event]) -> "World":
    """Run ``overlay_tool_calls`` on the batch and absorb
    the result into the post-fold World.

    ``overlay_tool_calls`` returns a dict that mirrors
    ``world.views`` (every base view is present) and
    installs the ``tool_requests`` / ``tool_completions``
    slots only on agents touched by tool events in
    the batch. Agents without tool events come back
    as the same object (no allocation).

    All that remains for the dispatcher is to:

      1. Detect whether any view actually changed
         (compared to ``world.views``).
      2. If so, replace the matching entries in
         ``world.views`` and update
         ``world.storage`` via ``clone_with_entity``
         so the next ``with_event`` call sees the
         slots.
      3. Otherwise, return ``world`` unchanged (no
         allocation).

    The returned World has the same ``tick`` as the
    input -- the tool projection is an overlay, not a
    fold step, and must not advance the tick clock.
    """
    tool_views = overlay_tool_calls(new_events, world.views)
    if not tool_views:
        return world
    new_storage = world.storage
    new_views: dict[str, AgentView] | None = None
    for agent_id, tool_view in tool_views.items():
        if world.views.get(agent_id) is tool_view:
            # Pass-through (no tool events for this agent).
            continue
        if new_views is None:
            new_views = dict(world.views)
        new_views[agent_id] = tool_view
        new_storage = new_storage.clone_with_entity(
            agent_id, dict(tool_view.components)
        )
    if new_views is None:
        return world
    return World(tick=world.tick, storage=new_storage, views=new_views)
