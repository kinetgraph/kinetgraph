# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
CachingLLMTransport — decorator transport that memoizes
completions by `idempotency_key`.

The transport is the unit of cache. The `LiteLLMTool`
forwards the framework's `idempotency_key` (from the
caller — usually a Role) into `transport(...)` via
kwarg. A caching transport intercepts that kwarg and
returns the cached response on a hit, or delegates to
the inner transport and stores the result on a miss.

Module layout
-------------

The cache package is split into 4 sub-modules, each
focused on a single concern:

  - ``_protocol``: the value object (``_CacheEntry``)
    and the abstract storage contract
    (``AsyncCacheStorage`` Protocol).
  - ``_in_memory``: the in-process LRU implementation
    (``InMemoryCacheStorage``).
  - ``_redis``: the multi-process adapter
    (``RedisCacheAdapter``) and its encode/decode
    helpers.
  - ``_transport``: the ``CachingLLMTransport``
    decorator that turns any ``LLMTransport`` into a
    cached one.

The transport depends on the Protocol, not on either
implementation. The two implementations are
interchangeable: pass ``InMemoryCacheStorage`` for
single-process (default) or ``RedisCacheAdapter`` for
multi-process.

Public re-exports
-----------------

The classes that callers actually use are re-exported
here so ``from kntgraph.agents.tools.cache import X`` keeps
working after the split.
"""

from __future__ import annotations

from ._in_memory import InMemoryCacheStorage
from ._protocol import AsyncCacheStorage, _CacheEntry
from ._redis import RedisCacheAdapter
from ._transport import CachingLLMTransport


__all__ = [
    "AsyncCacheStorage",
    "CachingLLMTransport",
    "InMemoryCacheStorage",
    "RedisCacheAdapter",
    "_CacheEntry",
]
