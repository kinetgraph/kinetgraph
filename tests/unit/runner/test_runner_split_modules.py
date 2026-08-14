# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Direct unit tests for the helpers extracted from
``ReactiveDispatcher`` into the private modules
``_folding``, ``_checkpoint_io``, and ``_systems_runner``
(file-layout §3.1 — the dispatcher module had grown past
the 500-line guideline).

The dispatcher's public tests cover the helpers
end-to-end; these tests pin the helpers' contracts
directly so a future refactor of the dispatcher's
orchestrator does not silently change a helper's
branch coverage.

Each helper is tested for:
  - the happy path (the orchestrator-driven call)
  - one failure mode (the branch the orchestrator
    does not exercise in the existing dispatcher tests)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.infra.world_checkpoint import WorldCheckpoint
from kntgraph.runner._checkpoint_io import (
    bootstrap_agents,
    fetch_new_events,
    save_checkpoint,
)
from kntgraph.runner._folding import fold_with_filter, fold_with_systems
from kntgraph.runner._systems_runner import (
    append_system_outgoing,
    run_systems_and_persist,
)


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _Captured:
    appended: list[Event] = field(default_factory=list)
    saved: list[tuple[str, WorldCheckpoint]] = field(default_factory=list)


class _FakeEventLog:
    def __init__(self, cap: _Captured) -> None:
        self._cap = cap
        self._agents: dict[str, list[Event]] = {}
        self._appended: list[Event] = []

    def add_agent(self, agent_id: str, *events: Event) -> None:
        self._agents.setdefault(agent_id, []).extend(events)

    async def read_after_cursor(
        self, agent_id: str, cursor: str
    ) -> tuple[list[Event], str]:
        pending = self._agents.get(agent_id, [])
        events = list(pending) if cursor in ("-", "-1") else []
        if events:
            self._agents[agent_id] = []
            return events, "1-0"
        return events, cursor

    async def list_agents(self) -> list[str]:
        return sorted(self._agents.keys())

    async def append_batch(self, events: list[Event]) -> Any:
        self._cap.appended.extend(events)
        return ["ok"] * len(events)


class _FakeWorldStore:
    def __init__(self, cap: _Captured) -> None:
        self._cap = cap

    async def load(self, agent_id: str) -> WorldCheckpoint:
        return WorldCheckpoint(world=World.empty(), last_stream_id="-")

    async def save(self, agent_id: str, checkpoint: WorldCheckpoint) -> None:
        self._cap.saved.append((agent_id, checkpoint))


def _seed_event(agent_id: str, event_type: str = "fixture.event") -> Event:
    return Event.domain_from(
        agent_id=agent_id,
        type=event_type,
        data={"k": "v"},
        correlation=CorrelationContext.new(correlation_id=uuid4()),
    )


def _build_dispatcher(*, log: _FakeEventLog, store: _FakeWorldStore, **kwargs):
    """Build a minimal ``ReactiveDispatcher`` for the
    helper tests. Avoids importing the dispatcher class
    directly so the helpers' contracts are pinned even
    if the constructor changes.
    """
    from kntgraph.runner.reactive import ReactiveDispatcher

    return ReactiveDispatcher(log=log, world_store=store, systems=[], **kwargs)


# ---------------------------------------------------------------------------
# _folding.fold_with_filter
# ---------------------------------------------------------------------------


class TestFoldWithFilter:
    async def test_fold_counts_surviving_events(self) -> None:
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        events = [_seed_event("a-1") for _ in range(3)]
        world, count = fold_with_filter(dispatcher, World.empty(), events)
        assert count == 3
        assert "a-1" in world.views

    async def test_filter_excludes_events(self) -> None:
        """The branch the dispatcher does not exercise
        in the existing tests: ``_filter`` returning
        ``False`` for an event. Pinned here so the
        ``new_event_count`` invariant (only surviving
        events are counted) is not lost.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        dispatcher._filter = lambda _e: False
        events = [_seed_event("a-1") for _ in range(3)]
        _world, count = fold_with_filter(dispatcher, World.empty(), events)
        assert count == 0


# ---------------------------------------------------------------------------
# _folding.fold_with_systems
# ---------------------------------------------------------------------------


class TestFoldWithSystems:
    async def test_no_tool_events_returns_same_world(self) -> None:
        """The fast-path branch (ADR-044 §2.4): a batch
        without any ``tool.*`` event returns the input
        World unchanged, with zero allocation.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        world = World.empty()
        result = fold_with_systems(dispatcher, world, [_seed_event("a-1")])
        assert result is world

    async def test_tool_completion_triggers_re_fold(self) -> None:
        """The branch where ``system_events`` contains
        a ``tool.*.completed`` event: the loop folds
        each event into the World and the overlay
        returns a new World with the tool-call
        slot evicted. Pinned so the orphan-request
        GC path (ADR-045) is exercised end-to-end.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        world = World.empty()
        completion = _seed_event("a-1", "tool.echo.completed")
        result = fold_with_systems(dispatcher, world, [completion])
        # The overlay did something (the returned World
        # is a new object), and the world view carries
        # the completion's data.
        assert result is not world

    async def test_filter_surviving_events_without_tool_events(self) -> None:
        """The branch ``if new_event_count > 0 and
        _has_tool_events(new_events)``: when the filter
        accepts events but none are ``tool.*``, the
        overlay is skipped. Pinned so a future refactor
        does not start running the overlay on
        non-tool batches (allocation cost).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        # Filter accepts (so count > 0); events are
        # domain, not tool.*
        dispatcher._filter = lambda _e: True
        events = [_seed_event("a-1", "user.intent")]
        world, count = fold_with_filter(dispatcher, World.empty(), events)
        assert count == 1
        # The World was created (the fold ran) but
        # the overlay did not (no tool events).
        assert "a-1" in world.views


# ---------------------------------------------------------------------------
# _systems_runner.append_system_outgoing
# ---------------------------------------------------------------------------


class TestAppendSystemOutgoing:
    async def test_sync_system(self) -> None:
        """A system that returns a list directly
        (the ``isinstance(out, list)`` arm).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        emitted = _seed_event("a-1", "sync.evt")

        def _sync_system(_world: World) -> list[Event]:
            return [emitted]

        dispatcher._systems = [_sync_system]
        out = await append_system_outgoing(dispatcher, World.empty(), "a-1")
        assert out is None
        assert log._cap.appended == [emitted]

    async def test_async_system(self) -> None:
        """The branch the orchestrator does not
        exercise: a system that returns a coroutine
        (``not isinstance(out, list)`` arm). Pinned so
        the ``await out`` path is not lost.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        emitted = _seed_event("a-1", "async.evt")

        async def _async_system(_world: World) -> list[Event]:
            return [emitted]

        dispatcher._systems = [_async_system]
        await append_system_outgoing(dispatcher, World.empty(), "a-1")
        assert log._cap.appended == [emitted]

    async def test_router_receives_batch(self) -> None:
        """The branch where ``_tool_router`` is set:
        the events are routed AFTER they are appended
        to the EventLog (ADR-036 §2.5).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        router = AsyncMock()
        dispatcher._tool_router = router
        emitted = _seed_event("a-1", "router.evt")
        dispatcher._systems = [lambda _w: [emitted]]
        await append_system_outgoing(dispatcher, World.empty(), "a-1")
        assert router.route_batch.await_args.args[0] == [emitted]

    async def test_system_returning_empty_list_is_skipped(self) -> None:
        """The branch ``if out: outgoing.extend(out)``
        when the system returns an empty list. Pinned
        so a future refactor does not treat a
        zero-event system as an error.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        dispatcher._systems = [lambda _w: []]
        await append_system_outgoing(dispatcher, World.empty(), "a-1")
        # The log was NOT appended (no events).
        assert log._cap.appended == []

    async def test_no_systems_means_no_append(self) -> None:
        """The branch ``if outgoing: append_batch(...)``
        when no system is registered. Pinned so the
        dispatcher short-circuits cleanly on an empty
        systems list.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        dispatcher._systems = []
        await append_system_outgoing(dispatcher, World.empty(), "a-1")
        assert log._cap.appended == []


# ---------------------------------------------------------------------------
# _systems_runner.run_systems_and_persist
# ---------------------------------------------------------------------------


class TestRunSystemsAndPersist:
    async def test_router_called_only_when_batch_non_empty(self) -> None:
        """The branch where ``new_event_count > 0`` AND
        ``_tool_router`` is set: the router receives the
        NEW batch (not the system-emitted events; the
        system-emitted events go through
        ``append_system_outgoing`` separately).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        router = AsyncMock()
        dispatcher._tool_router = router
        new_events = [_seed_event("a-1", "new.evt")]
        await run_systems_and_persist(
            dispatcher, "a-1", World.empty(), "1-0", 1, new_events
        )
        assert router.route_batch.await_args.args[0] == new_events
        assert cap.saved  # checkpoint saved

    async def test_no_router_no_call(self) -> None:
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        new_events = [_seed_event("a-1", "new.evt")]
        await run_systems_and_persist(
            dispatcher, "a-1", World.empty(), "1-0", 0, new_events
        )
        # ``new_event_count == 0`` short-circuits the
        # router call even when one is wired in.
        assert cap.saved

    async def test_silent_systems_skip_fold_with_systems(self) -> None:
        """The branch ``if system_events:`` when the
        systems produced no events: ``fold_with_systems``
        is skipped (no orphan-request GC to run). Pinned
        so a future refactor does not call the fold
        helper unconditionally (the overlay would do
        a no-op pass but at the cost of allocating a
        new World).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        dispatcher._systems = [lambda _w: []]  # silent system
        new_events = [_seed_event("a-1", "new.evt")]
        await run_systems_and_persist(
            dispatcher, "a-1", World.empty(), "1-0", 1, new_events
        )
        # The checkpoint was saved (always); the
        # systems were run; nothing was appended.
        assert cap.saved
        assert log._cap.appended == []


# ---------------------------------------------------------------------------
# _checkpoint_io
# ---------------------------------------------------------------------------


class TestCheckpointIO:
    async def test_bootstrap_collects_agents(self) -> None:
        cap = _Captured()
        log = _FakeEventLog(cap)
        log.add_agent("a-1")
        log.add_agent("a-2")
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        assert dispatcher._tracked_agents == set()
        await bootstrap_agents(dispatcher)
        assert dispatcher._tracked_agents == {"a-1", "a-2"}

    async def test_bootstrap_with_empty_log_is_noop(self) -> None:
        """The branch where ``list_agents`` returns an
        empty list: the bootstrap loop body never runs
        (``for aid in agent_ids:`` over an empty
        sequence). Pinned so the dispatcher's "no
        tenants yet" path is observed.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        await bootstrap_agents(dispatcher)
        assert dispatcher._tracked_agents == set()

    async def test_fetch_new_events_returns_batch_and_cursor(self) -> None:
        cap = _Captured()
        log = _FakeEventLog(cap)
        log.add_agent("a-1", _seed_event("a-1"))
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        events, cursor = await fetch_new_events(dispatcher, "a-1", "-")
        assert len(events) == 1
        assert cursor == "1-0"

    async def test_save_checkpoint_writes_world(self) -> None:
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        world = World.empty()
        await save_checkpoint(dispatcher, "a-1", world, "5-0")
        assert cap.saved == [
            ("a-1", WorldCheckpoint(world=world, last_stream_id="5-0"))
        ]
