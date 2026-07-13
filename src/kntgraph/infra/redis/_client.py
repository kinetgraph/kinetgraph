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


@runtime_checkable
class PipelineLike(Protocol):
    """Subset of ``redis.asyncio.client.Pipeline`` used by
    the EventLog + the short-memory adapters.

    Method coverage was audited against all module-level
    call sites that consume the result of
    ``client.pipeline()`` (the EventLog idempotency
    claim, the Session/Profile/Continuity cache writers,
    the tool cache adapter). Adding a new pipeline op at
    a call site must be followed by declaring it here.
    """

    def xadd(
        self,
        name: str,
        fields: dict,
        maxlen: int | None = None,
    ) -> "PipelineLike": ...

    def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
    ) -> "PipelineLike": ...

    def delete(self, *keys: str) -> "PipelineLike": ...

    def hset(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        mapping: dict | None = None,
    ) -> "PipelineLike": ...

    def expire(self, key: str, seconds: int) -> "PipelineLike": ...

    async def execute(self) -> list: ...


@runtime_checkable
class RedisLike(Protocol):
    """Async Redis client — framework-level view.

    The framework treats this as opaque. Methods not listed
    here are not part of the framework's contract.
    """

    # String / key-value
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

    # Hash
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
        key: str,
        match: str | None = None,
        count: int | None = None,
    ) -> AsyncIterator: ...

    # Streams
    async def xadd(
        self,
        name: str,
        fields: dict,
        maxlen: int | None = None,
    ) -> bytes | str: ...
    async def xrange(
        self,
        name: str,
        min: str,
        max: str,
        count: int | None = None,
    ) -> list: ...
    async def xrevrange(
        self,
        name: str,
        max: str,
        min: str,
        count: int | None = None,
    ) -> list: ...
    async def xinfo_stream(self, name: str) -> dict: ...
    async def xdel(self, name: str, *ids: str) -> int: ...

    # Streams -- Consumer Groups (ADR-036 / Tool Worker Pattern).
    # Method coverage matches ``redis.asyncio.Redis`` v7.x.
    async def xgroup_create(
        self,
        name: str,
        groupname: str,
        id: str = "$",
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
        min: str,
        max: str,
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


__all__ = ["PipelineLike", "RedisLike"]
