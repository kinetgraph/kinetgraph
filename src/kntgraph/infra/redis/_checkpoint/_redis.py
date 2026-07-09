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

Single Redis Hash at ``fmh:reactive:checkpoints`` with one
field per agent. Each field value is a JSON-encoded dict
with the keys ``last_event_id``, ``last_stream_id``,
``confirmed_at``, ``state_hash``.

Result contract (AGENTS.md §6): see ``CheckpointStorage``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

import structlog

from kntgraph.core.result import Err, Ok, Result

from .._client import RedisLike
from .._errors import MemoryDecodeError, MemoryError


logger = structlog.get_logger()

# Key prefix. Centralised here so the store does not
# need to know the wire convention.
CHECKPOINT_KEY = "fmh:reactive:checkpoints"


@dataclass(frozen=True)
class RedisCheckpointStorage:
    """Redis impl of :class:`CheckpointStorage`."""

    client: RedisLike

    async def load(
        self, agent_id: str
    ) -> Result[Optional[Mapping[str, str]], MemoryError | MemoryDecodeError]:
        """Load a checkpoint by agent_id.

        Returns ``Ok(None)`` on miss; ``Ok(dict)`` on hit;
        ``Err(MemoryDecodeError)`` on corrupt JSON;
        ``Err(MemoryError)`` on Redis failure.
        """
        try:
            raw = await self.client.hget(CHECKPOINT_KEY, agent_id)
        except Exception as e:
            logger.warning(
                "checkpoint_storage.load.redis_error",
                agent_id=agent_id,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
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
        """Persist a checkpoint (JSON-encoded)."""
        try:
            encoded = json.dumps(dict(payload), default=str)
            await self.client.hset(CHECKPOINT_KEY, agent_id, encoded)
        except Exception as e:
            logger.warning(
                "checkpoint_storage.save.redis_error",
                agent_id=agent_id,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        return Ok(None)

    async def load_all(
        self,
    ) -> Result[Mapping[str, Mapping[str, str]], MemoryError]:
        """Load every checkpoint. Malformed entries are skipped."""
        try:
            raw = await self.client.hgetall(CHECKPOINT_KEY)
        except Exception as e:
            logger.warning(
                "checkpoint_storage.load_all.redis_error",
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
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
        try:
            await self.client.hdel(CHECKPOINT_KEY, agent_id)
        except Exception as e:
            logger.warning(
                "checkpoint_storage.clear.redis_error",
                agent_id=agent_id,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        return Ok(None)

    async def clear_all(self) -> Result[None, MemoryError]:
        try:
            await self.client.delete(CHECKPOINT_KEY)
        except Exception as e:
            logger.warning(
                "checkpoint_storage.clear_all.redis_error",
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        return Ok(None)


__all__ = ["CHECKPOINT_KEY", "RedisCheckpointStorage"]
