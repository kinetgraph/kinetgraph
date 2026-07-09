# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
APIKeyCacheAdapter — in-process TTL cache for API keys.

Iter 17b (ADR-019 epílogo + Iter 17 do sharding):
the ``RedisAPIKeyStorage`` reads from Redis on every
``verify()`` call. For high-throughput deployments
(many concurrent requests, all hitting the same
``/verify`` endpoint), this is one HGET per request.

The cache wraps any ``APIKeyStorage`` and memoises
``lookup`` results in-process with a TTL. The cache
is:

  - **Storage-agnostic**: any ``APIKeyStorage`` works
    (Redis today, Memcached or in-process tomorrow).
  - **TTL-bounded**: entries expire after ``ttl_s``
    seconds (default 60s). Lazy expiration on read
    (no background sweep).
  - **Negative-cached**: a miss (digest not found)
    is cached too, so brute-force scans don't
    re-hit Redis.
  - **Fail-soft on errors**: a transient Redis error
    is **not** cached; the next call retries the
    storage. This prevents a single bad event from
    poisoning the cache for the TTL window.

When NOT to use
---------------

  - **Multi-process**: each process has its own cache.
    For multi-process deployments where one process
    rotates a key, the others will serve the stale
    binding for up to ``ttl_s`` seconds. For
    immediate rotation, set ``ttl_s=0`` (effectively
    no cache) or call ``invalidate(digest)`` on every
    process that observed the old key.
  - **Real-time revocation**: the cache TTL bounds
    how fast a revocation propagates. For instant
    revocation, do not cache (or use a cache with
    a callback to the storage on every read).

Composition
-----------

```python
from kntgraph.infra.redis._auth import (
    APIKeyCacheAdapter,
    RedisAPIKeyStorage,
)

storage = RedisAPIKeyStorage(client=redis_client)
cached = APIKeyCacheAdapter(storage, ttl_s=60.0)
verifier = RedisAPIKeyVerifier(storage=cached)
```

The verifier sees the cache as just another
``APIKeyStorage`` (composition, not inheritance).
The cache itself satisfies the Protocol via
``runtime_checkable``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from kntgraph.core.result import Ok, Result
from kntgraph.infra.redis._errors import MemoryError

from ._adapter import APIKeyStorage


@dataclass(frozen=True)
class _CacheEntry:
    """Stored cache entry: the raw bytes + insertion time.

    Using ``time.monotonic()`` so the TTL is robust to
    wall-clock adjustments. Tests inject a custom
    clock via the ``time_fn`` argument.
    """

    value: Optional[bytes]
    inserted_at: float


class APIKeyCacheAdapter(APIKeyStorage):
    """
    TTL-based in-process cache for API key lookups.

    Wraps any ``APIKeyStorage`` and memoises results.
    A negative result (``None``) is cached too so a
    brute-force scan does not re-hit the storage.

    The cache is fail-soft: errors are NOT cached. A
    transient storage failure causes the next call to
    retry (no negative caching of errors).

    Parameters
    ----------
    inner:
        The underlying storage to cache. Must satisfy
        ``APIKeyStorage`` (the Protocol).
    ttl_s:
        Time-to-live for cache entries, in seconds.
        ``0`` disables the cache (every call hits the
        inner storage). Default ``60.0``.
    maxsize:
        LRU cap. When the cache exceeds ``maxsize``,
        the oldest entry is evicted. ``0`` means
        unbounded. Default ``1024``.
    time_fn:
        Clock used for TTL checks. Default
        ``time.monotonic``. Override in tests to
        control time.
    """

    def __init__(
        self,
        inner: APIKeyStorage,
        *,
        ttl_s: float = 60.0,
        maxsize: int = 1024,
        time_fn=None,
    ) -> None:
        if ttl_s < 0:
            raise ValueError(f"ttl_s must be >= 0, got {ttl_s}")
        if maxsize < 0:
            raise ValueError(f"maxsize must be >= 0, got {maxsize}")
        self._inner = inner
        self._ttl_s = ttl_s
        self._maxsize = maxsize
        self._time_fn = time_fn if time_fn is not None else time.monotonic
        # Insertion-ordered dict for LRU.
        self._store: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    def _is_expired(self, entry: _CacheEntry, now: float) -> bool:
        if self._ttl_s <= 0:
            return True
        return (now - entry.inserted_at) > self._ttl_s

    async def lookup(self, digest: str) -> Result[Optional[bytes], MemoryError]:
        """Look up an API key binding, with TTL cache.

        Hit path: returns the cached value without
        calling ``inner.lookup``.

        Miss path: calls ``inner.lookup`` and caches
        the result (including ``None`` for negative
        caching). Errors are NOT cached.
        """
        now = self._time_fn()
        async with self._lock:
            entry = self._store.get(digest)
            if entry is not None and not self._is_expired(entry, now):
                return Ok(entry.value)
            # Drop expired entry.
            if entry is not None:
                del self._store[digest]

        # Cache miss — call inner.
        result = await self._inner.lookup(digest)

        # Only cache successful results (Ok values).
        # Errors are NOT cached: a transient Redis
        # failure must not poison the cache.
        if result.is_ok():
            async with self._lock:
                self._store[digest] = _CacheEntry(
                    value=result.ok_value(),
                    inserted_at=self._time_fn(),
                )
                # LRU eviction (insertion-ordered).
                while self._maxsize > 0 and len(self._store) > self._maxsize:
                    # popitem(last=False) removes the
                    # oldest inserted entry. We use a
                    # plain dict (insertion-ordered in
                    # Python 3.7+) and pop the first key.
                    oldest = next(iter(self._store))
                    del self._store[oldest]
        return result

    async def store(self, digest: str, payload: bytes) -> Result[None, MemoryError]:
        """Store bypasses the cache. The caller (admin
        path) writes the new binding and is expected
        to call ``invalidate(digest)`` to drop any
        cached value before the TTL expires.

        The cache is read-mostly; admin writes happen
        rarely and the caller is the source of truth.
        """
        result = await self._inner.store(digest, payload)
        if result.is_ok():
            await self.invalidate(digest)
        return result

    async def delete(self, digest: str) -> Result[None, MemoryError]:
        """Delete bypasses the cache. The caller
        invalidates after a successful delete so the
        next ``lookup`` re-reads from storage."""
        result = await self._inner.delete(digest)
        if result.is_ok():
            await self.invalidate(digest)
        return result

    async def invalidate(self, digest: str) -> None:
        """Drop the cache entry for a digest. Idempotent.

        Useful in admin paths after a key rotation or
        revocation. Tests use it to simulate TTL
        expiration without waiting.
        """
        async with self._lock:
            self._store.pop(digest, None)

    async def clear(self) -> None:
        """Drop every cached entry."""
        async with self._lock:
            self._store.clear()

    @property
    def size(self) -> int:
        """Current cache size. Synchronous for metrics."""
        return len(self._store)


__all__ = ["APIKeyCacheAdapter"]
