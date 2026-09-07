# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
RedisContinuityStorage — Hash-backed cache with sliding TTL.

Continuity storage is similar to Profile (Hash), but the TTL
is sliding: every ``put_record`` resets the EXPIRE so the
cache entry stays fresh as long as the user is active.

The PII hash-only encoding is enforced by the caller
(``memory/continuity/cache_codec.py``), not by this storage.

This module is part of Iteration 2 (ADR-019).

Result contract (AGENTS.md §6): see ``ShortMemoryStorage``
docstring for the full contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Mapping, Optional

from ....core._typing import JsonValue

import structlog

from kntgraph.core.result import Err, Ok, Result

from .._client import RedisLike
from .._codec import decode_dict, decode_value
from ._adapter import CacheRecord
from .._errors import MemoryError, MemoryMiss, MemorySerializationError


logger = structlog.get_logger()


@dataclass(frozen=True)
class RedisContinuityStorage:
    """Hash-encoded cache with sliding TTL."""

    client: RedisLike
    ttl_seconds: Optional[int] = None

    async def get_record(
        self, key: str
    ) -> Result[Mapping[str, JsonValue], MemoryError]:
        """Read a Hash mapping via ``HGETALL``.

        Returns ``Err(MemoryMiss(key))`` on miss (empty
        Hash).
        """
        try:
            raw = await self.client.hgetall(key)
        except Exception as e:
            logger.warning(
                "continuity_storage.get_record.redis_error",
                key=key,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}", key=key))
        if not raw:
            return Err(MemoryMiss(key))
        return Ok(decode_dict(raw))

    async def put_record(
        self,
        key: str,
        record: CacheRecord,
        *,
        ttl_seconds: Optional[int] = None,
    ) -> Result[None, MemoryError]:
        """Persist a Hash mapping. Sliding TTL: every write resets EXPIRE.

        The sliding TTL is the whole point of continuity:
        the cache stays warm as long as the user is active.
        """
        try:
            mapping: dict[str, str] = (
                {str(k): str(v) for k, v in record.items()}
                if isinstance(record, Mapping)
                else {}
            )
        except Exception as e:
            return Err(
                MemorySerializationError(f"cannot serialize to hash: {e}", key=key)
            )
        effective_ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        try:
            pipe = self.client.pipeline(transaction=True)
            pipe.delete(key)
            pipe.hset(key, mapping=mapping)
            if effective_ttl:
                pipe.expire(key, effective_ttl)
            await pipe.execute()
        except Exception as e:
            logger.warning(
                "continuity_storage.put_record.redis_error",
                key=key,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}", key=key))
        return Ok(None)

    async def delete_record(self, key: str) -> Result[None, MemoryError]:
        try:
            await self.client.delete(key)
        except Exception as e:
            logger.warning(
                "continuity_storage.delete_record.redis_error",
                key=key,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}", key=key))
        return Ok(None)

    async def iter_keys(self, prefix: str) -> AsyncIterator[str]:
        async for key in self.client.scan_iter(match=f"{prefix}*", count=100):
            decoded = decode_value(key) or ""
            if decoded.startswith(prefix):
                yield decoded

    # ------------------------------------------------------------ fold cursor (P4)

    async def read_fold_cursor(self, key: str) -> str | None:
        """
        Read the fold cursor from a plain string key.

        Continuity tier uses sliding TTL — every write
        resets ``EXPIRE`` on both the cache and the
        parallel cursor key, so the cursor tracks the
        cache's freshness.
        """
        try:
            raw = await self.client.get(key)
        except Exception as e:
            logger.warning(
                "continuity_storage.read_fold_cursor.redis_error",
                key=key,
                error=str(e),
            )
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return str(raw)

    async def write_fold_cursor(
        self,
        key: str,
        cursor: str,
        *,
        ttl_seconds: Optional[int] = None,
    ) -> Result[None, MemoryError]:
        """
        Persist the fold cursor at the parallel
        ``<key>:fold_cursor`` with sliding TTL (mirrors
        the cache payload's policy): every write
        refreshes the EXPIRE so the cursor and the
        cache age out together.

        The TTL priority is: explicit ``ttl_seconds``
        first (the base forwards the manager config),
        then ``self.ttl_seconds`` (the storage's own
        configured sliding window — typically 90 days),
        then no TTL.
        """
        effective_ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        try:
            if effective_ttl:
                await self.client.set(key, cursor, ex=effective_ttl)
            else:
                await self.client.set(key, cursor)
        except Exception as e:
            logger.warning(
                "continuity_storage.write_fold_cursor.redis_error",
                key=key,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}", key=key))
        return Ok(None)


__all__ = ["RedisContinuityStorage"]
