# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for RedisProfileStorage — Hash-backed memory cache.

Iteration 2 (ADR-019). Profile storage uses ``HSET key
field value`` + ``HGETALL key``.

All mutating operations return ``Result[Mapping, MemoryError]``
per AGENTS.md §6.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


pytestmark = pytest.mark.asyncio


def _fake_redis():
    redis = MagicMock()
    redis.hgetall = AsyncMock(return_value={})
    redis.hset = AsyncMock(return_value=1)
    redis.delete = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)
    redis.hdel = AsyncMock(return_value=1)

    async def fake_scan(match, count=None):
        for _ in []:
            yield _

    redis.scan_iter = MagicMock(
        side_effect=lambda match, count=None: fake_scan(match, count)
    )
    return redis


class TestRedisProfileStorage:
    def test_module_importable(self):
        from kntgraph.infra.redis._memory import RedisProfileStorage

        assert RedisProfileStorage is not None

    async def test_get_record_returns_mapping(self):
        from kntgraph.infra.redis._memory import RedisProfileStorage

        redis = _fake_redis()
        redis.hgetall = AsyncMock(
            return_value={
                b"tier": b"vip",
                b"created_at": b"1234567890",
                b"updated_at": b"1234567890",
            }
        )
        storage = RedisProfileStorage(client=redis)
        result = await storage.get_record("fmh:profile:t1:u1")
        assert result.is_ok()
        assert result.ok_value() == {
            "tier": "vip",
            "created_at": "1234567890",
            "updated_at": "1234567890",
        }

    async def test_get_record_returns_none_on_miss(self):
        """Cache miss surfaces as ``Err(MemoryMiss)`` (not ``Ok(None)``).

        Per the ``ShortMemoryStorage`` Protocol contract,
        miss and hit are modelled as distinct error types
        so callers can dispatch on ``isinstance`` instead
        of checking ``is None`` on the success channel.
        See ``kntgraph.infra.redis._errors.MemoryMiss``.
        """
        from kntgraph.infra.redis._memory import RedisProfileStorage
        from kntgraph.infra.redis._errors import MemoryMiss

        redis = _fake_redis()
        redis.hgetall = AsyncMock(return_value={})
        storage = RedisProfileStorage(client=redis)
        result = await storage.get_record("fmh:profile:t1:u1")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryMiss)
        assert result.err_value().key == "fmh:profile:t1:u1"

    async def test_put_record_returns_ok(self):
        from kntgraph.infra.redis._memory import RedisProfileStorage

        redis = _fake_redis()

        pipe = MagicMock()
        pipe.delete = MagicMock(return_value=pipe)
        pipe.hset = MagicMock(return_value=pipe)
        pipe.expire = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=[1, 1, True])
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=None)
        redis.pipeline = MagicMock(return_value=pipe)

        storage = RedisProfileStorage(client=redis)
        result = await storage.put_record(
            "fmh:profile:t1:u1",
            {"tier": "vip", "pref:lang": "pt-BR"},
            ttl_seconds=None,
        )
        assert result.is_ok()
        pipe.delete.assert_called_once_with("fmh:profile:t1:u1")
        pipe.hset.assert_called_once()
        pipe.execute.assert_awaited_once()

    async def test_put_record_with_ttl_sets_expire(self):
        from kntgraph.infra.redis._memory import RedisProfileStorage

        redis = _fake_redis()
        pipe = MagicMock()
        pipe.delete = MagicMock(return_value=pipe)
        pipe.hset = MagicMock(return_value=pipe)
        pipe.expire = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=[1, 1, True])
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=None)
        redis.pipeline = MagicMock(return_value=pipe)

        storage = RedisProfileStorage(client=redis, ttl_seconds=3600)
        result = await storage.put_record(
            "fmh:profile:t1:u1",
            {"tier": "vip"},
            ttl_seconds=3600,
        )
        assert result.is_ok()
        pipe.expire.assert_called_once_with("fmh:profile:t1:u1", 3600)

    async def test_put_record_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._errors import MemoryError
        from kntgraph.infra.redis._memory import RedisProfileStorage

        redis = _fake_redis()
        redis.pipeline = MagicMock(side_effect=RuntimeError("pipeline broken"))
        storage = RedisProfileStorage(client=redis)
        result = await storage.put_record("fmh:profile:t1:u1", {"tier": "vip"})
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_delete_record_returns_ok(self):
        from kntgraph.infra.redis._memory import RedisProfileStorage

        redis = _fake_redis()
        storage = RedisProfileStorage(client=redis)
        result = await storage.delete_record("fmh:profile:t1:u1")
        assert result.is_ok()
        redis.delete.assert_awaited_once_with("fmh:profile:t1:u1")

    async def test_iter_keys_yields_with_prefix(self):
        from kntgraph.infra.redis._memory import RedisProfileStorage

        redis = _fake_redis()

        async def fake_scan(match, count=None):
            for k in [
                b"fmh:profile:t1:u1",
                b"fmh:profile:t1:u2",
                b"fmh:session:s-1",  # should NOT match
            ]:
                yield k

        redis.scan_iter = MagicMock(
            side_effect=lambda match, count=None: fake_scan(match, count)
        )
        storage = RedisProfileStorage(client=redis)
        keys = []
        async for k in storage.iter_keys("fmh:profile:t1:"):
            keys.append(k)
        assert keys == ["fmh:profile:t1:u1", "fmh:profile:t1:u2"]
