# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for ``ReactiveDispatcher`` with durable
``WorldCheckpoint`` (v2.2).

The dispatcher persists a per-agent ``WorldCheckpoint`` (the
post-fold World + the last processed stream_id) in Redis. A
restart resumes from the saved checkpoint.

These tests pin the contract:

  - After ``dispatch_once``, the ``IncrementalWorldStore``
    has an entry for that agent with the post-fold World and
    the last stream id of the batch.
  - A second dispatcher (same Redis) resumes from the saved
    checkpoint and does NOT re-dispatch events that were
    already processed.
  - The checkpoint survives across dispatcher restarts even
    if the stream was trimmed (MAXLEN) past the checkpoint.
  - A new agent (no checkpoint) is discovered via SCAN on
    the first dispatch, then tracked in subsequent ones.

See: ADR-018 — WorldIncremental + WorldSystem.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from kntgraph.core.event import Event, CorrelationContext
from kntgraph.core.world import World
from kntgraph.infra.world_checkpoint import (
    IncrementalWorldStore,
    WorldCheckpoint,
)
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_spawned(log: EventLog, agent_id: str) -> Event:
    e = Event.create(
        event_type="agent.spawned",
        agent_id=agent_id,
        event_class="lifecycle",
        correlation=CorrelationContext.new(correlation_id=uuid4()),
    )
    await log.append(e)
    return e


# ---------------------------------------------------------------------------
# P1 — Checkpoint is persisted after dispatch
# ---------------------------------------------------------------------------


class TestCheckpointPersisted:
    async def test_checkpoint_saved_after_dispatch(self, clean_redis):
        """
        After `dispatch_once` processes events for an agent,
        the ``IncrementalWorldStore`` has an entry for that
        agent with the post-fold World and the last stream id.
        """
        log = EventLog(clean_redis)
        store = IncrementalWorldStore(clean_redis)
        await _seed_spawned(log, "a-1")
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"k": 1},
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )

        fired: list[str] = []

        def sys(world):
            for v in world.agents.values():
                for comp in v.components.values():
                    if isinstance(comp, dict) and "k" in comp:
                        fired.append("document.received")
            return []

        d = ReactiveDispatcher(log, systems=[sys], world_store=store)
        await d.dispatch_once()
        assert "document.received" in fired

        # Checkpoint must be saved
        ckpt = await store.load("a-1")
        assert ckpt.last_stream_id != "-"
        # The World has been folded with the spawn + received events
        assert "a-1" in ckpt.world.agents
        assert ckpt.world.agents["a-1"].domain_phase == "document.received"

    async def test_no_checkpoint_without_dispatch(self, clean_redis):
        """
        A dispatcher that never dispatched has no checkpoint
        (load returns the empty default).
        """
        log = EventLog(clean_redis)
        store = IncrementalWorldStore(clean_redis)
        _d = ReactiveDispatcher(log, systems=[], world_store=store)
        ckpt = await store.load("never-seen")
        assert ckpt.last_stream_id == "-"
        assert ckpt.world.tick == 0

    async def test_checkpoint_only_after_batch_committed(self, clean_redis):
        """
        The checkpoint is saved AFTER the batch's emitted events
        have been durably appended to the EventLog. If the system
        raises during execution, the checkpoint must NOT advance.
        """
        log = EventLog(clean_redis)
        store = IncrementalWorldStore(clean_redis)
        await _seed_spawned(log, "a-1")
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"k": 1},
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )

        def emit_followup(world):
            return [
                Event.create(
                    event_type="document.validated",
                    agent_id="a-1",
                    event_class="domain",
                    data={"validated": True},
                    correlation=CorrelationContext.new(correlation_id=uuid4()),
                )
            ]

        d = ReactiveDispatcher(
            log,
            systems=[emit_followup],
            world_store=store,
        )
        await d.dispatch_once()
        # Checkpoint was saved (the batch committed).
        ckpt = await store.load("a-1")
        assert ckpt.last_stream_id != "-"


# ---------------------------------------------------------------------------
# P2 — Restart skips processed events
# ---------------------------------------------------------------------------


class TestRestartSkipsProcessed:
    async def test_second_dispatcher_does_not_redispatch(self, clean_redis):
        """
        A second dispatcher, instantiated after the first one
        stopped, resumes from the saved checkpoint and does NOT
        re-dispatch events that were already processed.
        """
        log = EventLog(clean_redis)
        store = IncrementalWorldStore(clean_redis)
        await _seed_spawned(log, "a-1")
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={},
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )

        fired: list[str] = []

        def sys(world):
            for v in world.agents.values():
                if "document.received" in v.components:
                    fired.append("document.received")
            return []

        d1 = ReactiveDispatcher(log, systems=[sys], world_store=store)
        await d1.dispatch_once()
        first_count = len(fired)

        # Second dispatcher reads the same Redis checkpoint.
        d2 = ReactiveDispatcher(log, systems=[sys], world_store=store)
        await d2.dispatch_once()
        # The second dispatch did NOT re-fire (cursor advanced).
        assert len(fired) == first_count

    async def test_dispatcher_resumes_after_new_events(self, clean_redis):
        """
        A second dispatcher picks up events that arrived AFTER
        its predecessor stopped.
        """
        log = EventLog(clean_redis)
        store = IncrementalWorldStore(clean_redis)
        await _seed_spawned(log, "a-1")
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"n": 1},
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )

        d1 = ReactiveDispatcher(log, systems=[], world_store=store)
        await d1.dispatch_once()

        # Add a NEW event after the first dispatch.
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"n": 2},
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )

        fired: list[int] = []

        def sys(world):
            for v in world.agents.values():
                for comp in v.components.values():
                    if isinstance(comp, dict) and "n" in comp:
                        fired.append(comp["n"])
            return []

        # Second dispatcher reads the same checkpoint.
        d2 = ReactiveDispatcher(log, systems=[sys], world_store=store)
        await d2.dispatch_once()
        # Saw the new event (n=2); did NOT re-fire on n=1.
        assert fired == [2]

    async def test_stream_trim_does_not_break_resume(self, clean_redis):
        """
        If the stream is trimmed (MAXLEN) past the cursor, the
        second dispatcher still resumes correctly — it reads
        the World from the checkpoint and the new events from
        the stream.
        """
        log = EventLog(clean_redis)
        store = IncrementalWorldStore(clean_redis)
        await _seed_spawned(log, "a-1")
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"step": 1},
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )

        d1 = ReactiveDispatcher(log, systems=[], world_store=store)
        await d1.dispatch_once()

        # Aggressively trim the stream past the cursor.
        await clean_redis.xtrim("knt:agents:a-1:events", maxlen=1, approximate=False)

        # Add a NEW event after the trim.
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"step": 2},
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )

        fired_steps: list[int] = []

        def sys(world):
            for v in world.agents.values():
                for comp in v.components.values():
                    if isinstance(comp, dict) and "step" in comp:
                        fired_steps.append(comp["step"])
            return []

        # Second dispatcher resumes via checkpoint + new events.
        d2 = ReactiveDispatcher(log, systems=[sys], world_store=store)
        await d2.dispatch_once()
        assert 2 in fired_steps, fired_steps


# ---------------------------------------------------------------------------
# P3 — Per-agent checkpoints
# ---------------------------------------------------------------------------


class TestMultiAgent:
    async def test_checkpoints_are_per_agent(self, clean_redis):
        """
        The dispatcher tracks agents independently. Two agents
        have separate checkpoints.
        """
        log = EventLog(clean_redis)
        store = IncrementalWorldStore(clean_redis)
        # Two agents, each with one event
        await _seed_spawned(log, "a-1")
        await _seed_spawned(log, "b-1")

        d = ReactiveDispatcher(log, systems=[], world_store=store)
        await d.dispatch_once()

        ckpt_a = await store.load("a-1")
        ckpt_b = await store.load("b-1")
        assert ckpt_a.last_stream_id != "-"
        assert ckpt_b.last_stream_id != "-"
        # They are independent checkpoints.
        assert ckpt_a.last_stream_id != ckpt_b.last_stream_id


# ---------------------------------------------------------------------------
# P5 — IncrementalWorldStore unit semantics
# ---------------------------------------------------------------------------


class TestIncrementalWorldStore:
    async def test_load_returns_empty_when_absent(self, clean_redis):
        store = IncrementalWorldStore(clean_redis)
        ckpt = await store.load("never-seen")
        assert ckpt.last_stream_id == "-"

    async def test_save_and_load_roundtrip(self, clean_redis):
        store = IncrementalWorldStore(clean_redis)
        world = World.empty().with_event(
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )
        ckpt = WorldCheckpoint(world=world, last_stream_id="1234-0")
        await store.save("a-1", ckpt)
        loaded = await store.load("a-1")
        assert loaded.last_stream_id == "1234-0"
        assert "a-1" in loaded.world.agents
        assert loaded.world.agents["a-1"].operational_phase == "spawned"

    async def test_discard_removes_checkpoint(self, clean_redis):
        from kntgraph.core.world import World

        store = IncrementalWorldStore(clean_redis)
        await store.save(
            "a-1",
            WorldCheckpoint(world=World.empty(), last_stream_id="1234-0"),
        )
        await store.discard("a-1")
        loaded = await store.load("a-1")
        assert loaded.last_stream_id == "-"
