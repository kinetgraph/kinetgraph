# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Incremental World checkpoint — per-agent World persistence.

The ``ReactiveDispatcher`` (v2.2) maintains a per-agent
``WorldCheckpoint`` in Redis so the dispatcher can resume
after a restart without re-folding the entire agent history.

Storage layout
--------------

Key:    ``knt:world:{agent_id}``
Value:  pickled ``(World, last_stream_id)`` tuple
TTL:    7 days (matches the continuity default — an agent
         that has been idle for more than 7 days is re-bootstrapped
         from a full fold on its next activation).

Why pickle?
-----------

The ``World`` carries an ``ArchetypeStorage`` and a ``dict`` of
``AgentView`` instances — not-trivial to serialise in a
language-neutral format. Pickle is sufficient for an internal
Redis checkpoint. If cross-version migration becomes a
concern, switching to ``msgpack`` with a versioned schema is
a drop-in change.

Why not save the stream cursor alone?
-------------------------------------

The original v2.1 checkpoint was ``(last_stream_id,)`` — the
dispatcher re-folded on restart. For an agent with N events,
the cost was O(N) per restart. With the World itself in the
checkpoint, restart is O(M) where M is the number of events
that arrived since the last save. For a 10k-event agent that
saw 100 new events before crashing, restart drops from 10k
to 100 fold operations — a 100× speedup.

Idempotency
-----------

Saving the same checkpoint twice is safe: pickle.dumps is
deterministic for the same World + last_stream_id, and Redis
SET is atomic. Crash recovery is just ``load`` on startup.

See: ADR-018 — WorldIncremental + WorldSystem.

Iteration 5 (ADR-019): the Redis I/O is delegated to
``WorldCheckpointStorage`` (in
``kntgraph.infra.redis._world_checkpoint``). This
class becomes a thin composition that owns the wire
format (pickle encode/decode).
"""

from __future__ import annotations

# ``pickle`` is used here to serialise the World checkpoint
# into Redis. The data is **internal to the framework**
# (same process writes + reads), not untrusted network input;
# an attacker who can write to the Redis key already has
# operator-level access. Bandit's B403 / B301 warnings
# don't apply to this use case.
import pickle  # nosec B403 - internal-to-framework serialisation
from dataclasses import dataclass

import structlog

from kntgraph.core.world import World
from kntgraph.infra.redis._world_checkpoint import (
    WorldCheckpointStorage,
    WORLD_CHECKPOINT_KEY_TEMPLATE,
)


# 7 days matches the continuity default (ADR-014).
DEFAULT_WORLD_CHECKPOINT_TTL_S = 7 * 24 * 60 * 60


logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class WorldCheckpoint:
    """Persisted state of an agent's incremental World.

    Attributes:
        world: The World as of ``last_stream_id``. Includes
            all events whose stream_id is ``<= last_stream_id``.
        last_stream_id: The Redis stream id cursor — events
            with id ``> last_stream_id`` have NOT been folded.
            Use the ``(`` prefix form for exclusive ``XRANGE``.
    """

    world: World
    last_stream_id: str


class IncrementalWorldStore:
    """
    Per-agent World checkpoint persistence.

    Iteration 5: thin facade over ``WorldCheckpointStorage``.
    The store owns the wire format (pickle encode/decode +
    ``World`` rebuild). The Redis I/O lives in the storage.

    The checkpoint is durable; a dispatcher restart resumes
    from the last saved position. The EventLog dedupe ensures
    no duplicate appends on replay.
    """

    def __init__(
        self,
        storage: WorldCheckpointStorage,
        *,
        ttl_s: int = DEFAULT_WORLD_CHECKPOINT_TTL_S,
    ) -> None:
        """
        Construct the store.

        Inject a ``WorldCheckpointStorage`` (the Protocol).
        The store is a thin facade that owns the wire format
        (pickle encode/decode + ``World`` rebuild).
        """
        self._storage = storage
        self._ttl_s = ttl_s

    @staticmethod
    def key(agent_id: str) -> str:
        """The Redis key for an agent's checkpoint."""
        return WORLD_CHECKPOINT_KEY_TEMPLATE.format(agent_id=agent_id)

    async def load(self, agent_id: str) -> WorldCheckpoint:
        """
        Load the agent's checkpoint or return an empty one.

        Empty checkpoints (``World.empty()`` with cursor
        ``"-"``) are returned on first dispatch for a new
        agent. The dispatcher treats this identically to the
        loaded case — the fold just runs through the full
        agent history on the first batch.

        The pickled payload is ``(tick, storage, views,
        last_stream_id)`` — the tuple is rebuilt into a
        ``World`` which constructs the read-only proxy
        automatically.
        """
        result = await self._storage.load(agent_id)
        if result.is_err():
            logger.warning(
                "incremental_world_store.load.storage_error",
                agent_id=agent_id,
                error=str(result.err_value()),
            )
            return WorldCheckpoint(world=World.empty(), last_stream_id="-")
        raw = result.ok_value()
        if raw is None:
            return WorldCheckpoint(world=World.empty(), last_stream_id="-")
        tick, storage, views, last_stream_id = pickle.loads(  # nosec B301 - internal-to-framework load
            raw
        )
        world = World(tick=tick, storage=storage, views=views)
        return WorldCheckpoint(world=world, last_stream_id=last_stream_id)

    async def save(self, agent_id: str, ckpt: WorldCheckpoint) -> None:
        """
        Persist the agent's checkpoint. Atomic via Redis SET;
        the TTL is refreshed on every save (sliding window).

        The ``World`` is plain picklable: all components
        are frozen dataclasses, all collections are
        ``dict``. We serialise a ``(tick, storage, views,
        last_stream_id)`` tuple. ``World.__init__`` rebuilds
        the structure on load without further ceremony.

        Format: ``pickle.dumps((tick, storage, views, stream_id))``.
        Pickle is the MVP format. Trade-offs documented in
        ADR-018 §5 — when the system outgrows single-process
        pickle (cross-language, schema versioning, human-readable
        inspection), swap this for a Pydantic + JSON snapshot.
        """
        payload = pickle.dumps(
            (
                ckpt.world.tick,
                ckpt.world.storage,
                dict(ckpt.world.views),
                ckpt.last_stream_id,
            )
        )
        result = await self._storage.save(agent_id, payload, ttl_seconds=self._ttl_s)
        if result.is_err():
            logger.warning(
                "incremental_world_store.save.storage_error",
                agent_id=agent_id,
                error=str(result.err_value()),
            )

    async def discard(self, agent_id: str) -> None:
        """
        Drop the agent's checkpoint.

        Useful for tests that want to force a full fold on
        the next dispatch. Production code should not call
        this — the dispatcher manages the lifecycle.
        """
        result = await self._storage.discard(agent_id)
        if result.is_err():
            logger.warning(
                "incremental_world_store.discard.storage_error",
                agent_id=agent_id,
                error=str(result.err_value()),
            )


__all__ = [
    "DEFAULT_WORLD_CHECKPOINT_TTL_S",
    "IncrementalWorldStore",
    "WORLD_CHECKPOINT_KEY_TEMPLATE",
    "WorldCheckpoint",
]
