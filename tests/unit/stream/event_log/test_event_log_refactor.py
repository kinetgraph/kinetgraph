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
    def test_constructor_takes_storage(self):
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
        log = EventLog(storage=storage)
        events = []
        async for e in log.iter_all():
            events.append(e)
        storage.list_agents.assert_awaited_once()
        assert storage.read.await_count == 2
