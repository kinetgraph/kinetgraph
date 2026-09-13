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

import inspect
from dataclasses import replace
from typing import TYPE_CHECKING, Awaitable

from kntgraph.core.event import Event, correlation_middleware
from kntgraph.core.world import World

from ._folding import fold_with_systems

if TYPE_CHECKING:
    from kntgraph.runner.reactive import ReactiveDispatcher


__all__ = ["run_systems_and_persist", "append_system_outgoing"]


def _call_system_with_optional_new_events(
    system: object,
    world: "World",
    new_events: "list[Event] | None",
) -> "list[Event] | Awaitable[list[Event]]":
    """
    Call ``system`` with ``new_events`` if its signature
    accepts the kwarg, otherwise call without. Backward
    compat for systems written before the ``new_events``
    kwarg was added to the ``WorldSystem`` Protocol.
    """
    try:
        params = inspect.signature(system.__call__).parameters
    except (TypeError, ValueError):
        # Builtin / C-implemented callables: assume the
        # new kwarg is supported (the Protocol enforces it
        # for in-house systems; third-party callables are
        # rare and the dispatcher would crash loudly on
        # signature mismatch otherwise).
        return system(world, new_events=new_events)

    if "new_events" in params:
        return system(world, new_events=new_events)
    return system(world)


def _system_name(system: object) -> str:
    """
    Resolve the cursor key for a system instance (ADR-074).

    Default: ``type(system).__name__``. Override via
    ``__fsm_system_name__`` ClassVar when the class name
    collides with another module's class.
    """
    return getattr(system, "__fsm_system_name__", type(system).__name__)


def _advance_cursors_in_world(
    world: World,
    emitters: set[tuple[str, str]],
) -> World:
    """
    Advance the per-system cursor for each ``(system_name,
    agent_id)`` in ``emitters`` to the agent's current
    ``view.last_event_id`` (ADR-074).

    Returns a new ``World`` with the updated views.
    Cursors are not part of ``components``, so the
    ``storage`` field is unchanged — the cursor
    advancement is a pure view-level update.

    If ``emitters`` is empty, OR every emitter references
    an agent that has no view in the World (no anchor
    event), returns ``world`` unchanged (no allocation).
    """
    if not emitters:
        return world

    # Group emitters by agent_id for efficient per-view
    # updates.
    by_agent: dict[str, set[str]] = {}
    for sys_name, ag_id in emitters:
        by_agent.setdefault(ag_id, set()).add(sys_name)

    # Fast path: if no emitter has a corresponding view,
    # there is nothing to advance. Skip allocation.
    if not any(ag_id in world.views for ag_id in by_agent):
        return world

    new_views = dict(world.views)
    for ag_id, sys_names in by_agent.items():
        old_view = new_views.get(ag_id)
        if old_view is None:
            continue  # defensive
        if old_view.last_event_id is None:
            continue  # no anchor event
        new_cursors = dict(old_view.cursors)
        for sys_name in sys_names:
            new_cursors[sys_name] = old_view.last_event_id
        if new_cursors != old_view.cursors:
            new_views[ag_id] = replace(old_view, cursors=new_cursors)

    return World(tick=world.tick, storage=world.storage, views=new_views)


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
    :class:`ToolCallTTLSweeperSystem`` is the primary
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

    Checkpoint save is dirty-only (ADR-068 §3.5 P5c): the
    save runs when the cursor advanced (new events were
    consumed) or the systems emitted events (the World was
    re-folded by ``fold_with_systems``). A tick where both
    are empty skips the re-SET of an unchanged pickled
    World — the checkpoint on disk is already exactly what
    would be written.
    """
    from ._checkpoint_io import save_checkpoint

    if new_event_count > 0 and dispatcher._tool_router is not None:
        await dispatcher._tool_router.route_batch(new_events)
    # Pass the events the dispatcher just folded into the
    # world so systems that need multi-event processing
    # (e.g., the FSM cascading across events in a single
    # tick) can use them. The events are read from the
    # EventLog — they are already persisted (the framework
    # invariant: an event is only valid after it appears
    # in the log). Events generated BY systems in this
    # tick are NOT in this list; they are appended to the
    # log AFTER the system returns and become visible in
    # the next tick.
    system_events = await append_system_outgoing(
        dispatcher,
        world,
        agent_id,
        new_events=new_events,
        return_events=True,
    )
    if system_events:
        world = fold_with_systems(dispatcher, world, system_events)
    # ADR-074: advance per-system cursors for systems that
    # RAN this tick. Cursor advance is per-run (not
    # per-emit) so systems that filter or no-op don't get
    # stuck with a stale cursor. The cursor lives in
    # ``view.cursors``; persistence piggy-backs on the
    # WorldCheckpoint save below.
    runners = getattr(dispatcher, "_tick_runners", None)
    if runners:
        world = _advance_cursors_in_world(world, runners)
        dispatcher._tick_runners = set()
    # Dirty-only save (ADR-068 §3.5 P5c): the checkpoint is
    # re-persisted only when something actually changed — the
    # cursor advanced past consumed entries, or a system
    # emitted events that mutated the World (the ADR-045 Slot
    # GC re-fold). An idle tick with zero new events and zero
    # emitted events leaves the pickled World bit-for-bit
    # identical to the stored one; re-SETing it every tick
    # was the dominant idle payload of the dispatcher.
    if new_event_count > 0 or system_events:
        await save_checkpoint(dispatcher, agent_id, world, last_stream_id)


async def append_system_outgoing(
    dispatcher: "ReactiveDispatcher",
    world: "World",
    agent_id: str,
    *,
    new_events: list[Event] | None = None,
    return_events: bool = False,
) -> list[Event] | None:
    """Invoke every system with the post-fold World and
    append the resulting events to the log.

    Systems do NOT receive the triggering event directly
    via the World; they inspect the World via
    ``query_agents``. For systems that need the events
    that were just folded in this tick (e.g., the FSM
    cascading across multiple events in one call), the
    dispatcher passes them via the ``new_events`` kwarg.
    The events are read from the EventLog — they are
    already persisted (the framework invariant: an event
    is only valid after it appears in the log).

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
    # ADR-074: track per-(system, agent) RUNNERS for cursor
    # advancement. Cursor advances when the system ran
    # (not when it emitted) so systems that filter or
    # no-op don't get stuck with a stale cursor. Reset on
    # each tick so the set doesn't accumulate across ticks.
    # Lazily attached to the dispatcher so older dispatchers
    # (which never initialised the attribute) keep working.
    dispatcher._tick_runners = set()
    # Bind a correlation scope so systems that call
    # ``correlation_middleware.current()`` (e.g. to build
    # events via ``Event.domain_from``) receive a
    # non-None ``CorrelationContext`` per ADR-037. Without
    # this, the contextvar is empty inside the tick and
    # ``Event.create`` raises ``TypeError``.
    with correlation_middleware.scope():
        for system in dispatcher._systems:
            sys_name = _system_name(system)
            # Record the (system, agent) pair as a runner
            # for cursor advancement. We track the agent
            # the system was invoked for (= ``agent_id``);
            # the system may emit events for OTHER agents
            # too, but cursor advancement for those agents
            # is handled by the dispatcher emitting the
            # events and the per-agent cursor being read
            # by that agent's next tick.
            dispatcher._tick_runners.add((sys_name, agent_id))
            # Use ``inspect.signature`` to call the system
            # with or without ``new_events`` based on the
            # system's signature. Older systems (without the
            # new parameter) are called the old way; newer
            # systems receive the events.
            out = _call_system_with_optional_new_events(
                system, world, new_events
            )
            if not isinstance(out, list):
                out = await out
            if out:
                outgoing.extend(out)
                # Also track emitters (in addition to
                # runners) so we can advance cursors for
                # agents that were EMITTED TO (not just the
                # agent we ran for). Most systems emit for
                # the agent they were invoked for; cross-
                # agent emission is rare but supported.
                for event in out:
                    dispatcher._tick_runners.add(
                        (sys_name, event.agent_id)
                    )
    if outgoing:
        await dispatcher._log.append_batch(outgoing)
        if dispatcher._tool_router is not None:
            await dispatcher._tool_router.route_batch(outgoing)
    if return_events:
        return outgoing
    return None
