# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
RedisProfileStorage — Hash-backed memory cache.

Profile storage uses ``HSET key field value`` + ``HGETALL key``.
Profiles are long-lived (no TTL by default) and have a
two-part identity (``tenant_id``, ``user_id``).

This module is part of Iteration 2 (ADR-019). The base
class ``BaseShortTermMemory`` consumes it via the
``ShortMemoryStorage`` Protocol.

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
class RedisProfileStorage:
    """Hash-encoded cache via ``DEL + HSET + EXPIRE`` pipeline.

    ADR-076 -- ``key_prefix`` is recorded for
    introspection; keys are caller-supplied (built by
    :meth:`ProfileManager.cache_key`) so the prefix is
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
        Hash); decode errors surface as ``Err``.

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`;
        ``asyncio.CancelledError`` propagates so
        operator-driven shutdown works.
        """
        result = await translate_redis_call(
            self.client.hgetall(key),
            op_name="profile_storage.get_record",
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
        """Persist a Hash mapping via DEL+HSET+EXPIRE pipeline.

        The DEL+HSET sequence is not atomic against a
        concurrent writer — a parallel fold could read the
        empty key between the two commands. For our case
        (single-tenant cache writes) this is acceptable;
        the EventLog is the source of truth on conflict.
        """
        # All Mapping[str, JsonValue] values must be string-coercible
        # for Hash storage; reject early with a typed error.
        # The catch is narrow (``TypeError``, ``ValueError``) —
        # the dict comprehension's ``str(...)`` coercion and
        # a malformed ``Mapping.items()`` are the only
        # failure paths; per ADR-077 the storage layer
        # does not catch ``Exception`` blindly.
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

        # The pipeline is built and executed inside a
        # small async closure so :func:`translate_redis_call`
        # can await it under the canonical catch list.
        # ``.execute()`` is the only call that can raise
        # the third-party exceptions; the rest are
        # in-process queuing.
        async def _run_pipeline() -> None:
            pipe = self.client.pipeline(transaction=True)
            pipe.delete(key)
            pipe.hset(key, mapping=mapping)
            if effective_ttl:
                pipe.expire(key, effective_ttl)
            await pipe.execute()

        result = await translate_redis_call(
            _run_pipeline(),
            op_name="profile_storage.put_record",
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
            op_name="profile_storage.delete_record",
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

        Profile tier is long-lived (no TTL), so the
        cursor is a plain ``GET <key>:fold_cursor`` —
        it survives as long as the cache itself
        (Profile keys are not expired by default).

        Returns ``Ok(None)`` on miss; ``Err(MemoryError)``
        on Redis-side failure (per ADR-077).
        """
        result = await translate_redis_call(
            self.client.get(key),
            op_name="profile_storage.read_fold_cursor",
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
        ``<key>:fold_cursor``. The base passes the
        manager-configured TTL; Profile's policy is
        **no TTL** (matches the cache payload policy),
        so this implementation ignores ``ttl_seconds``
        and never sets an ``EXPIRE`` — the cursor lives
        as long as the cache.

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        result = await translate_redis_call(
            self.client.set(key, cursor),
            op_name="profile_storage.write_fold_cursor",
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
            op_name="profile_storage.delete_fold_cursor",
            error_cls=MemoryError,
            key=key,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(None)


__all__ = ["RedisProfileStorage"]
