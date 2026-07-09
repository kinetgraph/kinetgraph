# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Multi-process cache storage backed by Redis.

Consumes the framework-level ``RedisLike`` Protocol
(``kntgraph.infra.redis._client``) instead of a raw
``redis.asyncio.Redis`` — the caller passes any object
that satisfies the protocol, typically a client from
``create_redis_pool`` or a ``FakeRedis`` in tests.

The adapter is stateless beyond the ``RedisLike``
reference. No background tasks, no in-process state.
The transport's ``_size`` counter reconciles via
``count()`` after invalidation.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from kntgraph.infra.redis._client import RedisLike

from ._protocol import _CacheEntry


class RedisCacheAdapter:
    """
    Multi-process cache backed by a ``RedisLike`` Protocol.

    Iter 17 (ADR-019 epílogo): the previous
    ``RedisCacheStorage`` was deleted. The new adapter
    consumes the framework-level ``RedisLike`` boundary
    (``kntgraph.infra.redis._client``) instead of a
    raw ``redis.asyncio.Redis``.

    The caller passes any object that satisfies
    ``RedisLike`` — typically a Redis client from
    ``create_redis_pool`` (``infra/redis._pool``) or a
    ``FakeRedis`` in tests.

    Setup
    -----

    ```python
    from kntgraph.infra.redis import create_redis_pool
    from kntgraph.agents.tools.cache import RedisCacheAdapter

    client = await create_redis_pool()
    storage = RedisCacheAdapter(client, prefix="knt:llm:cache")
    transport = CachingLLMTransport(
        inner=LiteLLMTransport(),
        storage=storage,
        ttl_s=3600,
    )
    ```

    The adapter is **stateless** beyond the RedisLike
    reference. No background tasks, no in-process
    state. The transport's ``_size`` counter reconciles
    via ``count()`` after invalidation.

    Failure mode
    ------------

    Redis errors propagate as ``RedisError`` (the
    protocol-level error). The transport does NOT catch
    them — the caller (LiteLLMTool) handles the error
    path. Caching is best-effort; a Redis outage should
    not break the LLM call, only the cache.

    TTL
    ---

    The transport sets the TTL via the storage's ``set``
    method. Redis ``EXPIRE`` is applied alongside ``HSET``
    so the key auto-expires even if the cache is not
    read.

    Eviction strategy
    -----------------

    Unlike ``InMemoryCacheStorage``, Redis eviction is
    **not** LRU-at-capacity. The transport accepts a
    ``maxsize`` hint; if you want hard bounds, configure
    ``maxmemory-policy`` on the Redis server (e.g.
    ``allkeys-lru``) — the server-side LRU is the
    authoritative limit.

    If you want per-key eviction independent of TTL,
    use ``maxmemory_policy=volatile-lru`` (only keys with
    ``EXPIRE`` set are evicted; the framework's ``set``
    always sets ``EXPIRE`` when ``ttl_s`` is provided).
    """

    def __init__(
        self,
        redis: RedisLike,
        *,
        prefix: str = "knt:llm:cache",
        ttl_s: Optional[float] = None,
        maxsize: Optional[int] = None,
    ) -> None:
        """
        Args:
          redis: a ``RedisLike`` (the framework-level
            Protocol). Typically a Redis client from
            ``create_redis_pool`` or a ``FakeRedis`` in
            tests.
          prefix: namespace for the keys. Default
            ``knt:llm:cache``. Useful when multiple
            applications share a Redis instance.
          ttl_s: same TTL the transport uses. The
            storage applies it as ``EXPIRE`` on every
            write. When None, entries never expire at
            the Redis level.
          maxsize: hint for the size cap. The Redis
            backend does NOT enforce this locally — it
            relies on the server's ``maxmemory_policy``.
            The value is exposed via ``metrics`` so the
            operator can correlate ``size`` growth
            with ``maxmemory`` configuration.
        """
        if maxsize is not None and maxsize < 0:
            raise ValueError(f"maxsize must be >= 0, got {maxsize}")
        self._redis = redis
        self._prefix = prefix
        self._ttl_s = ttl_s
        self._maxsize = maxsize

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def get(self, key: str) -> Optional[_CacheEntry]:
        raw = await self._redis.hgetall(self._key(key))
        if not raw:
            return None
        return _decode_entry(raw)

    async def set(self, key: str, entry: _CacheEntry) -> None:
        rkey = self._key(key)
        mapping = _encode_entry(entry)
        # The transport's TTL is the same for the whole
        # transport lifetime. The pipe keeps HSET +
        # EXPIRE in a single round-trip.
        pipe = self._redis.pipeline()
        pipe.hset(rkey, mapping=mapping)
        if self._ttl_s is not None and self._ttl_s > 0:
            pipe.expire(rkey, int(self._ttl_s))
        await pipe.execute()

    async def delete(self, key: str) -> None:
        await self._redis.delete(self._key(key))

    async def clear_prefix(self, prefix: str) -> None:
        """Drop every entry whose FULL key (after the
        redis-namespace prefix) starts with ``prefix``.

        ``SCAN + UNLINK`` is the safe pattern for large
        prefixes (the cursor-based iteration never
        blocks the server). We pass ``count=100`` as
        a hint; the actual page size is server-side.
        """
        async for k in self._redis.scan_iter(
            match=f"{self._prefix}:{prefix}*", count=100
        ):
            await self._redis.unlink(k)

    async def clear(self) -> None:
        """Drop all entries under this prefix. Used in
        emergency recovery and tests."""
        async for k in self._redis.scan_iter(match=f"{self._prefix}:*", count=100):
            await self._redis.unlink(k)

    async def count(self) -> int:
        """Authoritative entry count via SCAN.

        ``DBSIZE`` would be cheaper but it counts every
        key in the Redis DB, not just ours. SCAN keeps
        us scoped to the namespace. The cost is O(N)
        where N is the number of entries; in practice
        the transport calls this once per metrics
        scrape.
        """
        n = 0
        async for _ in self._redis.scan_iter(match=f"{self._prefix}:*", count=100):
            n += 1
        return n


# ---------------------------------------------------------------------------
# Redis encoding helpers
# ---------------------------------------------------------------------------


def _encode_entry(entry: _CacheEntry) -> dict[str, str]:
    """Serialize a `_CacheEntry` to a Redis hash mapping.

    All values are strings (Redis hash field values must
    be strings / bytes). The completion is JSON-encoded
    by `json.dumps`. Floats are stringified.
    """
    return {
        "completion": json.dumps(entry.completion, default=str),
        "model": entry.model,
        "stored_at": repr(entry.stored_at),
        "prompt_tokens": str(entry.prompt_tokens),
        "completion_tokens": str(entry.completion_tokens),
        "cost_usd": ("" if entry.cost_usd is None else repr(entry.cost_usd)),
    }


def _decode_entry(raw: dict[Any, str]) -> _CacheEntry:
    """Deserialize a Redis hash mapping back to `_CacheEntry`.

    Tolerates bytes or str values (redis-py returns
    depending on the `decode_responses` flag).
    """

    def _s(v: Any) -> str:
        if isinstance(v, bytes):
            return v.decode("utf-8")
        return v if isinstance(v, str) else str(v)

    completion_str = _s(raw.get("completion", ""))
    try:
        completion = json.loads(completion_str) if completion_str else {}
    except (TypeError, ValueError):
        completion = {}

    cost_str = _s(raw.get("cost_usd", ""))
    cost = None
    if cost_str:
        try:
            cost = float(cost_str)
        except ValueError:
            cost = None

    return _CacheEntry(
        completion=completion,
        model=_s(raw.get("model", "")),
        stored_at=float(_s(raw.get("stored_at", "0")) or 0.0),
        prompt_tokens=int(_s(raw.get("prompt_tokens", "0")) or 0),
        completion_tokens=int(_s(raw.get("completion_tokens", "0")) or 0),
        cost_usd=cost,
    )
