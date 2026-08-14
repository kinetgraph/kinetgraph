# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Systems runner and persistence helpers for the reactive dispatcher.

The two functions that drive the per-tick systems pipeline
live here so the dispatcher's tick body stays flat.

- ``run_systems_and_persist`` is the full pipeline:
  route the new batch through the ``ToolRouter``, run the
  systems, re-fold the World with the system-emitted
  events (ADR-045 Slot GC), and persist the checkpoint.
- ``append_system_outgoing`` is the systems half on its
  own: invoke each system with the post-fold World and
  append the resulting events to the EventLog (and, when
  a ``ToolRouter`` is wired in, fan them out to the global
  tool queue right after the EventLog commit).

The functions are module-level rather than methods so
this module can be unit-tested in isolation. The
dispatcher passes ``self`` so the functions can read its
``_systems``, ``_log``, and ``_tool_router``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kntgraph.core.event import Event

from ._folding import fold_with_systems

if TYPE_CHECKING:
    from kntgraph.core.world import World
    from kntgraph.runner.reactive import ReactiveDispatcher


__all__ = ["run_systems_and_persist", "append_system_outgoing"]


async def run_systems_and_persist(
    dispatcher: "ReactiveDispatcher",
    agent_id: str,
    world: "World",
    last_stream_id: str,
    new_event_count: int,
    new_events: list[Event],
) -> None:
    """Run the systems, append the resulting events,
    re-fold the World with the emitted events (the
    ADR-045 Slot GC step), and persist the checkpoint.

    Durability ordering: append before save. The crash
    window between append and save is closed by the
    EventLog dedupe on the next dispatch.

    The systems are run on the post-fold World (which
    already has the tool-call overlay applied). Their
    emitted events are appended to the EventLog AND used
    to update the World via :func:`fold_with_systems` so
    the completion-driven eviction rule in
    ``overlay_tool_calls`` removes any orphan request
    whose TTL was just enforced by the
    :class:`ToolCallTTLSweeperSystem`. The resulting
    World is the one persisted to the checkpoint (the
    next tick's fold starts from a clean slot).

    The systems run on EVERY tick, even when
    ``new_event_count == 0``. The
    :class:`ToolCallTTLSweeperSystem` is the primary
    motivation: an orphan request sits in the slot until
    its TTL expires, which may happen several ticks after
    the request was emitted; the dispatcher must run the
    sweeper on those ticks even if the EventLog has no
    new events for the agent. The ``dispatch_once``
    short-circuit on ``not new_events`` (line 261) only
    skips the full pipeline when the log has nothing to
    fold AND the per-agent store is the source of truth;
    for the in-process ``run_systems_and_persist`` path
    used here, the systems must always run.

    The ``new_event_count > 0`` guard is replaced by a
    check on the EventLog/router side only (the router
    fan-out happens once per batch; the system pipeline
    is decoupled from the per-batch new-event count).
    """
    from ._checkpoint_io import save_checkpoint

    if new_event_count > 0 and dispatcher._tool_router is not None:
        await dispatcher._tool_router.route_batch(new_events)
    system_events = await append_system_outgoing(
        dispatcher, world, agent_id, return_events=True
    )
    if system_events:
        world = fold_with_systems(dispatcher, world, system_events)
    await save_checkpoint(dispatcher, agent_id, world, last_stream_id)


async def append_system_outgoing(
    dispatcher: "ReactiveDispatcher",
    world: "World",
    agent_id: str,
    *,
    return_events: bool = False,
) -> list[Event] | None:
    """Invoke every system with the post-fold World and
    append the resulting events to the log.

    Systems do NOT receive the triggering event -- they
    inspect the World via ``query_agents``.

    If a ``ToolRouter`` is wired in, every emitted
    ``tool.requested`` event is fanned out to the global
    tool queue right after the EventLog commit (ADR-036
    §2.5). The EventLog append happens first so the
    agent's history is the source of truth; the router
    copy is a best-effort transport to the worker pool.

    ``return_events``: when ``True`` (the
    :func:`run_systems_and_persist` path), the emitted
    events are returned to the caller so the World can be
    re-folded with them (ADR-045 Slot GC; see
    :func:`fold_with_systems`). When ``False`` (the legacy
    / test path), the events are appended to the log and
    discarded. The default is ``False`` to preserve the
    public contract for the existing tool-router tests.

    Note: ``agent_id`` is part of the signature for
    call-site symmetry with the original method; the
    systems read the agent identity from the World, not
    from the argument.
    """
    outgoing: list[Event] = []
    for system in dispatcher._systems:
        out = system(world)
        if not isinstance(out, list):
            out = await out
        if out:
            outgoing.extend(out)
    if outgoing:
        await dispatcher._log.append_batch(outgoing)
        if dispatcher._tool_router is not None:
            await dispatcher._tool_router.route_batch(outgoing)
    if return_events:
        return outgoing
    return None
