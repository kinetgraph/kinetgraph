# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for RedisSessionStorage — JSON-backed memory cache.

Iteration 2 (ADR-019). Session storage uses ``SET key value
EX ttl`` (JSON-encoded payload).

All mutating operations return ``Result[Mapping, MemoryError]``
per AGENTS.md §6.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


pytestmark = pytest.mark.asyncio


def _fake_redis():
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)

    async def fake_scan(match, count=None):
        for _ in []:
            yield _

    redis.scan_iter = MagicMock(
        side_effect=lambda match, count=None: fake_scan(match, count)
    )
    return redis


class TestRedisSessionStorage:
    def test_module_importable(self):
        from kntgraph.infra.redis._memory import RedisSessionStorage

        assert RedisSessionStorage is not None

    def test_redis_session_storage_implements_memory_storage(self):
        from kntgraph.infra.redis._memory import (
            ShortMemoryStorage,
            RedisSessionStorage,
        )

        for name in ("get_record", "put_record", "delete_record", "iter_keys"):
            assert hasattr(RedisSessionStorage, name), (
                f"RedisSessionStorage must implement {name!r}"
            )
        storage = RedisSessionStorage(client=_fake_redis(), ttl_seconds=3600)
        assert isinstance(storage, ShortMemoryStorage)
        assert callable(storage.get_record)
        assert callable(storage.put_record)

    async def test_get_record_returns_none_on_miss(self):
        """Cache miss surfaces as ``Err(MemoryMiss)`` (not ``Ok(None)``).

        Per the ``ShortMemoryStorage`` Protocol contract,
        miss and hit are modelled as distinct error types
        so callers can dispatch on ``isinstance`` instead
        of checking ``is None`` on the success channel.
        See ``kntgraph.infra.redis._errors.MemoryMiss``.
        """
        from kntgraph.infra.redis._memory import RedisSessionStorage
        from kntgraph.infra.redis._errors import MemoryMiss

        redis = _fake_redis()
        storage = RedisSessionStorage(client=redis)
        result = await storage.get_record("knt:session:abc")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryMiss)
        assert result.err_value().key == "knt:session:abc"

    async def test_get_record_returns_parsed_mapping(self):
        from kntgraph.infra.redis._memory import RedisSessionStorage

        redis = _fake_redis()
        redis.get = AsyncMock(
            return_value=json.dumps({"session_id": "abc", "messages": ["hi"]}).encode()
        )
        storage = RedisSessionStorage(client=redis)
        result = await storage.get_record("knt:session:abc")
        assert result.is_ok()
        assert result.ok_value() == {"session_id": "abc", "messages": ["hi"]}

    async def test_get_record_returns_err_on_invalid_json(self):
        from kntgraph.infra.redis._errors import MemoryDecodeError
        from kntgraph.infra.redis._memory import RedisSessionStorage

        redis = _fake_redis()
        redis.get = AsyncMock(return_value=b"not-json")
        storage = RedisSessionStorage(client=redis)
        result = await storage.get_record("knt:session:abc")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryDecodeError)

    async def test_get_record_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._errors import MemoryError
        from kntgraph.infra.redis._memory import RedisSessionStorage

        redis = _fake_redis()
        redis.get = AsyncMock(side_effect=RuntimeError("redis down"))
        storage = RedisSessionStorage(client=redis)
        result = await storage.get_record("knt:session:abc")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_put_record_returns_ok(self):
        from kntgraph.infra.redis._memory import RedisSessionStorage

        redis = _fake_redis()
        storage = RedisSessionStorage(client=redis, ttl_seconds=60)
        result = await storage.put_record(
            "knt:session:abc",
            {"session_id": "abc", "messages": ["hi"]},
            ttl_seconds=60,
        )
        assert result.is_ok()
        redis.set.assert_awaited_once()
        args, kwargs = redis.set.await_args
        assert args[0] == "knt:session:abc"
        payload = json.loads(args[1])
        assert payload == {"session_id": "abc", "messages": ["hi"]}
        assert kwargs.get("ex") == 60

    async def test_put_record_no_ttl(self):
        from kntgraph.infra.redis._memory import RedisSessionStorage

        redis = _fake_redis()
        storage = RedisSessionStorage(client=redis)
        result = await storage.put_record("knt:session:abc", {"k": "v"})
        assert result.is_ok()
        args, kwargs = redis.set.await_args
        assert kwargs.get("ex") is None

    async def test_put_record_returns_err_on_serialization_failure(self):
        from kntgraph.infra.redis._errors import MemorySerializationError
        from kntgraph.infra.redis._memory import RedisSessionStorage

        redis = _fake_redis()
        storage = RedisSessionStorage(client=redis)
        # A circular reference breaks json.dumps.
        circular: dict = {}
        circular["self"] = circular
        result = await storage.put_record("knt:session:abc", circular)
        assert result.is_err()
        assert isinstance(result.err_value(), MemorySerializationError)

    async def test_delete_record_returns_ok(self):
        from kntgraph.infra.redis._memory import RedisSessionStorage

        redis = _fake_redis()
        storage = RedisSessionStorage(client=redis)
        result = await storage.delete_record("knt:session:abc")
        assert result.is_ok()
        redis.delete.assert_awaited_once_with("knt:session:abc")

    async def test_iter_keys_yields_with_prefix(self):
        from kntgraph.infra.redis._memory import RedisSessionStorage

        redis = _fake_redis()

        async def fake_scan(match, count=None):
            for k in [
                b"knt:session:s-1",
                b"knt:session:s-2",
                b"knt:profile:t1:u1",  # should NOT match
            ]:
                yield k

        redis.scan_iter = MagicMock(
            side_effect=lambda match, count=None: fake_scan(match, count)
        )
        storage = RedisSessionStorage(client=redis)
        keys = []
        async for k in storage.iter_keys("knt:session:"):
            keys.append(k)
        assert keys == ["knt:session:s-1", "knt:session:s-2"]
