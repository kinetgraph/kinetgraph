# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``infra/redis/_memory/_profile.py``
(``RedisProfileStorage``).

Closes the infra/redis/_memory/_profile coverage gap
(DEBT §3, 85% → 100%). The module is structurally
identical to ``_continuity.py`` (Hash-backed cache +
``DEL + HSET + EXPIRE`` pipeline), but:
  - The default ``ttl_seconds`` is ``None`` (long-lived
    profiles, no TTL by default — unlike the
    sliding-TTL continuity storage).
  - ``EXPIRE`` is only called when the effective
    ``ttl_seconds`` is set.
  - The ``delete_record`` deletes the key outright
    (no per-reason support).
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.infra.redis._errors import (
    MemoryError,
    MemoryMiss,
    MemorySerializationError,
)
from kntgraph.infra.redis._memory._profile import (
    RedisProfileStorage,
)


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> MagicMock:
    client = MagicMock()
    client.hgetall = AsyncMock(return_value={})
    client.delete = AsyncMock(return_value=1)
    client.scan_iter = MagicMock()
    return client


@pytest.fixture
def storage(client: MagicMock) -> RedisProfileStorage:
    # Default ttl_seconds=None (long-lived profile).
    return RedisProfileStorage(client=client)


@pytest.fixture
def dataclass_record():
    """A non-Mapping ``record`` (frozen dataclass) that
    the codec must coerce to an empty dict."""

    @dataclasses.dataclass(frozen=True)
    class _NotAMapping:
        x: int = 1

    return _NotAMapping()


# ---------------------------------------------------------------------------
# get_record
# ---------------------------------------------------------------------------


class TestGetRecord:
    async def test_returns_mapping_on_hit(
        self, storage: RedisProfileStorage, client: MagicMock
    ) -> None:
        client.hgetall = AsyncMock(return_value={b"k": b"v"})
        result = await storage.get_record("key")
        assert result.is_ok()
        assert result.ok_value() == {"k": "v"}

    async def test_returns_miss_on_empty_hash(
        self, storage: RedisProfileStorage, client: MagicMock
    ) -> None:
        client.hgetall = AsyncMock(return_value={})
        result = await storage.get_record("key")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryMiss)

    async def test_returns_err_on_redis_failure(
        self, storage: RedisProfileStorage, client: MagicMock
    ) -> None:
        client.hgetall = AsyncMock(side_effect=ConnectionError("redis down"))
        result = await storage.get_record("key")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)


# ---------------------------------------------------------------------------
# put_record
# ---------------------------------------------------------------------------


class TestPutRecord:
    async def test_puts_without_ttl_by_default(
        self, storage: RedisProfileStorage, client: MagicMock
    ) -> None:
        pipe = MagicMock()
        pipe.delete = MagicMock()
        pipe.hset = MagicMock()
        pipe.expire = MagicMock()
        pipe.execute = AsyncMock()
        client.pipeline = MagicMock(return_value=pipe)

        result = await storage.put_record("key", {"k": "v"})
        assert result.is_ok()
        pipe.delete.assert_called_once_with("key")
        # ttl_seconds=None by default → expire NOT called.
        pipe.expire.assert_not_called()
        # The mapping is forwarded to HSET.
        hset_calls = pipe.hset.call_args_list
        assert len(hset_calls) == 1
        assert hset_calls[0].kwargs == {"mapping": {"k": "v"}}
        pipe.execute.assert_awaited_once()

    async def test_overrides_ttl_with_kwarg(
        self, storage: RedisProfileStorage, client: MagicMock
    ) -> None:
        pipe = MagicMock()
        pipe.delete = MagicMock()
        pipe.hset = MagicMock()
        pipe.expire = MagicMock()
        pipe.execute = AsyncMock()
        client.pipeline = MagicMock(return_value=pipe)

        await storage.put_record("key", {"k": "v"}, ttl_seconds=60)
        pipe.expire.assert_called_once_with("key", 60)

    async def test_non_mapping_record_falls_back_to_empty(
        self,
        storage: RedisProfileStorage,
        client: MagicMock,
        dataclass_record,
    ) -> None:
        pipe = MagicMock()
        pipe.delete = MagicMock()
        pipe.hset = MagicMock()
        pipe.expire = MagicMock()
        pipe.execute = AsyncMock()
        client.pipeline = MagicMock(return_value=pipe)

        result = await storage.put_record("key", dataclass_record)
        assert result.is_ok()
        hset_calls = pipe.hset.call_args_list
        assert len(hset_calls) == 1
        assert hset_calls[0].kwargs == {"mapping": {}}

    async def test_returns_serialization_err_when_items_fails(
        self, storage: RedisProfileStorage, client: MagicMock
    ) -> None:
        class _BadMapping(dict):
            def items(self):
                raise ValueError("boom")

        result = await storage.put_record("key", _BadMapping())
        assert result.is_err()
        assert isinstance(result.err_value(), MemorySerializationError)

    async def test_returns_err_on_redis_failure(
        self, storage: RedisProfileStorage, client: MagicMock
    ) -> None:
        client.pipeline = MagicMock(side_effect=ConnectionError("redis down"))
        result = await storage.put_record("key", {"k": "v"})
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)


# ---------------------------------------------------------------------------
# delete_record
# ---------------------------------------------------------------------------


class TestDeleteRecord:
    async def test_deletes_key(
        self, storage: RedisProfileStorage, client: MagicMock
    ) -> None:
        client.delete = AsyncMock(return_value=1)
        result = await storage.delete_record("key")
        assert result.is_ok()
        client.delete.assert_awaited_once_with("key")

    async def test_returns_err_on_redis_failure(
        self, storage: RedisProfileStorage, client: MagicMock
    ) -> None:
        client.delete = AsyncMock(side_effect=ConnectionError("redis down"))
        result = await storage.delete_record("key")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)


# ---------------------------------------------------------------------------
# iter_keys
# ---------------------------------------------------------------------------


class TestIterKeys:
    async def test_iterates_keys_with_prefix(
        self, storage: RedisProfileStorage, client: MagicMock
    ) -> None:
        async def fake_iter(*args, **kwargs):
            for key in [
                b"knt:profile:t1:u1",
                b"knt:profile:t1:u2",
                b"other:key",
            ]:
                yield key

        client.scan_iter = fake_iter
        keys = []
        async for key in storage.iter_keys("knt:profile:"):
            keys.append(key)
        assert keys == [
            "knt:profile:t1:u1",
            "knt:profile:t1:u2",
        ]
