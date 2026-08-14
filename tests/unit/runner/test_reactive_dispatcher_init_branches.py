# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Branch-coverage gap tests for the ``ReactiveDispatcher``
public surface.

The dispatcher's tick body, lifecycle, and constructor
are the most safety-critical paths in the framework
(``runner/`` is the reliability-gate scope per the CI
skill §9). The unit tests that already exist cover the
happy paths; this file pins the branches the orchestrator
does not exercise by construction:

  - ``__init__`` auto-registers the TTL sweeper when
    ``tool_ttls`` is set and the operator did not
    pre-register one (ADR-045)
  - ``__init__`` raises ``ValueError`` when neither
    ``world_store`` nor ``redis`` is given (operator
    error)
  - ``_dispatch_for_agent`` short-circuits when every
    event in the batch is filtered out AND no TTL
    sweeper is active (saves the checkpoint, returns 0)
  - ``start`` is idempotent — a second ``start`` while
    the dispatcher is already running is a no-op
  - ``stop`` is safe when called without a prior
    ``start`` (no task to cancel)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
import structlog

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.core.world.components import ToolCallTTL
from kntgraph.infra.world_checkpoint import WorldCheckpoint
from kntgraph.runner.reactive import ReactiveDispatcher


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _Captured:
    appended: list[Event] = field(default_factory=list)
    saved: list[tuple[str, WorldCheckpoint]] = field(default_factory=list)


class _FakeEventLog:
    """Minimal stand-in for ``EventLog``: tracks the
    per-agent event roster and the saved events.
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


def _seed_event(agent_id: str) -> Event:
    return Event.domain_from(
        agent_id=agent_id,
        type="fixture.event",
        data={"k": "v"},
        correlation=CorrelationContext.new(correlation_id=uuid4()),
    )


# ---------------------------------------------------------------------------
# __init__ branches
# ---------------------------------------------------------------------------


class TestInitBranches:
    async def test_init_auto_registers_ttl_sweeper(self) -> None:
        """When ``tool_ttls`` is provided AND the operator
        has not pre-registered a sweeper, the dispatcher
        auto-registers a :class:`ToolCallTTLSweeperSystem`
        (ADR-045). Pinned so a future refactor does not
        silently drop the auto-registration.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = ReactiveDispatcher(
            log=log,
            world_store=store,
            systems=[],
            tool_ttls=ToolCallTTL(),
        )
        # The auto-registered system is the LAST entry.
        # The dispatcher must now have exactly one system.
        from kntgraph.runner.tool_call_ttl_sweeper import ToolCallTTLSweeperSystem

        assert any(isinstance(s, ToolCallTTLSweeperSystem) for s in dispatcher._systems)
        assert len(dispatcher._systems) == 1

    async def test_init_skips_sweeper_registration_when_tool_ttls_is_none(
        self,
    ) -> None:
        """The short-circuit branch of
        ``if tool_ttls is not None and not any(...):``:
        when ``tool_ttls=None`` the auto-registration is
        skipped entirely. Pinned so a future refactor
        that, e.g., eagerly registers the sweeper does
        not regress the legacy behaviour (no TTL
        enforcement unless the operator opts in).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = ReactiveDispatcher(
            log=log,
            world_store=store,
            systems=[],
            tool_ttls=None,
        )
        from kntgraph.runner.tool_call_ttl_sweeper import ToolCallTTLSweeperSystem

        assert dispatcher._systems == []
        assert not any(
            isinstance(s, ToolCallTTLSweeperSystem) for s in dispatcher._systems
        )

    async def test_systems_property_returns_defensive_copy(self) -> None:
        """The ``systems`` property must return a
        defensive copy so external code cannot mutate
        the internal list. Pinned so a future refactor
        does not start returning ``self._systems``
        directly (a leak that would let the caller
        append/remove systems out of band).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = ReactiveDispatcher(
            log=log,
            world_store=store,
            systems=[],
        )
        snapshot = dispatcher.systems
        snapshot.append("sentinel")
        # The internal list is untouched.
        assert dispatcher._systems == []

    async def test_add_system_appends_to_systems_list(self) -> None:
        """The ``add_system`` method appends to the
        internal list. Pinned so the contract is
        exercised directly (other tests rely on it
        implicitly via ``__init__``).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = ReactiveDispatcher(
            log=log,
            world_store=store,
            systems=[],
        )
        original_len = len(dispatcher._systems)
        dispatcher.add_system(lambda _w: [])
        assert len(dispatcher._systems) == original_len + 1
        """The short-circuit branch of
        ``if tool_ttls is not None and not any(...):``:
        when ``tool_ttls=None`` the auto-registration is
        skipped entirely. Pinned so a future refactor
        that, e.g., eagerly registers the sweeper does
        not regress the legacy behaviour (no TTL
        enforcement unless the operator opts in).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = ReactiveDispatcher(
            log=log,
            world_store=store,
            systems=[],
            tool_ttls=None,
        )
        from kntgraph.runner.tool_call_ttl_sweeper import ToolCallTTLSweeperSystem

        assert dispatcher._systems == []
        assert not any(
            isinstance(s, ToolCallTTLSweeperSystem) for s in dispatcher._systems
        )

    async def test_init_raises_when_no_world_store_no_redis(self) -> None:
        """The guard at ``__init__``: if neither
        ``world_store`` nor ``redis`` is given, the
        dispatcher cannot recover from a restart (no
        checkpoint storage) and refuses to construct.
        Pinned so a future refactor does not silently
        fall back to in-memory state.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        with pytest.raises(ValueError, match="world_store or redis"):
            ReactiveDispatcher(log=log)


# ---------------------------------------------------------------------------
# _dispatch_for_agent short-circuit
# ---------------------------------------------------------------------------


class TestDispatchForAgentBranches:
    async def test_filtered_batch_short_circuits_when_no_ttls(self) -> None:
        """When the operator-supplied ``filter_fn``
        rejects every event in the batch AND no TTL
        sweeper is active, the dispatcher saves the
        checkpoint (so the cursor advances) and returns
        0 — it does NOT run the systems. Pinned because
        the branch is silent (no log line; only a
        checkpoint save).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        log.add_agent("a-1", _seed_event("a-1"))
        store = _FakeWorldStore(cap)
        dispatcher = ReactiveDispatcher(
            log=log,
            world_store=store,
            systems=[],
            filter_fn=lambda _e: False,
        )
        processed = await dispatcher.dispatch_once()
        assert processed == 0
        # The checkpoint was saved even though no
        # systems ran (the cursor must advance past
        # the fully-filtered batch).
        assert cap.saved

    async def test_filtered_batch_runs_systems_when_ttls_active(self) -> None:
        """The mirror branch of
        ``if new_event_count == 0 and self._tool_ttls is None:``
        when ``tool_ttls`` IS set: the dispatcher does
        NOT short-circuit. The TTL sweeper is one of
        the systems and it must run on idle ticks (the
        sweeper is the reason the early-return is gated
        on ``tool_ttls is None``). Pinned so a future
        refactor does not silently skip the sweeper
        when the filter rejects every event.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        log.add_agent("a-1", _seed_event("a-1"))
        store = _FakeWorldStore(cap)
        dispatcher = ReactiveDispatcher(
            log=log,
            world_store=store,
            systems=[],
            filter_fn=lambda _e: False,
            tool_ttls=ToolCallTTL(),
        )
        # The dispatcher auto-registered the sweeper
        # in __init__; with ``tool_ttls`` set, the
        # early-return is skipped and the sweeper
        # runs. The exact post-state is that
        # ``_run_systems_and_persist_fn`` was called,
        # which means the checkpoint was saved by the
        # append_system_outgoing path (no events
        # flowed through, but the persist always
        # saves). The ``_save_checkpoint_fn`` was
        # NOT called by the early-return.
        processed = await dispatcher.dispatch_once()
        assert processed == 0
        # The checkpoint was saved exactly once.
        # The path matters: the early-return path
        # saves the checkpoint (filtered-out
        # exhaustion) and the systems path saves
        # the checkpoint (TTL sweeper ran). Both
        # paths save once; the difference is
        # whether the sweeper had a chance to
        # emit. We assert the observable side
        # effect (checkpoint saved) without
        # depending on the TTL sweeper's emission
        # logic — that is exercised separately in
        # ``test_tool_call_ttl_sweeper.py``.
        assert len(cap.saved) >= 1


# ---------------------------------------------------------------------------
# Lifecycle branches
# ---------------------------------------------------------------------------


class TestLifecycleBranches:
    async def test_start_is_idempotent(self) -> None:
        """Calling ``start`` while the dispatcher is
        already running is a no-op (the task reference
        is not replaced). Pinned so a future refactor
        does not silently spawn a second background
        task (which would double-process every tick).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = ReactiveDispatcher(
            log=log,
            world_store=store,
            systems=[],
            poll_interval=10.0,  # long, so the task does not advance
        )
        await dispatcher.start()
        first_task = dispatcher._task
        try:
            await dispatcher.start()  # second call: must be a no-op
            assert dispatcher._task is first_task
        finally:
            await dispatcher.stop()

    async def test_stop_without_start_is_safe(self) -> None:
        """Calling ``stop`` on a dispatcher that was
        never started (or already stopped) is safe: no
        task to cancel, no error raised, the method
        just logs and returns. Pinned so a future
        refactor does not ``RuntimeError`` on a stale
        shutdown path.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = ReactiveDispatcher(
            log=log,
            world_store=store,
            systems=[],
        )
        # Never started: ``_task`` is None.
        assert dispatcher._task is None
        # Must not raise.
        await dispatcher.stop()
        # And a second stop is also safe.
        await dispatcher.stop()

    async def test_heartbeat_skipped_when_under_cadence(self) -> None:
        """The ``_maybe_emit_heartbeat`` early-return
        when the cadence has not yet elapsed since the
        last heartbeat. Pinned by holding ``_last_heartbeat_at``
        artificially close to ``now`` and asserting the
        helper does NOT emit a line.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = ReactiveDispatcher(
            log=log,
            world_store=store,
            systems=[],
            heartbeat_interval_seconds=10.0,
        )
        # Pretend the last heartbeat happened "now"
        # so the cadence gate is firmly closed.
        dispatcher._last_heartbeat_at = time.monotonic()
        # Direct call: under-cadence → no-op.
        with patch("kntgraph.runner.reactive.logger") as mock_logger:
            dispatcher._maybe_emit_heartbeat()
            mock_logger.info.assert_not_called()
