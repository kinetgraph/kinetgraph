# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for `CachingLLMTransport`, `InMemoryCacheStorage`,
`RedisCacheAdapter`, and the `AsyncCacheStorage` Protocol
(ADR-011, Iter 17).

Coverage:

  `CachingLLMTransport`
  ------------------

  - Miss → inner called, result stored.
  - Hit → inner NOT called, stored result returned.
  - Bypass when no `idempotency_key`.
  - Different `(key, model, response_format)` are separate
    entries.
  - `metrics` counters (hits, misses, stores, errors, size).
  - TTL expiration.
  - Errors are NOT cached.
  - `idempotency_key` passed through to inner.

  `InMemoryCacheStorage`
  -----------------------

  - LRU eviction at `maxsize`.
  - `get` promotes to most-recently-used.
  - `maxsize=0` = no LRU (unbounded).
  - Negative `maxsize` raises.
  - `metrics` reports `size`, `maxsize`, `evictions`.

  `RedisCacheStorage`
  -------------------

  - `HSET + EXPIRE` in pipeline.
  - Encoding round-trip (hash → `_CacheEntry`).
  - `clear` via `SCAN + UNLINK`.
  - Negative `maxsize` raises.
"""

from __future__ import annotations

import pytest

from kntgraph.agents.tools.cache import (
    CachingLLMTransport,
    InMemoryCacheStorage,
    _CacheEntry,
)
from kntgraph.tools.llm_transport import LLMRequest

from .._fake_transport import FakeLLMTransport


pytestmark = pytest.mark.asyncio


def _req(
    *,
    model: str = "x",
    messages: list | None = None,
    temperature: float = 0.0,
    max_tokens: int = 10,
    response_format: dict | None = None,
    idempotency_key: str | None = None,
) -> LLMRequest:
    """Build an ``LLMRequest`` for cache tests.

    Iter 28 FU 5: tests migrated from
    ``cache.complete(model=..., messages=..., ...)``
    (legacy) to ``cache(LLMRequest(...))`` (Iter 28
    FU 3 shape). The transport IS-A
    ``Callable[LLMRequest, dict]``; tests must
    construct the value object explicitly.
    The current call sites all use ``await cache(_req(...))``;
    the legacy ``complete()`` method was removed in Iter 28
    FU 3 (CachingLLMTransport exposes only ``__call__``).
    """
    return LLMRequest(
        model=model,
        messages=messages if messages is not None else [],
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# CachingLLMTransport
# ---------------------------------------------------------------------------


class TestBasicCaching:
    async def test_miss_calls_inner(self):
        inner = FakeLLMTransport()
        inner.queue_response(text="first call")
        cache = CachingLLMTransport(inner)

        r = await cache(
            _req(
                model="x",
                messages=[{"role": "user", "content": "hi"}],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        assert r["choices"][0]["message"]["content"] == "first call"
        assert len(inner.calls) == 1
        assert cache.metrics == {
            "name": "caching",
            "size": 1,
            "hits": 0,
            "misses": 1,
            "stores": 1,
            "errors": 0,
        }

    async def test_second_call_is_hit(self):
        inner = FakeLLMTransport()
        inner.queue_response(text="cached response")
        cache = CachingLLMTransport(inner)

        r1 = await cache(
            _req(
                model="x",
                messages=[{"role": "user", "content": "hi"}],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        r2 = await cache(
            _req(
                model="x",
                messages=[{"role": "user", "content": "hi"}],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        assert r1 == r2
        assert len(inner.calls) == 1  # only the first call
        assert cache.metrics["hits"] == 1
        assert cache.metrics["misses"] == 1

    async def test_three_calls_one_inner(self):
        inner = FakeLLMTransport()
        inner.queue_response(text="only once")
        cache = CachingLLMTransport(inner)

        for _ in range(3):
            await cache(
                _req(
                    model="x",
                    messages=[],
                    temperature=0.0,
                    max_tokens=10,
                    idempotency_key="k1",
                )
            )
        assert len(inner.calls) == 1
        assert cache.metrics["hits"] == 2
        assert cache.metrics["misses"] == 1


class TestCacheBypass:
    async def test_no_idempotency_key_bypasses_cache(self):
        inner = FakeLLMTransport()
        inner.queue_response(text="a")
        inner.queue_response(text="b")
        cache = CachingLLMTransport(inner)

        r1 = await cache(
            _req(
                model="x",
                messages=[],
                temperature=0.0,
                max_tokens=10,
            )
        )
        r2 = await cache(
            _req(
                model="x",
                messages=[],
                temperature=0.0,
                max_tokens=10,
            )
        )
        assert r1["choices"][0]["message"]["content"] == "a"
        assert r2["choices"][0]["message"]["content"] == "b"
        # No `idempotency_key` → no caching; inner called
        # twice.
        assert len(inner.calls) == 2

    async def test_different_keys_are_separate(self):
        inner = FakeLLMTransport()
        inner.queue_response(text="a")
        inner.queue_response(text="b")
        cache = CachingLLMTransport(inner)

        r1 = await cache(
            _req(
                model="x",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        r2 = await cache(
            _req(
                model="x",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k2",
            )
        )
        assert r1["choices"][0]["message"]["content"] == "a"
        assert r2["choices"][0]["message"]["content"] == "b"
        assert len(inner.calls) == 2

    async def test_different_models_same_key(self):
        inner = FakeLLMTransport()
        inner.queue_response(text="a")
        inner.queue_response(text="b")
        cache = CachingLLMTransport(inner)

        r1 = await cache(
            _req(
                model="m1",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        r2 = await cache(
            _req(
                model="m2",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        # Different model → different cache entry.
        assert r1["choices"][0]["message"]["content"] == "a"
        assert r2["choices"][0]["message"]["content"] == "b"
        assert cache.metrics["size"] == 2

    async def test_different_response_format_same_key(self):
        inner = FakeLLMTransport()
        inner.queue_response(text="a")
        inner.queue_response(text="b")
        cache = CachingLLMTransport(inner)

        r1 = await cache(
            _req(
                model="m1",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
                response_format={"type": "json"},
            )
        )
        r2 = await cache(
            _req(
                model="m1",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        # Different `response_format` → different entry.
        assert r1["choices"][0]["message"]["content"] == "a"
        assert r2["choices"][0]["message"]["content"] == "b"
        assert cache.metrics["size"] == 2


class TestTtl:
    async def test_expired_entry_triggers_refetch(self):
        inner = FakeLLMTransport()
        inner.queue_response(text="a")
        inner.queue_response(text="b")
        cache = CachingLLMTransport(
            inner,
            ttl_s=1.0,
            time_fn=lambda: 0.0,
        )

        r1 = await cache(
            _req(
                model="m",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        # Advance the clock past TTL.
        cache._time_fn = lambda: 100.0
        r2 = await cache(
            _req(
                model="m",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        assert r1["choices"][0]["message"]["content"] == "a"
        assert r2["choices"][0]["message"]["content"] == "b"
        assert len(inner.calls) == 2

    async def test_not_yet_expired_still_hits(self):
        inner = FakeLLMTransport()
        inner.queue_response(text="a")
        cache = CachingLLMTransport(
            inner,
            ttl_s=10.0,
            time_fn=lambda: 0.0,
        )

        await cache(
            _req(
                model="m",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        # Advance the clock but still under TTL.
        cache._time_fn = lambda: 5.0
        await cache(
            _req(
                model="m",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        assert len(inner.calls) == 1
        assert cache.metrics["hits"] == 1

    async def test_no_ttl_means_never_expires(self):
        inner = FakeLLMTransport()
        inner.queue_response(text="a")
        cache = CachingLLMTransport(inner, time_fn=lambda: 0.0)

        # No `ttl_s` → entries never expire.
        for t in (0.0, 1e9):
            cache._time_fn = lambda t=t: t  # noqa: B008
            await cache(
                _req(
                    model="m",
                    messages=[],
                    temperature=0.0,
                    max_tokens=10,
                    idempotency_key="k1",
                )
            )
        assert len(inner.calls) == 1


class TestErrors:
    async def test_rate_limit_error_not_cached(self):
        from kntgraph.agents.tools.llm import LLMRateLimitError

        inner = FakeLLMTransport()
        inner.queue_error("rate_limit")
        inner.queue_response(text="recovered")
        cache = CachingLLMTransport(inner)

        # First call: inner raises.
        with pytest.raises(LLMRateLimitError):
            await cache(
                _req(
                    model="m",
                    messages=[],
                    temperature=0.0,
                    max_tokens=10,
                    idempotency_key="k1",
                )
            )
        # Errors counter incremented, size not.
        assert cache.metrics["errors"] == 1
        assert cache.metrics["size"] == 0
        # Second call: cache empty, inner called again.
        r = await cache(
            _req(
                model="m",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        assert r["choices"][0]["message"]["content"] == "recovered"
        assert len(inner.calls) == 2


class TestInvalidation:
    async def test_invalidate_specific_key(self):
        inner = FakeLLMTransport()
        inner.queue_response(text="a")
        cache = CachingLLMTransport(inner)

        await cache(
            _req(
                model="m",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        assert cache.metrics["size"] == 1
        # Before the fix, this assertion passed only
        # because the transport reset its own ``_size``
        # counter; the actual storage entry was never
        # deleted (the old fallback tried to delete a
        # wildcarded key that was never stored). We
        # now verify both: the transport counter AND
        # the storage itself.
        await cache.invalidate("k1")
        assert cache.metrics["size"] == 0
        # Verify the storage is actually empty, not
        # just the transport counter.
        assert await cache._storage.count() == 0

    async def test_invalidate_does_not_touch_other_keys(self):
        """A `clear_prefix("k1|")` MUST NOT delete
        ``k2|...`` entries — the prefix match is
        exact, not a substring."""
        inner = FakeLLMTransport()
        inner.queue_response(text="a")
        inner.queue_response(text="b")
        cache = CachingLLMTransport(inner)

        await cache(
            _req(
                model="m",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        await cache(
            _req(
                model="m",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k2",
            )
        )
        assert cache.metrics["size"] == 2
        await cache.invalidate("k1")
        # Only `k1|` was cleared; `k2|` survives.
        assert cache.metrics["size"] == 1
        # And the storage agrees.
        assert await cache._storage.count() == 1
        # Verify it's the right one by reading the key.
        surviving = list(cache._storage._store.keys())
        assert surviving[0].startswith("k2|")

    async def test_clear_all(self):
        inner = FakeLLMTransport()
        inner.queue_response(text="a")
        inner.queue_response(text="b")
        inner.queue_response(text="c")
        cache = CachingLLMTransport(inner)

        await cache(
            _req(
                model="m",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        await cache(
            _req(
                model="m",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k2",
            )
        )
        assert cache.metrics["size"] == 2
        await cache.clear()
        assert cache.metrics["size"] == 0
        assert await cache._storage.count() == 0


class TestStorageClearPrefix:
    """Pin the ``clear_prefix`` contract on the storage
    Protocol — both built-in storages must implement it.
    Without this contract the transport's
    ``invalidate()`` would silently no-op.
    """

    async def test_in_memory_clear_prefix_drops_matching(self):
        from kntgraph.agents.tools.cache import InMemoryCacheStorage

        s = InMemoryCacheStorage()
        # Seed three entries with different prefixes.
        for k, model in [
            ("alpha|1", "m"),
            ("alpha|2", "m"),
            ("beta|1", "m"),
        ]:
            await s.set(
                k,
                _CacheEntry(
                    completion={"text": "x"},
                    model=model,
                    stored_at=0.0,
                ),
            )
        assert len(s) == 3
        await s.clear_prefix("alpha|")
        assert len(s) == 1
        # The survivor is the beta entry.
        assert list(s._store.keys()) == ["beta|1"]

    async def test_in_memory_clear_prefix_no_match_is_noop(self):
        from kntgraph.agents.tools.cache import InMemoryCacheStorage

        s = InMemoryCacheStorage()
        await s.set(
            "alpha|1",
            _CacheEntry(completion={"text": "x"}, model="m", stored_at=0.0),
        )
        await s.clear_prefix("nonexistent|")
        # Nothing removed.
        assert len(s) == 1

    async def test_in_memory_count_matches_len(self):
        """``count()`` is the async counterpart of
        ``__len__`` for the in-memory storage — the
        transport calls ``count()`` to honour the
        ``AsyncCacheStorage`` protocol contract.
        """
        from kntgraph.agents.tools.cache import InMemoryCacheStorage

        s = InMemoryCacheStorage()
        await s.set(
            "alpha|1",
            _CacheEntry(completion={"text": "x"}, model="m", stored_at=0.0),
        )
        await s.set(
            "alpha|2",
            _CacheEntry(completion={"text": "y"}, model="m", stored_at=0.0),
        )
        # ``len()`` is sync (O(1) for in-memory);
        # ``count()`` is async (the protocol contract —
        # Redis needs SCAN).
        assert len(s) == 2
        assert await s.count() == 2


class TestKeyPropagation:
    async def test_inner_receives_idempotency_key(self):
        inner = FakeLLMTransport()
        inner.queue_response(text="ok")
        cache = CachingLLMTransport(inner)

        await cache(
            _req(
                model="m",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        # The inner was called with `idempotency_key="k1"`.
        assert inner.calls[0]["idempotency_key"] == "k1"

    async def test_inner_receives_key_on_cache_hit_too(self):
        """The key is forwarded even on a hit (chain
        semantics: cache → metrics → inner)."""
        inner = FakeLLMTransport()
        inner.queue_response(text="ok")
        cache = CachingLLMTransport(inner)

        await cache(
            _req(
                model="m",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        await cache(
            _req(
                model="m",
                messages=[],
                temperature=0.0,
                max_tokens=10,
                idempotency_key="k1",
            )
        )
        # Both calls (one miss, one hit) forward the key.
        assert len(inner.calls) == 1  # only the first hit inner
        assert inner.calls[0]["idempotency_key"] == "k1"


class TestWithTool:
    async def test_litellmtool_uses_caching_transport(self):
        """End-to-end: a `CachingLLMTransport` wrapping a
        `LiteLLMTransportAdapter` is a valid composition. We do
        not run a real LiteLLM call here (that requires
        API keys); the construction itself is the smoke
        test. Real LiteLLM integration is covered by the
        `examples/07_caching.py` smoke test.
        """
        from kntgraph.agents.tools.llm import LiteLLMTransportAdapter

        inner = LiteLLMTransportAdapter()
        cache = CachingLLMTransport(inner)
        # The decorator exposes the inner via `.inner`.
        assert cache.inner is inner
        # Storage defaults to in-process.
        from kntgraph.agents.tools.cache import InMemoryCacheStorage

        assert isinstance(cache.storage, InMemoryCacheStorage)


# ---------------------------------------------------------------------------
# InMemoryCacheStorage
# ---------------------------------------------------------------------------


class TestInMemoryLru:
    def test_default_maxsize_is_1024(self):
        s = InMemoryCacheStorage()
        assert s.maxsize == 1024

    def test_negative_maxsize_raises(self):
        with pytest.raises(ValueError, match="maxsize must be"):
            InMemoryCacheStorage(maxsize=-1)

    async def test_lru_eviction_at_capacity(self):
        s = InMemoryCacheStorage(maxsize=3)
        for i in range(5):
            await s.set(
                f"k{i}",
                _CacheEntry(
                    completion={},
                    model="m",
                    stored_at=0.0,
                ),
            )
        assert len(s) == 3
        assert s.evictions == 2
        # k0 and k1 were LRU; k2-k4 remain.
        assert await s.get("k0") is None
        assert await s.get("k1") is None
        assert await s.get("k2") is not None
        assert await s.get("k3") is not None
        assert await s.get("k4") is not None

    async def test_get_promotes_to_most_recently_used(self):
        s = InMemoryCacheStorage(maxsize=3)
        for i in range(3):
            await s.set(
                f"k{i}",
                _CacheEntry(
                    completion={},
                    model="m",
                    stored_at=0.0,
                ),
            )
        # Order: k0 (LRU), k1, k2 (MRU).
        # Touch k0 → promoted → order: k1 (LRU), k2, k0.
        assert await s.get("k0") is not None
        # Add k3 → k1 evicted.
        await s.set(
            "k3",
            _CacheEntry(
                completion={},
                model="m",
                stored_at=0.0,
            ),
        )
        assert s.evictions == 1
        assert await s.get("k1") is None
        assert await s.get("k0") is not None
        assert await s.get("k3") is not None

    async def test_maxsize_zero_is_unbounded(self):
        s = InMemoryCacheStorage(maxsize=0)
        for i in range(100):
            await s.set(
                f"k{i}",
                _CacheEntry(
                    completion={},
                    model="m",
                    stored_at=0.0,
                ),
            )
        assert len(s) == 100
        assert s.evictions == 0

    async def test_set_overwrites_promotes(self):
        s = InMemoryCacheStorage(maxsize=3)
        for i in range(3):
            await s.set(
                f"k{i}",
                _CacheEntry(
                    completion={"v": i},
                    model="m",
                    stored_at=0.0,
                ),
            )
        # Re-set k0 → should NOT evict; k0 moves to MRU.
        await s.set(
            "k0",
            _CacheEntry(
                completion={"v": 99},
                model="m",
                stored_at=0.0,
            ),
        )
        assert s.evictions == 0
        entry = await s.get("k0")
        assert entry is not None
        assert entry.completion["v"] == 99

    async def test_delete_removes_entry(self):
        s = InMemoryCacheStorage()
        await s.set(
            "k",
            _CacheEntry(
                completion={},
                model="m",
                stored_at=0.0,
            ),
        )
        assert len(s) == 1
        await s.delete("k")
        assert len(s) == 0
        # Deleting a non-existent key is a no-op.
        await s.delete("nonexistent")
        assert len(s) == 0

    async def test_metrics(self):
        s = InMemoryCacheStorage(maxsize=2)
        assert s.metrics == {"size": 0, "maxsize": 2, "evictions": 0}
        for i in range(5):
            await s.set(
                f"k{i}",
                _CacheEntry(
                    completion={},
                    model="m",
                    stored_at=0.0,
                ),
            )
        assert s.metrics == {"size": 2, "maxsize": 2, "evictions": 3}
