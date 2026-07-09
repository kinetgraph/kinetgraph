# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``RedisCacheAdapter`` — cache LLM via
``RedisLike`` Protocol.

Iter 17 (ADR-019 epílogo + Iter 17 do sharding):
``RedisCacheStorage`` (legacy) was deleted. The new
adapter is composed of a ``RedisLike`` (the framework
boundary) instead of a raw ``redis.asyncio.Redis``.

Tests use a mock ``RedisLike`` to verify the encoding,
TTL pipeline, and scan-based ``clear_prefix``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import AsyncIterator

import pytest

from kntgraph.agents.tools.cache import (
    AsyncCacheStorage,
    RedisCacheAdapter,
    _CacheEntry,
)


# ---------------------------------------------------------------------------
# Mock RedisLike
# ---------------------------------------------------------------------------


@dataclass
class _MockRedisLike:
    """
    Mock that mimics the ``RedisLike`` Protocol subset
    used by ``RedisCacheAdapter``:

      - ``hgetall(key)``
      - ``delete(*keys)``
      - ``pipeline()`` → context manager with ``hset`` + ``expire``
      - ``scan_iter(match, count)`` → async iterator of keys
      - ``unlink(*keys)``
    """

    hgetall_data: dict[str, dict] = field(default_factory=dict)
    hset_calls: list[tuple[str, dict]] = field(default_factory=list)
    expire_calls: list[tuple[str, int]] = field(default_factory=list)
    delete_calls: list[tuple[str, ...]] = field(default_factory=list)
    unlink_calls: list[tuple[str, ...]] = field(default_factory=list)
    scan_data: list[list[str]] = field(default_factory=list)
    pipeline_pairs: list[tuple[list, list]] = field(default_factory=list)
    raise_on_hgetall: Exception | None = None

    async def hgetall(self, key: str) -> dict:
        if self.raise_on_hgetall is not None:
            raise self.raise_on_hgetall
        return self.hgetall_data.get(key, {})

    async def delete(self, *keys: str) -> int:
        self.delete_calls.append(keys)
        return len(keys)

    async def expire(self, key: str, seconds: int) -> bool:
        self.expire_calls.append((key, seconds))
        return True

    async def unlink(self, *keys: str) -> int:
        self.unlink_calls.append(keys)
        return len(keys)

    def scan_iter(
        self,
        match: str | None = None,
        count: int | None = None,
    ) -> AsyncIterator[str]:
        async def _iter():
            for batch in self.scan_data:
                for k in batch:
                    yield k

        return _iter()

    def pipeline(self, transaction: bool = True) -> "_MockPipeline":
        return _MockPipeline(self)


@dataclass
class _MockPipeline:
    redis: _MockRedisLike
    hset_call: dict | None = None
    expire_call: tuple[str, int] | None = None
    hset_executed: bool = False
    expire_executed: bool = False

    def hset(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        mapping: dict | None = None,
    ) -> "_MockPipeline":
        self.hset_call = mapping or {field: value}
        return self

    def expire(self, key: str, seconds: int) -> "_MockPipeline":
        self.expire_call = (key, seconds)
        return self

    async def execute(self) -> list:
        if self.hset_call is not None:
            self.redis.hset_calls.append(("", self.hset_call))
            self.hset_executed = True
        if self.expire_call is not None:
            self.redis.expire_calls.append(self.expire_call)
            self.expire_executed = True
        return []


# ---------------------------------------------------------------------------
# get / set / delete
# ---------------------------------------------------------------------------


class TestGetMiss:
    @pytest.mark.asyncio
    async def test_returns_none_when_key_absent(self):
        redis = _MockRedisLike()
        adapter = RedisCacheAdapter(redis, prefix="knt:llm:cache")
        result = await adapter.get("missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_decoded_entry(self):
        import time

        redis = _MockRedisLike()
        redis.hgetall_data["knt:llm:cache:k-1"] = {
            "completion": json.dumps({"answer": "42"}),
            "model": "gpt-4",
            "stored_at": repr(time.time()),
            "prompt_tokens": "10",
            "completion_tokens": "5",
            "cost_usd": "0.001",
        }
        adapter = RedisCacheAdapter(redis, prefix="knt:llm:cache")
        result = await adapter.get("k-1")
        assert result is not None
        assert result.model == "gpt-4"
        assert result.completion == {"answer": "42"}
        assert result.cost_usd == 0.001

    @pytest.mark.asyncio
    async def test_propagates_redis_error(self):
        redis = _MockRedisLike(raise_on_hgetall=ConnectionError("redis down"))
        adapter = RedisCacheAdapter(redis, prefix="knt:llm:cache")
        with pytest.raises(ConnectionError):
            await adapter.get("k-1")


class TestSet:
    @pytest.mark.asyncio
    async def test_pipeline_hset_and_expire_when_ttl_set(self):
        redis = _MockRedisLike()
        adapter = RedisCacheAdapter(redis, prefix="knt:llm:cache", ttl_s=3600)
        entry = _CacheEntry(
            completion={"answer": "42"},
            model="gpt-4",
            stored_at=1234.5,
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.001,
        )
        await adapter.set("k-1", entry)
        # The pipeline was used (single round-trip).
        assert len(redis.hset_calls) == 1
        # The EXPIRE was set.
        assert redis.expire_calls == [("knt:llm:cache:k-1", 3600)]

    @pytest.mark.asyncio
    async def test_no_expire_when_ttl_is_none(self):
        redis = _MockRedisLike()
        adapter = RedisCacheAdapter(redis, prefix="knt:llm:cache")
        entry = _CacheEntry(
            completion={"x": 1},
            model="gpt-4",
            stored_at=1234.5,
        )
        await adapter.set("k-1", entry)
        # No EXPIRE was scheduled.
        assert redis.expire_calls == []

    @pytest.mark.asyncio
    async def test_handles_none_cost(self):
        redis = _MockRedisLike()
        adapter = RedisCacheAdapter(redis, prefix="knt:llm:cache", ttl_s=60)
        entry = _CacheEntry(
            completion={"x": 1},
            model="gpt-4",
            stored_at=1234.5,
            cost_usd=None,
        )
        await adapter.set("k-1", entry)
        # cost_usd is "" (None) in the hash.
        assert redis.hset_calls[0][1]["cost_usd"] == ""


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_uses_full_key(self):
        redis = _MockRedisLike()
        adapter = RedisCacheAdapter(redis, prefix="knt:llm:cache")
        await adapter.delete("k-1")
        assert redis.delete_calls == [("knt:llm:cache:k-1",)]


# ---------------------------------------------------------------------------
# clear_prefix / count
# ---------------------------------------------------------------------------


class TestClearPrefix:
    @pytest.mark.asyncio
    async def test_scan_iter_then_unlink(self):
        redis = _MockRedisLike()
        redis.scan_data = [
            ["knt:llm:cache:k-1", "knt:llm:cache:k-2"],
            ["knt:llm:cache:k-3"],
        ]
        adapter = RedisCacheAdapter(redis, prefix="knt:llm:cache")
        await adapter.clear_prefix("k-1|")
        # All 3 keys were unlinked (UNLINK, not DELETE).
        assert redis.unlink_calls == [
            ("knt:llm:cache:k-1",),
            ("knt:llm:cache:k-2",),
            ("knt:llm:cache:k-3",),
        ]

    @pytest.mark.asyncio
    async def test_count_scans_with_prefix(self):
        redis = _MockRedisLike()
        redis.scan_data = [
            ["knt:llm:cache:k-1", "knt:llm:cache:k-2"],
        ]
        adapter = RedisCacheAdapter(redis, prefix="knt:llm:cache")
        n = await adapter.count()
        assert n == 2


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


class TestProtocolSatisfaction:
    def test_satisfies_async_cache_storage(self):
        redis = _MockRedisLike()
        adapter = RedisCacheAdapter(redis, prefix="x")
        assert isinstance(adapter, AsyncCacheStorage)
