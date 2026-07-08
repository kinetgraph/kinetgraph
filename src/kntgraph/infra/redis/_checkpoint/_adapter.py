# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
CheckpointStorage — domain Protocol for the ReactiveDispatcher
commit points (ADR-005).

Iteration 4 (ADR-019). The protocol abstracts the Redis
I/O for the ``ReactiveCheckpoint`` (one hash key with one
field per agent). The store class (``CheckpointStore``)
becomes a thin composition that owns the wire format
(JSON encode/decode + ReactiveCheckpoint construction).

Why split
---------

The previous ``CheckpointStore`` mixed:

  1. Redis I/O (HGET, HSET, HGETALL, HDEL, DEL)
  2. Wire format decode (bytes → JSON → dict)
  3. Domain construction (dict → ReactiveCheckpoint)
  4. Error handling (try/except → return None)

Iteration 4 moves (1) and (2) to the storage. The store
becomes a thin facade: delegate to storage, then build
the domain object. The CC of each piece drops.

The protocol is bytes-agnostic: payloads are pre-serialized
JSON dicts, leaving the wire format and the
``ReactiveCheckpoint`` constructor in the domain layer.

Result contract (AGENTS.md §6):

  - ``load``        returns ``Ok(dict)`` / ``Ok(None)`` /
    ``Err(MemoryError)`` / ``Err(MemoryDecodeError)``.
  - ``save``        returns ``Ok(None)`` / ``Err(MemoryError)``.
  - ``load_all``    returns ``Ok(dict[agent_id, dict])`` /
    ``Err(MemoryError)``.
  - ``clear``       returns ``Ok(None)`` / ``Err(MemoryError)``.
  - ``clear_all``   returns ``Ok(None)`` / ``Err(MemoryError)``.
"""

from __future__ import annotations

from typing import Mapping, Optional, Protocol, runtime_checkable

from kntgraph.core.result import Result

from .._errors import MemoryError, MemoryDecodeError


@runtime_checkable
class CheckpointStorage(Protocol):
    """Domain interface for the reactive-dispatcher checkpoints."""

    async def load(
        self, agent_id: str
    ) -> Result[Optional[Mapping[str, str]], MemoryError | MemoryDecodeError]:
        """Load the checkpoint payload for a single agent.

        Returns ``Ok(None)`` on miss; ``Ok(dict)`` on hit;
        ``Err(MemoryDecodeError)`` on corrupt JSON;
        ``Err(MemoryError)`` on Redis failure.
        """
        ...

    async def save(
        self, agent_id: str, payload: Mapping[str, str]
    ) -> Result[None, MemoryError]:
        """Persist a checkpoint payload (JSON-encoded by caller)."""
        ...

    async def load_all(
        self,
    ) -> Result[Mapping[str, Mapping[str, str]], MemoryError]:
        """Load every checkpoint. Returns ``Ok(dict)`` keyed by agent_id.

        Malformed entries are skipped (not failed) — the
        dispatcher can keep working with the valid ones.
        """
        ...

    async def clear(self, agent_id: str) -> Result[None, MemoryError]:
        """Remove one checkpoint. Idempotent."""
        ...

    async def clear_all(self) -> Result[None, MemoryError]:
        """Wipe every checkpoint. Used in tests and recovery."""
        ...


__all__ = ["CheckpointStorage"]
