# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Regression test for ``ReactiveDispatcher`` agent discovery.

The bug:

  - Production call sites (HTTP routers, cron schedulers, the
    backend NestJS outbox bridge) typically seed ``EventLog``
    with events for tenants that did not exist when the
    dispatcher booted. With the current ``start()``/``_loop()``
    contract, ``_bootstrap_agents`` runs exactly once and
    ``_tracked_agents`` becomes immutable for the lifetime
    of the dispatcher. New agents created after ``start()``
    are silently ignored: their events stay in the log, no
    fold happens, no system ever sees them.

The correct semantics are:

  - The dispatcher must periodically rediscover
    ``EventLog.list_agents()`` (cheap ``XINFO`` lookup or a
    pattern scan with cursor pagination on Redis Streams) and
    start tracking any newcomers.

  - The rediscovery cadence must be bounded so a deploy with
    10k agents does not pay a full pattern scan every tick.
    A configurable ``rediscovery_interval_seconds`` (default
    ``5.0``) is the right knob: short enough for E2E feedback
    loops, long enough that hot paths do not paginate.

The green strategy:

  - Add ``self._next_rediscovery_at`` initialised to
    ``time.monotonic()`` at ``__init__``.
  - At the top of ``dispatch_once``, if
    ``time.monotonic() >= self._next_rediscovery_at``, run
    ``_bootstrap_agents()`` (which is now idempotent because
    ``_tracked_agents`` is a set) and bump the next deadline.

This file ships the failing test (TDD red). The patch that
fixes the bug lives in the same branch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.infra.world_checkpoint import WorldCheckpoint
from kntgraph.runner.reactive import ReactiveDispatcher


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes (smallest possible stand-ins for ``EventLog`` and the
# world store; we do not need any LLM or Redis process for this
# regression).
# ---------------------------------------------------------------------------


@dataclass
class _Captured:
    appended: list[Event] = field(default_factory=list)


class _FakeEventLog:
    """Mutable agent roster + ``read_after_cursor`` stub.

    The roster starts empty (the dispatcher boots before any
    tenant has emitted). The test then ``add_agent``s a
    newcomer; ``read_after_cursor`` returns the seed event
    on the first call and zero events on subsequent ones.
    """

    def __init__(self, cap: _Captured) -> None:
        self._cap = cap
        self._agents: dict[str, list[Event]] = {}

    def add_agent(self, agent_id: str, *events: Event) -> None:
        self._agents.setdefault(agent_id, []).extend(events)

    async def read_after_cursor(
        self, agent_id: str, cursor: str
    ) -> tuple[list[Event], str]:
        pending = self._agents.get(agent_id, [])
        if cursor in ("-", "-1"):
            # First call for this agent — return the seed
            # event(s) if any. After that, never re-emit.
            events = list(pending)
        else:
            events = []
        if events:
            new_cursor = "1-0"
            # Mark them consumed.
            self._agents[agent_id] = []
        else:
            new_cursor = cursor
        return events, new_cursor

    async def list_agents(self) -> list[str]:
        return sorted(self._agents.keys())

    async def append_batch(self, events: list[Event]) -> Any:
        self._cap.appended.extend(events)
        return ["ok"] * len(events)


class _FakeWorldStore:
    def __init__(self) -> None:
        self.saves: list[tuple[str, WorldCheckpoint]] = []

    async def load(self, agent_id: str) -> WorldCheckpoint:
        # The framework's default ``IncrementalWorldStore`` materialises
        # an empty ``World`` on a fresh agent; mirror that contract
        # so ``with_event`` works below.
        return WorldCheckpoint(world=World.empty(), last_stream_id="-")

    async def save(self, agent_id: str, checkpoint: WorldCheckpoint) -> None:
        self.saves.append((agent_id, checkpoint))


class _RecordingSystem:
    """Tracks which agents it has seen across the dispatcher's
    ``dispatch_once`` calls. ``see`` returns True if the
    dispatcher called us with this ``agent_id`` since the
    last reset.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, world) -> list[Event]:
        # The framework invokes the system once per agent
        # per tick. We only have one agent here, so we
        # record the agent_id from the world's
        # ``view_for_agent_id`` lookup.
        for view in getattr(world, "views", {}).values():
            self.calls.append(getattr(view, "agent_id", "?"))
        return []


def _seed_event(agent_id: str, event_type: str = "fixture.event") -> Event:
    return Event.domain_from(
        agent_id=agent_id,
        type=event_type,
        data={"k": "v"},
        correlation=CorrelationContext.new(correlation_id=uuid4()),
    )


# ---------------------------------------------------------------------------
# The failing test (TDD red).
# ---------------------------------------------------------------------------


async def test_reactive_dispatcher_picks_up_agents_added_after_start():
    """A tenant that emits its first event AFTER ``start()``
    must be folded by the next ``dispatch_once`` once the
    rediscovery deadline elapses.

    Without the fix, this test hangs forever (the dispatcher
    never folds the newcomer). With the fix it eventually
    delegates the new agent to the registered system.
    """
    cap = _Captured()
    log = _FakeEventLog(cap)
    store = _FakeWorldStore()

    # Bootstrapping tick: no agents yet.
    dispatcher = ReactiveDispatcher(
        log=log,
        systems=[],
        world_store=store,
        rediscovery_interval_seconds=0.05,
    )

    # The dispatcher is not an async context manager itself;
    # it is a long-running loop (``start()`` spawns a task;
    # ``stop()`` cancels it). We drive it via explicit ``start``
    # + ``dispatch_once`` so the test stays deterministic and
    # does not depend on the asyncio loop's clock.
    await dispatcher.start()
    try:
        # Give the loop a chance to start; nothing to do yet.
        await asyncio.sleep(0.05)

        # Late-joining tenant. ``log.add_agent`` is the only way
        # an agent becomes visible to the EventLog roster from
        # outside; we mirror that by mutating the fake.
        late_tenant = "tenant-late"
        log.add_agent(late_tenant, _seed_event(late_tenant))

        # Wait long enough for the rediscovery sweep + one
        # dispatch cycle to run. We drive ``dispatch_once``
        # ourselves so the assertions are deterministic.
        deadline = asyncio.get_event_loop().time() + 1.5
        while asyncio.get_event_loop().time() < deadline:
            await dispatcher.dispatch_once()
            await asyncio.sleep(0.05)
            if store.saves and store.saves[-1][0] == late_tenant:
                break
        # The dispatcher must have called ``save`` at least
        # once for the late tenant. This is the bug-catcher:
        # on the broken implementation ``store.saves`` stays
        # empty because the newcomer is never tracked.
        assert store.saves, (
            "ReactiveDispatcher never tracked the late-joining "
            "tenant. ``_bootstrap_agents`` is only invoked on "
            "the very first tick; add periodic rediscovery."
        )
        saved_agents = [agent_id for agent_id, _ in store.saves]
        assert late_tenant in saved_agents, (
            f"new agent {late_tenant!r} never folded; saved={saved_agents}"
        )
    finally:
        await dispatcher.stop()
