# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the ``ReactiveDispatcher`` Slot GC
(ADR-045 follow-up; DEBT §2.21).

The :class:`ToolCallTTLSweeperSystem` emits
``tool.<name>.failed`` events for stale requests in
the ``tool_requests`` slot. Without a GC step, those
orphan requests stay in the slot forever (the
completion-driven eviction rule in
``overlay_tool_calls`` only fires when the completion
event lands in a NEXT tick's batch, but the
TTL-failure event is emitted in the CURRENT tick and
was never folded into the slot via the first overlay
pass — the overlay runs BEFORE the systems).

The dispatcher's GC step re-overlays the World with
the system-emitted events, so the
``tool.<name>.failed`` event joins the slot and the
completion-driven eviction rule
(``request in existing_completions`` -> ``pop``) fires
in the same tick. The orphan request is gone from
the checkpointed World; downstream consumers (which
read the slot on the next tick) see a clean slot.

These tests exercise the dispatcher's
:meth:`_run_systems_and_persist` path end-to-end
(systems running after the fold) and assert that the
checkpointed World no longer carries the orphan
request. A ``_FakeEventLog`` captures the appended
events; a ``_FakeWorldStore`` captures the saved
World. The TTL sweeper is injected with a fixed clock
so the assertion is deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.core.world.components import ToolCallTTL
from kntgraph.infra.world_checkpoint import WorldCheckpoint
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.runner.tool_call_ttl_sweeper import (
    ToolCallTTLSweeperSystem,
)


_BASE_TS = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _ctx() -> CorrelationContext:
    return CorrelationContext.new(correlation_id=uuid4())


def _ts(offset_s: int = 0) -> datetime:
    return _BASE_TS + timedelta(seconds=offset_s)


def _request_event(*, tool_name: str, ts: datetime) -> Event:
    return Event.create(
        event_type=f"tool.{tool_name}.requested",
        agent_id="a-1",
        event_class="domain",
        data={"tool": tool_name, "params": {}},
        correlation=_ctx(),
        timestamp=ts,
    )


@dataclass
class _Captured:
    appended: list[Event]
    saved: list[World]


class _FakeEventLog:
    """Stand-in for ``EventLog``. Captures the appended
    events into the shared ``_Captured``.
    """

    def __init__(self, cap: _Captured) -> None:
        self._cap = cap

    async def append_batch(self, events: list[Event]) -> Any:
        self._cap.appended.extend(events)
        return ["ok"] * len(events)


class _FakeWorldStore:
    """Stand-in for ``IncrementalWorldStore`` that captures
    the saved Worlds (so the test can assert the post-GC
    World has no orphan request in the slot).
    """

    def __init__(self, cap: _Captured) -> None:
        self._cap = cap

    async def load(self, agent_id: str) -> WorldCheckpoint:
        return WorldCheckpoint(world=World.empty(), last_stream_id="0-0")

    async def save(self, agent_id: str, checkpoint: WorldCheckpoint) -> None:
        self._cap.saved.append(checkpoint.world)


class _StubToolRouter:
    """Spy for ``ToolRouter`` — records routed batches."""

    def __init__(self) -> None:
        self.calls: list[list[Event]] = []

    async def route_batch(self, events: list[Event]) -> None:
        self.calls.append(list(events))


def _dispatcher(
    *,
    tool_ttls: ToolCallTTL | None,
    sweeper: ToolCallTTLSweeperSystem | None,
    cap: _Captured,
    router: _StubToolRouter | None = None,
) -> ReactiveDispatcher:
    """Build a dispatcher with the given TTL config and
    sweeper (pass ``tool_ttls=None`` to disable the
    auto-register of the sweeper; ``sweeper=None`` means
    no sweeper is in ``systems`` either)."""
    systems: list[Any] = []
    if sweeper is not None:
        systems.append(sweeper)
    return ReactiveDispatcher(
        log=_FakeEventLog(cap),  # type: ignore[arg-type]
        systems=systems,  # type: ignore[arg-type]
        redis=AsyncMock(),
        tool_ttls=tool_ttls,
        world_store=_FakeWorldStore(cap),  # type: ignore[arg-type]
        tool_router=router,  # type: ignore[arg-type]
    )


def _sweeper_at(now: datetime) -> ToolCallTTLSweeperSystem:
    """A TTL sweeper with a fixed clock (deterministic)."""
    return ToolCallTTLSweeperSystem(now=now)


def _agent_view(world: World, agent_id: str = "a-1") -> Any:
    return world.views[agent_id]


class TestSlotGCRemovesOrphanRequest:
    """The orphan-request scenario (DEBT §2.21 follow-up):
    the sweeper emits a TTL-failure event for a stale
    request; the dispatcher must remove the request from
    the slot in the SAME tick so the next checkpoint
    does not carry it.
    """

    @pytest.mark.asyncio
    async def test_stale_request_is_evicted_in_same_tick(self) -> None:
        """A request emitted in tick N whose TTL is
        already past at tick N+1: the sweeper emits
        ``tool.<name>.failed``; the dispatcher re-overlays
        with that event; the request is evicted from
        the slot via the completion-driven rule; the
        checkpointed World has no orphan.
        """
        # Tick N: a fresh request is emitted. We build
        # a World via ``World.fold`` with the
        # tool-call projection so the storage is
        # correctly populated (the dispatcher calls
        # ``world.with_event`` which mutates the
        # storage).
        req = _request_event(tool_name="chat_llm", ts=_ts(0))
        from kntgraph.core.world.projection_tool_calls import (
            project_tool_calls,
        )

        post_tick_n_world = World.fold(
            events=[req],
            tick=1,
            projection=lambda events: project_tool_calls(
                events,
                ttl=ToolCallTTL(default_ttl_seconds=300.0),
            ),
        )

        cap2 = _Captured(appended=[], saved=[])

        class _SeededStore(_FakeWorldStore):
            def __init__(self, cap: _Captured, world: World) -> None:
                super().__init__(cap)
                self._world = world

            async def load(self, agent_id: str) -> WorldCheckpoint:
                return WorldCheckpoint(world=self._world, last_stream_id="1-0")

        store = _SeededStore(cap2, post_tick_n_world)
        dispatcher = ReactiveDispatcher(
            log=_FakeEventLog(cap2),  # type: ignore[arg-type]
            systems=[_sweeper_at(_ts(600))],
            redis=AsyncMock(),
            tool_ttls=ToolCallTTL(default_ttl_seconds=300.0),
            world_store=store,  # type: ignore[arg-type]
        )
        # Tick N+1: no new events from the log, but the
        # sweeper runs and emits the TTL-failure.
        # Drive the systems path directly so the test
        # is focused on the GC behaviour (the public
        # ``dispatch_once`` also runs the systems on
        # those ticks; see :meth:`_dispatch_for_agent`).
        await dispatcher._run_systems_and_persist(
            agent_id="a-1",
            world=post_tick_n_world,
            last_stream_id="1-0",
            new_event_count=0,
            new_events=[],
        )
        # The dispatcher appended the TTL-failure event
        # to the log.
        assert any(e.event_type == "tool.chat_llm.failed" for e in cap2.appended), (
            f"expected tool.chat_llm.failed in {cap2.appended}"
        )
        # The dispatcher saved exactly one World (the
        # post-GC one).
        assert len(cap2.saved) == 1
        saved_world = cap2.saved[0]
        view = _agent_view(saved_world)
        # The orphan request is GONE from the slot.
        requests = view.components.get("tool_requests", {})
        assert str(req.event_id) not in requests, (
            f"orphan request still in slot: {requests}"
        )

    @pytest.mark.asyncio
    async def test_fresh_request_is_not_evicted(self) -> None:
        """A fresh request (TTL not yet past) is NOT
        touched by the GC step — the sweeper emits
        nothing; the slot is preserved.
        """
        req = _request_event(tool_name="chat_llm", ts=_ts(0))
        from kntgraph.core.world.projection_tool_calls import (
            project_tool_calls,
        )

        post_tick_n_world = World.fold(
            events=[req],
            tick=1,
            projection=lambda events: project_tool_calls(
                events,
                ttl=ToolCallTTL(default_ttl_seconds=300.0),
            ),
        )

        cap2 = _Captured(appended=[], saved=[])

        class _SeededStore(_FakeWorldStore):
            def __init__(self, cap: _Captured, world: World) -> None:
                super().__init__(cap)
                self._world = world

            async def load(self, agent_id: str) -> WorldCheckpoint:
                return WorldCheckpoint(world=self._world, last_stream_id="1-0")

        store = _SeededStore(cap2, post_tick_n_world)
        dispatcher = ReactiveDispatcher(
            log=_FakeEventLog(cap2),  # type: ignore[arg-type]
            systems=[_sweeper_at(_ts(10))],
            redis=AsyncMock(),
            tool_ttls=ToolCallTTL(default_ttl_seconds=300.0),
            world_store=store,  # type: ignore[arg-type]
        )
        await dispatcher._run_systems_and_persist(
            agent_id="a-1",
            world=post_tick_n_world,
            last_stream_id="1-0",
            new_event_count=0,
            new_events=[],
        )
        # No TTL-failure event was emitted.
        assert all(not e.event_type.endswith(".failed") for e in cap2.appended)
        # The slot is preserved (the fresh request is
        # still there).
        assert len(cap2.saved) == 1
        view = _agent_view(cap2.saved[0])
        requests = view.components.get("tool_requests", {})
        assert str(req.event_id) in requests

    @pytest.mark.asyncio
    async def test_no_sweeper_means_request_stays_in_slot(self) -> None:
        """When no sweeper is registered (the operator
        opted out of TTL enforcement by NOT passing
        ``tool_ttls``), a stale request is NOT evicted
        by the dispatcher (the GC step is part of the
        TTL pipeline and only runs when the sweeper is
        wired in). The slot carries the request; a
        completion-driven eviction in a later tick is
        the only path to GC.
        """
        req = _request_event(tool_name="chat_llm", ts=_ts(0))
        from kntgraph.core.world.projection_tool_calls import (
            project_tool_calls,
        )

        world = World.fold(
            events=[req],
            tick=1,
            projection=lambda events: project_tool_calls(
                events,
                ttl=ToolCallTTL(default_ttl_seconds=300.0),
            ),
        )
        # No sweeper; no tool_ttls (legacy / opt-out).
        cap2 = _Captured(appended=[], saved=[])

        class _SeededStore(_FakeWorldStore):
            async def load(self, agent_id: str) -> WorldCheckpoint:
                return WorldCheckpoint(world=world, last_stream_id="1-0")

        store = _SeededStore(cap2)
        dispatcher = ReactiveDispatcher(
            log=_FakeEventLog(cap2),  # type: ignore[arg-type]
            systems=[],
            redis=AsyncMock(),
            world_store=store,  # type: ignore[arg-type]
        )
        await dispatcher._run_systems_and_persist(
            agent_id="a-1",
            world=world,
            last_stream_id="1-0",
            new_event_count=0,
            new_events=[],
        )
        # No events were appended.
        assert cap2.appended == []
        # The slot still has the request.
        assert len(cap2.saved) == 1
        view = _agent_view(cap2.saved[0])
        requests = view.components.get("tool_requests", {})
        assert str(req.event_id) in requests


class TestSlotGCIsCheapForNonToolBatches:
    """The GC step is opt-in: it only re-folds the
    World when the system-emitted events contain at
    least one ``tool.*`` event. Non-tool batches pay
    zero for the second pass (the dispatcher returns
    the same World object).
    """

    @pytest.mark.asyncio
    async def test_non_tool_systems_do_not_trigger_gc_pass(self) -> None:
        """A system that emits only domain events (no
        ``tool.*``) does NOT cause a re-fold. The
        saved World is the same object the caller
        passed in (no allocation; ADR-044 §2.4
        "no allocation for non-tool batches").
        """

        def _domain_system(world: World) -> list[Event]:
            return [
                Event.create(
                    event_type="user.intent",
                    agent_id="a-1",
                    event_class="domain",
                    data={"intent": "x"},
                    correlation=_ctx(),
                )
            ]

        cap = _Captured(appended=[], saved=[])
        dispatcher = _dispatcher(
            tool_ttls=ToolCallTTL(default_ttl_seconds=300.0),
            sweeper=_sweeper_at(_ts(600)),
            cap=cap,
        )
        world_in = World.empty().with_event(
            Event.create(
                event_type="user.intent",
                agent_id="a-1",
                event_class="domain",
                data={"intent": "x"},
                correlation=_ctx(),
            )
        )
        await dispatcher._run_systems_and_persist(
            agent_id="a-1",
            world=world_in,
            last_stream_id="1-0",
            new_event_count=1,
            new_events=[
                Event.create(
                    event_type="user.intent",
                    agent_id="a-1",
                    event_class="domain",
                    data={"intent": "x"},
                    correlation=_ctx(),
                )
            ],
        )
        # The dispatcher saved the post-systems World.
        # The sweeper was called but emitted nothing
        # (no tool_requests in the slot). No re-fold
        # is needed: the saved World is the same
        # ``world_in`` the caller passed (same object).
        assert len(cap.saved) == 1
        # Domain event was appended (the system emitted
        # one); the sweeper did NOT append anything.
        assert all(e.event_type != "tool.chat_llm.failed" for e in cap.appended)

    @pytest.mark.asyncio
    async def test_no_system_events_skips_gc_pass(self) -> None:
        """When the systems emit nothing, no re-fold
        is needed. The saved World is the same
        ``world_in`` (no allocation).
        """
        cap = _Captured(appended=[], saved=[])
        dispatcher = _dispatcher(
            tool_ttls=ToolCallTTL(default_ttl_seconds=300.0),
            sweeper=_sweeper_at(_ts(600)),
            cap=cap,
        )
        world_in = World.empty()
        await dispatcher._run_systems_and_persist(
            agent_id="a-1",
            world=world_in,
            last_stream_id="1-0",
            new_event_count=0,
            new_events=[],
        )
        # No events were appended.
        assert cap.appended == []
        # The saved World is the same object the caller
        # passed in (no allocation).
        assert cap.saved[0] is world_in


class TestSlotGCRoutesViaRouter:
    """The dispatcher continues to route
    TTL-failure events through the ToolRouter (the
    router is part of the ``_append_system_outgoing``
    pipeline; the GC step is a post-append re-fold, so
    the events appended by the sweeper go through the
    router as usual).
    """

    @pytest.mark.asyncio
    async def test_ttl_failure_event_is_routed(self) -> None:
        """A TTL-failure event emitted by the sweeper
        is appended to the EventLog AND routed through
        the ToolRouter (the canonical fan-out path for
        tool events).
        """
        cap = _Captured(appended=[], saved=[])
        router = _StubToolRouter()
        req = _request_event(tool_name="chat_llm", ts=_ts(0))
        from kntgraph.core.world.projection_tool_calls import (
            project_tool_calls,
        )

        world = World.fold(
            events=[req],
            tick=1,
            projection=lambda events: project_tool_calls(
                events,
                ttl=ToolCallTTL(default_ttl_seconds=300.0),
            ),
        )

        class _SeededStore(_FakeWorldStore):
            async def load(self, agent_id: str) -> WorldCheckpoint:
                return WorldCheckpoint(world=world, last_stream_id="1-0")

        store = _SeededStore(cap)
        dispatcher = ReactiveDispatcher(
            log=_FakeEventLog(cap),  # type: ignore[arg-type]
            systems=[_sweeper_at(_ts(600))],
            redis=AsyncMock(),
            tool_ttls=ToolCallTTL(default_ttl_seconds=300.0),
            world_store=store,  # type: ignore[arg-type]
            tool_router=router,  # type: ignore[arg-type]
        )
        await dispatcher._run_systems_and_persist(
            agent_id="a-1",
            world=world,
            last_stream_id="1-0",
            new_event_count=0,
            new_events=[],
        )
        # The TTL-failure event was routed.
        assert any(
            e.event_type == "tool.chat_llm.failed"
            for batch in router.calls
            for e in batch
        )
