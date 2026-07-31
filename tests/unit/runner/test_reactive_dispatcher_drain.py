# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Regression test for the ``ReactiveDispatcher`` drain
contract (ADR-049 / ``SolutionLookupSystem``).

The lookup system's contract is:

  1. ``__call__(world)`` discovers new ``ToolCallRequest``s
     on the view and queues them.
  2. ``run_pending_lookups()`` drains the queue and
     builds synthetic ``tool.<name>.completed`` events
     into ``_pending_results``.
  3. The NEXT ``__call__(world)`` returns
     ``_pending_results`` to the dispatcher, which
     appends them to the EventLog.

This means a ``dispatch_once`` MUST call every
registered system on every tick when the system has
``_pending_results`` to surface -- not only when there
are new events in the log. Without this guarantee the
synthetic completion never lands in the EventLog and
downstream consumers (the session recorder, the
projection) never see the ZTA-served answer.

The bug the test exercises:

  - Tick 0: ``dispatch_once`` consumes the request
    event from the log; ``__call__`` returns ``[]``
    (no pending results yet); ``run_pending_lookups``
    populates ``_pending_results=[completion]``.
  - Tick 1: no new events in the log. The dispatcher
    short-circuits to ``return 0`` and does NOT call
    the systems; the ``_pending_results`` list is
    never consumed; the completion is lost.

The fix is in :meth:`ReactiveDispatcher._dispatch_for_agent`:
when the log has no new events but the systems have
unconsumed work (``_pending_results``), the dispatcher
must still run the systems. (The TTL sweeper already
forces a system run on every tick; the lookup
system's contract is the same shape.)

These tests fail on the current implementation (TDD
red); the green is the follow-up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from kntgraph.agents.memory.solution_lookup import (
    CachedSolution,
    InMemorySolutionStore,
    SolutionLookupSystem,
)
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
    appended: list[Event]
    saved: list[World]


class _FakeEventLog:
    """Minimal stand-in for ``EventLog`` -- captures the
    batches appended by the dispatcher so the test can
    assert that the synthetic completion actually
    landed in the log.
    """

    def __init__(
        self,
        cap: _Captured,
        *,
        pending: list[Event] | None = None,
    ) -> None:
        self._cap = cap
        # ``pending`` is the pre-seeded log content
        # (the events the dispatcher will read on
        # ``read_after_cursor``).
        self._pending: list[Event] = list(pending or [])
        self._appended_raw: list[Event] = []  # for read()
        self._cursor_after_pending = "0-0"

    async def read_after_cursor(
        self, agent_id: str, cursor: str
    ) -> tuple[list[Event], str]:
        # First call: return the pending events (once).
        if self._pending:
            events = list(self._pending)
            self._pending.clear()
            # ``events[-1].stream_id`` is the new cursor.
            cursor = str(events[-1].event_id) if events else cursor
            return events, cursor
        return [], cursor

    async def read(self, agent_id: str) -> list[Event]:
        # Return both the pre-seeded + appended events
        # so the test can inspect the final log state.
        return self._appended_raw

    async def list_agents(self) -> list[str]:
        return ["a-1"]

    async def append_batch(self, events: list[Event]) -> Any:
        self._cap.appended.extend(events)
        self._appended_raw.extend(events)
        return ["ok"] * len(events)


class _FakeWorldStore:
    """Stand-in for ``IncrementalWorldStore``."""

    def __init__(self, cap: _Captured) -> None:
        self._cap = cap

    async def load(self, agent_id: str) -> WorldCheckpoint:
        return WorldCheckpoint(world=World.empty(), last_stream_id="-")

    async def save(self, agent_id: str, checkpoint: WorldCheckpoint) -> None:
        self._cap.saved.append(checkpoint.world)


# ---------------------------------------------------------------------------
# Test fixture
# ---------------------------------------------------------------------------


def _request_event(*, agent_id: str = "a-1") -> Event:
    return Event.create(
        event_type="tool.knowledge_lookup.requested",
        agent_id=agent_id,
        event_class="domain",
        data={
            "tool": "knowledge_lookup",
            "params": {"question_id": "export-data-v1"},
        },
        correlation=CorrelationContext.new(correlation_id=uuid4()),
    )


def _store_with_hit() -> InMemorySolutionStore:
    """A pre-populated store with a Solution whose
    fingerprint matches the request the test will emit.
    """
    store = InMemorySolutionStore()
    # The ``overlay_tool_calls`` projection uses
    # ``dict(event.data)`` as the params dict, so the
    # fingerprint is ``short_hash(json.dumps({"tool":
    # "knowledge_lookup", "params": {"question_id":
    # "export-data-v1"}}, sort_keys=True, default=str))``.
    from kntgraph.infra.hashing import short_hash

    payload = json.dumps(
        {
            "tool": "knowledge_lookup",
            "params": {"question_id": "export-data-v1"},
        },
        sort_keys=True,
        default=str,
    )
    fp = short_hash(payload)
    store.add(
        CachedSolution(
            tool_name="knowledge_lookup",
            params_fingerprint=fp,
            confidence=5,
            result={"answer": "Click Settings → Export."},
            source_completion_event_id="00000000-0000-0000-0000-000000000001",
        )
    )
    return store


def _dispatcher(
    *,
    cap: _Captured,
    lookup_system: SolutionLookupSystem,
    pending_log: list[Event],
) -> tuple[ReactiveDispatcher, _FakeEventLog]:
    log = _FakeEventLog(cap, pending=pending_log)
    disp = ReactiveDispatcher(
        log=log,  # type: ignore[arg-type]
        systems=[lookup_system],  # type: ignore[list-item]
        redis=MagicMock(),
        world_store=_FakeWorldStore(cap),  # type: ignore[arg-type]
    )
    disp.track_agent("a-1")
    return disp, log


# ---------------------------------------------------------------------------
# The regression test (TDD red)
# ---------------------------------------------------------------------------


class TestDispatcherDrainsPendingLookups:
    """The dispatcher must run every system on every tick
    when the system has unconsumed ``_pending_results``,
    not only when there are new events in the log.
    """

    async def test_completion_lands_in_log_without_further_input(
        self,
    ) -> None:
        """Tick 0: request is emitted + consumed;
        ``run_pending_lookups`` populates
        ``_pending_results``. Tick 1: no new events,
        but the completion MUST still be appended to
        the EventLog (the lookup system's contract is
        "the next ``__call__`` returns
        ``_pending_results``").
        """
        cap = _Captured(appended=[], saved=[])
        store = _store_with_hit()
        lookup_system = SolutionLookupSystem(
            solution_store=store,
            allowlist=frozenset({"knowledge_lookup"}),
            min_confidence=3,
        )
        request = _request_event()
        disp, log = _dispatcher(
            cap=cap,
            lookup_system=lookup_system,
            pending_log=[request],
        )

        # Tick 0: dispatcher consumes the request from
        # the log; the lookup system queues the lookup.
        n0 = await disp.dispatch_once()
        assert n0 == 1, "the request should be consumed on tick 0"
        await lookup_system.run_pending_lookups()
        # After the drain the system has the
        # synthetic completion queued.
        assert len(lookup_system._pending_results) == 1, (
            "run_pending_lookups must have populated "
            "_pending_results with the cache hit"
        )

        # Tick 1: no new events in the log. The
        # dispatcher MUST still call the system so the
        # pending completion is consumed and appended.
        await disp.dispatch_once()
        await lookup_system.run_pending_lookups()

        # The synthetic completion must be in the log.
        completions = [
            e for e in cap.appended if e.event_type == "tool.knowledge_lookup.completed"
        ]
        assert len(completions) == 1, (
            "the lookup system's synthetic completion "
            "must be appended to the EventLog even "
            "when the next tick has no new events "
            f"(appended: {[e.event_type for e in cap.appended]})"
        )
        completion = completions[0]
        assert completion.data["source"] == "solution_lookup"
        assert completion.data["request_event_id"] == str(request.event_id)
        assert completion.data["result"] == {"answer": "Click Settings → Export."}
        # Stats reflect the cache hit.
        stats = lookup_system.stats
        assert stats.cache_hit == 1
        assert stats.cache_miss == 0

    async def test_pending_completion_drained_after_two_idle_ticks(
        self,
    ) -> None:
        """Same shape as above but the dispatcher runs
        two idle ticks after the drain. The
        completion must still land by the time the
        second idle tick runs (operators should not
        have to manually poke the dispatcher).
        """
        cap = _Captured(appended=[], saved=[])
        store = _store_with_hit()
        lookup_system = SolutionLookupSystem(
            solution_store=store,
            allowlist=frozenset({"knowledge_lookup"}),
            min_confidence=3,
        )
        request = _request_event()
        disp, _ = _dispatcher(
            cap=cap,
            lookup_system=lookup_system,
            pending_log=[request],
        )

        await disp.dispatch_once()
        await lookup_system.run_pending_lookups()

        # Two idle ticks. The completion must be
        # appended at the latest by the SECOND tick.
        await disp.dispatch_once()
        await disp.dispatch_once()
        await lookup_system.run_pending_lookups()

        completions = [
            e for e in cap.appended if e.event_type == "tool.knowledge_lookup.completed"
        ]
        assert len(completions) == 1
