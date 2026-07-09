# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
RedisAPIKeyStorage — Redis impl of APIKeyStorage.

Iteration 3 (ADR-019). Owns the Redis I/O and the key prefix
``knt:api:keys:<digest>``. Does NOT decode the wire format;
the verifier does that.

Result contract (AGENTS.md §6): see ``APIKeyStorage``
docstring for the full contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import structlog

from kntgraph.core.result import Err, Ok, Result

from .._client import RedisLike
from .._errors import MemoryError


logger = structlog.get_logger()

# Key prefix. Centralised here so the verifier does not
# need to know the wire convention.
KEY_PREFIX = "knt:api:keys:"


def storage_key(digest: str) -> str:
    """Build the Redis key for an API key binding."""
    return f"{KEY_PREFIX}{digest}"


@dataclass(frozen=True)
class RedisAPIKeyStorage:
    """Redis impl of :class:`APIKeyStorage`."""

    client: RedisLike

    async def lookup(self, digest: str) -> Result[Optional[bytes], MemoryError]:
        """Look up a key binding by digest.

        Returns ``Ok(None)`` on miss; ``Err(MemoryError)`` on
        Redis failure. The raw bytes are returned untouched
        — the verifier owns the wire format decode.
        """
        try:
            raw = await self.client.get(storage_key(digest))
        except Exception as e:
            logger.warning(
                "api_key_storage.lookup.redis_error",
                digest=digest,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        if raw is None:
            return Ok(None)
        # ``decode_responses=False`` keeps raw bytes; if
        # the caller flipped it, accept str too.
        if isinstance(raw, (bytes, bytearray)):
            return Ok(bytes(raw))
        if isinstance(raw, str):
            return Ok(raw.encode("utf-8"))
        return Err(MemoryError(f"unexpected redis return type: {type(raw).__name__}"))

    async def store(self, digest: str, payload: bytes) -> Result[None, MemoryError]:
        """Persist a key binding (raw bytes)."""
        try:
            await self.client.set(storage_key(digest), payload)
        except Exception as e:
            logger.warning(
                "api_key_storage.store.redis_error",
                digest=digest,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        return Ok(None)

    async def delete(self, digest: str) -> Result[None, MemoryError]:
        """Remove a key binding. Idempotent."""
        try:
            await self.client.delete(storage_key(digest))
        except Exception as e:
            logger.warning(
                "api_key_storage.delete.redis_error",
                digest=digest,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        return Ok(None)


__all__ = ["KEY_PREFIX", "RedisAPIKeyStorage", "storage_key"]
