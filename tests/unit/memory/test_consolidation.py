# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``memory/consolidation.py``.

Closes DEBT §3.2 (coverage 32% → 80%+). The fold functions
in ``profile`` / ``session`` / ``continuity`` are exercised
elsewhere; the consolidation module adds:

  - ``MemoryAgent`` — the discriminated identity of a memory
    agent (session / profile / continuity / business).
  - ``parse_agent_id`` — the only place that knows the
    agent_id string convention.
  - ``Consolidator`` — pure cyclic system that publishes
    ``CacheRefreshRequest``s onto a bus.
  - ``Projector`` — one-shot fold that writes the cache
    directly (used by tests and warmup scripts).
"""

from __future__ import annotations


import fakeredis.aioredis
import pytest
import pytest_asyncio

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._memory import (
    RedisContinuityStorage,
    RedisProfileStorage,
    RedisSessionStorage,
)
from kntgraph.memory.cache_warmer import (
    CacheRefreshBus,
)
from kntgraph.memory.consolidation import (
    Consolidator,
    MemoryAgent,
    Projector,
    parse_agent_id,
)
from kntgraph.memory.continuity.manager import ContinuityManager
from kntgraph.memory.profile import (
    ProfileEventType,
    ProfileManager,
)
from kntgraph.memory.session import (
    SessionEventType,
    SessionManager,
)
from kntgraph.stream.event_log import EventLog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def event_log(fake_redis):
    return EventLog(RedisEventLogAdapter(fake_redis))


@pytest_asyncio.fixture
async def session_manager(fake_redis, event_log):
    storage = RedisSessionStorage(fake_redis)
    return SessionManager(event_log, storage, ttl_seconds=60)


@pytest_asyncio.fixture
async def profile_manager(fake_redis, event_log):
    storage = RedisProfileStorage(fake_redis)
    return ProfileManager(event_log, storage, ttl_seconds=60)


@pytest_asyncio.fixture
async def continuity_manager(fake_redis, event_log):
    storage = RedisContinuityStorage(fake_redis)
    return ContinuityManager(event_log, storage, ttl_seconds=60)


# ---------------------------------------------------------------------------
# MemoryAgent
# ---------------------------------------------------------------------------


class TestMemoryAgent:
    def test_session_factory(self):
        m = MemoryAgent.session("sess-1")
        assert m.kind == "session"
        assert m.id1 == "sess-1"
        assert m.id2 == ""
        assert m.agent_id == "session:sess-1"
        assert m.cache_key == "knt:session:sess-1"

    def test_profile_factory(self):
        m = MemoryAgent.profile("tenant-a", "user-1")
        assert m.kind == "profile"
        assert m.id1 == "tenant-a"
        assert m.id2 == "user-1"
        assert m.agent_id == "profile:tenant-a:user-1"
        assert m.cache_key == "knt:profile:tenant-a:user-1"

    def test_continuity_factory(self):
        m = MemoryAgent.continuity("tenant-a", "user-1")
        assert m.kind == "continuity"
        assert m.id1 == "tenant-a"
        assert m.id2 == "user-1"
        assert m.agent_id == "continuity:tenant-a:user-1"
        assert m.cache_key == "knt:continuity:tenant-a:user-1"

    def test_repr_for_session(self):
        m = MemoryAgent.session("sess-1")
        assert repr(m) == "MemoryAgent(session, id1='sess-1')"

    def test_repr_for_profile(self):
        m = MemoryAgent.profile("tenant-a", "user-1")
        assert "profile" in repr(m)
        assert "tenant-a" in repr(m)
        assert "user-1" in repr(m)


# ---------------------------------------------------------------------------
# parse_agent_id
# ---------------------------------------------------------------------------


class TestParseAgentId:
    def test_none_for_empty(self):
        assert parse_agent_id("") is None

    def test_none_for_unknown_prefix(self):
        assert parse_agent_id("NF-001") is None
        assert parse_agent_id("fechamento:abc") is None
        assert parse_agent_id("agent.spawned") is None

    def test_session_with_simple_id(self):
        m = parse_agent_id("session:sess-1")
        assert m is not None
        assert m.kind == "session"
        assert m.id1 == "sess-1"
        assert m.id2 == ""

    def test_session_with_colon_in_id(self):
        m = parse_agent_id("session:tenant-x:user-y:sess-1")
        assert m is not None
        assert m.kind == "session"
        assert m.id1 == "tenant-x:user-y:sess-1"

    def test_profile_with_two_parts(self):
        m = parse_agent_id("profile:tenant-a:user-1")
        assert m is not None
        assert m.kind == "profile"
        assert m.id1 == "tenant-a"
        assert m.id2 == "user-1"

    def test_profile_with_extra_colon(self):
        m = parse_agent_id("profile:tenant-a:user-1:extra")
        assert m is not None
        assert m.kind == "profile"
        assert m.id1 == "tenant-a"
        assert m.id2 == "user-1:extra"

    def test_continuity_with_two_parts(self):
        m = parse_agent_id("continuity:tenant-a:user-1")
        assert m is not None
        assert m.kind == "continuity"
        assert m.id1 == "tenant-a"
        assert m.id2 == "user-1"

    def test_session_with_empty_body(self):
        assert parse_agent_id("session:") is None

    def test_profile_with_only_prefix(self):
        assert parse_agent_id("profile:") is None
        assert parse_agent_id("profile:tenant-a") is None

    def test_profile_with_empty_parts(self):
        assert parse_agent_id("profile::user-1") is None
        assert parse_agent_id("profile:tenant-a:") is None


# ---------------------------------------------------------------------------
# Consolidator
# ---------------------------------------------------------------------------


class TestConsolidator:
    pytestmark = pytest.mark.asyncio

    async def test_refresh_all_publishes_for_session(
        self, event_log, session_manager, profile_manager
    ):
        e = Event.domain_from(
            agent_id="session:sess-1",
            type=SessionEventType.STARTED,
            data={"session_id": "sess-1", "tenant_id": "t", "user_id": "u"},
            correlation=CorrelationContext.new(),
        )
        world = World.fold([e], tick=1)
        bus = CacheRefreshBus()
        cons = Consolidator(event_log, bus, session_manager, profile_manager)

        events = cons.refresh_all(world)

        assert events == []
        assert len(bus) == 1
        req = bus.drain()[0]
        assert req.kind == "session"
        assert req.id1 == "sess-1"
        assert req.id2 == ""

    async def test_refresh_all_publishes_for_profile(
        self, event_log, session_manager, profile_manager
    ):
        events_in = [
            Event.domain_from(
                agent_id="profile:tenant-a:user-1",
                type=ProfileEventType.PREFERENCE_SET,
                data={"tenant_id": "tenant-a", "user_id": "user-1"},
                correlation=CorrelationContext.new(),
            ),
            Event.domain_from(
                agent_id="session:sess-1",
                type=SessionEventType.STARTED,
                data={"session_id": "sess-1"},
                correlation=CorrelationContext.new(),
            ),
            Event.domain_from(
                agent_id="NF-001",
                type="document.received",
                data={"doc_id": "NF-001"},
                correlation=CorrelationContext.new(),
            ),
        ]
        world = World.fold(events_in, tick=1)
        bus = CacheRefreshBus()
        cons = Consolidator(event_log, bus, session_manager, profile_manager)

        cons.refresh_all(world)

        kinds = sorted(req.kind for req in bus.drain())
        assert kinds == ["profile", "session"]

    async def test_refresh_all_skips_non_memory_agents(
        self, event_log, session_manager, profile_manager
    ):
        events_in = [
            Event.domain_from(
                agent_id="fechamento:abc",
                type="fechamento.computed",
                data={"n": 1},
                correlation=CorrelationContext.new(),
            ),
            Event.domain_from(
                agent_id="NF-001",
                type="document.received",
                data={"doc_id": "NF-001"},
                correlation=CorrelationContext.new(),
            ),
        ]
        world = World.fold(events_in, tick=1)
        bus = CacheRefreshBus()
        cons = Consolidator(event_log, bus, session_manager, profile_manager)

        cons.refresh_all(world)

        assert len(bus) == 0

    async def test_refresh_all_with_empty_world(
        self, event_log, session_manager, profile_manager
    ):
        bus = CacheRefreshBus()
        cons = Consolidator(event_log, bus, session_manager, profile_manager)
        world = World.empty(tick=0)

        assert cons.refresh_all(world) == []
        assert len(bus) == 0

    async def test_as_cyclic_system_delegates(
        self, event_log, session_manager, profile_manager
    ):
        e = Event.domain_from(
            agent_id="session:sess-1",
            type=SessionEventType.STARTED,
            data={"session_id": "sess-1"},
            correlation=CorrelationContext.new(),
        )
        world = World.fold([e], tick=1)
        bus = CacheRefreshBus()
        cons = Consolidator(event_log, bus, session_manager, profile_manager)

        system = cons.as_cyclic_system()
        events = await system(world)

        assert events == []
        assert len(bus) == 1


# ---------------------------------------------------------------------------
# Projector
# ---------------------------------------------------------------------------


class TestProjector:
    pytestmark = pytest.mark.asyncio

    async def test_project_session_writes_cache(
        self, event_log, session_manager, profile_manager
    ):
        await event_log.append(
            Event.domain_from(
                agent_id="session:sess-1",
                type=SessionEventType.STARTED,
                data={"session_id": "sess-1", "tenant_id": "t", "user_id": "u"},
                correlation=CorrelationContext.new(),
            )
        )
        proj = Projector(event_log, session_manager, profile_manager)

        assert await proj.project_session("sess-1") is True
        cached = await session_manager.read("sess-1")
        assert cached is not None
        assert cached.session_id == "sess-1"

    async def test_project_session_returns_false_for_missing(
        self, event_log, session_manager, profile_manager
    ):
        proj = Projector(event_log, session_manager, profile_manager)

        assert await proj.project_session("nonexistent") is False

    async def test_project_profile_writes_cache(
        self, event_log, session_manager, profile_manager
    ):
        await event_log.append(
            Event.domain_from(
                agent_id="profile:tenant-a:user-1",
                type="profile.created",
                data={"tenant_id": "tenant-a", "user_id": "user-1"},
                correlation=CorrelationContext.new(),
            )
        )
        proj = Projector(event_log, session_manager, profile_manager)

        assert await proj.project_profile("tenant-a", "user-1") is True
        cached = await profile_manager.read("tenant-a", "user-1")
        assert cached is not None

    async def test_project_profile_returns_false_for_missing(
        self, event_log, session_manager, profile_manager
    ):
        proj = Projector(event_log, session_manager, profile_manager)

        assert await proj.project_profile("t", "u") is False

    async def test_project_continuity_returns_false_when_unconfigured(
        self, event_log, session_manager, profile_manager
    ):
        proj = Projector(
            event_log, session_manager, profile_manager, continuity_manager=None
        )

        assert await proj.project_continuity("t", "u") is False

    async def test_project_continuity_writes_cache(
        self, event_log, session_manager, profile_manager, continuity_manager
    ):
        await event_log.append(
            Event.domain_from(
                agent_id="continuity:tenant-a:user-1",
                type="continuity.created",
                data={"tenant_id": "tenant-a", "user_id": "user-1"},
                correlation=CorrelationContext.new(),
            )
        )
        proj = Projector(
            event_log, session_manager, profile_manager, continuity_manager
        )

        assert await proj.project_continuity("tenant-a", "user-1") is True
        cached = await continuity_manager.read("tenant-a", "user-1")
        assert cached is not None

    async def test_project_all_counts_each_kind(
        self, event_log, session_manager, profile_manager
    ):
        e1 = Event.domain_from(
            agent_id="session:sess-1",
            type=SessionEventType.STARTED,
            data={"session_id": "sess-1", "tenant_id": "t", "user_id": "u"},
            correlation=CorrelationContext.new(),
        )
        e2 = Event.domain_from(
            agent_id="profile:tenant-a:user-1",
            type="profile.created",
            data={"tenant_id": "tenant-a", "user_id": "user-1"},
            correlation=CorrelationContext.new(),
        )
        e3 = Event.domain_from(
            agent_id="NF-001",
            type="document.received",
            data={"doc_id": "NF-001"},
            correlation=CorrelationContext.new(),
        )
        for e in (e1, e2, e3):
            await event_log.append(e)
        proj = Projector(event_log, session_manager, profile_manager)

        counts = await proj.project_all()

        assert counts == {"sessions": 1, "profiles": 1, "continuity": 0}

    async def test_project_all_returns_zero_when_empty(
        self, event_log, session_manager, profile_manager
    ):
        proj = Projector(event_log, session_manager, profile_manager)

        counts = await proj.project_all()

        assert counts == {"sessions": 0, "profiles": 0, "continuity": 0}
