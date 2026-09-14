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


class RedisWorldCheckpointStorage:
    """Redis impl of :class:`WorldCheckpointStorage`.

    Holds a ``RedisLike`` client and (optionally) the name of
    the consumer group used by :class:`WorkerManager` for the
    ``knt:tools:<name>:queue`` streams. The default group name
    (``"fmh_tool_workers"``) matches the default in
    ``tools/manager.py``; operators with a custom group should
    pass it explicitly.
    """

    DEFAULT_TOOL_GROUP = "fmh_tool_workers"

    def __init__(
        self,
        client: RedisLike,
        *,
        tool_group_name: str = DEFAULT_TOOL_GROUP,
    ) -> None:
        self.client = client
        self._tool_group_name = tool_group_name

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

    # ------------------------------------------------------------------
    # Stream inspection (ADR-075 Tier 4: ``stuck_in_queue`` query).
    #
    # The Protocol exposes two primitives (``queue_length``,
    # ``pending_count``) so the dispatcher's observability
    # layer stays inside the adapter boundary. Both methods
    # fail-soft: a missing key or a Redis hiccup returns ``0``
    # — the dispatcher's query treats ``0`` as "no stuck this
    # tick" and reruns next tick.
    #
    # ``group_name`` defaults to ``"fmh_tool_workers"`` to
    # match :class:`WorkerManager` (``tools/manager.py``).
    # Operators with a custom group should pass it explicitly
    # via ``RedisWorldCheckpointStorage(tool_group_name=...)``.
    #
    # We use only methods declared on the ``RedisLike``
    # Protocol (``xinfo_stream`` and ``xpending_range``) — not
    # the bare ``xlen`` / ``xpending`` — so the implementation
    # is compatible with any Redis adapter that satisfies
    # ``RedisLike`` (e.g. in-memory test stubs, fakeredis).

    async def queue_length(self, stream_key: str) -> int:
        """Return ``XLEN stream_key`` (or 0 on missing/error).

        Uses ``XINFO STREAM`` (already on ``RedisLike``) rather
        than ``XLEN`` directly so the storage stays within the
        typed Redis Protocol — adapters like ``fakeredis`` that
        implement ``RedisLike`` but omit ``XLEN`` still work.
        """
        try:
            info = await self.client.xinfo_stream(stream_key)
        except Exception as e:
            # Missing stream → no work, no stuck.
            logger.debug(
                "world_checkpoint_storage.queue_length.miss_or_error",
                stream_key=stream_key,
                error=str(e),
            )
            return 0
        length = info.get("length") if isinstance(info, dict) else None
        try:
            return int(length) if length is not None else 0
        except (TypeError, ValueError):
            return 0

    async def pending_count(self, stream_key: str) -> int:
        """Return a positive count if ``stream_key`` has any
        pending (un-acked) entries in its tool consumer group.

        Returns ``1`` when at least one entry is in the PEL,
        ``0`` otherwise. The dispatcher only needs to know
        whether ANY worker is holding messages (the "no
        consumer" half of the stuck signal); the exact count
        is not actionable for recovery.

        The Redis ``XINFO GROUPS`` summary form would give
        the exact PEL count but is NOT on the ``RedisLike``
        Protocol (only ``xpending_range`` is). Reading one
        entry from the range with ``count=1`` is the
        cheapest "is PEL empty?" probe available without
        expanding the Protocol — full enumeration would be
        wasteful for the dispatcher's binary stuck/unstuck
        decision.
        """
        try:
            entries = await self.client.xpending_range(
                name=stream_key,
                groupname=self._tool_group_name,
                min="-",
                max="+",
                count=1,
            )
        except Exception as e:
            # Missing stream / missing group → no PEL.
            logger.debug(
                "world_checkpoint_storage.pending_count.miss_or_error",
                stream_key=stream_key,
                group_name=self._tool_group_name,
                error=str(e),
            )
            return 0
        return 1 if entries else 0


__all__ = [
    "RedisWorldCheckpointStorage",
    "WORLD_CHECKPOINT_KEY_TEMPLATE",
    "WORLD_CURSOR_KEY_TEMPLATE",
    "cursor_key",
    "storage_key",
]
