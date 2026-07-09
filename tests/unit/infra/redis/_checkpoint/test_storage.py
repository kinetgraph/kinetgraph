# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for RedisCheckpointStorage — Redis impl of CheckpointStorage.

Iteration 4 (ADR-019). The storage uses a single Redis Hash
``knt:reactive:checkpoints`` with one field per agent. The
payload is JSON-encoded by the storage (the protocol is
storage-format-agnostic, but the checkpoint is always
JSON in practice — see ``ReactiveCheckpoint.to_dict``).

Result contract (AGENTS.md §6): see ``CheckpointStorage``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


pytestmark = pytest.mark.asyncio


def _fake_redis():
    redis = MagicMock()
    redis.hget = AsyncMock(return_value=None)
    redis.hset = AsyncMock(return_value=1)
    redis.hgetall = AsyncMock(return_value={})
    redis.hdel = AsyncMock(return_value=1)
    redis.delete = AsyncMock(return_value=1)
    return redis


SAMPLE_PAYLOAD = {
    "last_event_id": "550e8400-e29b-41d4-a716-446655440000",
    "last_stream_id": "1-0",
    "confirmed_at": "2026-01-01T00:00:00+00:00",
    "state_hash": None,
}


class TestRedisCheckpointStorage:
    def test_module_importable(self):
        from kntgraph.infra.redis._checkpoint import RedisCheckpointStorage

        assert RedisCheckpointStorage is not None

    def test_implements_checkpoint_storage(self):
        from kntgraph.infra.redis._checkpoint import (
            CheckpointStorage,
            RedisCheckpointStorage,
        )

        for name in ("load", "save", "load_all", "clear", "clear_all"):
            assert hasattr(RedisCheckpointStorage, name), (
                f"RedisCheckpointStorage must implement {name!r}"
            )
        storage = RedisCheckpointStorage(client=_fake_redis())
        assert isinstance(storage, CheckpointStorage)

    async def test_load_returns_none_on_miss(self):
        from kntgraph.infra.redis._checkpoint import RedisCheckpointStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(return_value=None)
        storage = RedisCheckpointStorage(client=redis)
        result = await storage.load("agent-1")
        assert result.is_ok()
        assert result.ok_value() is None

    async def test_load_returns_mapping_on_hit(self):
        from kntgraph.infra.redis._checkpoint import RedisCheckpointStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(return_value=json.dumps(SAMPLE_PAYLOAD).encode())
        storage = RedisCheckpointStorage(client=redis)
        result = await storage.load("agent-1")
        assert result.is_ok()
        assert result.ok_value() == SAMPLE_PAYLOAD

    async def test_load_uses_single_hash_with_agent_field(self):
        from kntgraph.infra.redis._checkpoint import (
            CHECKPOINT_KEY,
            RedisCheckpointStorage,
        )

        redis = _fake_redis()
        storage = RedisCheckpointStorage(client=redis)
        await storage.load("agent-1")
        redis.hget.assert_awaited_once_with(CHECKPOINT_KEY, "agent-1")

    async def test_load_returns_err_on_invalid_json(self):
        from kntgraph.infra.redis._errors import MemoryDecodeError
        from kntgraph.infra.redis._checkpoint import RedisCheckpointStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(return_value=b"not-json")
        storage = RedisCheckpointStorage(client=redis)
        result = await storage.load("agent-1")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryDecodeError)

    async def test_load_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._errors import MemoryError
        from kntgraph.infra.redis._checkpoint import RedisCheckpointStorage

        redis = _fake_redis()
        redis.hget = AsyncMock(side_effect=RuntimeError("redis down"))
        storage = RedisCheckpointStorage(client=redis)
        result = await storage.load("agent-1")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_save_writes_json_payload(self):
        from kntgraph.infra.redis._checkpoint import (
            CHECKPOINT_KEY,
            RedisCheckpointStorage,
        )

        redis = _fake_redis()
        storage = RedisCheckpointStorage(client=redis)
        result = await storage.save("agent-1", SAMPLE_PAYLOAD)
        assert result.is_ok()
        redis.hset.assert_awaited_once()
        args, _ = redis.hset.await_args
        assert args[0] == CHECKPOINT_KEY
        assert args[1] == "agent-1"
        payload = json.loads(args[2])
        assert payload == SAMPLE_PAYLOAD

    async def test_save_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._errors import MemoryError
        from kntgraph.infra.redis._checkpoint import RedisCheckpointStorage

        redis = _fake_redis()
        redis.hset = AsyncMock(side_effect=RuntimeError("redis down"))
        storage = RedisCheckpointStorage(client=redis)
        result = await storage.save("agent-1", SAMPLE_PAYLOAD)
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_load_all_returns_dict(self):
        from kntgraph.infra.redis._checkpoint import RedisCheckpointStorage

        redis = _fake_redis()
        redis.hgetall = AsyncMock(
            return_value={
                b"agent-1": json.dumps(SAMPLE_PAYLOAD).encode(),
                b"agent-2": json.dumps(
                    {**SAMPLE_PAYLOAD, "last_event_id": "deadbeef"}
                ).encode(),
            }
        )
        storage = RedisCheckpointStorage(client=redis)
        result = await storage.load_all()
        assert result.is_ok()
        checkpoints = result.ok_value()
        assert "agent-1" in checkpoints
        assert "agent-2" in checkpoints
        assert checkpoints["agent-1"]["last_stream_id"] == "1-0"

    async def test_load_all_skips_malformed_entries(self):
        """Malformed entries are skipped, not failed."""
        from kntgraph.infra.redis._checkpoint import RedisCheckpointStorage

        redis = _fake_redis()
        redis.hgetall = AsyncMock(
            return_value={
                b"agent-1": json.dumps(SAMPLE_PAYLOAD).encode(),
                b"agent-bad": b"not-json",
            }
        )
        storage = RedisCheckpointStorage(client=redis)
        result = await storage.load_all()
        assert result.is_ok()
        checkpoints = result.ok_value()
        assert "agent-1" in checkpoints
        assert "agent-bad" not in checkpoints

    async def test_clear_removes_field(self):
        from kntgraph.infra.redis._checkpoint import (
            CHECKPOINT_KEY,
            RedisCheckpointStorage,
        )

        redis = _fake_redis()
        storage = RedisCheckpointStorage(client=redis)
        result = await storage.clear("agent-1")
        assert result.is_ok()
        redis.hdel.assert_awaited_once_with(CHECKPOINT_KEY, "agent-1")

    async def test_clear_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._errors import MemoryError
        from kntgraph.infra.redis._checkpoint import RedisCheckpointStorage

        redis = _fake_redis()
        redis.hdel = AsyncMock(side_effect=RuntimeError("redis down"))
        storage = RedisCheckpointStorage(client=redis)
        result = await storage.clear("agent-1")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_clear_all_deletes_key(self):
        from kntgraph.infra.redis._checkpoint import (
            CHECKPOINT_KEY,
            RedisCheckpointStorage,
        )

        redis = _fake_redis()
        storage = RedisCheckpointStorage(client=redis)
        result = await storage.clear_all()
        assert result.is_ok()
        redis.delete.assert_awaited_once_with(CHECKPOINT_KEY)

    async def test_clear_all_returns_err_on_redis_failure(self):
        from kntgraph.infra.redis._errors import MemoryError
        from kntgraph.infra.redis._checkpoint import RedisCheckpointStorage

        redis = _fake_redis()
        redis.delete = AsyncMock(side_effect=RuntimeError("redis down"))
        storage = RedisCheckpointStorage(client=redis)
        result = await storage.clear_all()
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)
