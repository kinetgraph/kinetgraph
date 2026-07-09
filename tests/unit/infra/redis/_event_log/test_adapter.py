# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for RedisEventLogAdapter — domain interface + Redis impl.

Part of the RED phase for Iteration 1 (ADR-019).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


pytestmark = pytest.mark.asyncio


def _fake_redis_with_stream(
    stream_data: list[tuple[bytes, dict]] | None = None,
) -> MagicMock:
    redis = MagicMock()
    redis.xadd = AsyncMock(return_value=b"1-0")
    redis.xrange = AsyncMock(return_value=stream_data or [])
    redis.xrevrange = AsyncMock(return_value=stream_data or [])
    redis.xinfo_stream = AsyncMock(return_value={"length": len(stream_data or [])})
    redis.delete = AsyncMock(return_value=1)
    redis.scan_iter = MagicMock(return_value=aiter([]))
    redis.pipeline = MagicMock(return_value=_fake_pipeline())
    redis.set = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return redis


def _fake_pipeline():
    pipe = MagicMock()
    pipe.xadd = MagicMock(return_value=pipe)
    pipe.set = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[b"1-0"])
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=None)
    return pipe


async def aiter(items):
    for x in items:
        yield x


def _make_event(agent_id: str = "a-1", event_type: str = "test.event"):
    from kntgraph.core.event import CorrelationContext, Event

    return Event(
        event_id=uuid4(),
        event_type=event_type,
        agent_id=agent_id,
        event_class="domain",
        data={"k": "v"},
        timestamp=datetime.now(timezone.utc),
        correlation=CorrelationContext.new(),
    )


class TestRedisEventLogAdapter:
    def test_module_importable(self):
        from kntgraph.infra.redis._event_log import (
            EventLogStorage,
            RedisEventLogAdapter,
        )

        assert EventLogStorage is not None
        assert RedisEventLogAdapter is not None

    def test_adapter_implements_storage_protocol(self):
        from kntgraph.infra.redis._event_log import (
            RedisEventLogAdapter,
        )

        # Duck-typed: all Protocol methods must exist on the adapter.
        for name in (
            "append",
            "read",
            "read_latest",
            "stream_len",
            "list_agents",
            "delete",
        ):
            assert hasattr(RedisEventLogAdapter, name), (
                f"RedisEventLogAdapter must implement {name!r}"
            )

    async def test_append_returns_stream_id_on_success(self):
        from kntgraph.infra.redis._event_log import RedisEventLogAdapter

        redis = _fake_redis_with_stream()
        adapter = RedisEventLogAdapter(client=redis, maxlen=1000)

        result = await adapter.append(agent_id="a-1", event=_make_event())
        # Ok is a frozen dataclass; check the value directly.
        assert result.is_ok(), f"expected Ok, got {result!r}"
        assert result.ok_value() == "1-0"

    async def test_read_returns_event_list(self):
        from kntgraph.infra.redis._event_log import RedisEventLogAdapter

        # Empty stream returns empty list.
        redis = _fake_redis_with_stream(stream_data=[])
        adapter = RedisEventLogAdapter(client=redis)
        result = await adapter.read("a-1")
        assert result == []

    async def test_read_with_count_passes_to_xrange(self):
        from kntgraph.infra.redis._event_log import RedisEventLogAdapter

        redis = _fake_redis_with_stream()
        adapter = RedisEventLogAdapter(client=redis)
        await adapter.read("a-1", count=10)
        kwargs = redis.xrange.await_args.kwargs
        assert kwargs.get("count") == 10

    async def test_stream_len_returns_zero_for_missing_agent(self):
        from kntgraph.infra.redis._event_log import RedisEventLogAdapter
        from redis.exceptions import ResponseError

        redis = _fake_redis_with_stream()
        redis.xinfo_stream = AsyncMock(side_effect=ResponseError("no key"))
        adapter = RedisEventLogAdapter(client=redis)
        assert await adapter.stream_len("missing") == 0

    async def test_list_agents_extracts_ids_from_keys(self):
        from kntgraph.infra.redis._event_log import RedisEventLogAdapter

        redis = _fake_redis_with_stream()

        async def fake_scan(match, count=None):
            for k in [
                b"knt:agents:tenant-a:events",
                b"knt:agents:tenant-b:events",
            ]:
                yield k

        redis.scan_iter = MagicMock(
            side_effect=lambda match, count=None: fake_scan(match, count)
        )
        adapter = RedisEventLogAdapter(client=redis)
        agents = await adapter.list_agents()
        assert "tenant-a" in agents
        assert "tenant-b" in agents

    async def test_delete_removes_stream(self):
        from kntgraph.infra.redis._event_log import RedisEventLogAdapter

        redis = _fake_redis_with_stream()
        adapter = RedisEventLogAdapter(client=redis)
        await adapter.delete("a-1")
        redis.delete.assert_awaited()
