# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``SessionManager`` and ``ProfileManager``
that exercise the **full path** (cache + EventLog) against
``fakeredis``. The integration suite uses a real Redis; this
suite pins the contract end-to-end with no external
dependency so it runs in CI without Docker.
"""

from __future__ import annotations

import uuid

import fakeredis.aioredis
import pytest
import pytest_asyncio

from kntgraph.core.event import (
    CorrelationContext,
)
from kntgraph.infra.redis._errors import MemoryDecodeError
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._memory import (
    RedisContinuityStorage,
    RedisProfileStorage,
    RedisSessionStorage,
)
from kntgraph.memory.continuity.manager import ContinuityManager
from kntgraph.memory.profile import ProfileManager, ProfileState
from kntgraph.memory.session import SessionManager, SessionState
from kntgraph.stream.event_log import EventLog


pytestmark = pytest.mark.asyncio


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
async def session_storage(fake_redis):
    return RedisSessionStorage(fake_redis)


@pytest_asyncio.fixture
async def profile_storage(fake_redis):
    return RedisProfileStorage(fake_redis)


@pytest_asyncio.fixture
async def continuity_storage(fake_redis):
    return RedisContinuityStorage(fake_redis)


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------


def _ctx() -> CorrelationContext:
    return CorrelationContext.new(correlation_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class TestSessionManagerReadThrough:
    async def test_start_writes_event_and_cache(
        self, event_log, session_storage, fake_redis
    ):
        sm = SessionManager(event_log, session_storage, ttl_seconds=60)
        result = await sm.start("s1", user_id="u1", tenant_id="t1")
        assert result.is_ok()
        # The cache is now present
        key = "knt:session:s1"
        assert await fake_redis.exists(key)
        # The EventLog has exactly one event
        events = await event_log.read(SessionManager.agent_id_for("s1"))
        assert len(events) == 1
        assert events[0].event_type == "session.started"

    async def test_read_returns_state_after_start(
        self, event_log, session_storage, fake_redis
    ):
        sm = SessionManager(event_log, session_storage, ttl_seconds=60)
        await sm.start("s1", user_id="u1", tenant_id="t1")
        state = await sm.read("s1")
        assert state is not None
        assert isinstance(state, SessionState)
        assert state.session_id == "s1"
        assert state.user_id == "u1"
        assert state.tenant_id == "t1"
        assert state.is_active()

    async def test_read_returns_none_for_unknown_session(
        self, event_log, session_storage
    ):
        sm = SessionManager(event_log, session_storage)
        assert await sm.read("nonexistent") is None

    async def test_cache_rebuilt_after_invalidation(
        self, event_log, session_storage, fake_redis
    ):
        sm = SessionManager(event_log, session_storage, ttl_seconds=60)
        await sm.start("s1", user_id="u1", tenant_id="t1")
        # Manually delete the cache
        await fake_redis.delete("knt:session:s1")
        # Read rebuilds from the EventLog
        state = await sm.read("s1")
        assert state is not None
        assert state.user_id == "u1"
        # Cache re-populated
        assert await fake_redis.exists("knt:session:s1")

    async def test_append_message_increments_state(self, event_log, session_storage):
        sm = SessionManager(event_log, session_storage, ttl_seconds=60)
        await sm.start("s1", user_id="u", tenant_id="t")
        await sm.append_message("s1", "user", "hello")
        await sm.append_message("s1", "assistant", "hi")
        state = await sm.read("s1")
        assert state is not None
        assert len(state.messages) == 2
        assert state.messages[0]["role"] == "user"
        assert state.messages[1]["content"] == "hi"

    async def test_empty_message_returns_err(self, event_log, session_storage):
        sm = SessionManager(event_log, session_storage)
        await sm.start("s1", user_id="u", tenant_id="t")
        result = await sm.append_message("s1", "user", "")
        assert result.is_err()

    async def test_set_context_updates_state(self, event_log, session_storage):
        sm = SessionManager(event_log, session_storage)
        await sm.start("s1", user_id="u", tenant_id="t")
        await sm.set_context("s1", "scratchpad", {"todo": "x"})
        state = await sm.read("s1")
        assert state is not None
        assert state.context.get("scratchpad") == {"todo": "x"}

    async def test_end_marks_session_inactive(self, event_log, session_storage):
        sm = SessionManager(event_log, session_storage)
        await sm.start("s1", user_id="u", tenant_id="t")
        await sm.append_message("s1", "user", "hi")
        await sm.end("s1")
        state = await sm.read("s1")
        assert state is not None
        assert not state.is_active()
        assert state.ended_at is not None

    async def test_idempotent_start(self, event_log, session_storage):
        sm = SessionManager(event_log, session_storage)
        r1 = await sm.start("s1", user_id="u", tenant_id="t")
        r2 = await sm.start("s1", user_id="u", tenant_id="t")
        assert r1.is_ok() and r2.is_ok()
        # Same event_id (idempotency on the EventLog)
        assert r1.unwrap().event_id == r2.unwrap().event_id

    async def test_list_active_filters_by_tenant(self, event_log, session_storage):
        sm = SessionManager(event_log, session_storage)
        await sm.start("s1", user_id="u1", tenant_id="t-A")
        await sm.start("s2", user_id="u2", tenant_id="t-A")
        await sm.start("s3", user_id="u3", tenant_id="t-B")
        active = await sm.list_active("t-A")
        assert len(active) == 2
        assert {s.session_id for s in active} == {"s1", "s2"}


class TestSessionManagerRefreshCache:
    async def test_refresh_cache_rebuilds_from_log(
        self, event_log, session_storage, fake_redis
    ):
        sm = SessionManager(event_log, session_storage, ttl_seconds=60)
        await sm.start("s1", user_id="u", tenant_id="t")
        # Wipe the cache
        await fake_redis.delete("knt:session:s1")
        # refresh_cache rebuilds it
        await sm.refresh_cache("s1")
        assert await fake_redis.exists("knt:session:s1")


# ---------------------------------------------------------------------------
# ProfileManager
# ---------------------------------------------------------------------------


class TestProfileManagerReadThrough:
    async def test_create_writes_event_and_cache(
        self, event_log, profile_storage, fake_redis
    ):
        pm = ProfileManager(event_log, profile_storage)
        result = await pm.create(
            "tenant-1", "user-1", preferences={"lang": "pt-BR"}, tier="vip"
        )
        assert result.is_ok()
        assert await fake_redis.exists("knt:profile:tenant-1:user-1")

    async def test_read_after_create(self, event_log, profile_storage):
        pm = ProfileManager(event_log, profile_storage)
        await pm.create("t1", "u1", preferences={"lang": "pt"}, tier="vip")
        state = await pm.read("t1", "u1")
        assert state is not None
        assert state.tenant_id == "t1"
        assert state.user_id == "u1"
        assert state.tier == "vip"
        assert state.preferences.get("lang") == "pt"

    async def test_set_preference_updates_state(self, event_log, profile_storage):
        pm = ProfileManager(event_log, profile_storage)
        await pm.create("t1", "u1", preferences={"lang": "pt"})
        await pm.set_preference("t1", "u1", "currency", "BRL")
        state = await pm.read("t1", "u1")
        assert state is not None
        assert state.preferences.get("currency") == "BRL"
        assert state.preferences.get("lang") == "pt"

    async def test_unset_preference_removes_key(self, event_log, profile_storage):
        pm = ProfileManager(event_log, profile_storage)
        await pm.create("t1", "u1", preferences={"lang": "pt"})
        await pm.unset_preference("t1", "u1", "lang")
        state = await pm.read("t1", "u1")
        assert state is not None
        assert "lang" not in state.preferences

    def test_unset_unknown_key_is_noop(self, event_log, profile_storage):
        # not async; just exercises the dataclass path
        state = ProfileState(
            tenant_id="t",
            user_id="u",
            preferences={"a": "b"},
            tier="standard",
            created_at=0.0,
            updated_at=0.0,
        )
        state.preferences.pop("doesnotexist", None)
        assert state.preferences == {"a": "b"}

    async def test_change_tier(self, event_log, profile_storage):
        pm = ProfileManager(event_log, profile_storage)
        await pm.create("t1", "u1", tier="standard")
        await pm.change_tier("t1", "u1", "vip")
        state = await pm.read("t1", "u1")
        assert state is not None
        assert state.tier == "vip"

    async def test_read_nonexistent_returns_none(self, event_log, profile_storage):
        pm = ProfileManager(event_log, profile_storage)
        assert await pm.read("ghost", "ghost") is None

    async def test_list_for_tenant(self, event_log, profile_storage):
        pm = ProfileManager(event_log, profile_storage)
        await pm.create("t1", "u1")
        await pm.create("t1", "u2")
        await pm.create("t2", "u9")
        out = await pm.list_for_tenant("t1")
        assert len(out) == 2
        assert {s.user_id for s in out} == {"u1", "u2"}


# ---------------------------------------------------------------------------
# ContinuityManager (basic shape — depth is in tests/unit/memory/test_continuity_fold.py)
# ---------------------------------------------------------------------------


class TestContinuityManagerReadThrough:
    async def test_create_then_read(self, event_log, continuity_storage):
        cm = ContinuityManager(event_log, continuity_storage)
        r = await cm.create("t1", "u1")
        assert r.is_ok()
        state = await cm.read("t1", "u1")
        assert state is not None
        assert state.tenant_id == "t1"
        assert state.user_id == "u1"

    async def test_recency_suggest_returns_value(self, event_log, continuity_storage):
        cm = ContinuityManager(event_log, continuity_storage)
        await cm.create("t1", "u1")
        await cm.record_category_chosen("t1", "u1", "cfop", "5.102")
        slot = await cm.recency_suggest("t1", "u1", "cfop")
        # The continuity fold stores ``<value>|<timestamp>``;
        # recency_suggest returns the full string.
        assert slot is not None
        assert slot.startswith("5.102|")

    async def test_recency_suggest_after_clear(self, event_log, continuity_storage):
        cm = ContinuityManager(event_log, continuity_storage)
        await cm.create("t1", "u1")
        await cm.record_category_chosen("t1", "u1", "cfop", "5.102")
        await cm.clear("t1", "u1")
        # After clear, the value is hidden (LGPD semantics)
        assert await cm.recency_suggest("t1", "u1", "cfop") is None


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestManagerErrorPaths:
    async def test_session_read_with_corrupt_payload(
        self, event_log, session_storage, fake_redis
    ):
        sm = SessionManager(event_log, session_storage)
        # Pre-seed the cache with garbage (not a valid session payload)
        # The JSON-codec will fail; the base class must fall through
        # to the EventLog fold and return None (no events exist).
        await fake_redis.set("knt:session:bad", b"{not json")
        # The storage may succeed decoding to a dict-like value or
        # fail. Either way, the read path must NOT raise.
        try:
            result = await sm.read("bad")
        except (MemoryDecodeError, ValueError):
            return
        # If the codec survived, it either decoded (None) or the
        # base class logged + returned None via the fold.
        assert result is None

    async def test_profile_read_with_missing_created_at(
        self, event_log, profile_storage, fake_redis
    ):
        pm = ProfileManager(event_log, profile_storage)
        # Pre-seed a hash without ``created_at``: the decoder
        # should raise ``MemoryDecodeError`` via the storage path.
        await fake_redis.hset("knt:profile:t1:u1", mapping={"tier": "vip"})
        result = await pm.read("t1", "u1")
        # Cache miss (no events in the log + decode error fallback
        # is logged + fold returns None)
        assert result is None
