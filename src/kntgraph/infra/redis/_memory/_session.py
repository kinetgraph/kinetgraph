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
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

import structlog

from kntgraph.core.result import Err, Ok, Result

from ....core._typing import JsonValue
from .._client import RedisLike
from .._codec import decode_value
from .._errors import (
    MemoryDecodeError,
    MemoryError,
    MemoryMiss,
    MemorySerializationError,
)
from .._prefix import namespaced
from .._translation import translate_redis_call
from ._adapter import CacheRecord

logger = structlog.get_logger()


@dataclass(frozen=True)
class RedisSessionStorage:
    """JSON-encoded cache via ``SET key value EX ttl``.

    ADR-076 -- ``key_prefix`` is the namespace prefix the
    storage was built with; the field is recorded here for
    introspection / observability but the keys are
    caller-supplied (the manager builds them), so the
    prefix is applied at the caller boundary. Empty
    string preserves the pre-076 wire format
    byte-for-byte (the same as ``key_prefix=""`` callers
    passing the unprefixed key they always did).
    """

    client: RedisLike
    ttl_seconds: int | None = None
    key_prefix: str = ""

    def _k(self, key: str) -> str:
        """Compose a namespaced key. Kept for symmetry with
        :class:`RedisDLQStorage` even though the Session
        adapter currently receives fully-built keys from
        the manager.
        """
        return namespaced(self.key_prefix, key)

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
        result = await translate_redis_call(
            self.client.get(key),
            op_name="session_storage.get_record",
            error_cls=MemoryError,
            key=key,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        raw = result.ok_value()
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
        ttl_seconds: int | None = None,
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
        result = await translate_redis_call(
            self.client.set(key, payload, ex=effective_ttl),
            op_name="session_storage.put_record",
            error_cls=MemoryError,
            key=key,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(None)

    async def delete_record(self, key: str) -> Result[None, MemoryError]:
        result = await translate_redis_call(
            self.client.delete(key),
            op_name="session_storage.delete_record",
            error_cls=MemoryError,
            key=key,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(None)

    async def iter_keys(self, prefix: str) -> AsyncIterator[str]:
        async for key in self.client.scan_iter(match=f"{prefix}*", count=100):
            decoded = decode_value(key) or ""
            if decoded.startswith(prefix):
                yield decoded

    # ------------------------------------------------------------ fold cursor (P4)

    async def read_fold_cursor(self, key: str) -> Result[str | None, MemoryError]:
        """
        Read the fold cursor from a plain string key.

        Session tier uses ``GET <key>:fold_cursor`` —
        the cursor is a string (Redis Stream id), not
        part of the JSON payload. No TTL is checked
        here (the parallel key inherits the cache's
        TTL on write).

        Returns ``Ok(None)`` on miss; ``Err(MemoryError)``
        on Redis-side failure (per ADR-077).
        """
        result = await translate_redis_call(
            self.client.get(key),
            op_name="session_storage.read_fold_cursor",
            error_cls=MemoryError,
            key=key,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        raw = result.ok_value()
        if raw is None:
            return Ok(None)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return Ok(str(raw))

    async def write_fold_cursor(
        self,
        key: str,
        cursor: str,
        *,
        ttl_seconds: int | None = None,
    ) -> Result[None, MemoryError]:
        """
        Persist the fold cursor at the parallel
        ``<key>:fold_cursor`` with the same TTL policy
        as the cache payload (honour
        ``ttl_seconds`` first, then
        ``self.ttl_seconds``, then no TTL).
        """
        effective_ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        result = await translate_redis_call(
            self.client.set(key, cursor, ex=effective_ttl),
            op_name="session_storage.write_fold_cursor",
            error_cls=MemoryError,
            key=key,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(None)

    async def delete_fold_cursor(self, key: str) -> Result[None, MemoryError]:
        """Drop the fold cursor at ``<key>:fold_cursor``.

        Idempotent: a missing key returns ``Ok(None)`` so the
        caller can use this on a hot path without first
        checking for existence.
        """
        result = await translate_redis_call(
            self.client.delete(key),
            op_name="session_storage.delete_fold_cursor",
            error_cls=MemoryError,
            key=key,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(None)


__all__ = ["RedisSessionStorage"]
