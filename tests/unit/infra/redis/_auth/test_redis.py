# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for RedisAPIKeyStorage — Redis impl of APIKeyStorage.

Iteration 3 (ADR-019). All mutating operations return
``Result`` per AGENTS.md §6.

Storage layer:
  - ``lookup(digest) -> Ok(raw_bytes)`` / ``Ok(None)`` /
    ``Err(MemoryError)``
  - ``store(digest, payload) -> Ok(None)`` /
    ``Err(MemoryError)``
  - ``delete(digest) -> Ok(None)`` / ``Err(MemoryError)``

Note: the Redis impl does NOT touch the JSON encoding;
that's the verifier's job (``RedisAPIKeyVerifier``). The
storage is just bytes-in-bytes-out with key prefix
``knt:api:keys:<digest>``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


pytestmark = pytest.mark.asyncio


def _fake_redis():
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    return redis


class TestRedisAPIKeyStorage:
    def test_module_importable(self):
        from kntgraph.infra.redis._auth import RedisAPIKeyStorage

        assert RedisAPIKeyStorage is not None

    def test_implements_api_key_storage(self):
        from kntgraph.infra.redis._auth import (
            APIKeyStorage,
            RedisAPIKeyStorage,
        )

        for name in ("lookup", "store", "delete"):
            assert hasattr(RedisAPIKeyStorage, name), (
                f"RedisAPIKeyStorage must implement {name!r}"
            )
        storage = RedisAPIKeyStorage(client=_fake_redis())
        assert isinstance(storage, APIKeyStorage)

    async def test_lookup_returns_none_on_miss(self):
        from kntgraph.infra.redis._auth import RedisAPIKeyStorage

        redis = _fake_redis()
        redis.get = AsyncMock(return_value=None)
        storage = RedisAPIKeyStorage(client=redis)
        result = await storage.lookup("digest-abc")
        assert result.is_ok()
        assert result.ok_value() is None

    async def test_lookup_returns_raw_bytes(self):
        from kntgraph.infra.redis._auth import RedisAPIKeyStorage

        redis = _fake_redis()
        redis.get = AsyncMock(
            return_value=b'{"agent_id": "tenant-A.a-1", "role": "agent"}'
        )
        storage = RedisAPIKeyStorage(client=redis)
        result = await storage.lookup("digest-abc")
        assert result.is_ok()
        assert result.ok_value() == b'{"agent_id": "tenant-A.a-1", "role": "agent"}'

    async def test_lookup_uses_prefixed_key(self):
        from kntgraph.infra.redis._auth import RedisAPIKeyStorage

        redis = _fake_redis()
        redis.get = AsyncMock(return_value=b"x")
        storage = RedisAPIKeyStorage(client=redis)
        await storage.lookup("digest-abc")
        redis.get.assert_awaited_once_with("knt:api:keys:digest-abc")

    async def test_lookup_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._errors import MemoryError
        from kntgraph.infra.redis._auth import RedisAPIKeyStorage

        redis = _fake_redis()
        redis.get = AsyncMock(side_effect=RuntimeError("redis down"))
        storage = RedisAPIKeyStorage(client=redis)
        result = await storage.lookup("digest-abc")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_store_writes_with_prefixed_key(self):
        from kntgraph.infra.redis._auth import RedisAPIKeyStorage

        redis = _fake_redis()
        storage = RedisAPIKeyStorage(client=redis)
        payload = b'{"agent_id": "tenant-A.a-1"}'
        result = await storage.store("digest-abc", payload)
        assert result.is_ok()
        redis.set.assert_awaited_once_with("knt:api:keys:digest-abc", payload)

    async def test_store_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._errors import MemoryError
        from kntgraph.infra.redis._auth import RedisAPIKeyStorage

        redis = _fake_redis()
        redis.set = AsyncMock(side_effect=RuntimeError("redis down"))
        storage = RedisAPIKeyStorage(client=redis)
        result = await storage.store("digest-abc", b"x")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_delete_removes_key(self):
        from kntgraph.infra.redis._auth import RedisAPIKeyStorage

        redis = _fake_redis()
        storage = RedisAPIKeyStorage(client=redis)
        result = await storage.delete("digest-abc")
        assert result.is_ok()
        redis.delete.assert_awaited_once_with("knt:api:keys:digest-abc")

    async def test_delete_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._errors import MemoryError
        from kntgraph.infra.redis._auth import RedisAPIKeyStorage

        redis = _fake_redis()
        redis.delete = AsyncMock(side_effect=RuntimeError("redis down"))
        storage = RedisAPIKeyStorage(client=redis)
        result = await storage.delete("digest-abc")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)
