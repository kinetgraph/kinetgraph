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
from collections.abc import Callable, Mapping

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
from ._observability import (
    InFlightTask,
    RecoveryReport,
    dead_lettered_tasks as _dead_lettered_tasks,
    in_flight_tasks as _in_flight_tasks,
    stale_tasks as _stale_tasks,
    stuck_in_queue as _stuck_in_queue,
)
from ._metrics import MetricsSink, NullMetricsSink
from ._systems_runner import (
    run_systems_and_persist as _run_systems_and_persist_fn,
)
from .tool_call_ttl_sweeper import ToolCallTTLSweeperSystem

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from ..core.event.correlation import CorrelationContext
    from ..core.world.view import AgentView
    from ..events.dlq.store import DeadLetterQueue
    from ..events.dlq.values import DeadLetterEvent
    from ..tools.router import ToolRouter
    from .reactive_extensions import WorldProjection

logger = structlog.get_logger()


def _anchor_event(correlation: "CorrelationContext") -> Event:
    """Build a synthetic anchor ``Event`` from a stored
    correlation so the dispatcher's idle-tick path can
    pass it to ``correlation_middleware.continue_from``.

    The Event's ``event_id`` is irrelevant to
    ``continue_from`` (only the correlation metadata
    flows); we mint a fresh UUID per call so two
    consecutive idle ticks don't share an anchor id
    (this keeps the audit chain's per-event ids unique
    if a downstream system ever logs them).
    """
    from datetime import datetime, timezone
    from uuid import uuid4

    return Event.create(
        event_type="knt.dispatcher.anchor",
        agent_id="_dispatcher_",
        event_class="lifecycle",
        correlation=correlation,
        event_id=uuid4(),
        timestamp=datetime.now(tz=timezone.utc),
    )


def _last_domain_correlation(events: list[Event]) -> "CorrelationContext | None":
    """Return the correlation of the LAST ``domain`` event
    in ``events`` (or ``None`` when no domain event is
    present).

    Domain events carry the flow's correlation_id;
    lifecycle events do NOT (they are operational
    metadata with a fresh uuid4 correlation). When a
    batch contains a mix (e.g. bootstrap's
    ``agent.spawned`` followed by a real
    ``request.received``), the dispatcher must thread
    the DOMAIN event's correlation to the systems, not
    the lifecycle one.

    The LAST domain event is preferred over the FIRST
    because systems react to the latest state of the
    World (which reflects the latest domain event).
    """
    for e in reversed(events):
        if e.event_class == "domain":
            return e.correlation
    return None


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
        fallback_poll_interval: Optional[float] = None,
        wake_on_event: bool = True,
        dlq: Optional["DeadLetterQueue"] = None,
        tool_stream_prefix: str = "knt:tools",
        metrics_sink: Optional["MetricsSink"] = None,
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
            fallback_poll_interval: how long a silent wake-up is
                tolerated before the fallback poll runs (the
                notification-is-a-hint correctness net, ADR-068
                §3.1). ``None`` reads the
                ``KNT_FALLBACK_POLL_INTERVAL`` knob (default 5.0).
            wake_on_event: when True (default), the loop's idle
                path blocks in ``subscribe_many`` (one held
                connection for all tracked agents) instead of
                sleeping the poll interval; a lost notification
                converges via the fallback poll. Set False to
                force the legacy pure-poll cadence (deployments
                whose pool budget forbids a held connection).
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
        if fallback_poll_interval is None:
            from kntgraph.infra.config import fresh_settings

            fallback_poll_interval = fresh_settings().fallback_poll_interval
        self._interval = poll_interval
        # Push-first switch (ADR-068 §3.2). ``subscribe`` is
        # duck-typed so legacy storages without the primitive
        # transparently fall back to the pure-poll cadence.
        self._wake_on_event = wake_on_event and hasattr(log, "subscribe")
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
        # Push-first wake-up state (ADR-068 §3.2): per-agent
        # durable cursors the ``subscribe_many`` fan-in read
        # starts from, plus the fallback-poll deadline that
        # guarantees convergence when a notification is lost.
        # Populated on the first full sweep (bootstrap) and
        # updated on every successful dispatch.
        self._subscribe_cursors: dict[str, str] = {}
        # Deadline (monotonic) after which a full dispatch_once
        # runs even when no wake-up arrived.
        self._next_fallback_at: float = 0.0
        # How long a silent wake-up is tolerated before the
        # fallback poll runs (ADR-068 §3.8 knob). Read once at
        # construction; the operator restarts to re-tune.
        self._fallback_interval: float = fallback_poll_interval
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
        # ADR-075 Tier 4: optional DLQ reference for the
        # ``dead_lettered_tasks`` query and the saga → DLQ
        # wire inside the TTL sweeper. ``None`` disables both.
        self._dlq: Optional[DeadLetterQueue] = dlq
        # ADR-075 Tier 4: stream-key prefix used by the
        # ``stuck_in_queue`` query. Defaults match the
        # ToolRouter convention (``knt:tools:<name>:queue``).
        self._tool_stream_prefix: str = tool_stream_prefix
        # ADR-075 Tier 4 observability surface: the metrics
        # backend the dispatcher pushes to. ``None`` selects
        # the no-op ``NullMetricsSink`` so the dispatcher's
        # hot path is unchanged when no backend is installed.
        # Concrete sinks live in optional extras
        # (``kntgraph[metrics]`` for Prometheus, etc.).
        self._metrics_sink: MetricsSink = (
            metrics_sink if metrics_sink is not None else NullMetricsSink()
        )

    @property
    def systems(self) -> list[WorldSystem]:
        return list(self._systems)

    def add_system(self, system: WorldSystem) -> None:
        self._systems.append(system)

    def add_projection(self, projection: "WorldProjection") -> None:
        """Register a post-fold projection (ADR-069 §9.2 item 6).

        Projections run after the base fold and the built-in
        memory hydration, and before the tool overlay, in the
        order they were registered. A Concordo that materialises
        a component from its own events (e.g. the saga's
        ``SagaProgressComponent``) registers its projection here
        so the fold auto-hydrates it.
        """
        self._projections.append(projection)

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

        sem = asyncio.Semaphore(50)

        async def _dispatch_sem(aid: str) -> int:
            async with sem:
                res = await self._dispatch_for_agent(aid)
                load_cursor = getattr(self._world_store, "load_cursor", None)
                if load_cursor is not None:
                    seeded = await load_cursor(aid)
                    if seeded is not None:
                        self._subscribe_cursors[aid] = seeded
                    else:
                        self._subscribe_cursors.pop(aid, None)
                return res

        results = await asyncio.gather(
            *(_dispatch_sem(aid) for aid in list(self._tracked_agents))
        )
        processed = sum(results)
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
        :class:`ToolCallTTLSweeperSystem` is the primary
        motivation: an orphan request sits in the slot
        until its TTL expires, which may happen several
        ticks after the request was emitted; the
        dispatcher must run the sweeper on those ticks
        even if the EventLog has no new events for the
        agent. When the log has no new events, the fold
        is a no-op (the World is unchanged) and the
        cursor is NOT advanced (the next non-empty batch
        still sees the same ``last_stream_id``).

        Correlation propagation (ADR-037): the dispatcher
        threads the trigger event's correlation to the
        systems via ``correlation_middleware.continue_from``.
        Systems that emit via
        ``correlation_middleware.current()`` inherit the
        trigger's ``correlation_id``, so the audit chain
        stitches the entry event through to all downstream
        events. On idle ticks (no new events), the
        correlation is loaded from the checkpoint's
        ``last_event_correlation`` so the chain stays
        intact across ticks that re-run the systems
        (e.g. the TTL sweeper's overdue-eviction path).
        """
        from kntgraph.core.event import correlation_middleware

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
            #
            # Correlation: load the last event's
            # correlation from the agent's view (the
            # projection keeps it in sync with the
            # EventLog; the dispatcher does NOT need
            # to re-read the EventLog for the audit
            # chain). ``continue_from`` mints a fresh
            # ``span_id`` (this tick is a new
            # operation) but keeps the
            # ``correlation_id``.
            last_view = ckpt.world.get_agent(agent_id)
            last_corr = (
                last_view.last_event_correlation if last_view is not None else None
            )
            if last_corr is not None:
                correlation_middleware.continue_from(_anchor_event(last_corr))
                try:
                    await _run_systems_and_persist_fn(
                        self,
                        agent_id=agent_id,
                        world=ckpt.world,
                        last_stream_id=ckpt.last_stream_id,
                        new_event_count=0,
                        new_events=[],
                    )
                finally:
                    correlation_middleware.clear()
            else:
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

        # Correlation propagation: thread the LAST domain
        # event's correlation to the systems. The batch may
        # include lifecycle events (e.g. ``agent.spawned``
        # on bootstrap) whose correlation is a fresh
        # uuid4 — those are operational metadata, NOT flow
        # events. The domain events carry the flow's
        # correlation_id; the LAST domain event in the batch
        # is the most-recent trigger the systems should
        # react to.
        #
        # After the fold above, the world's last_event_*
        # fields point to the LAST event folded (regardless
        # of class). We prefer a domain event's correlation
        # but fall back to the world's
        # ``last_event_correlation`` (which the projection
        # already keeps in sync — domain events overwrite
        # it, lifecycle events preserve it).
        last_corr = _last_domain_correlation(new_events)
        if last_corr is None:
            # No domain events in this batch (rare: a
            # batch of pure lifecycle events). Fall back to
            # the projection's bookkeeping.
            view = world.get_agent(agent_id)
            last_corr = view.last_event_correlation if view is not None else None
        if last_corr is not None:
            correlation_middleware.continue_from(_anchor_event(last_corr))
        else:
            # No correlation available (first-ever tick on
            # a fresh agent with no events). Mint a fresh
            # one — there's no flow to propagate.
            correlation_middleware.start()
        try:
            await _run_systems_and_persist_fn(
                self,
                agent_id,
                world,
                new_last_stream_id,
                new_event_count,
                new_events,
            )
        finally:
            correlation_middleware.clear()
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
                if self._wake_on_event:
                    await self._wake_once()
                else:
                    await self.dispatch_once()
                    # Legacy poll cadence: sleep between
                    # sweeps (the wake path's sleep is the
                    # blocking subscribe read itself).
                    await asyncio.sleep(self._interval)
                self._last_loop_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # ``exc_info=True`` routes the full traceback to the
                # log handler. Without it, the operator sees only
                # ``error=str(e)`` and cannot distinguish a transient
                # connection blip from a deterministic crash on the
                # same code path.
                logger.error("reactive.loop.error", error=str(e), exc_info=True)
                self._last_loop_error = repr(e)
                # Back off before the next attempt — a crash inside
                # the wake path would otherwise spin on a broken
                # connection at the poll interval's rate.
                await asyncio.sleep(self._interval)
                self._maybe_emit_heartbeat()
                continue
            self._maybe_emit_heartbeat()

    async def _wake_once(self) -> None:
        """One iteration of the push-first loop (ADR-068 §3.2).

        Idle path: block in ``subscribe_many`` on all tracked
        agents (ONE held connection) until any of them
        receives an event or ``fallback_poll_interval``
        elapses. The timeout arm runs the full
        ``dispatch_once`` — the convergence net that closes
        lost notifications and picks up rediscovered agents.
        The wake arm dispatches only the agents whose cursor
        moved, then loops straight back into the blocking
        read: zero round-trips while silent.
        """
        now = time.monotonic()
        # Bootstrap: the first sweep populates the tracked
        # agents AND the per-agent cursors the wake-up read
        # starts from.
        if not self._bootstrapped or now >= self._next_rediscovery_at:
            await _bootstrap_agents_fn(self)
            self._bootstrapped = True
            self._next_rediscovery_at = now + self._rediscovery_interval_seconds

        if not self._tracked_agents or not self._has_subscribable_agents():
            # Nothing to fan-in on yet (no agent has a durable
            # cursor). The full sweep also seeds the cursors.
            await self.dispatch_once()
            self._next_fallback_at = time.monotonic() + self._fallback_interval
            await asyncio.sleep(self._interval)
            return

        block_ms = int(self._fallback_interval * 1000)
        try:
            new_cursors, _wake_events = await self._log.subscribe(
                self._subscribable_agents(),
                cursors=self._subscribe_cursors,
                block_ms=block_ms,
            )
        except (asyncio.CancelledError,):
            raise
        except Exception as e:
            # A failed wake-up is an I/O crash signal: log, then
            # let the fallback poll below converge the state.
            # Swallowing without logging would turn a Redis
            # outage into a silent stall.
            logger.warning("reactive.wake.subscribe_failed", error=str(e))
            await self.dispatch_once()
            self._next_fallback_at = time.monotonic() + self._fallback_interval
            await asyncio.sleep(self._interval)
            return

        # The subscribe cursors advanced for the agents that
        # woke; merge them before the dispatch so the dispatch
        # cycle reads strictly-after-what-we-already-saw. The
        # per-agent dispatch still owns the durable checkpoint
        # (the subscribe cursor is the wake-up hint position;
        # the checkpoint cursor is the commit point).
        self._subscribe_cursors.update(new_cursors)
        self._last_activity_at = time.monotonic()
        if new_cursors:
            woken_agents = list(new_cursors.keys())
            sem = asyncio.Semaphore(50)

            async def _dispatch_sem(aid: str) -> int:
                async with sem:
                    res = await self._dispatch_for_agent(aid)
                    load_cursor = getattr(self._world_store, "load_cursor", None)
                    if load_cursor is not None:
                        seeded = await load_cursor(aid)
                        if seeded is not None:
                            self._subscribe_cursors[aid] = seeded
                        else:
                            self._subscribe_cursors.pop(aid, None)
                    return res

            results = await asyncio.gather(
                *(_dispatch_sem(aid) for aid in woken_agents)
            )
            self._events_processed_total += sum(results)
        else:
            self._events_processed_total += await self.dispatch_once()

    def _subscribable_agents(self) -> list[str]:
        """Tracked agents that have a known durable cursor.

        The ``subscribe_many`` call subscribes only these; an
        agent without a cursor (never dispatched, or its
        storage lacks the cursor key) is covered by the full
        sweep, which seeds its cursor on completion.
        """
        return [a for a in self._tracked_agents if a in self._subscribe_cursors]

    def _has_subscribable_agents(self) -> bool:
        """True when at least one tracked agent has a seeded
        cursor (the wake-up read can include it)."""
        return bool(self._subscribe_cursors)

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

    # ------------------------------------------------------------------
    # ADR-075 Tier 4: Recovery-driven observability queries
    # ------------------------------------------------------------------
    # Each query is async (does NOT block the dispatcher's tick
    # loop). They compose on the existing ``_world_store`` and
    # ``_dlq`` references — no new event types, no new
    # projections, no new system class. See
    # ``_observability.py`` for the low-level read helpers.

    async def _load_views(self, agent_id: str | None = None) -> Mapping[str, AgentView]:
        """Load agent views from the world_store for Tier 4
        queries.

        Each call is independent (one ``XGET`` per agent).
        Returns an empty dict if ``world_store`` is unset.

        Returns ``Mapping`` (not ``dict``) so callers see a
        read-only view of the agent set: ``dict`` is
        invariant in its value type, which trips pyright on
        the ``agent_id=`` branch (the value is
        ``AgentView | None`` before the ``None`` check).
        ``Mapping`` is covariant and lets callers iterate
        without forcing a value-narrowing on the
        ``get_agent(...)`` probe.
        """
        if self._world_store is None:
            return {}
        if agent_id is not None:
            ckpt = await self._world_store.load(agent_id)
            view = ckpt.world.get_agent(agent_id)
            return {agent_id: view} if view is not None else {}
        out: dict[str, AgentView] = {}
        for aid in self._tracked_agents:
            ckpt = await self._world_store.load(aid)
            view = ckpt.world.get_agent(aid)
            if view is not None:
                out[aid] = view
        return out

    async def in_flight_tasks(self, agent_id: str | None = None) -> list[InFlightTask]:
        """Return all in-flight tool tasks across the
        dispatcher's tracked agents (ADR-075 §2.4 row #8).

        In-flight = ``tool.<name>.requested`` event has
        landed in the projection but no
        ``tool.<name>.completed`` / ``failed`` event has
        landed yet. The query reads from ``tool_requests``
        minus ``tool_completions`` in the per-agent view
        (the same source the TTL sweeper uses; the EventLog
        is the source of truth).

        ``agent_id=None`` returns tasks for every tracked
        agent; pass an id to scope the result.

        Side effect: pushes the result length to the
        configured :class:`MetricsSink` via
        :meth:`MetricsSink.record_in_flight`.
        """
        views = await self._load_views(agent_id)
        result = _in_flight_tasks(agents_to_views=views, agent_id=agent_id)
        self._metrics_sink.record_in_flight(len(result))
        return result

    async def stale_tasks(
        self,
        threshold_seconds: float = 300.0,
        agent_id: str | None = None,
    ) -> list[InFlightTask]:
        """Subset of ``in_flight_tasks`` whose ``expires_at``
        is past ``threshold_seconds`` ago (ADR-075 §2.4 row
        #11: the "stale but not yet recovered" race window).

        The threshold defaults to 5 minutes — the standard
        tool recovery SLA. Operators tune this per deployment
        depending on tool-latency budgets.

        Side effect: pushes the result length to the
        configured :class:`MetricsSink` via
        :meth:`MetricsSink.record_stale`.
        """
        views = await self._load_views(agent_id)
        result = _stale_tasks(
            agents_to_views=views,
            threshold_seconds=threshold_seconds,
            agent_id=agent_id,
        )
        self._metrics_sink.record_stale(len(result))
        return result

    async def stuck_in_queue(
        self,
        threshold_seconds: float = 300.0,
    ) -> list[str]:
        """Stream-keys whose queue is non-empty and no
        consumer is pending (ADR-075 §2.4 row #9).

        Returns the **tool names** whose queue matches the
        pattern, NOT the message ids. The caller can fetch
        the message ids from ``Redis.xrange(...)`` if needed.

        **Decomposes into two primitives** on the
        ``WorldCheckpointStorage`` Protocol (per ADR-075
        §2.4):

        - ``storage.queue_length(stream_key)`` says "messages
          were ever entered but not consumed" (``XLEN``
          semantics).
        - ``storage.pending_count(stream_key)`` says
          "messages are currently being processed"
          (``XPENDING`` semantics, single-probe).

        The dispatcher passes its ``IncrementalWorldStore``
        (the facade over ``WorldCheckpointStorage``); the
        facade forwards to the underlying storage. No raw
        Redis client is held by the dispatcher for this
        query — the adapter boundary is preserved.

        A high ``queue_length`` AND zero ``pending_count``
        ⇒ stuck. The threshold controls the false-positive
        rate; the canonical primitive (``XPENDING``) is
        exact for the "in flight" half, so the only false
        positive is "long-idle PEL" which is rare.

        ``world_store`` is **always** wired by the
        dispatcher constructor (the constructor raises if
        neither ``world_store`` nor ``redis`` is supplied).
        The graceful-empty branch exists for callers who
        monkey-patch ``_world_store = None`` after
        construction (test scenario only); production code
        never hits it.

        Side effect: pushes the result length to the
        configured :class:`MetricsSink` via
        :meth:`MetricsSink.record_stuck_in_queue`.
        """
        if self._world_store is None:
            return []
        views = await self._load_views(None)
        result = await _stuck_in_queue(
            stream_inspector=self._world_store,
            stream_prefix=self._tool_stream_prefix,
            agents_to_views=views,
            threshold_seconds=threshold_seconds,
        )
        self._metrics_sink.record_stuck_in_queue(len(result))
        return result

    async def dead_lettered_tasks(
        self,
        reason=None,
        agent_id: str | None = None,
        count: int = 100,
    ) -> list[DeadLetterEvent]:
        """Read DLQ entries awaiting operator action
        (ADR-075 §2.4 row #10).

        Composes the existing ``DeadLetterQueue.list_*``
        API — no new storage path. The DLQ is wired via
        ``dlq=`` on the dispatcher constructor; if ``dlq``
        is ``None`` (default) the query returns ``[]`` so
        deployments that don't opt in don't crash.

        ``reason`` filters by ``DLQReason`` (typically
        ``TOOL_STALE_ACKNOWLEDGED`` / ``TOOL_STALE_UNACKNOWLEDGED``
        after this ADR lands). ``agent_id`` filters by agent.
        No filter ⇒ ``list_all``.

        Side effect: pushes the result length to the
        configured :class:`MetricsSink` via
        :meth:`MetricsSink.record_dead_lettered`.
        """
        result = await _dead_lettered_tasks(
            self._dlq,
            reason=reason,
            agent_id=agent_id,
            count=count,
        )
        self._metrics_sink.record_dead_lettered(len(result))
        return result

    async def detect_and_recover(
        self,
        stale_threshold_s: float = 300.0,
        stuck_in_queue_threshold_s: float = 300.0,
        dry_run: bool = False,
    ) -> RecoveryReport:
        """Convenience entry point (ADR-075 §2.4.1).

        Runs the recovery read-only queries and returns a
        structured ``RecoveryReport``. **Does NOT trigger the
        sweeper** — the sweeper runs once per tick as part
        of the dispatch loop. This report lets the operator
        (cron / alert) see the state without coupling to
        the tick loop.

        ``dry_run`` is reserved for future expansion (no
        current behaviour is gated on it; included in the
        signature so operator scripts can adopt the API
        without future code changes).
        """
        in_flight = await self.in_flight_tasks()
        stale = await self.stale_tasks(stale_threshold_s)
        stuck = await self.stuck_in_queue(stuck_in_queue_threshold_s)
        dlq = await self.dead_lettered_tasks(count=100)
        return RecoveryReport(
            in_flight_count=len(in_flight),
            stale_count=len(stale),
            stuck_in_queue_count=len(stuck),
            dead_lettered_count=len(dlq),
            dry_run=dry_run,
        )


__all__ = ["ReactiveDispatcher"]
