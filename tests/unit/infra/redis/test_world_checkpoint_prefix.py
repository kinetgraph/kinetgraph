# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for RedisWorldCheckpointStorage ADR-076 key prefixing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.infra.redis._world_checkpoint import (
    RedisWorldCheckpointStorage,
    cursor_key,
    storage_key,
)


def _fake_redis() -> MagicMock:
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.unlink = AsyncMock(return_value=1)

    pipe = MagicMock()
    pipe.set = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[True, True])
    redis.pipeline = MagicMock(return_value=pipe)
    return redis


class TestStorageKeyHelpers:
    def test_empty_prefix_returns_unprefixed_key(self) -> None:
        assert storage_key("", "agent-1") == "knt:world:agent-1"
        assert cursor_key("", "agent-1") == "knt:world-cursor:agent-1"

    def test_prefix_with_trailing_colon(self) -> None:
        assert storage_key("acme:", "agent-1") == "acme:knt:world:agent-1"
        assert cursor_key("acme:", "agent-1") == "acme:knt:world-cursor:agent-1"

    def test_single_argument_backward_compat(self) -> None:
        assert storage_key("agent-1") == "knt:world:agent-1"
        assert cursor_key("agent-1") == "knt:world-cursor:agent-1"


@pytest.mark.asyncio
class TestRedisWorldCheckpointStoragePrefix:
    async def test_invalid_prefix_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="redis_key_prefix"):
            RedisWorldCheckpointStorage(client=_fake_redis(), key_prefix="acme:{")

    async def test_instance_storage_key_uses_configured_prefix(self) -> None:
        storage = RedisWorldCheckpointStorage(client=_fake_redis(), key_prefix="acme:")
        assert storage.storage_key("agent-1") == "acme:knt:world:agent-1"
        assert storage.cursor_key("agent-1") == "acme:knt:world-cursor:agent-1"

    async def test_prefixed_load_uses_namespaced_key(self) -> None:
        redis = _fake_redis()
        storage = RedisWorldCheckpointStorage(client=redis, key_prefix="acme:")
        await storage.load("agent-1")
        redis.get.assert_awaited_once_with("acme:knt:world:agent-1")

    async def test_prefixed_load_cursor_uses_namespaced_key(self) -> None:
        redis = _fake_redis()
        storage = RedisWorldCheckpointStorage(client=redis, key_prefix="acme:")
        await storage.load_cursor("agent-1")
        redis.get.assert_awaited_once_with("acme:knt:world-cursor:agent-1")

    async def test_prefixed_save_with_cursor_uses_pipeline_namespaced_keys(
        self,
    ) -> None:
        redis = _fake_redis()
        storage = RedisWorldCheckpointStorage(client=redis, key_prefix="acme:")
        await storage.save("agent-1", b"payload", cursor="10-0")

        pipe = redis.pipeline.return_value
        pipe.set.assert_any_call("acme:knt:world:agent-1", b"payload", ex=None)
        pipe.set.assert_any_call("acme:knt:world-cursor:agent-1", b"10-0", ex=None)

    async def test_prefixed_discard_uses_namespaced_keys(self) -> None:
        redis = _fake_redis()
        storage = RedisWorldCheckpointStorage(client=redis, key_prefix="acme:")
        await storage.discard("agent-1")
        redis.unlink.assert_awaited_once_with(
            "acme:knt:world:agent-1", "acme:knt:world-cursor:agent-1"
        )


@pytest.mark.asyncio
class TestReactiveDispatcherPrefixWiring:
    async def test_dispatcher_passes_key_prefix_to_default_world_store(self) -> None:
        from kntgraph.runner.reactive import ReactiveDispatcher

        log = MagicMock()
        redis = _fake_redis()

        dispatcher = ReactiveDispatcher(log=log, redis=redis, key_prefix="tenant-a:")
        storage = getattr(dispatcher._world_store, "_storage", None)
        assert storage is not None
        assert getattr(storage, "key_prefix", None) == "tenant-a:"
