# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Behaviour tests for ``ReactiveDispatcher`` Tier 4 observability
queries (ADR-075 §2.4).

Five public methods on the dispatcher:
  - ``in_flight_tasks``
  - ``stale_tasks``
  - ``stuck_in_queue``
  - ``dead_lettered_tasks``
  - ``detect_and_recover``

All read-only. Composed on existing primitives (``view.tool_requests`` /
``tool_completions``, ``DeadLetterQueue.list_*``, ``XLEN``,
``XPENDING``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.core.world.projection_tool_calls import project_tool_calls
from kntgraph.infra.world_checkpoint import IncrementalWorldStore
from kntgraph.runner.reactive import ReactiveDispatcher


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers — build a World from raw tool events using the same projection
# pipeline the dispatcher uses (``World.fold(projection=project_tool_calls)``).
# ---------------------------------------------------------------------------


def _make_tool_request_event(
    *,
    agent_id: str,
    tool_name: str,
    timestamp: datetime | None = None,
) -> Event:
    """A ``tool.<name>.requested`` event."""
    return Event.create(
        event_type=f"tool.{tool_name}.requested",
        agent_id=agent_id,
        event_class="domain",
        correlation=CorrelationContext.new(),
        data=MappingProxyType({"tool": tool_name}),
        timestamp=timestamp or datetime.now(tz=timezone.utc),
    )


def _make_tool_completion_event(
    *,
    agent_id: str,
    tool_name: str,
    causation_id: UUID,
    status: str = "completed",
    timestamp: datetime | None = None,
) -> Event:
    """A ``tool.<name>.<status>`` event that joins to the
    request via ``causation_id`` (== request's ``event_id``).
    """
    return Event.create(
        event_type=f"tool.{tool_name}.{status}",
        agent_id=agent_id,
        event_class="domain",
        correlation=CorrelationContext.new(),
        data=MappingProxyType({}),
        causation_id=causation_id,
        timestamp=timestamp or datetime.now(tz=timezone.utc),
    )


def _world_from_tool_events(events: list[Event]) -> World:
    """Fold ``events`` with the tool-call projection so the
    resulting World has ``tool_requests`` / ``tool_completions``
    slots installed on each agent's view. This mirrors the
    production path (``ReactiveDispatcher``'s fold pipeline)."""
    return World.fold(events, projection=project_tool_calls)


class _FakeStorage:
    """Minimal in-memory ``WorldCheckpointStorage`` that
    satisfies the Protocol's ``load``/``save``/``discard``
    methods AND the Tier 4 ``queue_length`` / ``pending_count``
    primitives (so the dispatcher can call them through the
    same adapter it uses for checkpoints — no separate
    ``redis`` plumbing).

    ``load(agent_id)`` returns the **pickled payload** of
    the seeded World — matching the wire format the real
    ``RedisWorldCheckpointStorage`` produces
    (``zlib(pickle.dumps((tick, storage, views, last_stream_id)))``).
    The ``IncrementalWorldStore`` facade unpacks it back
    into a ``World`` so ``_load_views`` finds the seeded
    views. Without the payload, ``load`` would return
    ``World.empty()`` and the dispatcher would never see
    the test fixtures.
    """

    def __init__(
        self,
        world: World,
        *,
        queue_length: "AsyncMock | None" = None,
        pending_count: "AsyncMock | None" = None,
    ) -> None:
        self._world = world
        self._queue_length = queue_length or AsyncMock(return_value=0)
        self._pending_count = pending_count or AsyncMock(return_value=0)
        # Pre-pickle the seeded world once. The dispatcher's
        # ``IncrementalWorldStore.load`` unpacks this back
        # into a ``World`` instance.
        import pickle
        import zlib

        from kntgraph.infra.world_checkpoint import (
            _CHECKPOINT_ZLIB_LEVEL,
            _ZLIB_MAGIC,
        )

        self._pickled_payload = zlib.compress(
            pickle.dumps(
                (
                    self._world.tick,
                    self._world.storage,
                    dict(self._world.views),
                    "1-0",
                )
            ),
            _CHECKPOINT_ZLIB_LEVEL,
        )
        # Defensive: ensure the magic byte is what ``load``
        # sniffs for (zlib-compressed path).
        assert self._pickled_payload[0] == _ZLIB_MAGIC

    async def load(self, agent_id: str) -> Any:
        from kntgraph.core.result import Ok

        return Ok(self._pickled_payload)

    async def load_cursor(self, agent_id: str) -> Any:
        from kntgraph.core.result import Ok

        return Ok("1-0")

    async def save(self, agent_id: str, payload: bytes, **_: Any) -> Any:
        from kntgraph.core.result import Ok

        return Ok(None)

    async def discard(self, agent_id: str) -> Any:
        from kntgraph.core.result import Ok

        return Ok(None)

    async def queue_length(self, stream_key: str) -> int:
        return int(await self._queue_length(stream_key=stream_key))

    async def pending_count(self, stream_key: str) -> int:
        return int(await self._pending_count(stream_key=stream_key))


def _build_world_store(
    world: World,
    *,
    queue_length: "AsyncMock | None" = None,
    pending_count: "AsyncMock | None" = None,
) -> "IncrementalWorldStore":
    """Build an ``IncrementalWorldStore`` whose underlying
    storage is a ``_FakeStorage`` seeded with the test world.

    Returning the facade (not the bare storage) means
    ``stuck_in_queue`` exercises the same forwarding path
    production uses — the test catches regressions in the
    facade's ``getattr`` fallback as well as in the storage
    Protocol methods themselves.
    """
    from kntgraph.infra.world_checkpoint import IncrementalWorldStore

    return IncrementalWorldStore(
        _FakeStorage(
            world,
            queue_length=queue_length,
            pending_count=pending_count,
        )
    )


class _FakeEventLog:
    async def read_after_cursor(
        self, agent_id: str, cursor: str
    ) -> tuple[list[Event], str]:
        return [], "1-0"

    async def list_agents(self) -> list[str]:
        return []

    async def append_batch(self, events: list[Event]) -> Any:
        return ["ok"] * len(events)


def _dispatcher(
    world: World,
    *,
    dlq: Any = None,
    queue_length: "AsyncMock | None" = None,
    pending_count: "AsyncMock | None" = None,
    world_store: "IncrementalWorldStore | None" = None,
) -> ReactiveDispatcher:
    """Build a dispatcher with the given world seeded.

    Seeds ``_tracked_agents`` with every agent present in the
    World so that ``_load_views`` (which iterates the tracked
    set) actually loads the views the test pre-populated.

    ``queue_length`` / ``pending_count`` are wired through the
    underlying storage's Protocol methods (via
    ``_build_world_store``) so the ``stuck_in_queue`` query
    exercises the production adapter path — no separate
    ``redis`` plumbing, no Protocol drift.
    """
    if world_store is None:
        world_store = _build_world_store(
            world,
            queue_length=queue_length,
            pending_count=pending_count,
        )
    dispatcher = ReactiveDispatcher(
        log=_FakeEventLog(),
        world_store=world_store,
        systems=[],
        tool_ttls=None,
        dlq=dlq,
    )
    for agent_id in world.agents:
        dispatcher._tracked_agents.add(agent_id)
    return dispatcher


# ---------------------------------------------------------------------------
# in_flight_tasks
# ---------------------------------------------------------------------------


class TestInFlightTasks:
    async def test_returns_empty_when_no_views(self) -> None:
        """No agents in the world ⇒ empty in-flight list."""
        dispatcher = _dispatcher(World.empty())
        assert await dispatcher.in_flight_tasks() == []

    async def test_one_request_no_completion_is_in_flight(self) -> None:
        """A request without a matching completion is in-flight."""
        req_evt = _make_tool_request_event(agent_id="a-1", tool_name="weather_api")
        world = _world_from_tool_events([req_evt])
        dispatcher = _dispatcher(world)
        tasks = await dispatcher.in_flight_tasks()
        assert len(tasks) == 1
        t = tasks[0]
        assert t.request_event_id == str(req_evt.event_id)
        assert t.tool_name == "weather_api"
        assert t.agent_id == "a-1"

    async def test_request_with_completion_is_not_in_flight(self) -> None:
        """Request with matching completion ⇒ not in-flight."""
        req_evt = _make_tool_request_event(agent_id="a-1", tool_name="weather_api")
        comp_evt = _make_tool_completion_event(
            agent_id="a-1",
            tool_name="weather_api",
            causation_id=req_evt.event_id,
            status="completed",
        )
        world = _world_from_tool_events([req_evt, comp_evt])
        dispatcher = _dispatcher(world)
        assert await dispatcher.in_flight_tasks() == []

    async def test_scopes_to_agent_id(self) -> None:
        """``agent_id=`` scopes the query."""
        req_a = _make_tool_request_event(agent_id="a-1", tool_name="t1")
        req_b = _make_tool_request_event(agent_id="a-2", tool_name="t2")
        world = _world_from_tool_events([req_a, req_b])
        dispatcher = _dispatcher(world)
        a_tasks = await dispatcher.in_flight_tasks(agent_id="a-1")
        assert [t.request_event_id for t in a_tasks] == [str(req_a.event_id)]

    async def test_returns_empty_when_world_store_missing(self) -> None:
        """No world_store wired ⇒ empty list (graceful).

        The dispatcher constructor defaults ``world_store``
        to an ``IncrementalWorldStore`` when ``redis`` is
        provided; the "no world_store" branch in
        ``_load_views`` is only reachable after construction
        if ``_world_store`` is unset. We force it here to
        verify the graceful-empty contract.
        """
        dispatcher = ReactiveDispatcher(
            log=_FakeEventLog(),
            systems=[],
            redis=AsyncMock(),
        )
        dispatcher._world_store = None
        assert await dispatcher.in_flight_tasks() == []


# ---------------------------------------------------------------------------
# stale_tasks
# ---------------------------------------------------------------------------


class TestStaleTasks:
    async def test_fresh_request_is_not_stale(self) -> None:
        """Request whose ``expires_at`` is in the future ⇒ not stale.

        The fold pipeline stamps ``expires_at = requested_at + 300s``
        (default TTL); a request emitted at ``utcnow()`` therefore
        expires well after the query's ``now``.
        """
        now = datetime.now(tz=timezone.utc)
        req = _make_tool_request_event(
            agent_id="a-1",
            tool_name="weather_api",
            timestamp=now,
        )
        world = _world_from_tool_events([req])
        dispatcher = _dispatcher(world)
        # Threshold of 60s; request expires in ~300s ⇒ not stale
        assert await dispatcher.stale_tasks(threshold_seconds=60) == []

    async def test_expired_request_is_stale(self) -> None:
        """Request whose ``expires_at`` is far in the past ⇒ stale.

        With the default TTL (``300s``), a request whose
        ``requested_at`` is 1000s in the past has
        ``expires_at`` ~700s in the past. The query's ``now``
        is comfortably past ``expires_at + threshold``.
        """
        old = datetime.now(tz=timezone.utc) - timedelta(seconds=1000)
        req = _make_tool_request_event(
            agent_id="a-1",
            tool_name="weather_api",
            timestamp=old,
        )
        world = _world_from_tool_events([req])
        dispatcher = _dispatcher(world)
        tasks = await dispatcher.stale_tasks(threshold_seconds=60)
        assert [t.request_event_id for t in tasks] == [str(req.event_id)]


# ---------------------------------------------------------------------------
# stuck_in_queue
# ---------------------------------------------------------------------------


class TestStuckInQueue:
    async def test_returns_empty_when_world_store_missing(self) -> None:
        """No ``world_store`` wired ⇒ empty list (graceful).

        The constructor enforces ``world_store`` is set; the
        only way to exercise the empty branch in
        ``stuck_in_queue`` is to monkey-patch
        ``_world_store = None`` after construction. Production
        never hits this path.
        """
        dispatcher = _dispatcher(World.empty())
        dispatcher._world_store = None
        assert await dispatcher.stuck_in_queue() == []

    async def test_returns_empty_when_no_in_flight_tools(self) -> None:
        """No in-flight tool calls in views ⇒ no tool to scan.

        Even if the storage reports a non-empty queue, the
        query needs at least one in-flight tool request in a
        view to know WHICH stream key to probe.
        """
        dispatcher = _dispatcher(
            World.empty(),
            queue_length=AsyncMock(return_value=5),
            pending_count=AsyncMock(return_value=0),
        )
        assert await dispatcher.stuck_in_queue() == []

    async def test_returns_tool_when_queue_non_empty_no_pending(self) -> None:
        """``queue_length > 0`` AND ``pending_count == 0`` ⇒ stuck."""
        req = _make_tool_request_event(agent_id="a-1", tool_name="slow_tool")
        world = _world_from_tool_events([req])
        queue_length = AsyncMock(return_value=3)
        pending_count = AsyncMock(return_value=0)
        dispatcher = _dispatcher(
            world,
            queue_length=queue_length,
            pending_count=pending_count,
        )
        stuck = await dispatcher.stuck_in_queue()
        assert stuck == ["slow_tool"]
        # The dispatcher should have probed the right stream key.
        queue_length.assert_awaited()
        pending_count.assert_awaited()

    async def test_skips_tool_when_pending_consumer_exists(self) -> None:
        """``pending_count > 0`` ⇒ NOT stuck (a worker is processing)."""
        req = _make_tool_request_event(agent_id="a-1", tool_name="active_tool")
        world = _world_from_tool_events([req])
        dispatcher = _dispatcher(
            world,
            queue_length=AsyncMock(return_value=2),
            pending_count=AsyncMock(return_value=2),
        )
        assert await dispatcher.stuck_in_queue() == []

    async def test_stream_key_includes_prefix_and_tool_name(self) -> None:
        """The probed stream key is ``<prefix>:<tool>:queue``,
        matching ``ToolRouter``'s convention (ADR-036).
        """
        req = _make_tool_request_event(agent_id="a-1", tool_name="weather_api")
        world = _world_from_tool_events([req])
        queue_length = AsyncMock(return_value=1)
        pending_count = AsyncMock(return_value=0)
        dispatcher = _dispatcher(
            world,
            queue_length=queue_length,
            pending_count=pending_count,
        )
        await dispatcher.stuck_in_queue()
        queue_length.assert_awaited_once_with(stream_key="knt:tools:weather_api:queue")
        pending_count.assert_awaited_once_with(stream_key="knt:tools:weather_api:queue")


# ---------------------------------------------------------------------------
# dead_lettered_tasks
# ---------------------------------------------------------------------------


class TestDeadLetteredTasks:
    async def test_returns_empty_when_no_dlq_wired(self) -> None:
        """No DLQ wired ⇒ empty list (graceful)."""
        dispatcher = _dispatcher(World.empty(), dlq=None)
        assert await dispatcher.dead_lettered_tasks() == []

    async def test_delegates_to_dlq_list_all(self) -> None:
        """No filter ⇒ ``DeadLetterQueue.list_all``."""
        dlq = AsyncMock()
        dlq.list_all = AsyncMock(return_value=["entry-1", "entry-2"])
        dispatcher = _dispatcher(World.empty(), dlq=dlq)
        result = await dispatcher.dead_lettered_tasks()
        assert result == ["entry-1", "entry-2"]
        dlq.list_all.assert_awaited_once_with(count=100)

    async def test_delegates_to_dlq_list_by_reason(self) -> None:
        """``reason=`` ⇒ ``DeadLetterQueue.list_by_reason``."""
        from kntgraph.events.dlq.values import DLQReason

        dlq = AsyncMock()
        dlq.list_by_reason = AsyncMock(return_value=["entry-1"])
        dispatcher = _dispatcher(World.empty(), dlq=dlq)
        result = await dispatcher.dead_lettered_tasks(
            reason=DLQReason.TOOL_STALE_UNACKNOWLEDGED, count=10
        )
        assert result == ["entry-1"]
        dlq.list_by_reason.assert_awaited_once_with(
            DLQReason.TOOL_STALE_UNACKNOWLEDGED, count=10
        )

    async def test_delegates_to_dlq_list_for_agent(self) -> None:
        """``agent_id=`` (no ``reason=``) ⇒ ``list_for_agent``."""
        dlq = AsyncMock()
        dlq.list_for_agent = AsyncMock(return_value=["entry-1"])
        dispatcher = _dispatcher(World.empty(), dlq=dlq)
        result = await dispatcher.dead_lettered_tasks(agent_id="a-1", count=5)
        assert result == ["entry-1"]
        dlq.list_for_agent.assert_awaited_once_with("a-1", count=5)


# ---------------------------------------------------------------------------
# detect_and_recover
# ---------------------------------------------------------------------------


class TestDetectAndRecover:
    async def test_report_with_no_data(self) -> None:
        """Empty world ⇒ all counts zero."""
        dispatcher = _dispatcher(World.empty(), dlq=AsyncMock())
        report = await dispatcher.detect_and_recover()
        assert report.in_flight_count == 0
        assert report.stale_count == 0
        assert report.stuck_in_queue_count == 0
        assert report.dead_lettered_count == 0
        assert report.dry_run is False

    async def test_report_counts_in_flight(self) -> None:
        """Counts the in-flight tasks correctly."""
        req = _make_tool_request_event(agent_id="a-1", tool_name="tool")
        world = _world_from_tool_events([req])
        dispatcher = _dispatcher(world, dlq=AsyncMock())
        report = await dispatcher.detect_and_recover()
        assert report.in_flight_count == 1
        assert report.stale_count == 0

    async def test_report_dry_run_propagated(self) -> None:
        """``dry_run=True`` is recorded in the report."""
        dispatcher = _dispatcher(World.empty(), dlq=AsyncMock())
        report = await dispatcher.detect_and_recover(dry_run=True)
        assert report.dry_run is True
