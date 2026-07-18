# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
RedisDLQStorage — Redis impl of DLQStorage.

Iteration 5 (ADR-019). Owns the 4 Redis keys and the
hash-based idempotency protocol. The queue class
(``DeadLetterQueue``) consumes this storage and builds
``DeadLetterEvent`` domain objects.

Wire format
-----------

  - ``knt:dlq:events``           — Stream (one entry per DLQ row)
  - ``knt:dlq:by_event_id``      — Hash:
                                   ``<event_id>:<reason>`` → stream_id
                                   (with ``PLACEHOLDER`` marker during claim)
  - ``knt:dlq:by_agent``         — Hash: ``agent_id`` → first stream id
  - ``knt:dlq:reasons``          — Hash: ``reason`` → counter

Result contract (AGENTS.md §6): see ``DLQStorage``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

import structlog

from kntgraph.core.result import Err, Ok, Result

from .._client import RedisLike
from .._codec import decode_dict, decode_value
from .._errors import MemoryError


logger = structlog.get_logger()


# Key prefix conventions. Centralised here so the queue
# does not need to know the wire convention.
DLQ_STREAM_KEY = "knt:dlq:events"
DLQ_REASON_INDEX = "knt:dlq:reasons"
DLQ_AGENT_INDEX = "knt:dlq:by_agent"
DLQ_EVENT_INDEX = "knt:dlq:by_event_id"

# Idempotency placeholder. A concurrent writer holds this
# key while it appends; the next reader treats it as a
# concurrent-insert signal (raises ``IdempotencyConflict``
# in the legacy helper).
PLACEHOLDER = "PLACEHOLDER"

# Per-stream MAXLEN. The DLQ is bounded by retention policy;
# 1M entries is the default.
MAXLEN_DEFAULT = 1_000_000

# All 4 keys, in dependency order.
ALL_KEYS: tuple[str, ...] = (
    DLQ_STREAM_KEY,
    DLQ_AGENT_INDEX,
    DLQ_EVENT_INDEX,
    DLQ_REASON_INDEX,
)


def idem_key_for(event_id: str, reason: str) -> str:
    """Build the per-(event_id, reason) idempotency key."""
    return f"{event_id}:{reason}"


@dataclass(frozen=True)
class RedisDLQStorage:
    """Redis impl of :class:`DLQStorage`."""

    client: RedisLike
    maxlen: int = MAXLEN_DEFAULT

    async def append(
        self,
        idem_key: str,
        payload: Mapping[str, str],
    ) -> Result[str, MemoryError]:
        """Append a DLQ entry idempotently on ``idem_key``.

        Wire steps:

          1. ``XADD <stream> MAXLEN <maxlen>`` — append the
             stream entry.
          2. ``HSET <event_index> PLACEHOLDER NX`` — claim
             the per-event_id slot (concurrent-insert signal).
          3. Replace placeholder with the stream id via
             ``HSET <event_index> <stream_id>``.

        If the ``HSETNX`` at step 2 fails (placeholder
        already present), a concurrent insert is in flight;
        we return ``Ok(stream_id)`` because the original
        write is still in progress and will complete. The
        idempotency contract: same ``idem_key`` → same
        stream id (the caller's dedup boundary).
        """
        try:
            # Check if already exists (sequential idempotency)
            existing = await self.client.hget(DLQ_EVENT_INDEX, idem_key)
            if existing is not None:
                return Ok(
                    existing.decode("utf-8")
                    if isinstance(existing, (bytes, bytearray))
                    else str(existing)
                )

            stream_id_bytes = await self.client.xadd(
                DLQ_STREAM_KEY,
                dict(payload),
                maxlen=self.maxlen,
            )
            # Two-phase claim: placeholder → final id.
            success = await self.client.hsetnx(DLQ_EVENT_INDEX, idem_key, PLACEHOLDER)
            stream_id = (
                stream_id_bytes.decode("utf-8")
                if isinstance(stream_id_bytes, (bytes, bytearray))
                else str(stream_id_bytes)
            )
            if not success:
                # Concurrent insert won the race to set placeholder/final id.
                # Retrieve the winner's stream_id.
                val = await self.client.hget(DLQ_EVENT_INDEX, idem_key)
                if val is not None:
                    return Ok(
                        val.decode("utf-8")
                        if isinstance(val, (bytes, bytearray))
                        else str(val)
                    )
                return Ok(PLACEHOLDER)

            await self.client.hset(DLQ_EVENT_INDEX, idem_key, stream_id)
            return Ok(stream_id)
        except Exception as e:
            logger.warning(
                "dlq_storage.append.redis_error",
                idem_key=idem_key,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))

    async def read(
        self, stream_id: str
    ) -> Result[Optional[Mapping[str, str]], MemoryError]:
        """Read a single DLQ entry by stream id."""
        try:
            messages = await self.client.xrange(
                DLQ_STREAM_KEY, min=stream_id, max=stream_id
            )
        except Exception as e:
            logger.warning(
                "dlq_storage.read.redis_error",
                stream_id=stream_id,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        if not messages:
            return Ok(None)
        _, m = messages[0]
        return Ok(decode_dict(m))

    async def list_for_agent(
        self, agent_id: str, count: int = 100
    ) -> Result[list[Mapping[str, str]], MemoryError]:
        """List DLQ entries for one agent (forward-scan from head)."""
        try:
            raw = await self.client.hget(DLQ_AGENT_INDEX, agent_id)
        except Exception as e:
            logger.warning(
                "dlq_storage.list_for_agent.redis_error",
                agent_id=agent_id,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        head = decode_value(raw)
        if head is None:
            return Ok([])
        return await self._scan_from(head, count)

    async def list_by_reason(
        self, reason: str, count: int = 100
    ) -> Result[list[Mapping[str, str]], MemoryError]:
        """List DLQ entries with a given reason (full scan)."""
        result = await self.list_all(count)
        if result.is_err():
            return result
        return Ok([m for m in result.ok_value() if m.get("reason") == reason])

    async def list_all(
        self, count: int = 100
    ) -> Result[list[Mapping[str, str]], MemoryError]:
        """List DLQ entries (full scan)."""
        try:
            messages = await self.client.xrange(
                DLQ_STREAM_KEY, min="-", max="+", count=count
            )
        except Exception as e:
            logger.warning("dlq_storage.list_all.redis_error", error=str(e))
            return Err(MemoryError(f"redis error: {e}"))
        return Ok([decode_dict(m) for _, m in messages])

    async def read_index(
        self, event_id: str, reason: str
    ) -> Result[Optional[str], MemoryError]:
        """Look up the stream id for ``(event_id, reason)``."""
        try:
            raw = await self.client.hget(
                DLQ_EVENT_INDEX, idem_key_for(event_id, reason)
            )
        except Exception as e:
            logger.warning(
                "dlq_storage.read_index.redis_error",
                event_id=event_id,
                reason=reason,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        return Ok(decode_value(raw))

    async def find_by_event_id(
        self, event_id: str
    ) -> Result[Optional[str], MemoryError]:
        """Find the first stream id for ``event_id`` across all reasons.

        Scans ``<event_id>:*`` keys of the per-event_id index.
        """
        try:
            async for _, stream_id in self.client.hscan_iter(
                DLQ_EVENT_INDEX, match=f"{event_id}:*"
            ):
                decoded = decode_value(stream_id)
                if decoded is None or decoded == PLACEHOLDER:
                    continue
                return Ok(decoded)
            return Ok(None)
        except Exception as e:
            logger.warning(
                "dlq_storage.find_by_event_id.redis_error",
                event_id=event_id,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))

    async def bump_reason_counter(
        self, reason: str, delta: int
    ) -> Result[None, MemoryError]:
        """HINCRBY the per-reason counter by ``delta``."""
        try:
            await self.client.hincrby(DLQ_REASON_INDEX, reason, delta)
        except Exception as e:
            logger.warning(
                "dlq_storage.bump_reason_counter.redis_error",
                reason=reason,
                delta=delta,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        return Ok(None)

    async def get_stats(self) -> Result[dict, MemoryError]:
        """Aggregate stats: total events, unique agents, by-reason."""
        try:
            length = 0
            try:
                info = await self.client.xinfo_stream(DLQ_STREAM_KEY)
                length = int(info.get("length", 0))
            except Exception:
                # XINFO raises when the stream does not exist;
                # treat that as 0 entries.
                length = 0
            reasons_raw = await self.client.hgetall(DLQ_REASON_INDEX)
            reasons = _decode_int_dict(reasons_raw)
            agents_count = await self.client.hlen(DLQ_AGENT_INDEX)
            return Ok(
                {
                    "total_events": length,
                    "unique_agents": agents_count,
                    "by_reason": reasons,
                }
            )
        except Exception as e:
            logger.warning("dlq_storage.get_stats.redis_error", error=str(e))
            return Err(MemoryError(f"redis error: {e}"))

    async def purge(self) -> Result[int, MemoryError]:
        """Wipe all 4 DLQ keys. Returns the number of entries purged."""
        try:
            length = 0
            try:
                info = await self.client.xinfo_stream(DLQ_STREAM_KEY)
                length = int(info.get("length", 0))
            except Exception:
                length = 0
            await self.client.delete(*ALL_KEYS)
            return Ok(length)
        except Exception as e:
            logger.warning("dlq_storage.purge.redis_error", error=str(e))
            return Err(MemoryError(f"redis error: {e}"))

    async def drop_entry(
        self,
        event_id: str,
        reason: str,
        stream_id: str,
    ) -> Result[None, MemoryError]:
        """Remove a single DLQ entry.

        Skips XDEL when ``stream_id == PLACEHOLDER`` (the
        in-flight marker from a concurrent claim). The
        caller passes the stream_id (looked up via
        ``read_index``) so we avoid a second HGET.
        """
        try:
            if stream_id and stream_id != PLACEHOLDER:
                await self.client.xdel(DLQ_STREAM_KEY, stream_id)
            await self.client.hdel(DLQ_EVENT_INDEX, idem_key_for(event_id, reason))
            return Ok(None)
        except Exception as e:
            logger.warning(
                "dlq_storage.drop_entry.redis_error",
                event_id=event_id,
                reason=reason,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))

    async def _scan_from(
        self, head_stream_id: str, count: int
    ) -> Result[list[Mapping[str, str]], MemoryError]:
        """Forward-scan the stream from a given head."""
        try:
            messages = await self.client.xrange(
                DLQ_STREAM_KEY, min=head_stream_id, max="+", count=count
            )
        except Exception as e:
            logger.warning(
                "dlq_storage._scan_from.redis_error",
                head=head_stream_id,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        return Ok([decode_dict(m) for _, m in messages])


def _decode_int_dict(raw: dict) -> dict[str, int]:
    """Decode a hash whose values are decimal-encoded ints."""
    out: dict[str, int] = {}
    for k, v in raw.items():
        key = decode_value(k)
        val_str = decode_value(v)
        if key is None or val_str is None:
            continue
        try:
            out[key] = int(val_str)
        except (TypeError, ValueError):
            continue
    return out


__all__ = [
    "ALL_KEYS",
    "DLQ_AGENT_INDEX",
    "DLQ_EVENT_INDEX",
    "DLQ_REASON_INDEX",
    "DLQ_STREAM_KEY",
    "MAXLEN_DEFAULT",
    "PLACEHOLDER",
    "RedisDLQStorage",
    "idem_key_for",
]
