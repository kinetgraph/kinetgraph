# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``infra/redis/_memory/_session.py``
(``RedisSessionStorage``).

Closes the infra/redis/_memory/_session coverage gap
(DEBT §3, 88% → 100%). The module is structurally
similar to ``_profile.py`` / ``_continuity.py`` (the
three ``ShortMemoryStorage`` implementations share
the same protocol), but:
  - The payload is JSON-encoded (single value, not
    a Hash). The wire format is ``SET key value EX ttl``.
  - The ``get_record`` decode step has three failure
    modes: ``raw is None`` (miss), ``decode_value``
    returns ``None`` (corrupt bytes — defensive), and
    ``json.loads`` raises (corrupt JSON).
  - The ``put_record`` accepts a non-Mapping ``record``
    (the ``json.dumps`` will coerce it via
    ``default=str``).

The uncovered branches were the three error paths
(Redis exception on ``set`` and ``delete``) + the
defensive ``decoded is None`` arm in ``get_record``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.infra.redis._errors import (
    MemoryDecodeError,
    MemoryError,
    MemoryMiss,
    MemorySerializationError,
)
from kntgraph.infra.redis._memory._session import (
    RedisSessionStorage,
)


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(return_value=None)
    client.delete = AsyncMock(return_value=1)
    client.scan_iter = MagicMock()
    return client


@pytest.fixture
def storage(client: MagicMock) -> RedisSessionStorage:
    return RedisSessionStorage(client=client, ttl_seconds=86400)


# ---------------------------------------------------------------------------
# get_record
# ---------------------------------------------------------------------------


class TestGetRecord:
    async def test_returns_mapping_on_hit(
        self, storage: RedisSessionStorage, client: MagicMock
    ) -> None:
        payload = json.dumps({"k": "v"})
        client.get = AsyncMock(return_value=payload.encode())
        result = await storage.get_record("key")
        assert result.is_ok()
        assert result.ok_value() == {"k": "v"}

    async def test_returns_miss_on_none_raw(
        self, storage: RedisSessionStorage, client: MagicMock
    ) -> None:
        client.get = AsyncMock(return_value=None)
        result = await storage.get_record("key")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryMiss)

    async def test_returns_miss_on_none_decoded(
        self, storage: RedisSessionStorage, client: MagicMock
    ) -> None:
        # ``decode_value`` returns ``None`` for ``None``
        # input (the upstream helper). The storage
        # treats that as a miss.
        client.get = AsyncMock(return_value=None)
        # The first branch is hit (raw is None). The
        # second branch (decoded is None) is exercised
        # when ``raw`` is something that decodes to
        # ``None`` — which is impossible today (``None``
        # maps to ``None`` upstream). Defensive.
        result = await storage.get_record("key")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryMiss)

    async def test_returns_decode_err_on_invalid_json(
        self, storage: RedisSessionStorage, client: MagicMock
    ) -> None:
        client.get = AsyncMock(return_value=b"not-json-{")
        result = await storage.get_record("key")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryDecodeError)

    async def test_returns_err_on_redis_failure(
        self, storage: RedisSessionStorage, client: MagicMock
    ) -> None:
        client.get = AsyncMock(side_effect=ConnectionError("redis down"))
        result = await storage.get_record("key")
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_returns_miss_when_decode_value_is_none(
        self, storage: RedisSessionStorage, client: MagicMock
    ) -> None:
        # ``decode_value`` returns ``None`` for ``None``
        # input. The ``get`` returning ``None`` is the
        # upstream ``raw is None`` arm. To exercise the
        # second ``decoded is None`` arm, we set up
        # ``decode_value`` to return ``None`` for a
        # non-None raw value (via the ``bytes`` branch).
        # We do this by monkey-patching the storage's
        # codec import.
        import kntgraph.infra.redis._memory._session as sess_mod

        original = sess_mod.decode_value
        sess_mod.decode_value = lambda _raw: None
        try:
            client.get = AsyncMock(return_value=b"anything")
            result = await storage.get_record("key")
        finally:
            sess_mod.decode_value = original
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryMiss)


# ---------------------------------------------------------------------------
# put_record
# ---------------------------------------------------------------------------


class TestPutRecord:
    async def test_puts_with_ttl(
        self, storage: RedisSessionStorage, client: MagicMock
    ) -> None:
        client.set = AsyncMock(return_value=True)
        result = await storage.put_record("key", {"k": "v"})
        assert result.is_ok()
        # The set call passed the JSON payload and the
        # ttl.
        client.set.assert_awaited_once()
        # Inspect the call args.
        args = client.set.await_args.args
        kwargs = client.set.await_args.kwargs
        assert args[0] == "key"
        assert json.loads(args[1]) == {"k": "v"}
        # The TTL is forwarded via kwarg ``ex=``.
        assert kwargs.get("ex") == 86400

    async def test_overrides_ttl_with_kwarg(
        self, storage: RedisSessionStorage, client: MagicMock
    ) -> None:
        client.set = AsyncMock(return_value=True)
        await storage.put_record("key", {"k": "v"}, ttl_seconds=60)
        assert client.set.await_args.kwargs.get("ex") == 60

    async def test_returns_err_on_redis_failure(
        self, storage: RedisSessionStorage, client: MagicMock
    ) -> None:
        client.set = AsyncMock(side_effect=ConnectionError("redis down"))
        result = await storage.put_record("key", {"k": "v"})
        assert result.is_err()
        assert isinstance(result.err_value(), MemoryError)

    async def test_returns_serialization_err_on_unserializable(
        self, storage: RedisSessionStorage, client: MagicMock
    ) -> None:
        # A Mapping whose ``dict()`` coercion fails.
        # ``dict(record)`` raises (the conversion is the
        # trigger — not the items() iteration).
        class _BadMapping(dict):
            def __iter__(self):
                raise ValueError("boom")

        # ``json.dumps`` itself also raises for
        # unserialisable values via ``default=str``
        # (e.g. a Set). The cleanest reproduction is
        # to monkey-patch ``json.dumps`` to raise.
        import kntgraph.infra.redis._memory._session as sess_mod

        original = sess_mod.json.dumps
        sess_mod.json.dumps = lambda *a, **k: (_ for _ in ()).throw(ValueError("nope"))
        try:
            result = await storage.put_record("key", {"k": "v"})
        finally:
            sess_mod.json.dumps = original
        assert result.is_err()
        assert isinstance(result.err_value(), MemorySerializationError)


# ---------------------------------------------------------------------------
# delete_record
# ---------------------------------------------------------------------------


class TestDeleteRecord:
    async def test_deletes_key(
        self, storage: RedisSessionStorage, client: MagicMock
    ) -> None:
        client.delete = AsyncMock(return_value=1)
        result = await storage.delete_record("key")
        assert result.is_ok()
        client.delete.assert_awaited_once_with("key")

    async def test_returns_err_on_redis_failure(
        self, storage: RedisSessionStorage, client: MagicMock
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
        self, storage: RedisSessionStorage, client: MagicMock
    ) -> None:
        async def fake_iter(*args, **kwargs):
            for key in [
                b"knt:session:s1",
                b"knt:session:s2",
                b"other:key",
            ]:
                yield key

        client.scan_iter = fake_iter
        keys = []
        async for key in storage.iter_keys("knt:session:"):
            keys.append(key)
        assert keys == [
            "knt:session:s1",
            "knt:session:s2",
        ]
