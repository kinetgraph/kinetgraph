# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
In-process LRU cache storage.

Default backend for ``CachingLLMTransport``. Single-process,
no setup, no external dependencies. Concurrent access is
serialised by an ``asyncio.Lock``.

Multi-process deployments should use
``RedisCacheAdapter`` (see ``_redis.py``).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Optional

from ._protocol import _CacheEntry


class InMemoryCacheStorage:
    """
    In-process LRU cache.

    Backed by an `OrderedDict`; eviction is LRU when
    `maxsize` is reached. Concurrent access is serialised
    by `asyncio.Lock`.

    Multi-process safe? **No** — each process has its
    own cache. For multi-process deployments use
    `RedisCacheAdapter`.

    LRU semantics
    -------------

    A `get` that hits promotes the entry to "most
    recently used" (its position in the `OrderedDict`
    moves to the end). A `set` that inserts a new entry
    also appends at the end. When the size would exceed
    `maxsize`, the **least recently used** entry (the
    first item) is evicted.

    Eviction is counted in `evictions` for observability.
    A high eviction rate relative to hits means
    `maxsize` is too small; consider raising it (the
    cost of LRU is O(1) per op).
    """

    def __init__(self, maxsize: int = 1024) -> None:
        """
        Args:
          maxsize: maximum number of entries before
            LRU eviction kicks in. `0` or negative is
            treated as "no LRU" (the dict grows without
            bound). The default `1024` is a sane
            starting point for a single-process LLM
            cache.
        """
        if maxsize < 0:
            raise ValueError(f"maxsize must be >= 0, got {maxsize}")
        self._maxsize = maxsize
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        # Eviction counter for observability.
        self.evictions: int = 0

    async def get(self, key: str) -> Optional[_CacheEntry]:
        async with self._lock:
            try:
                entry = self._store[key]
            except KeyError:
                return None
            # LRU: promote to "most recently used".
            self._store.move_to_end(key)
            return entry

    async def set(self, key: str, entry: _CacheEntry) -> None:
        async with self._lock:
            if key in self._store:
                # Re-set: update value, keep at the
                # end (most recently used).
                self._store.move_to_end(key)
                self._store[key] = entry
            else:
                self._store[key] = entry
                # LRU eviction when at capacity.
                if self._maxsize > 0 and len(self._store) > self._maxsize:
                    self._store.popitem(last=False)
                    self.evictions += 1

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    async def clear(self) -> None:
        """Drop every entry. O(N) but bounded by ``maxsize``
        (or unbounded if ``maxsize=0``).
        """
        async with self._lock:
            self._store.clear()

    async def clear_prefix(self, prefix: str) -> None:
        """Drop every entry whose key starts with
        ``prefix``. O(N) over the store; the cache is
        in-process so this is bounded by ``maxsize``
        (or unbounded if ``maxsize=0``). The
        transport never invokes this on a million-key
        cache without explicit operator intent; for
        routine invalidation the cost is negligible."""
        async with self._lock:
            # Iterate over a snapshot of the keys so
            # we can mutate the dict during the loop.
            for k in list(self._store.keys()):
                if k.startswith(prefix):
                    del self._store[k]

    def __len__(self) -> int:
        """Synchronous size accessor for metrics."""
        return len(self._store)

    async def count(self) -> int:
        """Async count, for storage-agnostic callers
        (``RedisCacheAdapter.count`` is async because
        SCAN is I/O; the in-memory implementation just
        delegates to ``__len__`` for symmetry)."""
        return len(self._store)

    @property
    def maxsize(self) -> int:
        return self._maxsize

    @property
    def metrics(self) -> dict[str, int]:
        """
        Process-local metrics: `size` (current) and
        `evictions` (cumulative).
        """
        return {
            "size": len(self._store),
            "maxsize": self._maxsize,
            "evictions": self.evictions,
        }
