# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Observability contract for ``ReactiveDispatcher._loop``.

The dispatcher's "parou sem aviso" failure mode is
indistinguishable from a healthy idle loop without the
heartbeat line. These tests pin the observability contract:

  - heartbeat is emitted on the configured cadence
  - heartbeat carries the running event counter and the
    time since the last successful tick
  - the heartbeat reports ``last_error`` while the loop
    is in a "consistently failing" state and clears it on
    the next successful tick
  - the ``reactive.loop.error`` log call carries
    ``exc_info=True`` so the operator sees a traceback,
    not just ``str(e)``

The fixtures are minimal stand-ins for ``EventLog`` and
``IncrementalWorldStore`` so the test stays in-process
(no Redis, no FalkorDB).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
import structlog

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.infra.world_checkpoint import WorldCheckpoint
from kntgraph.runner.reactive import ReactiveDispatcher


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _Captured:
    appended: list[Event] = field(default_factory=list)


class _FakeEventLog:
    def __init__(self, cap: _Captured) -> None:
        self._cap = cap
        self._agents: dict[str, list[Event]] = {}

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
    def __init__(self) -> None:
        self.saves: list[tuple[str, WorldCheckpoint]] = []

    async def load(self, agent_id: str) -> WorldCheckpoint:
        return WorldCheckpoint(world=World.empty(), last_stream_id="-")

    async def save(self, agent_id: str, checkpoint: WorldCheckpoint) -> None:
        self.saves.append((agent_id, checkpoint))


class _BoomSystem:
    """A system that always raises so the loop's
    ``except Exception`` arm fires. The heartbeat must then
    surface ``last_error`` until the next successful tick.
    """

    def __call__(self, world: World) -> list[Event]:
        raise RuntimeError("boom")


def _seed_event(agent_id: str) -> Event:
    return Event.domain_from(
        agent_id=agent_id,
        type="fixture.event",
        data={"k": "v"},
        correlation=CorrelationContext.new(correlation_id=uuid4()),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _route_structlog_through_stdlib(caplog):
    """Route structlog through the stdlib ``logging`` tree so
    pytest's ``caplog`` can capture the heartbeat / error
    lines the dispatcher emits.
    """
    caplog.set_level(logging.INFO, logger="kntgraph.runner.reactive")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
    )
    yield
    structlog.reset_defaults()


def _build_dispatcher(
    *, log: _FakeEventLog, store: _FakeWorldStore, systems: list
) -> ReactiveDispatcher:
    """Construct a dispatcher with the observability knobs
    tightened so the heartbeat is observable inside a test
    body without slowing the suite.
    """
    return ReactiveDispatcher(
        log=log,
        systems=systems,
        world_store=store,
        poll_interval=0.02,
        rediscovery_interval_seconds=0.05,
        heartbeat_interval_seconds=0.05,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_heartbeat_emitted_with_counters():
    cap = _Captured()
    log = _FakeEventLog(cap)
    log.add_agent("agent-1", _seed_event("agent-1"))
    store = _FakeWorldStore()
    dispatcher = _build_dispatcher(log=log, store=store, systems=[])

    with _Capture() as records:
        await dispatcher.start()
        try:
            # Drive enough ticks for the heartbeat to fire at
            # least once and for the counter to advance.
            deadline = time.monotonic() + 1.0
            while (
                not any(r.event == "reactive.loop.heartbeat" for r in records)
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.02)
        finally:
            await dispatcher.stop()
    heartbeat = [r for r in records if r.event == "reactive.loop.heartbeat"]
    assert heartbeat, "heartbeat line never fired"
    first = heartbeat[0]
    # ``events_processed_total`` is a monotonic counter the
    # heartbeat exposes; the operator correlates a stalled
    # pipeline with ``events_processed_total`` not advancing.
    assert "events_processed_total" in first.kw
    assert "idle_seconds" in first.kw
    assert "tracked_agents" in first.kw
    assert "last_error" in first.kw


async def test_heartbeat_disabled_when_interval_non_positive():
    cap = _Captured()
    log = _FakeEventLog(cap)
    log.add_agent("agent-1", _seed_event("agent-1"))
    store = _FakeWorldStore()
    dispatcher = _build_dispatcher(log=log, store=store, systems=[])
    dispatcher._heartbeat_interval_seconds = 0

    with _Capture() as records:
        await dispatcher.start()
        try:
            for _ in range(10):
                await asyncio.sleep(0.02)
        finally:
            await dispatcher.stop()
    assert not any(r.event == "reactive.loop.heartbeat" for r in records)


async def test_heartbeat_surfaces_last_error():
    cap = _Captured()
    log = _FakeEventLog(cap)
    log.add_agent("agent-1", _seed_event("agent-1"))
    store = _FakeWorldStore()
    dispatcher = _build_dispatcher(log=log, store=store, systems=[_BoomSystem()])

    with _Capture() as records:
        await dispatcher.start()
        try:
            deadline = time.monotonic() + 1.0
            while (
                not any(
                    r.event == "reactive.loop.heartbeat" and r.kw.get("last_error")
                    for r in records
                )
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.02)
        finally:
            await dispatcher.stop()
    error_heartbeats = [
        r
        for r in records
        if r.event == "reactive.loop.heartbeat" and r.kw.get("last_error")
    ]
    assert error_heartbeats
    # ``repr(exception)`` keeps the class name so the
    # operator can tell ``RuntimeError("boom")`` from
    # ``ConnectionError("...")`` in the log.
    assert "RuntimeError" in error_heartbeats[0].kw["last_error"]


async def test_reactive_loop_error_carries_exc_info():
    cap = _Captured()
    log = _FakeEventLog(cap)
    log.add_agent("agent-1", _seed_event("agent-1"))
    store = _FakeWorldStore()
    dispatcher = _build_dispatcher(log=log, store=store, systems=[_BoomSystem()])

    with _Capture() as records:
        await dispatcher.start()
        try:
            deadline = time.monotonic() + 1.0
            while (
                not any(r.event == "reactive.loop.error" for r in records)
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.02)
        finally:
            await dispatcher.stop()
    errors = [r for r in records if r.event == "reactive.loop.error"]
    assert errors
    # ``exc_info=True`` is the contract: without it the
    # operator sees only ``str(e)`` and cannot distinguish
    # a transient blip from a deterministic crash on the
    # same code path.
    assert errors[0].kw.get("exc_info") is True


async def test_dispatch_once_increments_events_processed():
    cap = _Captured()
    log = _FakeEventLog(cap)
    log.add_agent("agent-1", _seed_event("agent-1"))
    store = _FakeWorldStore()
    dispatcher = _build_dispatcher(log=log, store=store, systems=[])
    await dispatcher.start()
    try:
        # Drive the loop manually so the assertion is
        # deterministic (the loop has at most one tick
        # per ``poll_interval``).
        deadline = time.monotonic() + 1.0
        while dispatcher._events_processed_total == 0 and time.monotonic() < deadline:
            await dispatcher.dispatch_once()
            await asyncio.sleep(0.02)
    finally:
        await dispatcher.stop()
    assert dispatcher._events_processed_total >= 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _Record:
    event: str
    kw: dict


class _Capture:
    """Context manager that records every structlog log
    call into a list of ``_Record`` so the test can assert
    on the event name and keyword arguments without
    scraping log text.
    """

    def __enter__(self) -> list[_Record]:
        self._records: list[_Record] = []

        def _processor(logger, method_name, event_dict):
            self._records.append(
                _Record(event=event_dict.get("event", "?"), kw=event_dict)
            )
            return event_dict

        structlog.configure(
            processors=[
                _processor,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
        )
        return self._records

    def __exit__(self, *_exc_info) -> None:
        structlog.reset_defaults()
