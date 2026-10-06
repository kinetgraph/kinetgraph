# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
RedisCheckpointStorage — Redis impl of CheckpointStorage.

Iteration 4 (ADR-019). Owns the Redis I/O and the JSON
encode/decode. The store class (``CheckpointStore``)
consumes the storage and builds ``ReactiveCheckpoint``
domain objects from the parsed dicts.

Wire format
-----------

Single Redis Hash at ``knt:reactive:checkpoints`` with one
field per agent. Each field value is a JSON-encoded dict
with the keys ``last_event_id``, ``last_stream_id``,
``confirmed_at``, ``state_hash``.

Result contract (AGENTS.md §6): see ``CheckpointStorage``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

import structlog

from kntgraph.core.result import Err, Ok, Result

from .._client import RedisLike
from .._errors import MemoryDecodeError, MemoryError
from .._translation import translate_redis_call

logger = structlog.get_logger()

# Key prefix. Centralised here so the store does not
# need to know the wire convention.
CHECKPOINT_KEY = "knt:reactive:checkpoints"


@dataclass(frozen=True)
class RedisCheckpointStorage:
    """Redis impl of :class:`CheckpointStorage`."""

    client: RedisLike

    async def load(
        self, agent_id: str
    ) -> Result[Mapping[str, str] | None, MemoryError | MemoryDecodeError]:
        """Load a checkpoint by agent_id.

        Returns ``Ok(None)`` on miss; ``Ok(dict)`` on hit;
        ``Err(MemoryDecodeError)`` on corrupt JSON;
        ``Err(MemoryError)`` on Redis failure.

        Per ADR-077: the Redis catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`;
        the JSON decode catch is narrow
        (``json.JSONDecodeError``, ``TypeError``).
        """
        result = await translate_redis_call(
            self.client.hget(CHECKPOINT_KEY, agent_id),
            op_name="checkpoint_storage.load",
            error_cls=MemoryError,
            key=CHECKPOINT_KEY,
            agent_id=agent_id,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        raw = result.ok_value()
        if raw is None:
            return Ok(None)
        try:
            decoded_str = (
                raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            )
            return Ok(json.loads(decoded_str))
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(
                "checkpoint_storage.load.invalid_json",
                agent_id=agent_id,
                error=str(e),
            )
            return Err(MemoryDecodeError(f"invalid JSON: {e}"))

    async def save(
        self, agent_id: str, payload: Mapping[str, str]
    ) -> Result[None, MemoryError]:
        """Persist a checkpoint (JSON-encoded).

        Per ADR-077: the JSON serialisation catch is
        narrow (``TypeError``, ``ValueError``); the
        Redis catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        try:
            encoded = json.dumps(dict(payload), default=str)
        except (TypeError, ValueError) as exc:
            return Err(MemoryError(f"json encoding failed: {exc}"))
        result = await translate_redis_call(
            self.client.hset(CHECKPOINT_KEY, agent_id, encoded),
            op_name="checkpoint_storage.save",
            error_cls=MemoryError,
            key=CHECKPOINT_KEY,
            agent_id=agent_id,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(None)

    async def load_all(
        self,
    ) -> Result[Mapping[str, Mapping[str, str]], MemoryError]:
        """Load every checkpoint. Malformed entries are skipped.

        Per ADR-077: the Redis catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`;
        the per-entry JSON decode catch is narrow
        (``json.JSONDecodeError``, ``TypeError``).
        """
        result = await translate_redis_call(
            self.client.hgetall(CHECKPOINT_KEY),
            op_name="checkpoint_storage.load_all",
            error_cls=MemoryError,
            key=CHECKPOINT_KEY,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        raw = result.ok_value()
        out: dict[str, Mapping[str, str]] = {}
        for k, v in raw.items():
            agent_id = (
                k.decode("utf-8") if isinstance(k, (bytes, bytearray)) else str(k)
            )
            payload_str = (
                v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else str(v)
            )
            try:
                out[agent_id] = json.loads(payload_str)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "checkpoint_storage.load_all.skipped_malformed",
                    agent_id=agent_id,
                )
                continue
        return Ok(out)

    async def clear(self, agent_id: str) -> Result[None, MemoryError]:
        """Remove a single checkpoint. Idempotent.

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        result = await translate_redis_call(
            self.client.hdel(CHECKPOINT_KEY, agent_id),
            op_name="checkpoint_storage.clear",
            error_cls=MemoryError,
            key=CHECKPOINT_KEY,
            agent_id=agent_id,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(None)

    async def clear_all(self) -> Result[None, MemoryError]:
        """Remove every checkpoint. Idempotent.

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        result = await translate_redis_call(
            self.client.delete(CHECKPOINT_KEY),
            op_name="checkpoint_storage.clear_all",
            error_cls=MemoryError,
            key=CHECKPOINT_KEY,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(None)


__all__ = ["CHECKPOINT_KEY", "RedisCheckpointStorage"]
