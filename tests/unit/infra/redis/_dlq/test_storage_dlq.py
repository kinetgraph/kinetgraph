# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for RedisDLQStorage — Redis impl of DLQStorage.

Iteration 5 (ADR-019). The storage owns 4 Redis keys:
- ``knt:dlq:events`` (Stream)
- ``knt:dlq:by_event_id`` (Hash: <event_id>:<reason> → stream_id)
- ``knt:dlq:reasons`` (Hash: reason → counter)
- ``knt:dlq:by_agent`` (Hash: agent_id → first-failure stream_id)

All mutating operations return ``Result`` per AGENTS.md §6.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


pytestmark = pytest.mark.asyncio


def _fake_redis():
    redis = MagicMock()
    redis.xadd = AsyncMock(return_value=b"1-0")
    redis.xrange = AsyncMock(return_value=[])
    redis.xdel = AsyncMock(return_value=1)
    redis.hset = AsyncMock(return_value=1)
    redis.hsetnx = AsyncMock(return_value=1)
    redis.hget = AsyncMock(return_value=None)
    redis.hgetall = AsyncMock(return_value={})
    redis.hdel = AsyncMock(return_value=1)
    redis.hincrby = AsyncMock(return_value=1)
    redis.hlen = AsyncMock(return_value=0)
    redis.hscan_iter = MagicMock(return_value=aiter([]))
    redis.xinfo_stream = AsyncMock(return_value={"length": 0})
    redis.delete = AsyncMock(return_value=1)
    return redis


async def aiter(items):
    for x in items:
        yield x


SAMPLE_PAYLOAD = {
    "event_id": "550e8400-e29b-41d4-a716-446655440000",
    "agent_id": "agent-1",
    "event_type": "tool.test",
    "event_class": "domain",
    "reason": "timeout",
    "error_message": "boom",
    "retry_count": "0",
}


class TestRedisDLQStorage:
    def test_module_importable(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        assert RedisDLQStorage is not None

    def test_implements_dlq_storage(self):
        from kntgraph.infra.redis._dlq import DLQStorage, RedisDLQStorage

        storage = RedisDLQStorage(client=_fake_redis())
        assert isinstance(storage, DLQStorage)

    async def test_append_writes_stream_entry(self):
        from kntgraph.infra.redis._dlq import (
            DLQ_STREAM_KEY,
            RedisDLQStorage,
        )

        redis = _fake_redis()
        storage = RedisDLQStorage(client=redis)
        result = await storage.append("dlq:abc:timeout", SAMPLE_PAYLOAD)
        assert result.is_ok()
        assert result.ok_value() == "1-0"
        redis.xadd.assert_awaited_once()
        args, kwargs = redis.xadd.await_args
        assert args[0] == DLQ_STREAM_KEY
        assert kwargs.get("maxlen") == 1_000_000

    async def test_append_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage
        from kntgraph.infra.redis._errors import MemoryError

        redis = _fake_redis()
        redis.xadd = AsyncMock(side_effect=RuntimeError("redis down"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.append("dlq:abc:timeout", SAMPLE_PAYLOAD)
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_read_returns_dict_on_hit(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.xrange = AsyncMock(
            return_value=[(b"1-0", {b"event_id": b"abc", b"reason": b"timeout"})]
        )
        storage = RedisDLQStorage(client=redis)
        result = await storage.read("1-0")
        assert result.is_ok()
        assert result.ok_value() == {
            "event_id": "abc",
            "reason": "timeout",
        }

    async def test_read_returns_none_on_miss(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.xrange = AsyncMock(return_value=[])
        storage = RedisDLQStorage(client=redis)
        result = await storage.read("999-0")
        assert result.is_ok()
        assert result.ok_value() is None

    async def test_read_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage
        from kntgraph.infra.redis._errors import MemoryError

        redis = _fake_redis()
        redis.xrange = AsyncMock(side_effect=RuntimeError("redis down"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.read("1-0")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_list_for_agent_returns_empty_when_head_missing(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(return_value=None)
        storage = RedisDLQStorage(client=redis)
        result = await storage.list_for_agent("agent-1")
        assert result.is_ok()
        assert result.ok_value() == []

    async def test_list_for_agent_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage
        from kntgraph.infra.redis._errors import MemoryError

        redis = _fake_redis()
        redis.hget = AsyncMock(side_effect=RuntimeError("redis down"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.list_for_agent("agent-1")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_find_by_event_id_returns_err_on_hscan_failure(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage
        from kntgraph.infra.redis._errors import MemoryError

        redis = _fake_redis()
        redis.hscan_iter = MagicMock(side_effect=RuntimeError("redis down"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.find_by_event_id("abc")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_purge_returns_err_on_delete_failure(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage
        from kntgraph.infra.redis._errors import MemoryError

        redis = _fake_redis()
        redis.delete = AsyncMock(side_effect=RuntimeError("redis down"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.purge()
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_read_index_returns_stream_id(self):
        from kntgraph.infra.redis._dlq import (
            DLQ_EVENT_INDEX,
            RedisDLQStorage,
        )

        redis = _fake_redis()
        redis.hget = AsyncMock(return_value=b"1-0")
        storage = RedisDLQStorage(client=redis)
        result = await storage.read_index("abc", "timeout")
        assert result.is_ok()
        assert result.ok_value() == "1-0"
        redis.hget.assert_awaited_once_with(DLQ_EVENT_INDEX, "abc:timeout")

    async def test_read_index_returns_none_on_miss(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(return_value=None)
        storage = RedisDLQStorage(client=redis)
        result = await storage.read_index("abc", "timeout")
        assert result.is_ok()
        assert result.ok_value() is None

    async def test_bump_reason_counter_calls_hincrby(self):
        from kntgraph.infra.redis._dlq import (
            DLQ_REASON_INDEX,
            RedisDLQStorage,
        )

        redis = _fake_redis()
        redis.hincrby = AsyncMock(return_value=5)
        storage = RedisDLQStorage(client=redis)
        result = await storage.bump_reason_counter("timeout", 1)
        assert result.is_ok()
        redis.hincrby.assert_awaited_once_with(DLQ_REASON_INDEX, "timeout", 1)

    async def test_bump_reason_counter_accepts_negative_delta(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.hincrby = AsyncMock(return_value=2)
        storage = RedisDLQStorage(client=redis)
        result = await storage.bump_reason_counter("timeout", -1)
        assert result.is_ok()
        redis.hincrby.assert_awaited_once_with("knt:dlq:reasons", "timeout", -1)

    async def test_get_stats_returns_aggregate(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.xinfo_stream = AsyncMock(return_value={"length": 7})
        redis.hgetall = AsyncMock(
            return_value={b"timeout": b"3", b"validation_error": b"4"}
        )
        redis.hlen = AsyncMock(return_value=2)
        storage = RedisDLQStorage(client=redis)
        result = await storage.get_stats()
        assert result.is_ok()
        stats = result.ok_value()
        assert stats["total_events"] == 7
        assert stats["unique_agents"] == 2
        assert stats["by_reason"] == {"timeout": 3, "validation_error": 4}

    async def test_purge_deletes_all_keys(self):
        from kntgraph.infra.redis._dlq import (
            DLQ_AGENT_INDEX,
            DLQ_EVENT_INDEX,
            DLQ_REASON_INDEX,
            DLQ_STREAM_KEY,
            RedisDLQStorage,
        )

        redis = _fake_redis()
        redis.xinfo_stream = AsyncMock(return_value={"length": 5})
        storage = RedisDLQStorage(client=redis)
        result = await storage.purge()
        assert result.is_ok()
        assert result.ok_value() == 5
        redis.delete.assert_awaited_once_with(
            DLQ_STREAM_KEY,
            DLQ_AGENT_INDEX,
            DLQ_EVENT_INDEX,
            DLQ_REASON_INDEX,
        )

    async def test_drop_entry_xdel_and_hdel(self):
        from kntgraph.infra.redis._dlq import (
            DLQ_EVENT_INDEX,
            RedisDLQStorage,
        )

        redis = _fake_redis()
        redis.hget = AsyncMock(return_value=b"1-0")
        storage = RedisDLQStorage(client=redis)
        result = await storage.drop_entry("abc", "timeout", "1-0")
        assert result.is_ok()
        redis.xdel.assert_awaited_once_with("knt:dlq:events", "1-0")
        redis.hdel.assert_awaited_once_with(DLQ_EVENT_INDEX, "abc:timeout")

    async def test_drop_entry_skips_xdel_for_placeholder(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(return_value=b"PLACEHOLDER")
        storage = RedisDLQStorage(client=redis)
        result = await storage.drop_entry("abc", "timeout", "PLACEHOLDER")
        assert result.is_ok()
        redis.xdel.assert_not_called()
