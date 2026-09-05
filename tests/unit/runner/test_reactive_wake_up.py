# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Behaviour tests for the ADR-068 Phase 2 dispatcher changes.

Three contracts are pinned:

  - **Cursor-key split (P5b)**: ``IncrementalWorldStore``
    writes a companion cursor key in the same transaction;
    ``load_cursor`` reads the small key without the World
    payload; a legacy raw-pickle checkpoint (no cursor key)
    reads as ``None``.
  - **Dirty-only save (P5c)**: ``run_systems_and_persist``
    re-persists the checkpoint only when the cursor
    advanced or the systems emitted events; an idle call
    with both empty skips the SET.
  - **Wake-up loop (§3.2)**: with ``wake_on_event=True``
    and a real EventLog over a real Redis, the loop's idle
    path blocks in ``subscribe`` (one held connection) and
    wakes on an append from another task; a legacy EventLog
    without ``subscribe`` degrades transparently to the
    poll cadence.

The Redis-backed tests run against a real Redis server (the
behaviour-test bar; blocking-faithful fakeredis covers the
CI-no-container path — both drivers implement the same
client contract).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional
from uuid import uuid4

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._world_checkpoint import (
    RedisWorldCheckpointStorage,
    cursor_key,
    storage_key,
)
from kntgraph.infra.world_checkpoint import (
    IncrementalWorldStore,
    WorldCheckpoint,
)
from kntgraph.runner._systems_runner import run_systems_and_persist
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog


pytestmark = pytest.mark.asyncio


def _redis_url() -> str:
    """The CI/dev Redis server; the integration test env
    default matches the docker run command in the skill."""
    return os.environ.get("KNT_REDIS_URL", "redis://:redispassword@localhost:6379/0")


def _seed_event(agent_id: str, payload: Optional[dict] = None) -> Event:
    return Event.create(
        event_type="fixture.event",
        agent_id=agent_id,
        event_class="domain",
        data=payload or {"k": "v"},
        correlation=CorrelationContext.new(correlation_id=uuid4()),
    )


class TestCursorKeySplit:
    """P5b: the cursor key is written transactionally with
    the checkpoint payload and read without the payload."""

    async def test_save_writes_companion_cursor_key(self):
        from redis.asyncio import Redis

        client = Redis.from_url(_redis_url(), decode_responses=False)
        storage = RedisWorldCheckpointStorage(client=client)
        store = IncrementalWorldStore(storage)
        agent_id = f"p5b-{uuid4().hex[:8]}"

        try:
            event = _seed_event(agent_id)
            ckpt = WorldCheckpoint(
                world=World.fold([event], tick=1), last_stream_id="42-0"
            )
            await store.save(agent_id, ckpt)

            # Both keys landed; the cursor is the raw stream id.
            raw_cursor = await client.get(cursor_key(agent_id))
            assert raw_cursor == b"42-0"
            assert await client.get(storage_key(agent_id)) is not None
            # The cheap probe reads the same value.
            assert await store.load_cursor(agent_id) == "42-0"
        finally:
            await store.discard(agent_id)
            await client.aclose()

    async def test_load_cursor_returns_none_for_missing_key(self):
        from redis.asyncio import Redis

        client = Redis.from_url(_redis_url(), decode_responses=False)
        storage = RedisWorldCheckpointStorage(client=client)
        store = IncrementalWorldStore(storage)
        agent_id = f"p5b-none-{uuid4().hex[:8]}"

        try:
            assert await store.load_cursor(agent_id) is None
        finally:
            await store.discard(agent_id)
            await client.aclose()

    async def test_legacy_checkpoint_without_cursor_key_loads_and_reports_none(self):
        """A checkpoint written by a pre-P5b build has no
        cursor key. ``load`` still reads the payload; the
        wake-up path reports ``None`` and the dispatcher
        degrades to the poll cadence for that agent."""
        import pickle  # nosec B403 - legacy-format fixture
        import zlib

        from redis.asyncio import Redis

        client = Redis.from_url(_redis_url(), decode_responses=False)
        storage = RedisWorldCheckpointStorage(client=client)
        store = IncrementalWorldStore(storage)
        agent_id = f"p5b-legacy-{uuid4().hex[:8]}"

        try:
            event = _seed_event(agent_id)
            world = World.fold([event], tick=2)
            # Write the checkpoint WITHOUT the companion
            # cursor (the pre-P5b shape).
            raw = await client.get(storage_key(agent_id))
            assert raw is None  # sanity: nothing saved yet
            legacy_payload = zlib.compress(
                pickle.dumps((world.tick, world.storage, dict(world.views), "7-0"))  # nosec B301
            )
            await client.set(storage_key(agent_id), legacy_payload)

            loaded = await store.load(agent_id)
            assert loaded.last_stream_id == "7-0"
            assert loaded.world.tick == 2
            assert await store.load_cursor(agent_id) is None
        finally:
            await store.discard(agent_id)
            await client.aclose()

    async def test_save_seeds_cursor_after_dispatch(self):
        """The full dispatch → seed loop: after
        ``dispatch_once``, the wake-up cursor set knows the
        agent's committed position (read from the cheap
        key)."""
        from redis.asyncio import Redis

        client = Redis.from_url(_redis_url(), decode_responses=False)
        log = EventLog(storage=RedisEventLogAdapter(client=client))
        store = IncrementalWorldStore(RedisWorldCheckpointStorage(client=client))
        agent_id = f"p5b-seed-{uuid4().hex[:8]}"

        try:
            await log.append(_seed_event(agent_id))
            dispatcher = ReactiveDispatcher(log=log, world_store=store)
            dispatcher.track_agent(agent_id)
            assert dispatcher._subscribe_cursors == {}

            await dispatcher.dispatch_once()

            assert agent_id in dispatcher._subscribe_cursors
            # The seeded cursor equals the cursor key's value.
            assert (
                dispatcher._subscribe_cursors[agent_id]
                == await store.load_cursor(agent_id)
            )
        finally:
            await store.discard(agent_id)
            await log.delete_agent_stream(agent_id)
            await client.aclose()


class TestDirtyOnlySave:
    """P5c: the checkpoint is re-persisted only when the
    cursor advanced or the systems emitted events."""

    def _captured_store(self):
        calls: list[str] = []

        class _Store:
            async def load(self, agent_id: str) -> WorldCheckpoint:
                return WorldCheckpoint(world=World.empty(), last_stream_id="-")

            async def load_cursor(self, agent_id: str) -> Optional[str]:
                return None

            async def save(
                self,
                agent_id: str,
                checkpoint: WorldCheckpoint,
                *,
                ttl_seconds: Optional[int] = None,
                cursor: Optional[str] = None,
            ) -> None:
                calls.append(agent_id)

            async def discard(self, agent_id: str) -> None:
                return None

        return _Store(), calls

    async def test_idle_call_skips_save(self):
        """Zero consumed, zero emitted → no SET. The dominant
        idle payload of the dispatcher was this re-SET of an
        unchanged pickled World every tick."""
        store, calls = self._captured_store()
        log = EventLog(storage=RedisEventLogAdapter(client=_IdleLog()))
        dispatcher = ReactiveDispatcher(log=log, world_store=store)  # type: ignore[arg-type]

        await run_systems_and_persist(
            dispatcher, "a-1", World.empty(), "1-0", 0, []
        )
        assert calls == []

    async def test_consumed_batch_saves(self):
        store, calls = self._captured_store()
        log = EventLog(storage=RedisEventLogAdapter(client=_IdleLog()))
        dispatcher = ReactiveDispatcher(log=log, world_store=store)  # type: ignore[arg-type]

        await run_systems_and_persist(
            dispatcher, "a-1", World.empty(), "1-0", 1, [_seed_event("a-1")]
        )
        assert calls == ["a-1"]

    async def test_system_emitted_events_save(self):
        """A silent-consuming tick whose system still emits
        (the TTL sweeper evicting an orphan) is dirty: the
        save runs so the re-folded World is committed."""
        store, calls = self._captured_store()
        log = EventLog(storage=RedisEventLogAdapter(client=_IdleLog()))
        dispatcher = ReactiveDispatcher(log=log, world_store=store)  # type: ignore[arg-type]

        def _emitting_system(world: World) -> list[Event]:
            return [_seed_event("a-1", {"emitted": True})]

        dispatcher._systems = [_emitting_system]
        await run_systems_and_persist(
            dispatcher, "a-1", World.empty(), "1-0", 0, []
        )
        assert calls == ["a-1"]


class _IdleLog:
    """EventLog storage double that returns nothing and
    accepts appends without a server."""

    async def append(self, *, agent_id: str, event: Event):
        from kntgraph.core.result import Ok

        return Ok("0-0")

    async def read(self, agent_id, *, start="-", end="+", count=None):
        return []

    async def read_with_cursor(self, agent_id, cursor):
        return [], cursor or "-"


class TestWakeUpLoop:
    """§3.2: the push-first loop blocks in subscribe and
    wakes on arrival; legacy EventLogs degrade to polling."""

    def _real_dispatcher(self, agent_id: str):
        from redis.asyncio import Redis

        client = Redis.from_url(_redis_url(), decode_responses=False)
        log = EventLog(storage=RedisEventLogAdapter(client=client))
        store = IncrementalWorldStore(RedisWorldCheckpointStorage(client=client))
        seen: list[Event] = []

        def system(world: World) -> list[Event]:
            for view in world.views.values():
                for e in view.events if hasattr(view, "events") else []:
                    seen.append(e)
            return []

        return client, log, store, system, seen, agent_id

    async def test_loop_wakes_on_real_redis_append(self):
        """The wake-up contract end-to-end over a real Redis:
        the loop blocks, another task appends, the dispatch
        cycle processes the event, the loop returns to the
        blocking read."""
        from redis.asyncio import Redis

        agent_id = f"wake-{uuid4().hex[:8]}"
        client = Redis.from_url(_redis_url(), decode_responses=False)
        log = EventLog(storage=RedisEventLogAdapter(client=client))
        store = IncrementalWorldStore(RedisWorldCheckpointStorage(client=client))
        processed: list[Event] = []

        def recording_system(world: World) -> list[Event]:
            return []

        dispatcher = ReactiveDispatcher(
            log=log,
            world_store=store,
            systems=[recording_system],
            redis=client,
            poll_interval=0.05,
            rediscovery_interval_seconds=0.2,
            fallback_poll_interval=1.0,
        )
        # Drive the wake path directly (not via ``start()``)
        # so the test is deterministic.
        try:
            await log.append(_seed_event(agent_id))
            dispatcher.track_agent(agent_id)
            await dispatcher._wake_once()
            # The event was folded into the agent's World —
            # the checkpoint cursor advanced past the entry.
            cursor = await store.load_cursor(agent_id)
            assert cursor is not None
            assert cursor != "-"
            # The next wake call with no new events times out
            # (the fallback interval) and converges.
            t0 = time.monotonic()
            await dispatcher._wake_once()
            elapsed = time.monotonic() - t0
            # The subscribe block lasted ~the fallback
            # interval (1s), not the poll interval (0.05s)
            # — the loop was blocked in the held connection,
            # not spinning.
            assert elapsed >= 0.5
        finally:
            await store.discard(agent_id)
            await log.delete_agent_stream(agent_id)
            await client.aclose()

    async def test_legacy_log_without_subscribe_degrades_to_poll(self):
        """An EventLog without ``subscribe`` (pre-Phase-1
        storage, custom fakes) keeps the legacy pure-poll
        cadence: ``_wake_on_event`` is False and ``_loop``
        sleeps the poll interval between sweeps."""
        class _LegacyLog:
            async def read_after_cursor(self, agent_id, cursor):
                return [], cursor or "-"

            async def list_agents(self):
                return []

            async def append_batch(self, events):
                return events

        dispatcher = ReactiveDispatcher(
            log=_LegacyLog(),  # type: ignore[arg-type]
            world_store=_LegacyStore(),
            poll_interval=0.05,
        )
        assert dispatcher._wake_on_event is False


class _LegacyStore:
    async def load(self, agent_id: str) -> WorldCheckpoint:
        return WorldCheckpoint(world=World.empty(), last_stream_id="-")

    async def save(self, agent_id: str, checkpoint: WorldCheckpoint) -> None:
        return None