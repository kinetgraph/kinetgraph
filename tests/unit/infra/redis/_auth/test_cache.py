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
    def test_satisfies_api_key_storage(self):
        binding = _MockAPIKeyStorage()
        cache = APIKeyCacheAdapter(binding, ttl_s=60.0)
        assert isinstance(cache, APIKeyStorage)
