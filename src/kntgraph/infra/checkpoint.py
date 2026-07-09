# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Reactive checkpoint store — durable commit points for the
ReactiveDispatcher.

A checkpoint is the dispatcher's "commit point" for a single
agent. It is persisted in Redis (`knt:reactive:checkpoints` as
a hash, one field per agent) and survives process restarts and
deploys. The ReactiveDispatcher reads it on startup to know
"where to resume from" without re-scanning the keyspace and
without re-dispatching events it has already processed.

Invariants
----------

  1. The `last_stream_id` is the EXCLUSIVE lower bound for the
     next `XRANGE`. The dispatcher must always read events with
     `min="(<last_stream_id>"` after loading a checkpoint.

  2. The `last_event_id` is the LOGICAL id of the last event
     whose side effects are durable. Both ids are saved in the
     same `HSET` (single-statement, atomic in Redis), so they
     are always in sync.

  3. The checkpoint is saved AFTER the dispatcher has finished
     processing the event batch and AFTER any `.completed` /
     `.failed` events emitted by reactive systems have been
     durably appended to the EventLog. This narrows the
     at-least-once window to "between the EventLog append and
     the HSET" — submillisecond.

  4. `state_hash` is OPTIONAL. When supplied, the caller can
     verify that the World produced by the fold matches the
     state at the time the checkpoint was taken (useful for
     detecting projection drift or replay divergence).

Crash safety
------------

  - Process crash before save: dispatcher re-dispatches the
    last batch on next boot. The EventLog deduplication covers
    re-emitted `.requested` / `.completed` events. Side
    effects on tools require `idempotency_key` to be
    truly at-most-once (see `tools.invoker`).

  - Process crash mid-save: Redis `HSET` is single-statement
    and atomic. Either the old checkpoint or the new one
    remains. There is no "half-saved" state.

  - Redis data loss: checkpoints are not replicated beyond
    the configured Redis persistence. For stronger durability,
    use a Redis instance with AOF every-second or stronger.

Iteration 4 (ADR-019): the Redis I/O is delegated to the
``CheckpointStorage`` Protocol (see
``kntgraph.infra.redis._checkpoint``). This class
becomes a thin composition that owns the wire-format
decode (dict → ``ReactiveCheckpoint``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog

from ..infra.redis._checkpoint import CheckpointStorage

# Re-export CHECKPOINT_KEY for backward compat. The single
# source of truth is now ``infra.redis._checkpoint.CHECKPOINT_KEY``.
from ..infra.redis._checkpoint import CHECKPOINT_KEY  # noqa: E402, F401

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class ReactiveCheckpoint:
    """
    Durable commit point for a single agent's reactive
    dispatch. The pair (last_event_id, last_stream_id) is
    always written together and read together.

    `state_hash` is optional. When the caller computes a hash
    of the World at the time of save (e.g. via
    `World.to_map()` → deterministic JSON), subsequent loads
    can compare to detect projection drift.
    """

    agent_id: str
    last_event_id: UUID
    last_stream_id: str
    confirmed_at: datetime
    state_hash: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "last_event_id": str(self.last_event_id),
            "last_stream_id": self.last_stream_id,
            "confirmed_at": self.confirmed_at.isoformat(),
            "state_hash": self.state_hash,
        }

    @classmethod
    def from_dict(cls, agent_id: str, data: dict) -> "ReactiveCheckpoint":
        return cls(
            agent_id=agent_id,
            last_event_id=UUID(data["last_event_id"]),
            last_stream_id=data["last_stream_id"],
            confirmed_at=datetime.fromisoformat(data["confirmed_at"]),
            state_hash=data.get("state_hash"),
        )


class CheckpointStore:
    """
    Redis-backed store for ReactiveCheckpoint.

    One hash key (``knt:reactive:checkpoints``) holds every
    agent's checkpoint as one field. Reads and writes are
    O(1) per agent. The store is safe to share across
    dispatcher processes — ``HSET`` is atomic in Redis.

    Iteration 4 (ADR-019): the store delegates all Redis
    I/O to the injected ``CheckpointStorage``. This class
    builds ``ReactiveCheckpoint`` from the parsed dicts;
    the storage owns bytes↔JSON encode/decode.
    """

    def __init__(self, storage: CheckpointStorage) -> None:
        """
        Construct the store.

        Inject a ``CheckpointStorage`` (the Protocol). The
        store is a thin facade over the storage — every
        public method delegates a single I/O call.
        """
        self._storage = storage

    async def load(self, agent_id: str) -> Optional[ReactiveCheckpoint]:
        """
        Load the checkpoint for a single agent. Returns `None`
        if no checkpoint exists (the agent has never been
        dispatched, or was reset).
        """
        result = await self._storage.load(agent_id)
        if result.is_err():
            logger.warning(
                "checkpoint.load.storage_error",
                agent_id=agent_id,
                error=str(result.err_value()),
            )
            return None
        data = result.ok_value()
        if data is None:
            return None
        try:
            return ReactiveCheckpoint.from_dict(agent_id, data)
        except (KeyError, ValueError) as e:
            logger.warning(
                "checkpoint.load.invalid_payload",
                agent_id=agent_id,
                error=str(e),
            )
            return None

    async def save(self, checkpoint: ReactiveCheckpoint) -> None:
        """
        Persist a checkpoint. Atomic: the (event_id, stream_id)
        pair is written as a single ``HSET`` call.

        The dispatcher should call this AFTER it has durably
        appended all events emitted by reactive systems for
        the batch.
        """
        result = await self._storage.save(checkpoint.agent_id, checkpoint.to_dict())
        if result.is_err():
            logger.warning(
                "checkpoint.save.storage_error",
                agent_id=checkpoint.agent_id,
                error=str(result.err_value()),
            )

    async def clear(self, agent_id: str) -> None:
        """Remove a checkpoint. Used in tests and recovery."""
        result = await self._storage.clear(agent_id)
        if result.is_err():
            logger.warning(
                "checkpoint.clear.storage_error",
                agent_id=agent_id,
                error=str(result.err_value()),
            )

    async def load_all(self) -> dict[str, ReactiveCheckpoint]:
        """
        Load every checkpoint. Used for diagnostics and for
        a dispatcher that wants to enumerate its working set
        without a SCAN of the EventLog keyspace.
        """
        result = await self._storage.load_all()
        if result.is_err():
            logger.warning(
                "checkpoint.load_all.storage_error",
                error=str(result.err_value()),
            )
            return {}
        out: dict[str, ReactiveCheckpoint] = {}
        for agent_id, data in result.ok_value().items():
            try:
                out[agent_id] = ReactiveCheckpoint.from_dict(agent_id, data)
            except (KeyError, ValueError) as e:
                logger.warning(
                    "checkpoint.load_all.skipped_invalid",
                    agent_id=agent_id,
                    error=str(e),
                )
                continue
        return out

    async def clear_all(self) -> None:
        """
        Wipe every checkpoint. Used in tests and during
        emergency recovery. The dispatcher will re-bootstrap
        from the beginning of each agent's stream on next
        `dispatch_once`.
        """
        result = await self._storage.clear_all()
        if result.is_err():
            logger.warning(
                "checkpoint.clear_all.storage_error",
                error=str(result.err_value()),
            )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
