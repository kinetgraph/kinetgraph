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

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

import structlog

from kntgraph.core.result import Err, Ok, Result

from ....core._typing import JsonValue
from .._client import RedisLike
from .._codec import decode_dict, decode_value
from .._errors import MemoryError, MemoryMiss, MemorySerializationError
from .._translation import translate_redis_call
from ._adapter import CacheRecord

logger = structlog.get_logger()


@dataclass(frozen=True)
class RedisContinuityStorage:
    """Hash-encoded cache with sliding TTL.

    ADR-076 -- ``key_prefix`` is recorded for
    introspection; keys are caller-supplied (built by
    :meth:`ContinuityManager.cache_key`) so the prefix is
    applied at the manager boundary, not here. Empty
    string preserves the pre-076 wire format.
    """

    client: RedisLike
    ttl_seconds: int | None = None
    key_prefix: str = ""

    async def get_record(
        self, key: str
    ) -> Result[Mapping[str, JsonValue], MemoryError]:
        """Read a Hash mapping via ``HGETALL``.

        Returns ``Err(MemoryMiss(key))`` on miss (empty
        Hash).

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        result = await translate_redis_call(
            self.client.hgetall(key),
            op_name="continuity_storage.get_record",
            error_cls=MemoryError,
            key=key,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        raw = result.ok_value()
        if not raw:
            return Err(MemoryMiss(key))
        return Ok(decode_dict(raw))

    async def put_record(
        self,
        key: str,
        record: CacheRecord,
        *,
        ttl_seconds: int | None = None,
    ) -> Result[None, MemoryError]:
        """Persist a Hash mapping. Sliding TTL: every write resets EXPIRE.

        The sliding TTL is the whole point of continuity:
        the cache stays warm as long as the user is active.

        Per ADR-077: the serialisation catch is narrow
        (``TypeError``, ``ValueError``); the Redis pipeline
        catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        try:
            mapping: dict[str, str] = (
                {str(k): str(v) for k, v in record.items()}
                if isinstance(record, Mapping)
                else {}
            )
        except (TypeError, ValueError) as exc:
            return Err(
                MemorySerializationError(f"cannot serialize to hash: {exc}", key=key)
            )
        effective_ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds

        async def _run_pipeline() -> None:
            pipe = self.client.pipeline(transaction=True)
            pipe.delete(key)
            pipe.hset(key, mapping=mapping)
            if effective_ttl:
                pipe.expire(key, effective_ttl)
            await pipe.execute()

        result = await translate_redis_call(
            _run_pipeline(),
            op_name="continuity_storage.put_record",
            error_cls=MemoryError,
            key=key,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(None)

    async def delete_record(self, key: str) -> Result[None, MemoryError]:
        """Remove a record. Idempotent.

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        result = await translate_redis_call(
            self.client.delete(key),
            op_name="continuity_storage.delete_record",
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

    async def read_fold_cursor(
        self, key: str
    ) -> Result[str | None, MemoryError]:
        """
        Read the fold cursor from a plain string key.

        Continuity tier uses sliding TTL — every write
        resets ``EXPIRE`` on both the cache and the
        parallel cursor key, so the cursor tracks the
        cache's freshness.

        Returns ``Ok(None)`` on miss; ``Err(MemoryError)``
        on Redis-side failure (per ADR-077).

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        result = await translate_redis_call(
            self.client.get(key),
            op_name="continuity_storage.read_fold_cursor",
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
        ``<key>:fold_cursor`` with sliding TTL (mirrors
        the cache payload's policy): every write
        refreshes the EXPIRE so the cursor and the
        cache age out together.

        The TTL priority is: explicit ``ttl_seconds``
        first (the base forwards the manager config),
        then ``self.ttl_seconds`` (the storage's own
        configured sliding window — typically 90 days),
        then no TTL.

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        effective_ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        if effective_ttl:
            set_op = self.client.set(key, cursor, ex=effective_ttl)
        else:
            set_op = self.client.set(key, cursor)
        result = await translate_redis_call(
            set_op,
            op_name="continuity_storage.write_fold_cursor",
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

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        result = await translate_redis_call(
            self.client.delete(key),
            op_name="continuity_storage.delete_fold_cursor",
            error_cls=MemoryError,
            key=key,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(None)


__all__ = ["RedisContinuityStorage"]
