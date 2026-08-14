# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``APIKeyCacheAdapter`` — in-process cache
for API key lookups.

Iter 17b (ADR-019 epílogo + Iter 17 do sharding): a
TTL-based cache that wraps any ``APIKeyStorage`` to
avoid hitting Redis on every request. The cache is
fail-soft: a Redis miss + cache hit returns the cached
value; a cache miss + Redis miss returns ``Ok(None)``.

Tests use a mock ``APIKeyStorage`` (the framework
boundary) — not Redis. The cache is pure composition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from kntgraph.core.result import Err, Ok, Result
from kntgraph.infra.redis._auth import APIKeyStorage
from kntgraph.infra.redis._auth._cache import APIKeyCacheAdapter
from kntgraph.infra.redis._errors import MemoryError


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Mock APIKeyStorage
# ---------------------------------------------------------------------------


@dataclass
class _MockAPIKeyStorage:
    """Mock that records ``lookup`` calls and returns
    canned bytes per digest."""

    bindings: dict[str, bytes] = field(default_factory=dict)
    lookup_calls: list[str] = field(default_factory=list)
    raise_on_lookup: Exception | None = None

    async def lookup(self, digest: str) -> Result[Optional[bytes], MemoryError]:
        self.lookup_calls.append(digest)
        if self.raise_on_lookup is not None:
            return Err(MemoryError(f"redis: {self.raise_on_lookup}"))
        return Ok(self.bindings.get(digest))

    async def store(self, digest: str, payload: bytes) -> Result[None, MemoryError]:
        self.bindings[digest] = payload
        return Ok(None)

    async def delete(self, digest: str) -> Result[None, MemoryError]:
        self.bindings.pop(digest, None)
        return Ok(None)


# ---------------------------------------------------------------------------
# Cache hit / miss
# ---------------------------------------------------------------------------


class TestCacheMiss:
    @pytest.mark.asyncio
    async def test_first_call_hits_storage(self):
        binding = _MockAPIKeyStorage(bindings={"abc": b'{"agent_id": "NF-001"}'})
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        result = await cache.lookup("abc")
        assert result.is_ok()
        assert result.ok_value() == b'{"agent_id": "NF-001"}'
        # Storage was hit once.
        assert binding.lookup_calls == ["abc"]

    @pytest.mark.asyncio
    async def test_miss_returns_none(self):
        binding = _MockAPIKeyStorage()
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        result = await cache.lookup("missing")
        assert result.is_ok()
        assert result.ok_value() is None


class TestCacheHit:
    @pytest.mark.asyncio
    async def test_second_call_does_not_hit_storage(self):
        binding = _MockAPIKeyStorage(bindings={"abc": b"payload"})
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        # First call: hit storage, populate cache.
        await cache.lookup("abc")
        # Second call: should be a cache hit.
        await cache.lookup("abc")
        assert binding.lookup_calls == ["abc"]  # only once

    @pytest.mark.asyncio
    async def test_none_is_cached_too(self):
        """A miss is also cached (negative caching).

        Without negative caching, a brute-force scan
        against the same digest would hit Redis every
        time. With it, after the first miss, the cache
        serves ``None`` until the TTL expires.
        """
        binding = _MockAPIKeyStorage()
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        await cache.lookup("missing")
        await cache.lookup("missing")
        assert binding.lookup_calls == ["missing"]


class TestCacheTTL:
    @pytest.mark.asyncio
    async def test_expired_entry_refetches(self):
        binding = _MockAPIKeyStorage(bindings={"abc": b"original"})
        cache = APIKeyCacheAdapter(binding, ttl_s=0.0)
        # TTL=0 → every call is a miss (entry expires
        # immediately on insertion).
        await cache.lookup("abc")
        await cache.lookup("abc")
        assert binding.lookup_calls == ["abc", "abc"]

    @pytest.mark.asyncio
    async def test_invalidate_forces_refetch(self):
        binding = _MockAPIKeyStorage(bindings={"abc": b"payload"})
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        await cache.lookup("abc")
        await cache.invalidate("abc")
        await cache.lookup("abc")
        assert binding.lookup_calls == ["abc", "abc"]


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    @pytest.mark.asyncio
    async def test_storage_error_propagates(self):
        binding = _MockAPIKeyStorage(raise_on_lookup=ConnectionError("redis down"))
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        result = await cache.lookup("abc")
        assert result.is_err()
        # The cache does NOT cache errors — a transient
        # Redis failure must not poison the cache.
        assert binding.lookup_calls == ["abc"]
        # A second call retries the storage (no negative
        # caching of errors).
        await cache.lookup("abc")
        assert binding.lookup_calls == ["abc", "abc"]


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


class TestProtocolSatisfaction:
    async def test_satisfies_api_key_storage(self):
        binding = _MockAPIKeyStorage()
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        assert isinstance(cache, APIKeyStorage)


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    async def test_negative_ttl_raises(self):
        with pytest.raises(ValueError, match="ttl_s"):
            APIKeyCacheAdapter(_MockAPIKeyStorage(), ttl_s=-1)

    async def test_negative_maxsize_raises(self):
        with pytest.raises(ValueError, match="maxsize"):
            APIKeyCacheAdapter(_MockAPIKeyStorage(), maxsize=-1)

    async def test_zero_ttl_disables_cache(self):
        """``ttl_s=0`` makes every entry "expired on
        creation" — every call hits the storage."""
        binding = _MockAPIKeyStorage(bindings={"d": b"payload"})
        cache = APIKeyCacheAdapter(binding, ttl_s=0.0)
        # Two lookups = two storage calls.
        assert (await cache.lookup("d")).ok_value() == b"payload"
        assert (await cache.lookup("d")).ok_value() == b"payload"
        assert binding.lookup_calls == ["d", "d"]

    async def test_custom_clock(self):
        """A custom ``time_fn`` is honoured (the cache
        uses ``time.monotonic`` by default; tests
        inject a fake clock to control time)."""
        binding = _MockAPIKeyStorage(bindings={"d": b"payload"})
        clock = {"now": 0.0}

        def fake_time() -> float:
            return clock["now"]

        cache = APIKeyCacheAdapter(binding, ttl_s=10.0, time_fn=fake_time)
        # First lookup at t=0.
        assert (await cache.lookup("d")).ok_value() == b"payload"
        # Advance 5s — still within TTL.
        clock["now"] = 5.0
        assert (await cache.lookup("d")).ok_value() == b"payload"
        assert binding.lookup_calls == ["d"]
        # Advance 11s — past TTL.
        clock["now"] = 11.0
        assert (await cache.lookup("d")).ok_value() == b"payload"
        assert binding.lookup_calls == ["d", "d"]


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


class TestLrueviction:
    async def test_eviction_when_over_maxsize(self):
        binding = _MockAPIKeyStorage(
            bindings={f"d{i}": f"payload-{i}".encode() for i in range(3)}
        )
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0, maxsize=2)
        await cache.lookup("d0")
        await cache.lookup("d1")
        await cache.lookup("d2")
        # After 3 inserts with maxsize=2, the oldest
        # entry (``d0``) is evicted; the cache now
        # holds ``d1`` and ``d2``.
        assert cache.size == 2
        assert "d0" not in cache._store
        assert "d1" in cache._store
        assert "d2" in cache._store

    async def test_unbounded_maxsize(self):
        binding = _MockAPIKeyStorage(
            bindings={f"d{i}": f"p{i}".encode() for i in range(5)}
        )
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0, maxsize=0)
        for i in range(5):
            await cache.lookup(f"d{i}")
        # ``maxsize=0`` means unbounded.
        assert cache.size == 5


# ---------------------------------------------------------------------------
# store / delete invalidate the cache
# ---------------------------------------------------------------------------


class TestWriteInvalidates:
    async def test_store_invalidates_cache(self):
        binding = _MockAPIKeyStorage()
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        # Prime the cache via a direct insertion.
        binding.bindings["d"] = b"old"
        assert (await cache.lookup("d")).ok_value() == b"old"
        assert binding.lookup_calls == ["d"]
        # Now write a new value — the cache should
        # invalidate and the next lookup hits the
        # storage.
        await cache.store("d", b"new")
        assert (await cache.lookup("d")).ok_value() == b"new"
        assert binding.lookup_calls == ["d", "d"]

    async def test_store_does_not_invalidate_on_error(self):
        from kntgraph.infra.redis._errors import MemoryError

        class _FailingStore(_MockAPIKeyStorage):
            async def store(self, digest: str, payload: bytes):
                return Err(MemoryError("redis down"))

        binding = _FailingStore(bindings={"d": b"old"})
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        # Prime the cache.
        assert (await cache.lookup("d")).ok_value() == b"old"
        # Failing store — the cache entry is NOT
        # invalidated (the contract is "invalidate
        # only on success").
        result = await cache.store("d", b"new")
        assert result.is_err()
        # Next lookup is still a cache hit.
        assert (await cache.lookup("d")).ok_value() == b"old"
        assert binding.lookup_calls == ["d"]

    async def test_delete_invalidates_cache(self):
        binding = _MockAPIKeyStorage(bindings={"d": b"old"})
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        # Prime the cache.
        assert (await cache.lookup("d")).ok_value() == b"old"
        # Delete — the cache is invalidated.
        binding.bindings.pop("d", None)
        await cache.delete("d")
        # Next lookup is a cache miss.
        assert (await cache.lookup("d")).ok_value() is None
        assert binding.lookup_calls == ["d", "d"]

    async def test_delete_does_not_invalidate_on_error(self):
        from kntgraph.infra.redis._errors import MemoryError

        class _FailingDeleteStore(_MockAPIKeyStorage):
            async def delete(self, digest: str):
                return Err(MemoryError("redis down"))

        binding = _FailingDeleteStore(bindings={"d": b"old"})
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        # Prime the cache.
        assert (await cache.lookup("d")).ok_value() == b"old"
        # Failing delete — cache entry preserved.
        result = await cache.delete("d")
        assert result.is_err()
        assert (await cache.lookup("d")).ok_value() == b"old"
        assert binding.lookup_calls == ["d"]


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


class TestClear:
    async def test_clear_drops_every_entry(self):
        binding = _MockAPIKeyStorage(
            bindings={f"d{i}": f"p{i}".encode() for i in range(3)}
        )
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        for i in range(3):
            await cache.lookup(f"d{i}")
        assert cache.size == 3
        await cache.clear()
        assert cache.size == 0

    async def test_clear_is_idempotent(self):
        cache = APIKeyCacheAdapter(_MockAPIKeyStorage())
        await cache.clear()
        await cache.clear()  # no-op, no error


# ---------------------------------------------------------------------------
# size property
# ---------------------------------------------------------------------------


class TestSize:
    async def test_size_starts_at_zero(self):
        cache = APIKeyCacheAdapter(_MockAPIKeyStorage())
        assert cache.size == 0

    async def test_size_tracks_inserts(self):
        binding = _MockAPIKeyStorage(bindings={"d1": b"p1", "d2": b"p2"})
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        await cache.lookup("d1")
        assert cache.size == 1
        await cache.lookup("d2")
        assert cache.size == 2
