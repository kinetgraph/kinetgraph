# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``infra/redis/_memory/_continuity.py``
(``RedisContinuityStorage``).

Closes the infra/redis/_memory/_continuity coverage gap
(DEBT §3, 80% → 100%). The ``RedisContinuityStorage``
is the Hash-backed cache with sliding TTL that backs
the continuity-manager. The public surface is:

  - ``get_record`` — reads the Hash via ``HGETALL``;
    returns ``Err(MemoryMiss)`` on an empty hash and
    ``Err(MemoryError)`` on a Redis failure.
  - ``put_record`` — writes the Hash via a
    transaction pipeline (``DEL`` + ``HSET`` +
    ``EXPIRE``); the ``EXPIRE`` is the sliding TTL
    (the cache stays warm as long as the user is
    active).
  - ``delete_record`` — ``DEL``s the key.
  - ``iter_keys`` — ``SCAN`` with the given prefix.

The uncovered branches were the four error paths
(Redis exception on each I/O method) and the
non-Mapping ``record`` arg (the storage falls back
to an empty dict rather than raising).
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
from kntgraph.infra.redis._memory._continuity import (
    RedisContinuityStorage,
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
def storage(client: MagicMock) -> RedisContinuityStorage:
    return RedisContinuityStorage(client=client, ttl_seconds=3600)


@pytest.fixture
def dataclass_record():
    """A non-Mapping ``record`` (frozen dataclass) that
    the codec must coerce to an empty dict (the
    ``isinstance(record, Mapping)`` branch)."""

    @dataclasses.dataclass(frozen=True)
    class _NotAMapping:
        x: int = 1

    return _NotAMapping()


# ---------------------------------------------------------------------------
# get_record
# ---------------------------------------------------------------------------


class TestGetRecord:
    async def test_returns_mapping_on_hit(
        self, storage: RedisContinuityStorage, client: MagicMock
    ) -> None:
        client.hgetall = AsyncMock(return_value={b"k": b"v"})
        result = await storage.get_record("key")
        assert result.is_ok()
        assert result.ok_value() == {"k": "v"}

    async def test_returns_miss_on_empty_hash(
        self, storage: RedisContinuityStorage, client: MagicMock
    ) -> None:
        client.hgetall = AsyncMock(return_value={})
        result = await storage.get_record("key")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryMiss)

    async def test_returns_err_on_redis_failure(
        self, storage: RedisContinuityStorage, client: MagicMock
    ) -> None:
        client.hgetall = AsyncMock(side_effect=ConnectionError("redis down"))
        result = await storage.get_record("key")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)
        assert "redis error" in str(result.err_value())


# ---------------------------------------------------------------------------
# put_record
# ---------------------------------------------------------------------------


class TestPutRecord:
    async def test_puts_with_sliding_ttl(
        self, storage: RedisContinuityStorage, client: MagicMock
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
        # The adapter passes ``key`` as a keyword arg.
        # We assert on the call record directly to
        # avoid the MagicMock repr asymmetry.
        hset_calls = pipe.hset.call_args_list
        assert len(hset_calls) == 1
        assert hset_calls[0].kwargs == {"mapping": {"k": "v"}}
        pipe.expire.assert_called_once_with("key", 3600)
        pipe.execute.assert_awaited_once()

    async def test_puts_without_ttl(self, client: MagicMock) -> None:
        storage = RedisContinuityStorage(client=client, ttl_seconds=None)
        pipe = MagicMock()
        pipe.delete = MagicMock()
        pipe.hset = MagicMock()
        pipe.expire = MagicMock()
        pipe.execute = AsyncMock()
        client.pipeline = MagicMock(return_value=pipe)

        await storage.put_record("key", {"k": "v"})
        pipe.expire.assert_not_called()

    async def test_overrides_ttl_with_kwarg(
        self, storage: RedisContinuityStorage, client: MagicMock
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
        storage: RedisContinuityStorage,
        client: MagicMock,
        dataclass_record,
    ) -> None:
        pipe = MagicMock()
        pipe.delete = MagicMock()
        pipe.hset = MagicMock()
        pipe.expire = MagicMock()
        pipe.execute = AsyncMock()
        client.pipeline = MagicMock(return_value=pipe)

        # A non-Mapping record is silently coerced to an
        # empty dict (defensive — the codec never sends
        # non-Mapping, but the storage is forgiving).
        result = await storage.put_record("key", dataclass_record)
        assert result.is_ok()
        hset_calls = pipe.hset.call_args_list
        assert len(hset_calls) == 1
        assert hset_calls[0].kwargs == {"mapping": {}}

    async def test_returns_err_on_redis_failure(
        self, storage: RedisContinuityStorage, client: MagicMock
    ) -> None:
        client.pipeline = MagicMock(side_effect=ConnectionError("redis down"))
        result = await storage.put_record("key", {"k": "v"})
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_returns_serialization_err_when_items_fails(
        self, storage: RedisContinuityStorage, client: MagicMock
    ) -> None:
        # A Mapping whose ``items()`` raises when
        # iterated surfaces as ``MemorySerializationError``
        # (the storage is defensive — the codec should
        # never send us such a payload, but the
        # storage safeguards against the case).
        class _BadMapping(dict):
            def items(self):
                raise ValueError("boom")

        result = await storage.put_record("key", _BadMapping())
        assert result.is_err()
        assert isinstance(result.err_value(), MemorySerializationError)


# ---------------------------------------------------------------------------
# delete_record
# ---------------------------------------------------------------------------


class TestDeleteRecord:
    async def test_deletes_key(
        self, storage: RedisContinuityStorage, client: MagicMock
    ) -> None:
        client.delete = AsyncMock(return_value=1)
        result = await storage.delete_record("key")
        assert result.is_ok()
        client.delete.assert_awaited_once_with("key")

    async def test_returns_err_on_redis_failure(
        self, storage: RedisContinuityStorage, client: MagicMock
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
        self, storage: RedisContinuityStorage, client: MagicMock
    ) -> None:
        async def fake_iter(*args, **kwargs):
            for key in [
                b"knt:continuity:t1:u1",
                b"knt:continuity:t1:u2",
                b"other:key",
            ]:
                yield key

        client.scan_iter = fake_iter
        keys = []
        async for key in storage.iter_keys("knt:continuity:"):
            keys.append(key)
        assert keys == [
            "knt:continuity:t1:u1",
            "knt:continuity:t1:u2",
        ]
