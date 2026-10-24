# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
RedisLike — framework-level Protocol for the Redis client.

Per AGENTS.md §1, the framework never imports third-party
libraries at the boundary. Every Redis call site accepts a
``RedisLike`` Protocol; the concrete implementation
(``redis.asyncio.Redis``) is hidden behind the adapter.

Method coverage was audited against all modules that today
import ``redis.asyncio`` directly. Only methods the
framework actually uses appear here. Adding a new method
to the call sites must be followed by declaring it in this
Protocol.

The pool behind the client is declared separately, as
``ConnectionPoolLike`` (see :mod:`._connection_pool`): the
client's surface is driven by the framework's Redis commands,
the pool's by the connection-lifecycle policy (ADR-084).

Why ``@runtime_checkable``
--------------------------

The Protocol is decorated ``@runtime_checkable`` so callers
can do ``isinstance(client, RedisLike)`` defensively (e.g.
to detect a misconfigured mock in tests). The check is
structural (duck typing), not nominal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from ._connection_pool import ConnectionPoolLike
from ._pipeline import PipelineLike


@runtime_checkable
class RedisLike(Protocol):
    """Async Redis client — framework-level view.

    The framework treats this as opaque: the concrete
    ``redis.asyncio.Redis`` never leaks past the adapter layer.

    Method groups, in the order they appear:

      - **String / key-value** — the short-memory tier (session,
        profile, continuity) reads and writes one JSON blob per
        entity. ``set(..., nx=True)`` is the idempotency claim.
      - **Hash** — the auth key-binding table (ADR-076).
      - **Streams** — the EventLog (one stream per agent) and the
        tool queues (one stream per tool), trimmed with ``maxlen``.
      - **Scan** — key enumeration for the DLQ and the sweeper.
      - **Pipelines** — the transactional path used by the
        EventLog idempotency claim.
      - **Lifecycle** — ``aclose()`` plus ``connection_pool``.
    """

    # String / key-value. The short-memory tier (session, profile,
    # continuity) reads and writes JSON blobs under one key per
    # entity; ``set(..., nx=True)`` is the idempotency claim.
    async def get(self, key: str) -> bytes | str | None: ...
    async def set(
        self,
        key: str,
        value: str | bytes,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None: ...
    async def delete(self, *keys: str) -> int: ...
    async def expire(self, key: str, seconds: int) -> bool: ...
    async def unlink(self, *keys: str) -> int: ...

    # Hash. The auth key-binding table (ADR-076) maps a digest to
    # its principal and tenant scope.
    async def hset(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        mapping: dict | None = None,
    ) -> int: ...
    async def hget(self, key: str, field: str) -> bytes | str | None: ...
    async def hgetall(self, key: str) -> dict: ...
    async def hdel(self, key: str, *fields: str) -> int: ...
    async def hsetnx(self, key: str, field: str, value: str) -> bool: ...
    async def smembers(self, key: str) -> set: ...
    async def sismember(self, key: str, value: str) -> bool: ...
    async def hincrby(self, key: str, field: str, amount: int = 1) -> int: ...
    async def hlen(self, key: str) -> int: ...
    def hscan_iter(
        self,
        name: str,
        match: str | None = None,
        count: int | None = None,
    ) -> AsyncIterator: ...

    # Streams. The EventLog stores one stream per agent; the tool
    # queues are one stream per tool. Both are trimmed with
    # ``maxlen`` (ADR-077's per-tenant cap).
    async def xadd(
        self,
        name: str,
        fields: dict,
        maxlen: int | None = None,
    ) -> bytes | str: ...
    # The Protocol guarantees ``list`` for both ``xrange`` and
    # ``xrevrange``; redis-py types them as ``StreamRangeResponse
    # | None`` because a server can reply ``*0\r\n`` (encoded as
    # Python ``None`` by some client bindings). Adapters MUST
    # route through :func:`safe_xrange` / :func:`safe_xrevrange`
    # to uphold the Protocol — a direct call would expose the
    # ``None`` case to the framework and break the consumer
    # contract (``for mid, mdata in messages`` would raise
    # ``TypeError``).
    async def xrange(
        self,
        name: str,
        min: str,  # noqa: A002 — mirrors the redis-py keyword
        max: str,  # noqa: A002 — mirrors the redis-py keyword
        count: int | None = None,
    ) -> list: ...
    async def xrevrange(
        self,
        name: str,
        max: str,  # noqa: A002 — mirrors the redis-py keyword
        min: str,  # noqa: A002 — mirrors the redis-py keyword
        count: int | None = None,
    ) -> list: ...
    async def xinfo_stream(self, name: str) -> dict: ...
    async def xdel(self, name: str, *ids: str) -> int: ...

    # Streams -- blocking read (ADR-068 Phase 1). One held
    # connection per subscriber; the read waits up to ``block``
    # milliseconds for an entry strictly after the given
    # per-stream id, then returns ``[]`` on timeout. This is
    # the wake-up transport behind ``EventLog.subscribe``.
    async def xread(
        self,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list: ...

    # Consumer groups (ADR-036).
    async def xgroup_create(
        self,
        name: str,
        groupname: str,
        id: str = "$",  # noqa: A002 — mirrors the redis-py keyword
        mkstream: bool = False,
    ) -> None: ...
    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list: ...
    async def xack(self, name: str, groupname: str, *ids: str) -> int: ...
    async def xpending_range(
        self,
        name: str,
        groupname: str,
        min: str,  # noqa: A002 — mirrors the redis-py keyword
        max: str,  # noqa: A002 — mirrors the redis-py keyword
        count: int,
        consumername: str | None = None,
    ) -> list: ...
    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str = "0-0",
        count: int | None = None,
    ) -> tuple: ...

    # Scan
    def scan_iter(
        self,
        match: str | None = None,
        count: int | None = None,
    ) -> AsyncIterator: ...

    # Pipelines
    def pipeline(self, transaction: bool = True) -> PipelineLike: ...

    # Lifecycle
    async def aclose(self) -> None: ...

    @property
    def connection_pool(self) -> ConnectionPoolLike: ...


async def safe_xrange(
    client: RedisLike,
    name: str,
    *,
    min: str = "-",  # noqa: A002 — mirrors the redis-py keyword
    max: str = "+",  # noqa: A002 — mirrors the redis-py keyword
    count: int | None = None,
) -> list:
    """``xrange`` wrapper that upholds the ``RedisLike``
    Protocol's ``list`` return contract.

    The redis-py library declares ``StreamRangeResponse |
    None``: a server reply of ``*0\r\n`` round-trips to
    ``None`` in some bindings. The framework's Protocol
    promises ``list``; this helper enforces that promise by
    converting ``None`` to ``[]`` before returning.

    Adapters must route ``xrange`` through this helper.
    """
    result = await client.xrange(name, min=min, max=max, count=count)
    return result if result is not None else []


async def safe_xrevrange(
    client: RedisLike,
    name: str,
    *,
    max: str = "+",  # noqa: A002 — mirrors the redis-py keyword
    min: str = "-",  # noqa: A002 — mirrors the redis-py keyword
    count: int | None = None,
) -> list:
    """``xrevrange`` wrapper that upholds the ``RedisLike``
    Protocol's ``list`` return contract. Mirrors
    :func:`safe_xrange` for the reverse-direction scan."""
    result = await client.xrevrange(name, max=max, min=min, count=count)
    return result if result is not None else []


__all__ = ["PipelineLike", "RedisLike", "safe_xrange", "safe_xrevrange"]
