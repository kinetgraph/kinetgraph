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
    async def test_module_importable(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        assert RedisDLQStorage is not None

    async def test_implements_dlq_storage(self):
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


class TestAppendRaceLoser:
    """Append path: ``hsetnx`` returns ``False`` (a
    concurrent writer claimed the slot first). The
    storage reads the winner's stream id back and
    returns ``Ok`` with it."""

    async def test_hsetnx_loser_returns_winner_id(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(side_effect=[None, b"winner-1"])
        redis.hsetnx = AsyncMock(return_value=False)
        storage = RedisDLQStorage(client=redis)
        result = await storage.append("abc:timeout", SAMPLE_PAYLOAD)
        assert result.is_ok()
        assert result.ok_value() == "winner-1"

    async def test_hsetnx_loser_no_winner_returns_placeholder(self):
        from kntgraph.infra.redis._dlq import PLACEHOLDER, RedisDLQStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(side_effect=[None, None])
        redis.hsetnx = AsyncMock(return_value=False)
        storage = RedisDLQStorage(client=redis)
        result = await storage.append("abc:timeout", SAMPLE_PAYLOAD)
        assert result.is_ok()
        assert result.ok_value() == PLACEHOLDER


class TestAppendIdempotent:
    """Append path: the index already has the idem key
    (the caller is deduplicating). Returns the
    existing stream id without re-appending."""

    async def test_existing_idem_key_returns_existing(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(return_value=b"existing-1")
        storage = RedisDLQStorage(client=redis)
        result = await storage.append("abc:timeout", SAMPLE_PAYLOAD)
        assert result.is_ok()
        assert result.ok_value() == "existing-1"
        redis.xadd.assert_not_called()


class TestAppendStrStreamId:
    """Append path: ``xadd`` returns a ``str`` (some
    redis clients / new fakeredis returns ``str``,
    not bytes)."""

    async def test_str_stream_id_decoded(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.xadd = AsyncMock(return_value="1234-0")
        storage = RedisDLQStorage(client=redis)
        result = await storage.append("abc:timeout", SAMPLE_PAYLOAD)
        assert result.is_ok()
        assert result.ok_value() == "1234-0"


class TestAppendExistingStr:
    """Append path: existing idem key returns ``str``
    (the repo's ``decode_value`` handles both)."""

    async def test_existing_idem_key_str(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(return_value="existing-1")
        storage = RedisDLQStorage(client=redis)
        result = await storage.append("abc:timeout", SAMPLE_PAYLOAD)
        assert result.is_ok()
        assert result.ok_value() == "existing-1"


class TestListByReason:
    async def test_filters_by_reason(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.xrange = AsyncMock(
            return_value=[
                (b"1-0", {**SAMPLE_PAYLOAD, "reason": "timeout"}),
                (b"2-0", {**SAMPLE_PAYLOAD, "reason": "validation_error"}),
                (b"3-0", {**SAMPLE_PAYLOAD, "reason": "timeout"}),
            ]
        )
        storage = RedisDLQStorage(client=redis)
        result = await storage.list_by_reason("timeout")
        assert result.is_ok()
        assert len(result.ok_value()) == 2

    async def test_returns_empty_on_storage_error(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.xrange = AsyncMock(side_effect=ConnectionError("redis down"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.list_by_reason("timeout")
        assert result.is_err()


class TestListForAgentScans:
    """``list_for_agent`` reads the head pointer and
    forward-scans."""

    async def test_returns_scanned_entries(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(return_value=b"1-0")
        redis.xrange = AsyncMock(return_value=[(b"1-0", SAMPLE_PAYLOAD)])
        storage = RedisDLQStorage(client=redis)
        result = await storage.list_for_agent("agent-1")
        assert result.is_ok()
        assert len(result.ok_value()) == 1

    async def test_scan_from_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(return_value=b"1-0")
        redis.xrange = AsyncMock(side_effect=ConnectionError("redis down"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.list_for_agent("agent-1")
        assert result.is_err()


class TestListAllError:
    async def test_list_all_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.xrange = AsyncMock(side_effect=ConnectionError("redis down"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.list_all()
        assert result.is_err()


class TestReadIndexError:
    async def test_read_index_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(side_effect=ConnectionError("redis down"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.read_index("abc", "timeout")
        assert result.is_err()


class TestFindByEventId:
    """``find_by_event_id`` skips ``None`` and
    ``PLACEHOLDER`` values during the scan."""

    async def test_skips_placeholder(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()

        async def fake_hscan_iter(*args, **kwargs):
            yield b"abc:timeout", b"PLACEHOLDER"
            yield b"abc:validation_error", b"real-1"

        redis.hscan_iter = fake_hscan_iter  # type: ignore[assignment]
        storage = RedisDLQStorage(client=redis)
        result = await storage.find_by_event_id("abc")
        assert result.is_ok()
        assert result.ok_value() == "real-1"

    async def test_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()

        async def failing_hscan_iter(*args, **kwargs):
            raise ConnectionError("redis down")
            yield  # noqa: ERA001

        redis.hscan_iter = failing_hscan_iter  # type: ignore[assignment]
        storage = RedisDLQStorage(client=redis)
        result = await storage.find_by_event_id("abc")
        assert result.is_err()


class TestBumpReasonCounterError:
    async def test_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.hincrby = AsyncMock(side_effect=ConnectionError("redis down"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.bump_reason_counter("timeout", 1)
        assert result.is_err()


class TestGetStatsMissingStream:
    """``get_stats`` catches the ``XINFO`` ``no such key``
    error and treats the stream as empty."""

    async def test_get_stats_returns_zero_when_stream_missing(self):
        from kntgraph.infra.redis._errors import MemoryError
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.xinfo_stream = AsyncMock(side_effect=MemoryError("no such key"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.get_stats()
        assert result.is_ok()
        assert result.ok_value()["total_events"] == 0

    async def test_get_stats_returns_err_when_hgetall_fails(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.hgetall = AsyncMock(side_effect=ConnectionError("redis down"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.get_stats()
        assert result.is_err()


class TestPurgeMissingStream:
    """``purge`` catches the ``XINFO`` ``no such key``
    error and treats the cached length as 0 (the
    purge is still performed)."""

    async def test_purge_returns_zero_when_stream_missing(self):
        from kntgraph.infra.redis._errors import MemoryError
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.xinfo_stream = AsyncMock(side_effect=MemoryError("no such key"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.purge()
        assert result.is_ok()
        assert result.ok_value() == 0
        redis.delete.assert_awaited_once()


class TestDropEntryError:
    async def test_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._dlq import RedisDLQStorage

        redis = _fake_redis()
        redis.xdel = AsyncMock(side_effect=ConnectionError("redis down"))
        storage = RedisDLQStorage(client=redis)
        result = await storage.drop_entry("abc", "timeout", "1-0")
        assert result.is_err()


class TestDecodeIntDict:
    """The ``_decode_int_dict`` helper filters out
    ``None`` keys and unparseable values."""

    async def test_skips_none_keys(self):
        from kntgraph.infra.redis._dlq._redis import (
            _decode_int_dict,
        )

        # ``None`` key — the inner ``decode_value`` returns
        # ``None`` and the helper skips.
        # Note: a real Redis client never returns
        # ``None`` keys, but the helper is defensive.
        result = _decode_int_dict({None: b"1"})
        assert result == {}

    async def test_skips_unparseable_values(self):
        from kntgraph.infra.redis._dlq._redis import (
            _decode_int_dict,
        )

        result = _decode_int_dict({b"k": b"not-a-number"})
        assert result == {}
