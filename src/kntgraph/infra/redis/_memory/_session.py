# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
RedisSessionStorage — JSON-backed memory cache.

Session storage uses ``SET key value EX ttl`` with a
JSON-encoded payload. Sessions have a single-part identity
(``session_id``) and a TTL (default 24h from Settings).

This module is part of Iteration 2 (ADR-019). The base
class ``BaseShortTermMemory`` consumes it via the
``ShortMemoryStorage`` Protocol.

Result contract (AGENTS.md §6):

  - ``get_record``  returns ``Ok(mapping)`` / ``Ok(None)`` /
    ``Err(MemoryDecodeError)``.
  - ``put_record``  returns ``Ok(None)`` /
    ``Err(MemorySerializationError)``.
  - ``delete_record`` returns ``Ok(None)``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Mapping, Optional

from ....core._typing import JsonValue

import structlog

from kntgraph.core.result import Err, Ok, Result

from .._client import RedisLike
from .._codec import decode_value
from ._adapter import CacheRecord
from .._errors import (
    MemoryDecodeError,
    MemoryError,
    MemoryMiss,
    MemorySerializationError,
)


logger = structlog.get_logger()


@dataclass(frozen=True)
class RedisSessionStorage:
    """JSON-encoded cache via ``SET key value EX ttl``."""

    client: RedisLike
    ttl_seconds: Optional[int] = None

    async def get_record(
        self, key: str
    ) -> Result[Mapping[str, JsonValue], MemoryError]:
        """Read a JSON-encoded payload.

        Returns ``Err(MemoryMiss(key))`` on miss;
        ``Err(MemoryDecodeError(...))`` on corrupt JSON.
        Redis transport failures surface as
        ``Err(MemoryError(...))`` with the raw exception
        string.
        """
        try:
            raw = await self.client.get(key)
        except Exception as e:
            logger.warning(
                "session_storage.get_record.redis_error",
                key=key,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}", key=key))
        if raw is None:
            return Err(MemoryMiss(key))
        decoded = decode_value(raw)
        if decoded is None:
            return Err(MemoryMiss(key))
        try:
            return Ok(json.loads(decoded))
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(
                "session_storage.get_record.invalid_json",
                key=key,
                error=str(e),
            )
            return Err(MemoryDecodeError(f"invalid JSON: {e}", key=key))

    async def put_record(
        self,
        key: str,
        record: CacheRecord,
        *,
        ttl_seconds: Optional[int] = None,
    ) -> Result[None, MemoryError]:
        """Persist a JSON-encoded payload via ``SET`` with optional TTL."""
        try:
            payload = json.dumps(
                dict(record) if isinstance(record, Mapping) else record,
                default=str,
            )
        except (TypeError, ValueError) as e:
            return Err(MemorySerializationError(f"cannot serialize: {e}", key=key))
        effective_ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        try:
            await self.client.set(key, payload, ex=effective_ttl)
        except Exception as e:
            logger.warning(
                "session_storage.put_record.redis_error",
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
                "session_storage.delete_record.redis_error",
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


__all__ = ["RedisSessionStorage"]
