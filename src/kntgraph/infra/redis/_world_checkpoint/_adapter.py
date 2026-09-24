# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
WorldCheckpointStorage — domain Protocol for the per-agent
World checkpoint.

Iteration 5 (ADR-019). The storage abstracts the Redis
I/O; the facade ``IncrementalWorldStore`` becomes a thin
composition that owns the wire format (pickle for now).

Wire format is pickle-based (see ``infra/world_checkpoint``
module docstring). A future iteration may swap to msgpack
+ JSON; the Protocol does not change.

Result contract (AGENTS.md §6):

  - ``load``    returns ``Ok(bytes)`` / ``Ok(None)`` /
    ``Err(MemoryError)``.
  - ``save``    returns ``Ok(None)`` / ``Err(MemoryError)``.
  - ``discard`` returns ``Ok(None)`` / ``Err(MemoryError)``.

The payload (``bytes``) is the pickled (tick, storage, views,
last_stream_id) tuple. The facade unpacks it.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from kntgraph.core.result import Result

from .._errors import MemoryError


@runtime_checkable
class WorldCheckpointStorage(Protocol):
    """Domain interface for the per-agent World checkpoint."""

    async def load(self, agent_id: str) -> Result[Optional[bytes], MemoryError]:
        """Load the pickled checkpoint payload.

        Returns ``Ok(None)`` on miss (first dispatch for the
        agent); ``Ok(bytes)`` on hit; ``Err(MemoryError)`` on
        Redis failure.
        """
        ...

    async def load_cursor(self, agent_id: str) -> Result[Optional[str], MemoryError]:
        """Load the agent's stream cursor (P5b split).

        The cheap probe: a small ``GET`` that answers "is
        there anything new?" without the pickled World.
        ``Ok(None)`` on miss; ``Ok(str)`` on hit;
        ``Err(MemoryError)`` on Redis failure.
        """
        ...

    async def save(
        self,
        agent_id: str,
        payload: bytes,
        *,
        ttl_seconds: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Result[None, MemoryError]:
        """Persist a pickled checkpoint payload with sliding TTL.

        When ``cursor`` is given, the companion cursor key is
        written in the same transaction so the pair never
        disagrees.
        """
        ...

    async def discard(self, agent_id: str) -> Result[None, MemoryError]:
        """Drop the checkpoint. Idempotent."""
        ...

    # ------------------------------------------------------------------
    # Stream inspection (ADR-075 Tier 4: ``stuck_in_queue`` query).
    #
    # The dispatcher needs to know whether a tool's queue stream
    # (``knt:tools:<name>:queue``) is non-empty AND has no active
    # consumer — that's the "stuck" signal. The Protocol exposes
    # the two primitives directly so the dispatcher's
    # observability layer never reaches into the Redis client
    # for ``xlen`` / ``xpending`` (which are NOT part of the
    # framework's ``RedisLike`` Protocol). Implementations map
    # these to whatever backend they sit on: the Redis impl uses
    # ``XINFO STREAM`` (length) and a single ``XPENDING`` summary
    # probe; an in-memory test stub can satisfy the Protocol
    # without spinning up Redis.
    #
    # Return contract (AGENTS.md §6):
    #
    #   - Returns ``0`` for a missing key (no such stream). The
    #     caller interprets ``0`` as "no work, no stuck".
    #   - Returns ``>= 0`` for an existing stream.
    #   - On backend failure, the implementation may either
    #     return ``0`` (fail-soft; the query is best-effort and
    #     the dispatcher will rerun it on the next tick) or
    #     raise; the dispatcher treats both as "no stuck
    #     detected this tick" so a Redis hiccup never escalates
    #     into a recovery loop.

    async def queue_length(self, stream_key: str) -> int:
        """Return the number of entries in ``stream_key``.

        Maps to ``XLEN`` on Redis. ``0`` means the stream does
        not exist or is empty.
        """
        ...

    async def pending_count(self, stream_key: str) -> int:
        """Return a positive count if ``stream_key`` has any
        pending (un-acked) entries in its consumer group.

        ``0`` means the PEL is empty (or the stream/group
        does not exist). Any positive value means at least
        one entry is held by a consumer — the dispatcher's
        stuck-in-queue query interprets "positive" as "a
        worker is processing, NOT stuck".

        Implementations return a binary-ish count (``0`` or
        ``1``) by design: the dispatcher's stuck detection
        only branches on the existence of pending entries,
        not their count. Returning the exact count would
        require either a Protocol extension (``XINFO
        GROUPS``) or full PEL enumeration; the dispatcher's
        decision does not justify either cost.
        """
        ...


__all__ = ["WorldCheckpointStorage"]
