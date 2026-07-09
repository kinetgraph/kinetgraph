# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``CachingLLMTransport`` — decorator transport that
memoizes completions by ``idempotency_key``.

The transport is the unit of cache. The ``LiteLLMTool``
forwards the framework's ``idempotency_key`` (from the
caller — usually a Role) into ``transport(...)`` via
kwarg. A caching transport intercepts that kwarg and
returns the cached response on a hit, or delegates to
the inner transport and stores the result on a miss.

Semantics
---------

  - **Key**: `idempotency_key` (forwarded by the Tool).
  - **Hit**: same key + same model + same response_format
    → return cached response dict. Inner transport is
    NOT called.
  - **Miss**: any of the above differ (or key absent) →
    call inner, store response under that key, return.
    - **Storage**: an `AsyncCacheStorage` (Protocol with
    `get/set/delete`). Default is `InMemoryCacheStorage`
    (in-process, with `asyncio.Lock`). For multi-process
    deployments, pass `RedisCacheAdapter` (shared cache
    across processes; ADR-008 §4).
  - **TTL**: optional. If set, entries expire after N
    seconds. Uses lazy expiration (checked on read, no
    background sweep).
  - **Errors**: NOT cached. If the inner transport raises,
    nothing is stored; the next call retries.

Why at the transport level
--------------------------

  - Single point of caching — all Roles / systems
    benefit transparently.
  - Test-friendly: tests can swap `FakeLLMTransport` for
    `CachingLLMTransport(FakeLLMTransport())` and assert
    that the fake was called only once across N requests
    with the same key.
  - Composable: chain with a metrics transport, a
    logging transport, etc.

When NOT to use
---------------

  - Streaming: this transport is for the full-response
    call only. The streaming path bypasses the cache.
  - Non-deterministic models: if the model has
    temperature > 0, caching the output breaks the
    contract — same input should give different output.
    The cache does not check temperature; callers are
    responsible for using temperature=0 when they want
    caching to be safe.
  - Long-lived caches with PII: the cache stores the
    full prompt + response in memory. If the response
    contains secrets, use TTL or Redis with encryption.
"""

from __future__ import annotations

import asyncio
import json
import time as _time
from typing import Any, Optional

from kntgraph.tools.llm_transport import LLMRequest, LLMTransport

from ..llm import _to_llm_response
from ._in_memory import InMemoryCacheStorage
from ._protocol import AsyncCacheStorage, _CacheEntry


class CachingLLMTransport(LLMTransport):
    """
    Decorator transport: caches completions by
    `idempotency_key`. Wrap an inner transport (typically
    `LiteLLMTransport` or `FakeLLMTransport` in tests).

    Cache key shape: `(idempotency_key, model, response_format)`.
    Different models with the same key are stored
    separately — useful for A/B comparisons.

    Parameters
    ----------

      inner: the underlying transport to call on misses.
      ttl_s: optional expiration (seconds). None = never.
      storage: an `AsyncCacheStorage` (e.g.
        `InMemoryCacheStorage` (default) or
        `RedisCacheStorage`). When None, an
        `InMemoryCacheStorage` is created lazily.
      name: human-readable name for metrics.
      time_fn: clock used for entry timestamps and TTL
        checks. Default `time.monotonic`. Override in
        tests to control the clock.
    """

    def __init__(
        self,
        inner: LLMTransport,
        *,
        ttl_s: Optional[float] = None,
        storage: Optional[AsyncCacheStorage] = None,
        name: str = "caching",
        time_fn=None,
    ) -> None:
        """
        `time_fn` is the clock used for entry timestamps and
        TTL checks. Defaults to `time.monotonic`. Override in
        tests to control the clock.
        """
        self._inner = inner
        self._ttl_s = ttl_s
        self._name = name
        self._time_fn = time_fn if time_fn is not None else _time.monotonic
        # Default storage: in-process, maxsize=1024, LRU.
        self._storage: AsyncCacheStorage = (
            storage if storage is not None else InMemoryCacheStorage()
        )
        # Per-instance lock for counter updates under
        # concurrent calls. The storage has its own
        # internal lock; this keeps `metrics` consistent.
        self._lock = asyncio.Lock()
        # Counters. `size` is the transport's view of how
        # many entries it has stored; it tracks the
        # in-process counter for the local storage and is
        # best-effort for the Redis storage (the Redis
        # server may have evicted keys under
        # `maxmemory-policy` that we did not observe —
        # see ADR-011 §3).
        self.hits: int = 0
        self.misses: int = 0
        self.stores: int = 0
        self.errors: int = 0
        self._size: int = 0

    @property
    def inner(self) -> LLMTransport:
        return self._inner

    @property
    def storage(self) -> AsyncCacheStorage:
        return self._storage

    @property
    def size(self) -> int:
        return self._size

    @property
    def metrics(self) -> dict[str, Any]:
        """
        Snapshot of counters. `name` is a str; the rest are
        ints. Typed as `dict[str, Any]` to keep the literal
        construction simple.
        """
        return {
            "name": self._name,
            "size": self.size,
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "errors": self.errors,
        }

    async def __call__(
        self,
        request: "LLMRequest",
    ) -> dict:
        """
        Cached completion. On hit, returns the stored dict
        without calling the inner transport.

        Without `idempotency_key`, the cache is bypassed
        (the call goes through to the inner transport
        every time). Pass the key explicitly to enable
        caching.

        Iter 28 FU 3: the method is now ``__call__``
        (not ``complete``). The request is an
        ``LLMRequest`` value object that bundles the
        9 keyword parameters of the old ``complete()``
        method.
        """
        # 1. Cache lookup
        idempotency_key = request.idempotency_key
        if idempotency_key is not None:
            cache_key = self._make_key(
                idempotency_key,
                request.model,
                request.response_format,
            )
            entry = await self._storage.get(cache_key)
            now = self._time_fn()
            if entry is not None and not entry.is_expired(self._ttl_s, now):
                self.hits += 1
                return entry.completion
            if entry is not None and entry.is_expired(self._ttl_s, now):
                # Expired — drop and proceed to inner
                await self._storage.delete(cache_key)
                async with self._lock:
                    self._size = max(0, self._size - 1)

        # 2. Cache miss — call inner
        self.misses += 1
        try:
            completion = await self._inner(request)
        except Exception:
            self.errors += 1
            raise

        # 3. Store
        if idempotency_key is not None:
            cache_key = self._make_key(
                idempotency_key,
                request.model,
                request.response_format,
            )
            # Try to extract usage / cost for the entry
            # metadata (best-effort — does not fail the
            # call).
            try:
                resp = _to_llm_response(completion, request.model, 0.0)
                prompt_tokens = resp.usage.prompt_tokens
                completion_tokens = resp.usage.completion_tokens
                cost_usd = resp.cost_usd
            except Exception:
                prompt_tokens = 0
                completion_tokens = 0
                cost_usd = None
            entry = _CacheEntry(
                completion=completion,
                model=request.model,
                stored_at=self._time_fn(),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
            )
            await self._storage.set(cache_key, entry)
            async with self._lock:
                self._size += 1
            self.stores += 1

        return completion

    async def invalidate(self, idempotency_key: str) -> None:
        """
        Drop every cache entry for the given
        ``idempotency_key``.

        Different ``(model, response_format)`` variants
        are stored under the same prefix
        (``{idempotency_key}|...``) so a single prefix
        match clears them all. Useful in tests and after
        a model update.

        The storage's ``clear_prefix`` does the actual
        work; this method delegates and reconciles the
        transport's local ``_size`` counter. Reconciliation
        matters because Redis may have evicted entries
        that the transport never observed (so the local
        counter can drift above reality) — a simple
        ``self._size = 0`` after invalidation would
        under-report on subsequent ``metrics`` reads.

        Pre-condition: the storage implements
        ``clear_prefix`` and ``count``. Both built-in
        storages do; custom storages that don't will
        raise ``AttributeError`` — that's intentional:
        we want a hard failure rather than silent no-op
        (which was the previous behaviour of the
        ``getattr`` fallback).
        """
        await self._storage.clear_prefix(f"{idempotency_key}|")
        # Reconcile via the storage's authoritative count.
        async with self._lock:
            self._size = await self._storage.count()

    async def clear(self) -> None:
        """Drop all entries. Used in tests and emergency
        recovery.

        Reconciles the local ``_size`` counter via
        ``storage.count()`` the same way ``invalidate``
        does — keeps the metric honest when the
        storage's eviction policy evicts entries behind
        the transport's back.
        """
        clear = getattr(self._storage, "clear", None)
        if not callable(clear):
            raise AttributeError(
                f"{type(self._storage).__name__} does not "
                f"implement clear(); cannot clear the cache"
            )
        await clear()
        async with self._lock:
            self._size = await self._storage.count()

    @staticmethod
    def _make_key(
        idempotency_key: str,
        model: str,
        response_format: Optional[dict],
    ) -> str:
        # response_format may be a dict (JSON schema) or
        # None. Serialize deterministically.
        rf = json.dumps(response_format, sort_keys=True, default=str)
        return f"{idempotency_key}|{model}|{rf}"
