# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``_CacheEntry`` and ``AsyncCacheStorage`` Protocol.

The cache module is split into 4 sub-modules:

  - ``_protocol`` (this file): the value object and the
    abstract storage contract.
  - ``_in_memory``: the in-process LRU implementation.
  - ``_redis``: the multi-process adapter (consumes the
    framework's ``RedisLike`` boundary).
  - ``_transport``: the ``CachingLLMTransport`` decorator
    that turns any ``LLMTransport`` into a cached one.

The Protocol is the only seam between the in-memory
and Redis implementations. The transport depends on
the Protocol, not on either implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# _CacheEntry — the stored shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """Stored in the cache: the raw completion + metadata."""

    completion: dict
    model: str
    stored_at: float
    # Token usage and cost, copied out for observability
    # without forcing a `_to_llm_response` parse at every
    # hit.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Optional[float] = None

    def is_expired(self, ttl_s: Optional[float], now: float) -> bool:
        if ttl_s is None:
            return False
        return (now - self.stored_at) > ttl_s


# ---------------------------------------------------------------------------
# AsyncCacheStorage Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AsyncCacheStorage(Protocol):
    """
    The contract for a cache backend.

    Methods are async because distributed backends
    (Redis, Memcached, etc.) need an awaitable round-trip.
    The in-process implementation also exposes them as
    `async` (the overhead of one event-loop hop is
    negligible compared to the value of a uniform shape).

    Implementations MUST be safe under concurrent access.
    The default `InMemoryCacheStorage` uses an
    `asyncio.Lock`; `RedisCacheAdapter` relies on Redis's
    own atomicity.
    """

    async def get(self, key: str) -> Optional[_CacheEntry]:
        """Return the entry, or `None` if missing/expired.
        The transport handles the TTL check itself; the
        storage just returns what is there."""
        ...

    async def set(self, key: str, entry: _CacheEntry) -> None:
        """Persist the entry. Overwrites any existing value."""
        ...

    async def delete(self, key: str) -> None:
        """Remove the entry. No-op if missing."""
        ...

    async def clear_prefix(self, prefix: str) -> None:
        """Remove every entry whose key starts with
        ``prefix``. Used by ``CachingLLMTransport.invalidate``
        to drop every model/format variant under one
        idempotency key without the caller knowing the
        exact set.

        Implementations must match ``prefix`` as a
        prefix of the full storage key (i.e. the
        ``prefix`` already includes any namespace the
        storage applies — for ``RedisCacheAdapter``
        callers pass ``f"{idempotency_key}|"`` and the
        storage adds the redis-namespace prefix
        internally).
        """
        ...

    async def count(self) -> int:
        """Return the current entry count. Used by the
        transport to reconcile its local counter when
        a remote store may have evicted entries behind
        its back (Redis ``maxmemory-policy``).

        Async because the in-memory implementation can
        answer in O(1) but the Redis implementation
        needs SCAN (O(N) with I/O). The transport awaits
        this once per metrics scrape — never in the hot
        path.
        """
        ...
