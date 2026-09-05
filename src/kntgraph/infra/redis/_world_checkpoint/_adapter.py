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

    async def load_cursor(
        self, agent_id: str
    ) -> Result[Optional[str], MemoryError]:
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


__all__ = ["WorldCheckpointStorage"]
