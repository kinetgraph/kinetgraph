# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Reactive dispatch — apply ``WorldSystem`` instances to new events.

The runner loop is for periodic sweeps. Reactive systems need
to fire on the arrival of a new event, before the next tick. This
module provides a polling-based reactive dispatcher that:

  1. Loads the per-agent ``WorldCheckpoint`` from Redis
     (one Redis key per agent — ``IncrementalWorldStore``).
  2. Polls the EventLog for new events since the checkpoint.
  3. Folds the new events into the agent's World incrementally
     (O(M) per tick, where M is the number of new events).
  4. Calls each registered ``WorldSystem`` once with the
     post-fold World.
  5. Appends the resulting events (idempotent).
  6. Saves a new ``WorldCheckpoint`` AFTER the batch is
     durably committed to the EventLog.

Tick model
----------

A "tick" is one ``dispatch_once`` call. The dispatcher
processes every tracked agent. For each agent, the tick is:

  1. ``load checkpoint`` → ``(World, last_stream_id)``
  2. ``xrange(last_stream_id, "+")`` → batch of new events
  3. ``World.with_event(e)`` for each → post-fold World
  4. ``out = system(world)`` for each system
  5. ``append_batch(out)`` → ``EventLog``
  6. ``save checkpoint`` → Redis

World model
-----------

The World is the fold. It is built incrementally via
``World.with_event(event)`` (O(1) per event) and checkpointed
in Redis. On restart, the dispatcher resumes from the last
saved checkpoint — no full re-fold needed.

This replaces the v2.1 model where the dispatcher re-folded
on every tick (O(N) per tick, O(N × M) per batch of M new
events).

Systems are not told which event triggered the tick. They
inspect the World (via ``world.query_agents(MyComponent)``)
and emit events based on the rules they encode. This is
documented in ADR-018.

Idempotency
-----------

Re-running the dispatcher on the same batch produces the same
World, which produces the same output events. The EventLog
deduplicates via ``event_id``. The checkpoint is saved AFTER
the append so a crash between them replays the same events
on restart (the idempotency window).

For truly at-most-once side effects (external tool calls,
payments), tools must honor the ``idempotency_key`` injected
by ``ToolInvoker``. The dispatcher cannot guarantee at-most-once
across crashes — only the tool can.

See: ADR-018 — WorldIncremental + WorldSystem.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Optional

import structlog

from ..core.event import Event
from ..core.system import WorldSystem
from ..core.world import World
from ..infra.world_checkpoint import (
    IncrementalWorldStore,
    WorldCheckpoint,
)
from ..stream.event_log import EventLog
from .reactive_tool_projection import (
    _has_tool_events,
    _overlay_tool_projection,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from ..tools.router import ToolRouter

logger = structlog.get_logger()


class ReactiveDispatcher:
    """
    Polls the EventLog for new events, folds them into a
    per-agent World incrementally, and calls each registered
    ``WorldSystem`` once with the post-fold World.

    The dispatcher maintains a durable ``WorldCheckpoint`` per
    agent in Redis (via ``IncrementalWorldStore``). The
    checkpoint is the commit point: it is saved AFTER the
    batch's emitted events have been durably appended to the
    EventLog.

    See ADR-018 for the design rationale.
    """

    def __init__(
        self,
        log: EventLog,
        *,
        systems: Optional[list[WorldSystem]] = None,
        poll_interval: float = 0.25,
        filter_fn: Optional[Callable[[Event], bool]] = None,
        world_store: Optional[IncrementalWorldStore] = None,
        redis: Optional["Redis"] = None,
        tool_router: Optional["ToolRouter"] = None,
    ) -> None:
        """
        Args:
            log: the EventLog to poll and append to.
            systems: list of ``WorldSystem`` callables to run
                once per tick.
            poll_interval: how often ``_loop`` calls
                ``dispatch_once`` (seconds).
            filter_fn: optional pre-filter for events. Events
                that fail the filter are still folded into the
                World (so the World reflects the full history)
                but are not surfaced to the systems.
            world_store: checkpoint store. Defaults to
                ``IncrementalWorldStore(redis)`` if ``redis``
                is given; otherwise the dispatcher cannot
                recover from a restart and falls back to
                in-memory ``World`` instances (tests only).
            redis: required if ``world_store`` is not given.
                The default store uses this Redis client.
            tool_router: optional ``ToolRouter`` (ADR-036).
                When set, every ``tool.requested`` event
                emitted by a system is fanned out to the
                global tool queue (``fmh:tools:<name>:queue``)
                right after being appended to the EventLog.
                Without a router, the dispatcher behaves as
                before -- no fan-out is attempted.
        """
        self._log = log
        self._systems: list[WorldSystem] = list(systems or [])
        self._interval = poll_interval
        self._filter = filter_fn
        self._tool_router = tool_router
        if world_store is None:
            if redis is None:
                raise ValueError(
                    "ReactiveDispatcher requires either "
                    "world_store or redis (the default "
                    "IncrementalWorldStore wraps a Redis client)."
                )
            from kntgraph.infra.redis._world_checkpoint import (
                RedisWorldCheckpointStorage,
            )

            from typing import Any, cast

            world_store = IncrementalWorldStore(
                RedisWorldCheckpointStorage(cast(Any, redis))
            )
        self._world_store = world_store
        # In-memory cache of agents tracked by the dispatcher.
        # Populated lazily on first dispatch (per-agent via
        # ``track_agent`` or via the existing checkpoint keys).
        # The store is the source of truth for the World; the
        # cache is just a hot-path optimisation for ``list(agents)``.
        self._tracked_agents: set[str] = set()
        # Once the initial discovery has run, the dispatcher
        # only iterates ``_tracked_agents`` (no further SCAN).
        # New agents that show up at runtime are NOT picked up
        # automatically. To opt in, call ``track_agent``.
        self._bootstrapped: bool = False
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def systems(self) -> list[WorldSystem]:
        return list(self._systems)

    def add_system(self, system: WorldSystem) -> None:
        self._systems.append(system)

    def track_agent(self, agent_id: str) -> None:
        """
        Register an agent for the dispatcher to watch.
        Idempotent. Production callers should invoke this for
        every agent they create.
        """
        self._tracked_agents.add(agent_id)

    async def dispatch_once(self) -> int:
        """
        Polls the log once for new events and dispatches them.
        Returns the number of new events processed across all
        agents.
        """
        if not self._bootstrapped:
            await self._bootstrap_agents()
            self._bootstrapped = True

        processed = 0
        for agent_id in list(self._tracked_agents):
            processed += await self._dispatch_for_agent(agent_id)
        return processed

    async def _dispatch_for_agent(self, agent_id: str) -> int:
        """Run one dispatch cycle for a single agent.

        Returns the number of events that survived the
        filter (i.e. were surfaced to systems). Pulled
        out of ``dispatch_once`` so the orchestrator stays
        flat (CC ≤ 2) and the per-agent path is easy to
        test in isolation.
        """
        ckpt = await self._world_store.load(agent_id)
        new_events, new_last_stream_id = await self._fetch_new_events(
            agent_id, ckpt.last_stream_id
        )
        if not new_events:
            return 0

        world, new_event_count = self._fold_with_filter(ckpt.world, new_events)
        await self._run_systems_and_persist(
            agent_id, world, new_last_stream_id, new_event_count, new_events
        )
        return new_event_count

    def _fold_with_filter(
        self,
        world: "World",
        new_events: list[Event],
    ) -> tuple["World", int]:
        """Fold every new event into the World and count
        the ones that survive ``_filter`` (i.e. should be
        surfaced to systems).

        Folding happens regardless of the filter result
        so the World stays consistent with the full
        stream history; skipping a fold would desync it.

        After the base fold, if the batch contains any
        ``tool.*`` event (``tool.requested``,
        ``tool.<name>.completed``, ``tool.<name>.failed``)
        the ``overlay_tool_calls`` projection is applied
        on top of the post-fold World so systems that
        use ``ToolAwareSystem`` see the materialised
        ``tool_requests`` and ``tool_completions`` slots
        (ADR-036 §2.3).

        The overlay is base-projection-free: it reuses
        the views the incremental ``with_event`` loop
        already produced, so the cost is one extra pass
        over the batch (no second fold).
        """
        new_event_count = 0
        for event in new_events:
            world = world.with_event(event)
            if self._filter is not None and not self._filter(event):
                continue
            new_event_count += 1
        if new_event_count > 0 and _has_tool_events(new_events):
            world = _overlay_tool_projection(world, new_events)
        return world, new_event_count

    async def _run_systems_and_persist(
        self,
        agent_id: str,
        world: "World",
        last_stream_id: str,
        new_event_count: int,
        new_events: list[Event],
    ) -> None:
        """Run the systems, append the resulting events,
        and persist the checkpoint.

        Durability ordering: append before save. The
        crash window between append and save is closed
        by the EventLog dedupe on the next dispatch.
        """
        if new_event_count > 0:
            if self._tool_router is not None:
                await self._tool_router.route_batch(new_events)
            await self._append_system_outgoing(world, agent_id)
        await self._save_checkpoint(agent_id, world, last_stream_id)

    async def _append_system_outgoing(self, world: "World", agent_id: str) -> None:
        """Invoke every system with the post-fold World
        and append the resulting events to the log.

        Systems do NOT receive the triggering event --
        they inspect the World via ``query_agents``.

        If a ``ToolRouter`` is wired in, every emitted
        ``tool.requested`` event is fanned out to the
        global tool queue right after the EventLog
        commit (ADR-036 §2.5). The EventLog append
        happens first so the agent's history is the
        source of truth; the router copy is a best-
        effort transport to the worker pool.
        """
        outgoing: list[Event] = []
        for system in self._systems:
            out = system(world)
            if not isinstance(out, list):
                out = await out
            if out:
                outgoing.extend(out)
        if outgoing:
            await self._log.append_batch(outgoing)
            if self._tool_router is not None:
                await self._tool_router.route_batch(outgoing)

    async def _save_checkpoint(
        self, agent_id: str, world: "World", last_stream_id: str
    ) -> None:
        """Persist the World checkpoint.

        Always called, even when ``new_event_count == 0``,
        so the cursor advances past fully-filtered
        batches.
        """
        await self._world_store.save(
            agent_id,
            WorldCheckpoint(
                world=world,
                last_stream_id=last_stream_id,
            ),
        )

    async def _bootstrap_agents(self) -> None:
        """
        Initial discovery of agents. Called once on the first
        dispatch. After bootstrap, the dispatcher iterates only
        ``self._tracked_agents``.

        Iteration 5 (ADR-019): uses ``EventLog.list_agents``
        (the public delegation added in this iteration)
        instead of the legacy private ``_list_agent_ids``.
        The dispatcher no longer reaches through
        ``self._log._redis`` to enumerate agents.
        """
        agent_ids = await self._log.list_agents()
        for aid in agent_ids:
            self._tracked_agents.add(aid)

    async def _fetch_new_events(
        self, agent_id: str, cursor: str
    ) -> tuple[list[Event], str]:
        """
        Read events for one agent STRICTLY AFTER ``cursor``.

        Returns parsed ``Event`` objects. Iteration 5
        (ADR-019): uses the public ``EventLog.read_after_cursor``
        instead of the legacy ``self._log._redis.xrange(...)``
        direct access.
        """
        return await self._log.read_after_cursor(agent_id, cursor)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="fmh-reactive")
        logger.info(
            "reactive.start",
            poll_interval=self._interval,
            systems=len(self._systems),
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("reactive.stop")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.dispatch_once()
            except Exception as e:
                logger.error("reactive.loop.error", error=str(e))
            await asyncio.sleep(self._interval)


__all__ = ["ReactiveDispatcher"]
