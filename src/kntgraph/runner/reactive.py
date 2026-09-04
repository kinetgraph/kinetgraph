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
import time
from collections.abc import Callable

from typing import TYPE_CHECKING, Optional

import structlog

from ..core.event import Event
from ..core.system import WorldSystem
from ..core.world.components import ToolCallTTL
from ..infra.world_checkpoint import IncrementalWorldStore
from ..stream.event_log import EventLog
from ._checkpoint_io import (
    bootstrap_agents as _bootstrap_agents_fn,
)
from ._checkpoint_io import (
    fetch_new_events as _fetch_new_events_fn,
)
from ._checkpoint_io import (
    save_checkpoint as _save_checkpoint_fn,
)
from ._folding import (
    fold_with_filter as _fold_with_filter_fn,
)
from ._systems_runner import (
    run_systems_and_persist as _run_systems_and_persist_fn,
)
from .tool_call_ttl_sweeper import ToolCallTTLSweeperSystem

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from ..tools.router import ToolRouter
    from .reactive_extensions import WorldProjection

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
        poll_interval: Optional[float] = None,
        filter_fn: Optional[Callable[[Event], bool]] = None,
        world_store: Optional[IncrementalWorldStore] = None,
        redis: Optional["Redis"] = None,
        tool_router: Optional["ToolRouter"] = None,
        tool_ttls: Optional[ToolCallTTL] = None,
        rediscovery_interval_seconds: Optional[float] = None,
        heartbeat_interval_seconds: float = 30.0,
        projections: Optional[list["WorldProjection"]] = None,
    ) -> None:
        """
        Args:
            (existing args unchanged)
            poll_interval: how often ``_loop`` calls
                ``dispatch_once`` (seconds). ``None`` reads the
                ``KNT_REACTIVE_POLL_INTERVAL`` knob (ADR-068 §3.8;
                default 0.25). Explicit values keep the legacy
                behaviour (tests tighten this to 0.05).
            rediscovery_interval_seconds: how often the
                dispatcher re-runs ``EventLog.list_agents()`` to
                pick up brand-new tenants. ``None`` reads the
                ``KNT_REACTIVE_REDISCOVERY_SECONDS`` knob (default
                5.0). Explicit values keep the legacy behaviour.
            projections: optional list of
                :class:`WorldProjection` objects to run
                **after the base fold and before the
                tool overlay**. The list is composed in
                order; each projection receives the
                World returned by the previous one. The
                built-in memory-hydration projection
                (ADR-042 §6.1) always runs before any
                caller-supplied projection; the tool
                overlay (ADR-044 §2.3) always runs last.
                See
                :mod:`kntgraph.runner.reactive_extensions`
                for the extension protocol and the
                built-in :class:`MemoryHydrationProjection`.
        """
        """
        Args:
            log: the EventLog to poll and append to.
            systems: list of ``WorldSystem`` callables to run
                once per tick.
            poll_interval: how often ``_loop`` calls
                ``dispatch_once`` (seconds). ``None`` reads the
                ``KNT_REACTIVE_POLL_INTERVAL`` knob (ADR-068 §3.8).
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
                global tool queue (``knt:tools:<name>:queue``)
                right after being appended to the EventLog.
                Without a router, the dispatcher behaves as
                before -- no fan-out is attempted.
            tool_ttls: optional ``ToolCallTTL`` (ADR-045).
                Per-tool TTL for ``ToolCallRequest`` entries;
                a request whose ``expires_at`` is in the past
                at fold time is evicted from the slot. The
                default is ``ToolCallTTL()`` (5-minute global
                TTL). Set ``per_tool_ttls`` to tune individual
                tools (e.g. tight TTL for synchronous helpers,
                loose TTL for long-running batch tools).
            rediscovery_interval_seconds: how often the
                dispatcher re-runs ``EventLog.list_agents()``
                to pick up brand-new tenants. ``None`` reads the
                ``KNT_REACTIVE_REDISCOVERY_SECONDS`` knob. The
                first discovery runs in ``start()``/the first
                tick; this knob bounds the staleness of
                subsequent ones (the rediscovery is a cheap
                ``SCAN`` over ``knt:agents:*:events``; the cost
                is dominated by network round-trips, not Redis
                CPU).
            heartbeat_interval_seconds: how often the
                dispatcher's background loop emits a
                structured heartbeat log line carrying
                the running event counter, the time
                since the last successful tick, and the
                last error string (if any). Defaults to
                30s; tests may tighten it (e.g. 0.05s)
                so the heartbeat is observable inside a
                single test body. Set to a non-positive
                number to disable the heartbeat.
        """
        self._log = log
        self._systems: list[WorldSystem] = list(systems or [])
        # Cadence knobs resolve through Settings when the
        # caller leaves them unset (ADR-068 §3.8). Explicit
        # values keep the legacy behaviour (tests tighten
        # these to sub-second values).
        if poll_interval is None:
            from kntgraph.infra.config import fresh_settings

            poll_interval = fresh_settings().reactive_poll_interval
        if rediscovery_interval_seconds is None:
            from kntgraph.infra.config import fresh_settings

            rediscovery_interval_seconds = fresh_settings().reactive_rediscovery_seconds
        self._interval = poll_interval
        self._filter = filter_fn
        self._tool_router = tool_router
        # ADR-042 §6.1 follow-up: caller-supplied
        # projections. Stored as-is; the dispatcher
        # composes them in ``_fold_with_filter`` after
        # the base fold and before the tool overlay.
        # An empty / ``None`` list keeps the legacy
        # behaviour (built-in memory hydration + tool
        # overlay only; no opt-in needed).
        self._projections: list["WorldProjection"] = list(projections or [])
        # ADR-045: the dispatcher's tool TTL config. The
        # overlay SETS ``expires_at`` on each new request
        # (using this config); the
        # :class:`ToolCallTTLSweeperSystem` (auto-
        # registered below when ``tool_ttls`` is not
        # ``None``) ENFORCES the TTL by emitting
        # ``tool.<name>.failed`` events for stale requests.
        self._tool_ttls = tool_ttls
        # Auto-register the TTL sweeper when the operator
        # has opted in to TTL enforcement (i.e. has
        # passed an explicit ``tool_ttls`` config). The
        # default (``tool_ttls=None``) keeps the legacy
        # behaviour (no TTL enforcement; see ADR-045 for
        # the migration path).
        if tool_ttls is not None and not any(
            isinstance(s, ToolCallTTLSweeperSystem) for s in self._systems
        ):
            self._systems.append(ToolCallTTLSweeperSystem())
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
        # repeats it every ``_rediscovery_interval_seconds``
        # (configurable; default 5s). Production callers can keep
        # the default; tests can shrink the value so a newcomer
        # is picked up within one or two polls. To opt in to
        # the new behaviour immediately, callers may also
        # ``track_agent`` proactively.
        self._rediscovery_interval_seconds: float = rediscovery_interval_seconds
        self._next_rediscovery_at: float = 0.0
        self._bootstrapped: bool = False
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Observability surface (ADR-style): counters and timestamps
        # that ``_loop`` turns into a periodic heartbeat log entry.
        # Without this, a "loop silently stuck on the same exception"
        # or "loop iterating but processing nothing" failure mode is
        # indistinguishable from healthy operation in the logs.
        self._events_processed_total: int = 0
        self._last_activity_at: float = time.monotonic()
        self._last_heartbeat_at: float = 0.0
        # Last exception text observed by ``_loop``; the heartbeat
        # surfaces it so a "loop keeps raising the same error"
        # failure mode is distinguishable from "loop is healthy".
        self._last_loop_error: Optional[str] = None
        # How often the loop emits a heartbeat log line. The default
        # is 30 seconds — short enough that an operator looking at
        # tail -f sees liveness, long enough that the log volume is
        # negligible under steady-state load.
        self._heartbeat_interval_seconds: float = heartbeat_interval_seconds

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

    def _has_unconsumed_work(self) -> bool:
        """
        True when at least one registered system has
        unconsumed work from a prior async drain.

        The lookup system (``SolutionLookupSystem``)
        queues synthetic completions in
        ``_pending_results`` on ``run_pending_lookups``;
        the next ``__call__`` is the one that surfaces
        them. Without this check, an idle tick
        (no new events in the log) would skip the
        systems and the queued completion would
        never be appended to the EventLog.

        Duck-typed: any system with a non-empty
        ``_pending_results`` list is considered to
        have unconsumed work. This matches the
        :class:`SolutionLookupSystem` contract
        (ADR-049 §2.1) and any future
        ``WorldSystem`` that follows the same
        sync-pump / async-drain shape.
        """
        for system in self._systems:
            pending = getattr(system, "_pending_results", None)
            if pending:
                return True
        return False

    def _should_run_systems_on_idle_tick(self) -> bool:
        """True when an idle tick (no new events) still
        needs to invoke the systems. Two reasons:

        - ``tool_ttls`` is set: the TTL sweeper may
          have orphan requests to evict.
        - some system has unconsumed ``_pending_results``
          (the lookup system contract, ADR-049).

        Pulled out of ``_dispatch_for_agent`` so the
        per-agent path stays flat (CC ≤ 5).
        """
        return self._tool_ttls is not None or self._has_unconsumed_work()

    async def dispatch_once(self) -> int:
        """
        Polls the log once for new events and dispatches them.
        Returns the number of new events processed across all
        agents.
        """
        # Periodic rediscovery of brand-new agents. The first
        # call also acts as the historical bootstrap. Newcomers
        # are merged into ``_tracked_agents`` idempotently.
        now = time.monotonic()
        if not self._bootstrapped or now >= self._next_rediscovery_at:
            await _bootstrap_agents_fn(self)
            self._bootstrapped = True
            self._next_rediscovery_at = now + self._rediscovery_interval_seconds

        processed = 0
        for agent_id in list(self._tracked_agents):
            processed += await self._dispatch_for_agent(agent_id)
        # Observability: refresh the activity timestamp on every
        # successful tick (even if processed == 0, the tick ran;
        # the heartbeat distinguishes "loop is alive but idle" from
        # "loop is stuck").
        self._last_activity_at = time.monotonic()
        self._events_processed_total += processed
        return processed

    async def _dispatch_for_agent(self, agent_id: str) -> int:
        """Run one dispatch cycle for a single agent.

        Returns the number of events that survived the
        filter (i.e. were surfaced to systems). Pulled
        out of ``dispatch_once`` so the orchestrator stays
        flat (CC ≤ 2) and the per-agent path is easy to
        test in isolation.

        The cycle ALWAYS runs the systems, even when
        the EventLog has no new events for the agent
        (DEBT §2.21 follow-up). The
        :class:`ToolCallTTLSweeperSystem` is the
        primary motivation: an orphan request sits in
        the slot until its TTL expires, which may
        happen several ticks after the request was
        emitted; the dispatcher must run the sweeper
        on those ticks even if the EventLog has no
        new events for the agent. When the log has
        no new events, the fold is a no-op (the
        World is unchanged) and the cursor is NOT
        advanced (the next non-empty batch still
        sees the same ``last_stream_id``).
        """
        ckpt = await self._world_store.load(agent_id)
        new_events, new_last_stream_id = await _fetch_new_events_fn(
            self, agent_id, ckpt.last_stream_id
        )
        if not new_events:
            if not self._should_run_systems_on_idle_tick():
                return 0
            # No new events from the log; still run
            # the systems (the TTL sweeper may have
            # orphan requests to evict, or a
            # ``WorldSystem`` may have queued work
            # from a prior async drain that has not
            # yet been surfaced -- ADR-049). The
            # fold is a no-op; the cursor is not
            # advanced (we did not consume any new
            # stream entries).
            await _run_systems_and_persist_fn(
                self,
                agent_id=agent_id,
                world=ckpt.world,
                last_stream_id=ckpt.last_stream_id,
                new_event_count=0,
                new_events=[],
            )
            return 0

        world, new_event_count = _fold_with_filter_fn(self, ckpt.world, new_events)
        if new_event_count == 0 and self._tool_ttls is None:
            # If all events were filtered out, and no TTL sweeper is active,
            # we don't need to run systems. We still save the checkpoint so
            # the cursor advances past the filtered events.
            await _save_checkpoint_fn(self, agent_id, world, new_last_stream_id)
            return 0

        await _run_systems_and_persist_fn(
            self, agent_id, world, new_last_stream_id, new_event_count, new_events
        )
        return new_event_count

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
        # Carries the last error string on the instance so the
        # heartbeat line tells the operator the loop is in a
        # "consistently failing" state across many ticks (not just
        # the most recent one). Reset on the first successful tick.
        self._last_loop_error: Optional[str] = None
        while self._running:
            try:
                await self.dispatch_once()
                self._last_loop_error = None
            except Exception as e:
                # ``exc_info=True`` routes the full traceback to the
                # log handler. Without it, the operator sees only
                # ``error=str(e)`` and cannot distinguish a transient
                # connection blip from a deterministic crash on the
                # same code path.
                logger.error("reactive.loop.error", error=str(e), exc_info=True)
                self._last_loop_error = repr(e)
            self._maybe_emit_heartbeat()
            await asyncio.sleep(self._interval)

    def _maybe_emit_heartbeat(self) -> None:
        """Emit a structured liveness line on the cadence
        ``_heartbeat_interval_seconds``. Disabled when the
        interval is non-positive (tests that don't want log
        noise; production callers should leave the default).
        """
        if self._heartbeat_interval_seconds <= 0:
            return
        now = time.monotonic()
        if now - self._last_heartbeat_at < self._heartbeat_interval_seconds:
            return
        self._last_heartbeat_at = now
        logger.info(
            "reactive.loop.heartbeat",
            events_processed_total=self._events_processed_total,
            idle_seconds=now - self._last_activity_at,
            tracked_agents=len(self._tracked_agents),
            last_error=self._last_loop_error,
        )


__all__ = ["ReactiveDispatcher"]
