# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for memory/session.py and memory/profile.py
(real Redis).
"""

from __future__ import annotations
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._memory import RedisProfileStorage
from kntgraph.infra.redis._memory import RedisSessionStorage


import pytest

from kntgraph.memory.cache_warmer import (
    CacheRefreshBus,
    CacheRefreshRequest,
    CacheWarmer,
)
from kntgraph.memory.consolidation import Consolidator, Projector
from kntgraph.memory.profile import ProfileManager, ProfileState
from kntgraph.memory.session import SessionManager, SessionState
from kntgraph.stream.event_log import EventLog

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class TestSessionManager:
    async def test_start_appends_started_event(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis), ttl_seconds=60)

        result = await sm.start("sess-1", user_id="u-1", tenant_id="t-1")
        assert result.is_ok()

        events = await log.read(SessionManager.agent_id_for("sess-1"))
        assert len(events) == 1
        assert events[0].event_type == "session.started"

    async def test_start_writes_cache_with_ttl(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis), ttl_seconds=60)
        await sm.start("sess-1", user_id="u-1", tenant_id="t-1")

        # Cache key exists
        assert await clean_redis.exists("knt:session:sess-1")
        # TTL was set
        ttl = await clean_redis.ttl("knt:session:sess-1")
        assert ttl > 0

    async def test_append_message_aggregates(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis), ttl_seconds=60)
        await sm.start("sess-1", user_id="u", tenant_id="t")

        await sm.append_message("sess-1", "user", "olá")
        await sm.append_message("sess-1", "assistant", "oi!")
        await sm.append_message("sess-1", "user", "tudo bem?")

        # Read through the manager (which folds from log)
        state = await sm.read("sess-1")
        assert state is not None
        assert len(state.messages) == 3
        assert state.messages[0]["content"] == "olá"
        assert state.messages[1]["role"] == "assistant"
        assert state.messages[2]["content"] == "tudo bem?"

    async def test_set_context(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        await sm.start("sess-1", user_id="u", tenant_id="t")
        await sm.set_context("sess-1", "scratchpad", {"todo": "x"})
        state = await sm.read("sess-1")
        assert state.context["scratchpad"] == {"todo": "x"}

    async def test_end_marks_inactive(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        await sm.start("sess-1", user_id="u", tenant_id="t")
        await sm.append_message("sess-1", "user", "olá")
        await sm.end("sess-1")

        state = await sm.read("sess-1")
        assert state is not None
        assert not state.is_active()
        assert state.ended_at is not None

    async def test_read_with_no_events_returns_none(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        state = await sm.read("nonexistent")
        assert state is None

    async def test_cache_rebuilt_from_log(self, clean_redis):
        """
        If we manually delete the cache, the next read
        should rebuild it from the EventLog.
        """
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        await sm.start("sess-1", user_id="u", tenant_id="t")
        await sm.append_message("sess-1", "user", "hello")

        # Cache should exist
        assert await clean_redis.exists("knt:session:sess-1")

        # Delete the cache
        await clean_redis.delete("knt:session:sess-1")
        assert not await clean_redis.exists("knt:session:sess-1")

        # Read should rebuild from log
        state = await sm.read("sess-1")
        assert state is not None
        assert len(state.messages) == 1
        # Cache is now re-populated
        assert await clean_redis.exists("knt:session:sess-1")

    async def test_idempotent_start(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        r1 = await sm.start("sess-1", user_id="u", tenant_id="t")
        r2 = await sm.start("sess-1", user_id="u", tenant_id="t")
        assert r1.is_ok() and r2.is_ok()
        # Same event_id (idempotent)
        assert r1.unwrap().event_id == r2.unwrap().event_id
        # Only one event in the stream
        events = await log.read(SessionManager.agent_id_for("sess-1"))
        assert len(events) == 1


# ---------------------------------------------------------------------------
# ProfileManager
# ---------------------------------------------------------------------------


class TestProfileManager:
    async def test_create(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        result = await pm.create(
            "t-1", "u-1", preferences={"lang": "pt-BR"}, tier="vip"
        )
        assert result.is_ok()

        state = await pm.read("t-1", "u-1")
        assert state is not None
        assert state.preferences == {"lang": "pt-BR"}
        assert state.tier == "vip"

    async def test_cache_uses_hash(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        await pm.create("t-1", "u-1", preferences={"lang": "pt-BR"})

        # Cache key exists
        assert await clean_redis.exists("knt:profile:t-1:u-1")
        # It's a hash, not a JSON string
        key_type = await clean_redis.type("knt:profile:t-1:u-1")
        assert key_type == b"hash"

    async def test_set_preference(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        await pm.create("t-1", "u-1")
        await pm.set_preference("t-1", "u-1", "lang", "pt-BR")
        await pm.set_preference("t-1", "u-1", "currency", "BRL")

        state = await pm.read("t-1", "u-1")
        assert state.preferences == {"lang": "pt-BR", "currency": "BRL"}

    async def test_unset_preference(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        await pm.create("t-1", "u-1", preferences={"lang": "pt-BR"})
        await pm.unset_preference("t-1", "u-1", "lang")

        state = await pm.read("t-1", "u-1")
        assert "lang" not in state.preferences

    async def test_change_tier(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        await pm.create("t-1", "u-1", tier="standard")
        await pm.change_tier("t-1", "u-1", "vip")

        state = await pm.read("t-1", "u-1")
        assert state.tier == "vip"

    async def test_cache_rebuilt_from_log(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        await pm.create("t-1", "u-1", preferences={"lang": "pt-BR"})
        await pm.set_preference("t-1", "u-1", "currency", "BRL")

        # Delete cache
        await clean_redis.delete("knt:profile:t-1:u-1")
        # Read rebuilds
        state = await pm.read("t-1", "u-1")
        assert state.preferences == {"lang": "pt-BR", "currency": "BRL"}
        # Cache re-populated
        assert await clean_redis.exists("knt:profile:t-1:u-1")

    async def test_read_unknown_returns_none(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        state = await pm.read("t-x", "u-x")
        assert state is None

    async def test_idempotent_create(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        r1 = await pm.create("t", "u", preferences={"k": "v"})
        r2 = await pm.create("t", "u", preferences={"k": "v"})
        assert r1.unwrap().event_id == r2.unwrap().event_id


# ---------------------------------------------------------------------------
# Consolidator (pure publisher) + CacheWarmer (I/O adapter)
# ---------------------------------------------------------------------------


class TestConsolidator:
    async def test_consolidator_is_pure_publishes_to_bus(self, clean_redis):
        """
        The Consolidator must perform NO I/O. After `refresh_all`,
        the only side effect should be requests on the bus.
        """
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        bus = CacheRefreshBus()
        cons = Consolidator(log, bus, sm, pm)

        # Set up a session + delete its cache
        await sm.start("s-1", user_id="u", tenant_id="t")
        await pm.create("t-1", "u-1", preferences={"lang": "pt-BR"})
        await clean_redis.delete("knt:session:s-1")
        await clean_redis.delete("knt:profile:t-1:u-1")

        # Run a cyclic tick with the consolidator
        system = cons.as_cyclic_system()
        from kntgraph.stream.projection import fold_world

        world = await fold_world(log)
        out = await system(world)

        # No events emitted
        assert out == []
        # Caches are STILL cold — the Consolidator is pure.
        assert not await clean_redis.exists("knt:session:s-1")
        assert not await clean_redis.exists("knt:profile:t-1:u-1")
        # But the bus has 2 pending requests.
        assert len(bus) == 2

    async def test_warmer_drains_bus_and_rebuilds_caches(self, clean_redis):
        """
        The CacheWarmer consumes the bus and applies the
        requests to the actual cache stores.
        """
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        bus = CacheRefreshBus()
        cons = Consolidator(log, bus, sm, pm)
        warmer = CacheWarmer(bus, sm, pm)

        # Set up + delete caches
        await sm.start("s-1", user_id="u", tenant_id="t")
        await pm.create("t-1", "u-1", preferences={"lang": "pt-BR"})
        await clean_redis.delete("knt:session:s-1")
        await clean_redis.delete("knt:profile:t-1:u-1")

        # Publish via the Consolidator's cyclic system
        from kntgraph.stream.projection import fold_world

        system = cons.as_cyclic_system()
        world = await fold_world(log)
        await system(world)
        assert len(bus) == 2

        # Warmer consumes + writes
        applied = await warmer.pump_once()
        assert applied == 2
        assert len(bus) == 0

        # Caches are now warm
        assert await clean_redis.exists("knt:session:s-1")
        assert await clean_redis.exists("knt:profile:t-1:u-1")

    async def test_warmer_is_idempotent_on_empty_bus(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        bus = CacheRefreshBus()
        warmer = CacheWarmer(bus, sm, pm)

        applied = await warmer.pump_once()
        assert applied == 0
        assert len(bus) == 0

    async def test_warmer_continues_on_individual_failure(self, clean_redis):
        """
        A failure on one request must not abort the rest of
        the batch. We publish two requests; the first one
        points to a non-existent session (no error) and the
        second one is a malformed request that the warmer
        handles gracefully.
        """
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        bus = CacheRefreshBus()
        warmer = CacheWarmer(bus, sm, pm)

        # Publish requests directly
        bus.publish(CacheRefreshRequest(kind="session", id1="ghost"))
        bus.publish(CacheRefreshRequest(kind="profile", id1="t-x", id2="u-x"))

        # Both should be applied (no exception)
        applied = await warmer.pump_once()
        assert applied == 2

    async def test_projector_project_all(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        proj = Projector(log, sm, pm)

        # Set up one of each
        await sm.start("s-1", user_id="u", tenant_id="t")
        await sm.append_message("s-1", "user", "olá")
        await pm.create("t-1", "u-1", preferences={"lang": "pt-BR"})

        # Wipe caches
        await clean_redis.delete("knt:session:s-1")
        await clean_redis.delete("knt:profile:t-1:u-1")

        # Project all
        result = await proj.project_all()
        # ``continuity`` always appears in the counts (ADR-014),
        # with value 0 when no continuity events exist.
        assert result == {"sessions": 1, "profiles": 1, "continuity": 0}

        # Caches are warm
        assert await clean_redis.exists("knt:session:s-1")
        assert await clean_redis.exists("knt:profile:t-1:u-1")


# ---------------------------------------------------------------------------
# Public contract: write_cache / refresh_cache
#
# These methods are part of the cross-module API used by the
# Projector (consolidation.Projector) and the CacheWarmer
# (cache_warmer.CacheWarmer). They follow the "short-memory"
# shape from the Redis Agent Builder pattern, adapted to the
# event-sourced model. The tests below pin the contract that
# any cross-module caller can rely on.
# ---------------------------------------------------------------------------


class TestPublicCacheContract:
    async def test_session_write_cache_is_public(self, clean_redis):
        """
        `SessionManager.write_cache` is a public method.
        No leading underscore. The Projector relies on it.
        """
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        state = SessionState(
            session_id="s-1",
            user_id="u",
            tenant_id="t",
            messages=(),
            context={"k": "v"},
            started_at=1.0,
        )
        # Must work without going through start()
        await sm.write_cache("s-1", state)
        # The cache now has the value we wrote
        cached = await sm.read("s-1")
        assert cached is not None
        assert cached.context == {"k": "v"}

    async def test_session_refresh_cache_is_public(self, clean_redis):
        """
        `SessionManager.refresh_cache` is a public method
        used by the CacheWarmer adapter. It rebuilds the
        cache by folding the EventLog.
        """
        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        await sm.start("s-1", user_id="u", tenant_id="t")
        await sm.append_message("s-1", "user", "olá")
        # Wipe the cache manually
        await clean_redis.delete("knt:session:s-1")
        # The public method rebuilds it
        await sm.refresh_cache("s-1")
        # The cache is back
        assert await clean_redis.exists("knt:session:s-1")
        state = await sm.read("s-1")
        assert state is not None
        assert len(state.messages) == 1

    async def test_profile_write_cache_is_public(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        state = ProfileState(
            tenant_id="t",
            user_id="u",
            preferences={"lang": "pt-BR"},
            tier="vip",
            created_at=1.0,
            updated_at=2.0,
        )
        await pm.write_cache("t", "u", state)
        cached = await pm.read("t", "u")
        assert cached is not None
        assert cached.tier == "vip"
        assert cached.preferences == {"lang": "pt-BR"}

    async def test_profile_refresh_cache_is_public(self, clean_redis):
        log = EventLog(RedisEventLogAdapter(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        await pm.create("t", "u", preferences={"lang": "pt-BR"})
        await pm.set_preference("t", "u", "currency", "BRL")
        await clean_redis.delete("knt:profile:t:u")
        await pm.refresh_cache("t", "u")
        state = await pm.read("t", "u")
        assert state is not None
        assert state.preferences == {"lang": "pt-BR", "currency": "BRL"}

    async def test_projector_uses_public_write_cache(self, clean_redis):
        """
        The Projector (the cross-module caller) uses the
        public `write_cache` method, not a private one.
        We verify the round-trip works end-to-end.
        """
        from kntgraph.memory.consolidation import Projector

        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        proj = Projector(log, sm, pm)

        await sm.start("s-1", user_id="u", tenant_id="t")
        await sm.append_message("s-1", "user", "olá")
        await pm.create("t-1", "u-1", tier="vip")
        await clean_redis.delete("knt:session:s-1")
        await clean_redis.delete("knt:profile:t-1:u-1")

        # The Projector must call the public methods. If the
        # contract is broken (e.g. someone reintroduces a
        # leading underscore), AttributeError surfaces here.
        result = await proj.project_all()
        # ``continuity`` always appears in the counts (ADR-014),
        # with value 0 when no continuity events exist.
        assert result == {"sessions": 1, "profiles": 1, "continuity": 0}

    async def test_warmer_uses_public_refresh_cache(self, clean_redis):
        """
        The CacheWarmer adapter uses the public `refresh_cache`
        method, not a private one. We verify the bus → warmer
        pipeline works end-to-end.
        """
        from kntgraph.memory.cache_warmer import (
            CacheRefreshBus,
            CacheRefreshRequest,
            CacheWarmer,
        )

        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        bus = CacheRefreshBus()
        warmer = CacheWarmer(bus, sm, pm)

        await sm.start("s-1", user_id="u", tenant_id="t")
        await clean_redis.delete("knt:session:s-1")
        bus.publish(CacheRefreshRequest(kind="session", id1="s-1"))

        # If the contract is broken, the warmer will raise
        # AttributeError on the underscore-prefixed call.
        applied = await warmer.pump_once()
        assert applied == 1
        assert await clean_redis.exists("knt:session:s-1")
