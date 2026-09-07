# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
RedisWorldCheckpointStorage — Redis impl of WorldCheckpointStorage.

Iteration 5 (ADR-019). Owns the Redis I/O for the per-agent
World checkpoint (one key per agent, pickled payload).

Wire format: ``SET knt:world:{agent_id} <pickled payload> EX <ttl>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import structlog

from kntgraph.core.result import Err, Ok, Result

from .._client import RedisLike
from .._errors import MemoryError


logger = structlog.get_logger()

# Re-export the legacy constant for backward compat.
WORLD_CHECKPOINT_KEY_TEMPLATE = "knt:world:{agent_id}"

# The cursor lives in its own small key (ADR-068 §3.5 P5b):
# a ``GET cursor`` (a ~20-byte read) is enough to answer "is
# there anything new for this agent?" without ever touching
# the pickled World payload. The cursor key shares the
# checkpoint's TTL on save, so the pair expires together.
WORLD_CURSOR_KEY_TEMPLATE = "knt:world-cursor:{agent_id}"


def storage_key(agent_id: str) -> str:
    """Build the Redis key for an agent's checkpoint."""
    return WORLD_CHECKPOINT_KEY_TEMPLATE.format(agent_id=agent_id)


def cursor_key(agent_id: str) -> str:
    """Build the Redis key for an agent's stream cursor."""
    return WORLD_CURSOR_KEY_TEMPLATE.format(agent_id=agent_id)


@dataclass(frozen=True)
class RedisWorldCheckpointStorage:
    """Redis impl of :class:`WorldCheckpointStorage`."""

    client: RedisLike

    async def load(self, agent_id: str) -> Result[Optional[bytes], MemoryError]:
        """Load the pickled checkpoint payload (or None on miss)."""
        try:
            raw = await self.client.get(storage_key(agent_id))
        except Exception as e:
            logger.warning(
                "world_checkpoint_storage.load.redis_error",
                agent_id=agent_id,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        if raw is None:
            return Ok(None)
        if isinstance(raw, (bytes, bytearray)):
            return Ok(bytes(raw))
        return Err(MemoryError(f"unexpected redis return type: {type(raw).__name__}"))

    async def load_cursor(self, agent_id: str) -> Result[Optional[str], MemoryError]:
        """Load the agent's stream cursor (or None on miss).

        The cheap probe of the P5b split: callers read this
        small key first and only escalate to the full
        ``load`` (pickled World) when there is actually new
        work past the cursor.
        """
        try:
            raw = await self.client.get(cursor_key(agent_id))
        except Exception as e:
            logger.warning(
                "world_checkpoint_storage.load_cursor.redis_error",
                agent_id=agent_id,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        if raw is None:
            return Ok(None)
        if isinstance(raw, (bytes, bytearray)):
            return Ok(bytes(raw).decode("utf-8"))
        return Err(MemoryError(f"unexpected redis return type: {type(raw).__name__}"))

    async def save(
        self,
        agent_id: str,
        payload: bytes,
        *,
        ttl_seconds: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Result[None, MemoryError]:
        """Persist the checkpoint with sliding TTL.

        When ``cursor`` is given, the companion cursor key is
        written in the same call so the two never disagree
        (the cursor is derived from the payload's
        ``last_stream_id`` at the facade level).
        """
        try:
            if cursor is not None:
                cursor_payload: bytes = cursor.encode("utf-8")
                pipe = self.client.pipeline(transaction=True)
                pipe.set(storage_key(agent_id), payload, ex=ttl_seconds)
                pipe.set(cursor_key(agent_id), cursor_payload, ex=ttl_seconds)
                await pipe.execute()
            else:
                await self.client.set(storage_key(agent_id), payload, ex=ttl_seconds)
        except Exception as e:
            logger.warning(
                "world_checkpoint_storage.save.redis_error",
                agent_id=agent_id,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        return Ok(None)

    async def discard(self, agent_id: str) -> Result[None, MemoryError]:
        """Drop the checkpoint. Idempotent: the companion
        cursor key goes with it (UNLINK is a no-op for a
        missing key)."""
        try:
            await self.client.unlink(storage_key(agent_id), cursor_key(agent_id))
        except Exception as e:
            logger.warning(
                "world_checkpoint_storage.discard.redis_error",
                agent_id=agent_id,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        return Ok(None)


__all__ = [
    "RedisWorldCheckpointStorage",
    "WORLD_CHECKPOINT_KEY_TEMPLATE",
    "WORLD_CURSOR_KEY_TEMPLATE",
    "cursor_key",
    "storage_key",
]
