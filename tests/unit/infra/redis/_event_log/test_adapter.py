# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``infra/redis/_event_log/_adapter.py``
(``RedisEventLogAdapter``).

Closes the infra/redis/_event_log/_adapter coverage gap
(DEBT §3, 79% → 100%). The adapter is the concrete
``EventLogStorage`` Protocol implementation that owns
the wire format (codec, MAXLEN, idempotency, key
conventions). The ``EventLog`` class in
``stream/event_log`` is a thin orchestrator on top of
this; the per-method delegation is covered in
``tests/unit/stream/event_log/test_event_log_refactor.py``.

The uncovered branches were the per-method happy path
and the three error paths:

  - ``append`` returns ``Err`` on
    ``IdempotencyConflict`` (the idempotency window
    detected a concurrent insert; the caller retries).
  - ``append`` returns ``Err`` on a generic
    ``Exception`` (Redis is down).
  - ``read_with_cursor`` honours the
    ``-`` / ``0-0`` cursor (start from the
    beginning) and the exclusive
    ``(cursor`` branch (read after the given
    stream id).
  - ``read_with_cursor`` returns ``([], cursor)``
    when the stream has no events past the cursor.
  - ``read_with_cursor`` decodes a bytes stream id
    to a ``str``.
  - ``read_latest`` parses the
    ``xrevrange`` result.
  - ``stream_len`` returns ``0`` when the stream
    does not exist (the underlying
    ``ResponseError`` is caught by the
    ``except self._response_error()`` arm).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.infra.redis._errors import IdempotencyConflict
from kntgraph.infra.redis._event_log._adapter import (
    RedisEventLogAdapter,
)
from kntgraph.stream.event_log.codec import event_to_redis


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> MagicMock:
    return MagicMock()


@pytest.fixture
def adapter(client: MagicMock) -> RedisEventLogAdapter:
    return RedisEventLogAdapter(client=client, maxlen=1000)


@pytest.fixture
def make_event():
    def _make() -> Event:
        return Event.create(
            event_type="document.received",
            agent_id="agent-1",
            event_class="domain",
            data={"doc_id": "NF-001"},
            correlation=CorrelationContext.new(),
        )

    return _make


@pytest.fixture
def wire_entry(make_event):
    """Build a single wire-format stream entry (the
    shape that ``xrange`` / ``xrevrange`` return in
    production — bytes keys, str values, bytes
    stream id)."""

    def _make(
        stream_id: bytes = b"1-0", event: Event | None = None
    ) -> tuple[bytes, dict]:
        e = event or make_event()
        return (
            stream_id,
            {k.encode(): v for k, v in event_to_redis(e).items()},
        )

    return _make


# ---------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------


class TestAppend:
    async def test_ok(
        self,
        adapter: RedisEventLogAdapter,
        client: MagicMock,
        make_event,
    ) -> None:
        from kntgraph.infra.redis._event_log import _idempotency

        client.xadd = AsyncMock(return_value=b"1-0")
        original = _idempotency.claim_event_id_slot
        _idempotency.claim_event_id_slot = AsyncMock(return_value="1-0")
        try:
            result = await adapter.append(agent_id="agent-1", event=make_event())
        finally:
            _idempotency.claim_event_id_slot = original

        assert result.is_ok()
        assert result.ok_value() == "1-0"

    async def test_idempotency_conflict(
        self,
        adapter: RedisEventLogAdapter,
        make_event,
    ) -> None:
        from kntgraph.infra.redis._event_log import _idempotency

        original = _idempotency.claim_event_id_slot
        _idempotency.claim_event_id_slot = AsyncMock(
            side_effect=IdempotencyConflict("dup")
        )
        try:
            result = await adapter.append(agent_id="agent-1", event=make_event())
        finally:
            _idempotency.claim_event_id_slot = original

        assert result.is_err()
        assert "Concurrent insert" in str(result.err_value())

    async def test_redis_error(
        self,
        adapter: RedisEventLogAdapter,
        make_event,
    ) -> None:
        from kntgraph.infra.redis._event_log import _idempotency

        original = _idempotency.claim_event_id_slot
        _idempotency.claim_event_id_slot = AsyncMock(
            side_effect=ConnectionError("redis down")
        )
        try:
            result = await adapter.append(agent_id="agent-1", event=make_event())
        finally:
            _idempotency.claim_event_id_slot = original

        assert result.is_err()
        assert "Redis error" in str(result.err_value())


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


class TestRead:
    async def test_with_count(
        self,
        adapter: RedisEventLogAdapter,
        client: MagicMock,
        wire_entry,
    ) -> None:
        client.xrange = AsyncMock(return_value=[wire_entry()])
        result = await adapter.read("agent-1", start="-", end="+", count=10)
        assert len(result) == 1
        assert result[0].event_type == "document.received"

    async def test_without_count(
        self, adapter: RedisEventLogAdapter, client: MagicMock
    ) -> None:
        client.xrange = AsyncMock(return_value=[])
        result = await adapter.read("agent-1")
        assert result == []


# ---------------------------------------------------------------------------
# read_with_cursor
# ---------------------------------------------------------------------------


class TestReadWithCursor:
    async def test_cursor_minus(
        self, adapter: RedisEventLogAdapter, client: MagicMock
    ) -> None:
        client.xrange = AsyncMock(return_value=[])
        events, cursor = await adapter.read_with_cursor("agent-1", "-")
        assert events == []
        assert cursor == "-"

    async def test_cursor_zero_zero(
        self, adapter: RedisEventLogAdapter, client: MagicMock
    ) -> None:
        client.xrange = AsyncMock(return_value=[])
        events, cursor = await adapter.read_with_cursor("agent-1", "0-0")
        assert events == []
        assert cursor == "0-0"

    async def test_cursor_exclusive(
        self,
        adapter: RedisEventLogAdapter,
        client: MagicMock,
        wire_entry,
    ) -> None:
        client.xrange = AsyncMock(return_value=[wire_entry(b"2-0")])
        events, cursor = await adapter.read_with_cursor("agent-1", "1-0")
        assert len(events) == 1
        assert cursor == "2-0"

    async def test_cursor_with_str_id(
        self,
        adapter: RedisEventLogAdapter,
        client: MagicMock,
        wire_entry,
    ) -> None:
        # Real falkordb returns str (not bytes) when
        # decode_responses is on.
        client.xrange = AsyncMock(return_value=[("3-0", wire_entry(b"3-0")[1])])
        events, cursor = await adapter.read_with_cursor("agent-1", "2-0")
        assert len(events) == 1
        assert cursor == "3-0"


# ---------------------------------------------------------------------------
# read_latest
# ---------------------------------------------------------------------------


class TestReadLatest:
    async def test_returns_n_latest(
        self,
        adapter: RedisEventLogAdapter,
        client: MagicMock,
        wire_entry,
    ) -> None:
        # ``xrevrange`` returns most-recent first.
        client.xrevrange = AsyncMock(
            return_value=[wire_entry(b"3-0"), wire_entry(b"2-0")]
        )
        result = await adapter.read_latest("agent-1", n=2)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# stream_len
# ---------------------------------------------------------------------------


class TestStreamLen:
    async def test_returns_length(
        self, adapter: RedisEventLogAdapter, client: MagicMock
    ) -> None:
        client.xinfo_stream = AsyncMock(return_value={"length": 42})
        assert await adapter.stream_len("agent-1") == 42

    async def test_returns_zero_on_missing_stream(
        self, adapter: RedisEventLogAdapter, client: MagicMock
    ) -> None:
        from redis.exceptions import ResponseError

        client.xinfo_stream = AsyncMock(side_effect=ResponseError("no such key"))
        assert await adapter.stream_len("agent-1") == 0

    async def test_returns_zero_on_missing_length_key(
        self, adapter: RedisEventLogAdapter, client: MagicMock
    ) -> None:
        client.xinfo_stream = AsyncMock(return_value={})
        assert await adapter.stream_len("agent-1") == 0


# ---------------------------------------------------------------------------
# list_agents
# ---------------------------------------------------------------------------


class TestListAgents:
    async def test_returns_parsed_agent_ids(
        self, adapter: RedisEventLogAdapter, client: MagicMock
    ) -> None:
        async def fake_scan_iter(*args, **kwargs):
            for key in [
                b"knt:agents:agent-1:events",
                b"knt:agents:agent-2:events",
            ]:
                yield key

        client.scan_iter = fake_scan_iter
        result = await adapter.list_agents()
        assert result == ["agent-1", "agent-2"]


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_deletes_stream(
        self, adapter: RedisEventLogAdapter, client: MagicMock
    ) -> None:
        from kntgraph.infra.redis._event_log._keys import (
            stream_key_for_agent,
        )

        client.delete = AsyncMock(return_value=1)
        await adapter.delete("agent-1")
        client.delete.assert_awaited_once_with(stream_key_for_agent("agent-1"))
