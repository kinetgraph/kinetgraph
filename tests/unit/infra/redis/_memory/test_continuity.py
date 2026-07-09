# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for RedisContinuityStorage — Hash-backed with sliding TTL.

Iteration 2 (ADR-019). Continuity storage is similar to
Profile (Hash), but the TTL is sliding (renewed on every
``put_record``).

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
    redis.delete = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)

    async def fake_scan(match, count=None):
        for _ in []:
            yield _

    redis.scan_iter = MagicMock(
        side_effect=lambda match, count=None: fake_scan(match, count)
    )
    return redis


class TestRedisContinuityStorage:
    def test_module_importable(self):
        from kntgraph.infra.redis._memory import RedisContinuityStorage

        assert RedisContinuityStorage is not None

    async def test_get_record_returns_mapping(self):
        from kntgraph.infra.redis._memory import RedisContinuityStorage

        redis = _fake_redis()
        redis.hgetall = AsyncMock(
            return_value={
                b"last_tool": b"city.lookup",
                b"last_seen": b"1234567890",
            }
        )
        storage = RedisContinuityStorage(client=redis)
        result = await storage.get_record("knt:continuity:t1:u1")
        assert result.is_ok()
        assert result.ok_value() == {
            "last_tool": "city.lookup",
            "last_seen": "1234567890",
        }

    async def test_get_record_returns_none_on_miss(self):
        """Cache miss surfaces as ``Err(MemoryMiss)`` (not ``Ok(None)``).

        Per the ``ShortMemoryStorage`` Protocol contract,
        miss and hit are modelled as distinct error types
        so callers can dispatch on ``isinstance`` instead
        of checking ``is None`` on the success channel.
        See ``kntgraph.infra.redis._errors.MemoryMiss``.
        """
        from kntgraph.infra.redis._memory import RedisContinuityStorage
        from kntgraph.infra.redis._errors import MemoryMiss

        redis = _fake_redis()
        redis.hgetall = AsyncMock(return_value={})
        storage = RedisContinuityStorage(client=redis)
        result = await storage.get_record("knt:continuity:t1:u1")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryMiss)
        assert result.err_value().key == "knt:continuity:t1:u1"

    async def test_put_record_renews_sliding_ttl(self):
        """Continuity cache has a sliding TTL: every write resets EXPIRE."""
        from kntgraph.infra.redis._memory import RedisContinuityStorage

        redis = _fake_redis()
        pipe = MagicMock()
        pipe.delete = MagicMock(return_value=pipe)
        pipe.hset = MagicMock(return_value=pipe)
        pipe.expire = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=[1, 1, True])
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=None)
        redis.pipeline = MagicMock(return_value=pipe)

        storage = RedisContinuityStorage(client=redis, ttl_seconds=1800)
        result = await storage.put_record(
            "knt:continuity:t1:u1",
            {"last_tool": "city.lookup"},
            ttl_seconds=1800,
        )
        assert result.is_ok()
        pipe.expire.assert_called_once_with("knt:continuity:t1:u1", 1800)

    async def test_delete_record_returns_ok(self):
        from kntgraph.infra.redis._memory import RedisContinuityStorage

        redis = _fake_redis()
        storage = RedisContinuityStorage(client=redis)
        result = await storage.delete_record("knt:continuity:t1:u1")
        assert result.is_ok()
        redis.delete.assert_awaited_once_with("knt:continuity:t1:u1")
