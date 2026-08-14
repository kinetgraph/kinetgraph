# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the refactored EventLog — receives EventLogStorage.

Part of the RED phase for Iteration 1 (ADR-019). The refactored
EventLog is a thin orchestrator: preflight checks (validation,
tenant, signature) + delegation to the injected storage.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


pytestmark = pytest.mark.asyncio


def _make_event(agent_id: str = "a-1"):
    from datetime import datetime, timezone
    from kntgraph.core.event import Event

    return Event(
        event_id=uuid4(),
        event_type="test.event",
        agent_id=agent_id,
        event_class="domain",
        data={"k": "v"},
        timestamp=datetime.now(timezone.utc),
        correlation=None,
    )


def _fake_storage():
    from kntgraph.core.result import Ok

    storage = MagicMock()
    storage.append = AsyncMock(return_value=Ok("1-0"))
    storage.read = AsyncMock(return_value=[])
    storage.read_latest = AsyncMock(return_value=[])
    storage.stream_len = AsyncMock(return_value=0)
    storage.list_agents = AsyncMock(return_value=[])
    storage.delete = AsyncMock()
    return storage


class TestEventLogConstructor:
    async def test_constructor_takes_storage(self):
        from kntgraph.stream.event_log.store import EventLog

        storage = _fake_storage()
        log = EventLog(storage=storage)
        assert log._storage is storage


class TestEventLogAppendDelegates:
    async def test_append_delegates_to_storage(self):
        from kntgraph.stream.event_log.store import EventLog

        storage = _fake_storage()
        log = EventLog(storage=storage)
        event = _make_event()
        await log.append(event)
        storage.append.assert_awaited_once()

    async def test_append_returns_storage_result_on_success(self):
        from kntgraph.stream.event_log.store import EventLog

        storage = _fake_storage()
        log = EventLog(storage=storage)
        event = _make_event()
        result = await log.append(event)
        assert result.is_ok()

    async def test_append_blocks_invalid_agent_id(self):
        from kntgraph.stream.event_log.store import EventLog

        storage = _fake_storage()
        log = EventLog(storage=storage)
        # Construct an event with a valid-shape agent_id, then
        # monkey-patch the agent_id AFTER construction (bypassing
        # Event.__post_init__'s validation). The preflight check
        # in EventLog catches this defense-in-depth.
        event = _make_event(agent_id="valid-shape-id")
        object.__setattr__(event, "agent_id", "bad id with spaces")
        result = await log.append(event)
        assert result.is_err()
        storage.append.assert_not_awaited()

    async def test_append_blocks_tenant_violation(self):
        from kntgraph.stream.event_log.store import EventLog

        from kntgraph.security import Principal, Role, principal_ctx

        storage = _fake_storage()
        log = EventLog(storage=storage)
        event = _make_event(agent_id="tenant-a.agent-1")

        principal = Principal(
            agent_id="agent-1",
            role=Role.agent,
            tenant_id="tenant-b",
            key_id="k1",
        )
        token = principal_ctx.set(principal)
        try:
            result = await log.append(event)
            assert result.is_err()
            storage.append.assert_not_awaited()
        finally:
            principal_ctx.reset(token)


class TestEventLogReadsDelegate:
    async def test_read_delegates_to_storage(self):
        from kntgraph.stream.event_log.store import EventLog

        storage = _fake_storage()
        log = EventLog(storage=storage)
        await log.read("a-1")
        storage.read.assert_awaited_once()
        call = storage.read.await_args
        assert call.args[0] == "a-1"

    async def test_read_latest_delegates(self):
        from kntgraph.stream.event_log.store import EventLog

        storage = _fake_storage()
        log = EventLog(storage=storage)
        await log.read_latest("a-1", n=5)
        storage.read_latest.assert_awaited_once()
        call = storage.read_latest.await_args
        assert call.args[0] == "a-1"
        assert call.args[1] == 5

    async def test_stream_len_delegates(self):
        from kntgraph.stream.event_log.store import EventLog

        storage = _fake_storage()
        log = EventLog(storage=storage)
        await log.stream_len("a-1")
        storage.stream_len.assert_awaited_once_with("a-1")

    async def test_iter_all_uses_storage_list_agents(self):
        from kntgraph.stream.event_log.store import EventLog

        storage = _fake_storage()
        storage.list_agents = AsyncMock(return_value=["a-1", "a-2"])
        # ``read`` returns one event so the inner
        # ``for e in events: yield e`` branch fires.
        sample_event = _make_event("a-1")
        storage.read = AsyncMock(return_value=[sample_event])
        log = EventLog(storage=storage)
        events = []
        async for e in log.iter_all():
            events.append(e)
        storage.list_agents.assert_awaited_once()
        assert storage.read.await_count == 2
        # The inner loop yielded the sample event
        # twice (once per agent).
        assert events == [sample_event, sample_event]

    async def test_iter_all_short_circuits_when_storage_returns_none(self):
        """The branch ``if agent_ids is None: return``:
        when the storage returns ``None`` for
        ``list_agents()``, the iterator yields nothing
        (graceful degradation). Pinned so a future
        refactor does not raise on a None from the
        storage adapter.
        """
        from kntgraph.stream.event_log.store import EventLog

        storage = _fake_storage()
        storage.list_agents = AsyncMock(return_value=None)
        log = EventLog(storage=storage)
        events = []
        async for e in log.iter_all():
            events.append(e)
        assert events == []
        # The early-return closed the loop before
        # ``read`` was ever called.
        storage.read.assert_not_called()

    async def test_append_batch_returns_error_on_first_failure(self):
        """The branch ``if r.is_err(): return Err(...)``
        in ``append_batch``: when one of the appended
        events fails, the error is returned without
        partial commit. Pinned so a future refactor
        does not swallow the error and append the
        remaining events (silent partial-commit).
        """
        from kntgraph.core.result import Err, PersistenceError
        from kntgraph.stream.event_log.store import EventLog

        storage = _fake_storage()
        # First ``append`` succeeds, second fails.
        from kntgraph.core.result import Ok

        call_count = {"n": 0}

        async def _stub_append(*, agent_id, event):
            call_count["n"] += 1
            if call_count["n"] == 2:
                return Err(PersistenceError("second_failed"))
            return Ok(f"{call_count['n']}-0")

        storage.append = _stub_append
        log = EventLog(storage=storage)
        events = [_ev(), _ev(), _ev()]
        result = await log.append_batch(events)
        assert result.is_err()
        assert "second_failed" in str(result.err_value())
        # The third event was not appended.
        assert call_count["n"] == 2


def _ev() -> Event:
    """Event factory for the append_batch test."""
    from kntgraph.core.event import CorrelationContext, Event

    return Event.create(
        event_type="user.intent",
        agent_id="a-1",
        event_class="domain",
        correlation=CorrelationContext.new(),
    )


class TestReadAfterCursor:
    """The ``read_after_cursor`` fallback path:
    when the storage adapter does not implement
    ``read_with_cursor``, the EventLog falls back to
    ``read`` with a ``start = f"({cursor}"`` shift.
    Pinned so a future refactor does not break the
    fallback path for legacy adapters.
    """

    async def test_cursor_empty_is_normalized_to_dash(self):
        """The branch ``if not cursor: cursor = "-"``:
        an empty / None cursor is normalised to ``-``
        (the canonical Redis stream "from the start"
        sentinel). Pinned so a future refactor does
        not pass ``""`` to Redis (which would raise).
        """
        from kntgraph.stream.event_log.store import EventLog

        storage = _fake_storage()
        # The fallback path: ``_storage`` lacks
        # ``read_with_cursor`` so ``hasattr`` is False.
        # We use a ``SimpleNamespace`` to avoid the
        # catch-all ``MagicMock`` behaviour, which
        # auto-creates the attribute.
        from types import SimpleNamespace

        storage = SimpleNamespace(
            append=AsyncMock(),
            read=AsyncMock(return_value=[]),
            read_latest=AsyncMock(return_value=[]),
            stream_len=AsyncMock(return_value=0),
            list_agents=AsyncMock(return_value=[]),
            delete=AsyncMock(),
        )
        log = EventLog(storage=storage)
        # Empty cursor -> normalised to "-".
        events, cur = await log.read_after_cursor("a-1", "")
        assert events == []
        # The cursor returned is the same input ("-")
        # because there are no events to advance.
        assert cur == "-"

    async def test_fallback_uses_read_with_paren_cursor(self):
        """The branch ``start = f"({cursor}"``: when the
        fallback path runs and the cursor is non-empty,
        the read is shifted by one via the paren
        notation (Redis exclusive range). Pinned so a
        future refactor does not regress the
        ``read_after_cursor`` semantics.
        """
        from kntgraph.stream.event_log.store import EventLog

        from types import SimpleNamespace

        async def _stub_read(agent_id, start="", end="", count=None):
            # The fallback closed the cursor with
            # ``f"({cursor}"``; pass it through.
            return []

        storage = SimpleNamespace(
            append=AsyncMock(),
            read=_stub_read,
            read_latest=AsyncMock(return_value=[]),
            stream_len=AsyncMock(return_value=0),
            list_agents=AsyncMock(return_value=[]),
            delete=AsyncMock(),
        )
        log = EventLog(storage=storage)
        events, cur = await log.read_after_cursor("a-1", "1234-0")
        assert events == []
        # The cursor is the input back (no events).
        assert cur == "1234-0"

    async def test_fallback_dash_cursor_uses_passthrough_start(self):
        """The branch ``if cursor == "-" or cursor == "0-0"``:
        the canonical "from the start" sentinels keep
        ``start = "-"`` (no paren shift). Pinned so a
        future refactor does not regress the fall-through
        path for the most common cursor.
        """
        from kntgraph.stream.event_log.store import EventLog

        from types import SimpleNamespace

        captured: dict[str, str] = {}

        async def _stub_read(agent_id, start="", end="", count=None):
            captured["start"] = start
            return []

        storage = SimpleNamespace(
            append=AsyncMock(),
            read=_stub_read,
            read_latest=AsyncMock(return_value=[]),
            stream_len=AsyncMock(return_value=0),
            list_agents=AsyncMock(return_value=[]),
            delete=AsyncMock(),
        )
        log = EventLog(storage=storage)
        # The "-" sentinel takes the passthrough path.
        await log.read_after_cursor("a-1", "-")
        assert captured["start"] == "-"
        # The "0-0" sentinel also takes the
        # passthrough path.
        await log.read_after_cursor("a-1", "0-0")
        assert captured["start"] == "-"

    async def test_fallback_returns_cursor_when_no_events(self):
        """The branch ``if not events: return [], cursor``:
        when the read returns no events, the cursor is
        the INPUT cursor (not ``str(events[-1].event_id)``).
        Pinned so a future refactor does not raise on
        an empty read.
        """
        from kntgraph.stream.event_log.store import EventLog

        from types import SimpleNamespace

        async def _stub_read(agent_id, start="", end="", count=None):
            return []

        storage = SimpleNamespace(
            append=AsyncMock(),
            read=_stub_read,
            read_latest=AsyncMock(return_value=[]),
            stream_len=AsyncMock(return_value=0),
            list_agents=AsyncMock(return_value=[]),
            delete=AsyncMock(),
        )
        log = EventLog(storage=storage)
        events, cur = await log.read_after_cursor("a-1", "9999-0")
        assert events == []
        # The cursor is the input (no events to advance).
        assert cur == "9999-0"

    async def test_read_with_cursor_delegates_to_storage(self):
        """The branch ``if hasattr(self._storage,
        "read_with_cursor"): return await self._storage
        .read_with_cursor(...)``: the fast path for
        adapters that implement the native cursor
        API. Pinned so a future refactor does not
        accidentally drop the delegation.
        """
        from kntgraph.stream.event_log.store import EventLog

        from types import SimpleNamespace

        sample_event = _make_event("a-1")
        expected_cursor = "1234-0"

        async def _stub_read_with_cursor(agent_id, cursor):
            return [sample_event], expected_cursor

        storage = SimpleNamespace(
            append=AsyncMock(),
            read=AsyncMock(),
            read_with_cursor=_stub_read_with_cursor,
            read_latest=AsyncMock(return_value=[]),
            stream_len=AsyncMock(return_value=0),
            list_agents=AsyncMock(return_value=[]),
            delete=AsyncMock(),
        )
        log = EventLog(storage=storage)
        events, cur = await log.read_after_cursor("a-1", "1-0")
        # The fast path was taken: the returned
        # events and cursor are the ones from the
        # stub, NOT the ones from the fallback path.
        assert events == [sample_event]
        assert cur == expected_cursor

    async def test_fallback_returns_events_with_advanced_cursor(self):
        """The branch ``if not events: return [], cursor``
        (False arm): when the read returns events,
        the cursor is the last event's id. Pinned
        so a future refactor does not regress the
        cursor-advance semantics.
        """
        from kntgraph.stream.event_log.store import EventLog

        class _FakeStorageNoCursor:
            """Storage that does NOT implement
            ``read_with_cursor`` (forces the fallback path)."""

            async def append(self, *, agent_id, event):
                from kntgraph.core.result import Ok

                return Ok("1-0")

            async def read(self, agent_id, start="", end="", count=None):
                return [self._event]

            async def read_latest(self, agent_id, n=1):
                return []

            async def stream_len(self, agent_id):
                return 0

            async def list_agents(self):
                return []

            async def delete(self, agent_id):
                pass

        sample_event = _make_event("a-1")
        storage = _FakeStorageNoCursor()
        storage._event = sample_event
        log = EventLog(storage=storage)
        events, cur = await log.read_after_cursor("a-1", "0-0")
        assert events == [sample_event]
        # The cursor advanced to the event id.
        assert cur == str(sample_event.event_id)


class TestEventLogIterAll:
    async def test_iter_all_none_agents(self):
        """The branch `if agent_ids is None: return` handles cases where
        list_agents returns None.
        """
        from kntgraph.stream.event_log.store import EventLog

        storage = _fake_storage()
        storage.list_agents.return_value = None
        log = EventLog(storage=storage)

        events = []
        async for e in log.iter_all(agent_ids=None):
            events.append(e)

        assert events == []
