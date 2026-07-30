# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``memory/cache_warmer.py``.

Closes DEBT §3.1 (coverage 43% → 80%+). The module has three
public classes:

  - ``CacheRefreshRequest`` — frozen dataclass, three factories.
  - ``CacheRefreshBus`` — in-memory FIFO queue with
    ``publish``/``drain``/``__len__``/``__repr__``.
  - ``CacheWarmer`` — drains the bus and applies each
    request to the right manager's ``refresh_cache``.
    ``pump_once`` is the single sink; ``run_forever`` is the
    cooperative background loop.

The tests use the real ``EventLog`` + managers against
fakeredis (AGENTS.md §7.1). The error branch of
``pump_once`` is exercised with an ``AsyncMock`` that
raises on ``refresh_cache``; the cancel branch of
``run_forever`` is exercised with ``asyncio.CancelledError``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
import pytest_asyncio

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._memory import (
    RedisContinuityStorage,
    RedisProfileStorage,
    RedisSessionStorage,
)
from kntgraph.memory.cache_warmer import (
    CacheRefreshBus,
    CacheRefreshRequest,
    CacheWarmer,
)
from kntgraph.memory.continuity.manager import ContinuityManager
from kntgraph.memory.profile import ProfileManager
from kntgraph.memory.session import SessionEventType, SessionManager
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
# CacheRefreshRequest
# ---------------------------------------------------------------------------


class TestCacheRefreshRequest:
    def test_session_kind(self):
        r = CacheRefreshRequest(kind="session", id1="sess-1")
        assert r.kind == "session"
        assert r.id1 == "sess-1"
        assert r.id2 == ""

    def test_profile_kind(self):
        r = CacheRefreshRequest(kind="profile", id1="t", id2="u")
        assert r.kind == "profile"
        assert r.id1 == "t"
        assert r.id2 == "u"

    def test_continuity_kind(self):
        r = CacheRefreshRequest(kind="continuity", id1="t", id2="u")
        assert r.kind == "continuity"


# ---------------------------------------------------------------------------
# CacheRefreshBus
# ---------------------------------------------------------------------------


class TestCacheRefreshBus:
    def test_empty_on_init(self):
        bus = CacheRefreshBus()
        assert len(bus) == 0
        assert bus.drain() == []

    def test_publish_enqueues(self):
        bus = CacheRefreshBus()
        bus.publish(CacheRefreshRequest(kind="session", id1="s1"))
        bus.publish(CacheRefreshRequest(kind="profile", id1="t", id2="u"))
        assert len(bus) == 2

    def test_drain_returns_all_and_clears(self):
        bus = CacheRefreshBus()
        bus.publish(CacheRefreshRequest(kind="session", id1="s1"))
        bus.publish(CacheRefreshRequest(kind="profile", id1="t", id2="u"))
        drained = bus.drain()
        assert len(drained) == 2
        assert len(bus) == 0

    def test_drain_after_publish_keeps_new(self):
        bus = CacheRefreshBus()
        bus.publish(CacheRefreshRequest(kind="session", id1="s1"))
        first = bus.drain()
        bus.publish(CacheRefreshRequest(kind="profile", id1="t", id2="u"))
        second = bus.drain()
        assert len(first) == 1
        assert len(second) == 1
        assert first[0].kind == "session"
        assert second[0].kind == "profile"

    def test_repr_shows_pending_count(self):
        bus = CacheRefreshBus()
        bus.publish(CacheRefreshRequest(kind="session", id1="s1"))
        bus.publish(CacheRefreshRequest(kind="profile", id1="t", id2="u"))
        assert repr(bus) == "CacheRefreshBus(pending=2)"


# ---------------------------------------------------------------------------
# CacheWarmer.pump_once
# ---------------------------------------------------------------------------


class TestCacheWarmerPumpOnce:
    pytestmark = pytest.mark.asyncio

    async def test_pump_empty_bus_returns_zero(self, session_manager, profile_manager):
        bus = CacheRefreshBus()
        warmer = CacheWarmer(bus, session_manager, profile_manager)

        assert await warmer.pump_once() == 0

    async def test_pump_applies_session_request(
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
        bus = CacheRefreshBus()
        warmer = CacheWarmer(bus, session_manager, profile_manager)
        bus.publish(CacheRefreshRequest(kind="session", id1="sess-1"))

        assert await warmer.pump_once() == 1
        cached = await session_manager.read("sess-1")
        assert cached is not None
        assert cached.session_id == "sess-1"

    async def test_pump_applies_profile_request(
        self, event_log, session_manager, profile_manager
    ):
        await event_log.append(
            Event.domain_from(
                agent_id="profile:t:u",
                type="profile.created",
                data={"tenant_id": "t", "user_id": "u"},
                correlation=CorrelationContext.new(),
            )
        )
        bus = CacheRefreshBus()
        warmer = CacheWarmer(bus, session_manager, profile_manager)
        bus.publish(CacheRefreshRequest(kind="profile", id1="t", id2="u"))

        assert await warmer.pump_once() == 1
        cached = await profile_manager.read("t", "u")
        assert cached is not None

    async def test_pump_applies_continuity_request(
        self, event_log, session_manager, profile_manager, continuity_manager
    ):
        await event_log.append(
            Event.domain_from(
                agent_id="continuity:t:u",
                type="continuity.created",
                data={"tenant_id": "t", "user_id": "u"},
                correlation=CorrelationContext.new(),
            )
        )
        bus = CacheRefreshBus()
        warmer = CacheWarmer(bus, session_manager, profile_manager, continuity_manager)
        bus.publish(CacheRefreshRequest(kind="continuity", id1="t", id2="u"))

        assert await warmer.pump_once() == 1
        cached = await continuity_manager.read("t", "u")
        assert cached is not None

    async def test_pump_skips_continuity_when_unconfigured(
        self, session_manager, profile_manager
    ):
        bus = CacheRefreshBus()
        warmer = CacheWarmer(
            bus, session_manager, profile_manager, continuity_manager=None
        )
        bus.publish(CacheRefreshRequest(kind="continuity", id1="t", id2="u"))

        assert await warmer.pump_once() == 1

    async def test_pump_continues_after_request_failure(
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
        bus = CacheRefreshBus()
        warmer = CacheWarmer(bus, session_manager, profile_manager)
        warmer._sessions = AsyncMock()
        warmer._sessions.refresh_cache = AsyncMock(side_effect=RuntimeError("boom"))
        bus.publish(CacheRefreshRequest(kind="session", id1="sess-1"))
        bus.publish(CacheRefreshRequest(kind="session", id1="sess-2"))

        assert await warmer.pump_once() == 2
        assert warmer._sessions.refresh_cache.await_count == 2

    async def test_pump_drains_bus_after_processing(
        self, event_log, session_manager, profile_manager
    ):
        await event_log.append(
            Event.domain_from(
                agent_id="session:sess-1",
                type=SessionEventType.STARTED,
                data={"session_id": "sess-1"},
                correlation=CorrelationContext.new(),
            )
        )
        bus = CacheRefreshBus()
        warmer = CacheWarmer(bus, session_manager, profile_manager)
        bus.publish(CacheRefreshRequest(kind="session", id1="sess-1"))

        await warmer.pump_once()

        assert len(bus) == 0


# ---------------------------------------------------------------------------
# CacheWarmer.run_forever
# ---------------------------------------------------------------------------


class TestCacheWarmerRunForever:
    pytestmark = pytest.mark.asyncio

    async def test_run_forever_pumps_until_cancelled(
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
        bus = CacheRefreshBus()
        bus.publish(CacheRefreshRequest(kind="session", id1="sess-1"))
        warmer = CacheWarmer(bus, session_manager, profile_manager)

        task = asyncio.create_task(warmer.run_forever(interval=0))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        cached = await session_manager.read("sess-1")
        assert cached is not None

    async def test_run_forever_drains_on_cancel(
        self, event_log, session_manager, profile_manager
    ):
        await event_log.append(
            Event.domain_from(
                agent_id="session:sess-1",
                type=SessionEventType.STARTED,
                data={"session_id": "sess-1"},
                correlation=CorrelationContext.new(),
            )
        )
        bus = CacheRefreshBus()
        bus.publish(CacheRefreshRequest(kind="session", id1="sess-1"))
        warmer = CacheWarmer(bus, session_manager, profile_manager)

        original_pump = warmer.pump_once
        pump_count = 0

        async def counting_pump() -> int:
            nonlocal pump_count
            pump_count += 1
            return await original_pump()

        warmer.pump_once = counting_pump  # type: ignore[method-assign]

        task = asyncio.create_task(warmer.run_forever(interval=0.05))
        await asyncio.sleep(0.12)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert pump_count >= 2
        cached = await session_manager.read("sess-1")
        assert cached is not None
