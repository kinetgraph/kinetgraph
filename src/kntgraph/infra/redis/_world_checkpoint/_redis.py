# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
RedisWorldCheckpointStorage — Redis impl of WorldCheckpointStorage.

Iteration 5 (ADR-019). Owns the Redis I/O for the per-agent
World checkpoint (one key per agent, pickled payload).

Wire format: ``SET fmh:world:{agent_id} <pickled payload> EX <ttl>``.
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
WORLD_CHECKPOINT_KEY_TEMPLATE = "fmh:world:{agent_id}"


def storage_key(agent_id: str) -> str:
    """Build the Redis key for an agent's checkpoint."""
    return WORLD_CHECKPOINT_KEY_TEMPLATE.format(agent_id=agent_id)


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

    async def save(
        self,
        agent_id: str,
        payload: bytes,
        *,
        ttl_seconds: Optional[int] = None,
    ) -> Result[None, MemoryError]:
        """Persist the checkpoint with sliding TTL."""
        try:
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
        """Drop the checkpoint."""
        try:
            await self.client.delete(storage_key(agent_id))
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
    "storage_key",
]
