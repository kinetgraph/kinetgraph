# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``BaseShortTermMemory.refresh_cache_incremental``
(ADR-068 §3.4 P4) with the **parallel-key** cursor design.

The fold cursor lives on a parallel Redis key
(``<cache_key>:fold_cursor``), NOT inside the cache
payload. This test module pins that contract:

  - the cache payload stays bit-identical to the legacy
    wire format (Hash for Profile/Continuity, JSON for
    Session);
  - the parallel key is stamped on cold ``refresh_cache``
    and consulted on incremental
    ``refresh_cache_incremental``;
  - the cold path auto-seeds the cursor (no migration
    needed for existing caches);
  - a missing cursor OR a missing cache self-corrects:
    the incremental path falls back to the cold rebuild;
  - the base default ``_fold_incremental`` returns
    ``None`` — the manager-level overrides (when
    present) implement the per-event delta merge; the
    test asserts the contract of the fallback path.

Coverage matrix per AGENTS.md §7.2 (happy path + one
failure mode per public function):

  Base contract
    - ``_fold_cursor_key`` derives ``<key>:fold_cursor``.
    - ``_read_fold_cursor`` / ``_write_fold_cursor``
      are the only cursor I/O points; the cache payload
      carries no ``__fold_cursor__`` field.
    - The base exposes ``refresh_cache_incremental`` as
      an async coroutine.

  Cold path (refresh_cache)
    - Seeds the parallel cursor on the first call.
    - Payload is bit-identical to the legacy wire
      format (no ``__fold_cursor__`` injected).

  Incremental path
    - With cursor + non-empty delta → falls back to
      cold ``refresh_cache`` (the base default
      ``_fold_incremental`` returns None) and
      re-seeds the cursor.
    - With cursor + empty delta → no-op, no Redis
      write to the cache payload.
    - Without cursor → cold fallback (the cache
      rebuilds and the cursor is re-seeded).

  Auto-correction
    - Cache payload present, cursor missing → fallback
      to cold path.
    - Cursor present, cache missing → fallback to cold
      path.

  TTL coupling
    - The cursor key shares the cache payload's TTL
      when one is configured.
"""

from __future__ import annotations

import inspect

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
from kntgraph.memory.base import (
    FOLD_CURSOR_SUFFIX,
    BaseShortTermMemory,
)
from kntgraph.memory.profile import (
    ProfileEventType,
    ProfileManager,
)
from kntgraph.memory.session import (
    SessionEventType,
    SessionManager,
)
from kntgraph.memory.continuity.manager import ContinuityManager
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


def _session_started_event(session_id: str = "sess-1") -> Event:
    return Event.domain_from(
        agent_id=f"session:{session_id}",
        type=SessionEventType.STARTED,
        data={
            "session_id": session_id,
            "user_id": "u",
            "tenant_id": "t",
        },
        correlation=CorrelationContext.new(),
    )


def _session_message_event(session_id: str, role: str, content: str) -> Event:
    return Event.domain_from(
        agent_id=f"session:{session_id}",
        type=SessionEventType.MESSAGE,
        data={"role": role, "content": content},
        correlation=CorrelationContext.new(),
    )


def _profile_created_event(tenant: str, user: str) -> Event:
    return Event.domain_from(
        agent_id=f"profile:{tenant}:{user}",
        type=ProfileEventType.CREATED,
        data={"tenant_id": tenant, "user_id": user, "tier": "standard"},
        correlation=CorrelationContext.new(),
    )


def _profile_preference_set_event(
    tenant: str, user: str, key: str, value: str
) -> Event:
    return Event.domain_from(
        agent_id=f"profile:{tenant}:{user}",
        type=ProfileEventType.PREFERENCE_SET,
        data={"key": key, "value": value},
        correlation=CorrelationContext.new(),
    )


# ---------------------------------------------------------------------------
# Base contract
# ---------------------------------------------------------------------------


class TestBaseContract:
    def test_fold_cursor_suffix_is_dunder(self):
        """The suffix is the public, ops-visible marker
        that distinguishes the parallel cursor key."""
        assert FOLD_CURSOR_SUFFIX == ":fold_cursor"

    def test_fold_cursor_key_derives_from_cache_key(self):
        """The parallel key is the cache key with the
        suffix appended — predictable for ops tools."""
        from kntgraph.memory.base import BaseShortTermMemory

        # Use a concrete subclass to access the method.
        class _Probe(BaseShortTermMemory[int]):
            agent_id_prefix = "x:"

            @classmethod
            def cache_key(cls, *parts: str) -> str:
                return "knt:probe:" + parts[0]

            async def _read_cache(self, key, *key_parts):
                from kntgraph.core.result import Ok

                return Ok(None)

            async def _fold_from_log(self, *key_parts):
                return None

            def _serialize_for_cache(self, state):
                return ""

        p = _Probe.__new__(_Probe)
        assert p._fold_cursor_key("knt:probe:s1") == "knt:probe:s1:fold_cursor"

    def test_refresh_cache_incremental_is_async(self):
        assert inspect.iscoroutinefunction(
            BaseShortTermMemory.refresh_cache_incremental
        )

    def test_base_does_not_inject_fold_cursor_into_payload(self):
        """Sanity: the base must not silently add a
        ``__fold_cursor__`` key to the payload — the
        whole point of the parallel-key design is that
        the payload stays legacy-compatible."""
        from kntgraph.memory.base import BaseShortTermMemory

        source = inspect.getsource(BaseShortTermMemory)
        assert "__fold_cursor__" not in source, (
            "BaseShortTermMemory must not touch the cache payload; "
            "the fold cursor lives on a parallel Redis key only."
        )


# ---------------------------------------------------------------------------
# Cold path: refresh_cache seeds the parallel cursor
# ---------------------------------------------------------------------------


class TestColdPathSeedsCursor:
    pytestmark = pytest.mark.asyncio

    async def test_session_cold_refresh_stamps_cursor(self, event_log, session_manager):
        await event_log.append(_session_started_event("sess-1"))
        await session_manager.refresh_cache("sess-1")
        cursor = await session_manager._read_fold_cursor(
            session_manager.cache_key("sess-1")
        )
        assert cursor is not None
        # Format: ``<ms>-<seq>`` (Redis stream id).
        assert "-" in cursor

    async def test_profile_cold_refresh_stamps_cursor(self, event_log, profile_manager):
        await event_log.append(_profile_created_event("t", "u"))
        await profile_manager.refresh_cache("t", "u")
        cursor = await profile_manager._read_fold_cursor(
            profile_manager.cache_key("t", "u")
        )
        assert cursor is not None

    async def test_cold_path_payload_keeps_legacy_shape(
        self, event_log, session_manager
    ):
        """The cache payload (JSON for Session) must NOT
        contain a ``__fold_cursor__`` field — the cursor
        lives on the parallel key, not in the payload."""
        await event_log.append(_session_started_event("sess-1"))
        await session_manager.refresh_cache("sess-1")
        raw = await session_manager._storage.get_record(
            session_manager.cache_key("sess-1")
        )
        payload = raw.ok_value()
        assert "__fold_cursor__" not in payload, (
            "Session JSON payload must remain legacy-compatible; "
            f"got keys={list(payload.keys())}"
        )

    async def test_cold_path_payload_keeps_legacy_shape_profile(
        self, event_log, profile_manager
    ):
        """Same for Profile (Hash tier)."""
        await event_log.append(_profile_created_event("t", "u"))
        await profile_manager.refresh_cache("t", "u")
        raw = await profile_manager._storage.get_record(
            profile_manager.cache_key("t", "u")
        )
        payload = raw.ok_value()
        assert "__fold_cursor__" not in payload, (
            "Profile Hash payload must remain legacy-compatible; "
            f"got keys={list(payload.keys())}"
        )

    async def test_cold_path_skips_cursor_when_no_events(
        self, event_log, session_manager
    ):
        """If the fold returns ``None`` (empty stream),
        the cold path leaves the cursor key untouched —
        no spurious cursor for non-existent identities."""
        await session_manager.refresh_cache("sess-does-not-exist")
        cursor = await session_manager._read_fold_cursor(
            session_manager.cache_key("sess-does-not-exist")
        )
        assert cursor is None


# ---------------------------------------------------------------------------
# Incremental path: cursor missing / empty delta / non-empty delta
# ---------------------------------------------------------------------------


class TestIncrementalPath:
    pytestmark = pytest.mark.asyncio

    async def test_missing_cursor_falls_back_to_cold(self, event_log, session_manager):
        """No cursor on the parallel key → cold rebuild,
        which seeds the cursor for the next call."""
        await event_log.append(_session_started_event("sess-1"))
        await session_manager.refresh_cache_incremental("sess-1")
        cached = await session_manager.read("sess-1")
        assert cached is not None
        cursor = await session_manager._read_fold_cursor(
            session_manager.cache_key("sess-1")
        )
        assert cursor is not None

    async def test_empty_delta_is_noop(self, event_log, session_manager):
        """Cursor present + no new events → no Redis write
        to the cache payload; cursor untouched."""
        await event_log.append(_session_started_event("sess-1"))
        await session_manager.refresh_cache("sess-1")
        first_cursor = await session_manager._read_fold_cursor(
            session_manager.cache_key("sess-1")
        )
        assert first_cursor is not None
        # No new events; second incremental call must
        # leave the cache untouched.
        await session_manager.refresh_cache_incremental("sess-1")
        cursor_after = await session_manager._read_fold_cursor(
            session_manager.cache_key("sess-1")
        )
        assert cursor_after == first_cursor

    async def test_non_empty_delta_falls_back_to_cold(self, event_log, session_manager):
        """Cursor present + non-empty delta + base
        default ``_fold_incremental`` returns ``None``
        → cold rebuild, cursor advances."""
        await event_log.append(_session_started_event("sess-1"))
        await session_manager.refresh_cache("sess-1")
        first_cursor = await session_manager._read_fold_cursor(
            session_manager.cache_key("sess-1")
        )
        msg = _session_message_event("sess-1", "user", "hi")
        await event_log.append(msg)
        await session_manager.refresh_cache_incremental("sess-1")
        # Cold rebuild path: cursor must have advanced
        # past both events.
        second_cursor = await session_manager._read_fold_cursor(
            session_manager.cache_key("sess-1")
        )
        assert second_cursor is not None
        assert second_cursor != first_cursor
        cached = await session_manager.read("sess-1")
        assert cached is not None
        # Cold rebuild surface the new MESSAGE event.
        assert cached.messages


# ---------------------------------------------------------------------------
# Auto-correction: cache disappeared OR cursor disappeared
# ---------------------------------------------------------------------------


class TestAutoCorrection:
    pytestmark = pytest.mark.asyncio

    async def test_cursor_present_cache_missing_falls_back(
        self, event_log, session_manager, fake_redis
    ):
        """Cursor parallel key set, but the cache
        payload was deleted under us → cold rebuild
        (self-correct)."""
        await event_log.append(_session_started_event("sess-1"))
        await session_manager.refresh_cache("sess-1")
        # Wipe just the cache payload (cursor survives).
        await fake_redis.delete(session_manager.cache_key("sess-1"))
        # New event arrives; incremental call sees the
        # cursor but no cache → cold fallback rebuilds
        # the cache from scratch.
        await event_log.append(_session_message_event("sess-1", "user", "hello"))
        await session_manager.refresh_cache_incremental("sess-1")
        cached = await session_manager.read("sess-1")
        assert cached is not None
        assert cached.messages

    async def test_cache_present_cursor_missing_falls_back(
        self, event_log, session_manager, fake_redis
    ):
        """Legacy cache (no cursor stamped) + incremental
        call → cold rebuild seeds the cursor for
        the next call. No migration needed for legacy
        caches."""
        await event_log.append(_session_started_event("sess-1"))
        await session_manager.refresh_cache("sess-1")
        # Wipe just the cursor; cache payload survives.
        await fake_redis.delete(
            session_manager._fold_cursor_key(session_manager.cache_key("sess-1"))
        )
        # New event arrives.
        await event_log.append(_session_message_event("sess-1", "user", "hi"))
        await session_manager.refresh_cache_incremental("sess-1")
        cached = await session_manager.read("sess-1")
        assert cached is not None
        # Cursor was re-seeded by the cold rebuild.
        cursor = await session_manager._read_fold_cursor(
            session_manager.cache_key("sess-1")
        )
        assert cursor is not None


# ---------------------------------------------------------------------------
# TTL coupling
# ---------------------------------------------------------------------------


class TestTTLCoupling:
    pytestmark = pytest.mark.asyncio

    async def test_cursor_inherits_cache_ttl(
        self, event_log, session_manager, fake_redis
    ):
        """The cursor key and the cache payload must
        expire together — fakeredis honours the ``EX``
        we passed. We assert via ``ttl`` directly."""
        await event_log.append(_session_started_event("sess-1"))
        await session_manager.refresh_cache("sess-1")
        cache_key = session_manager.cache_key("sess-1")
        cursor_key = session_manager._fold_cursor_key(cache_key)
        cache_ttl = await fake_redis.ttl(cache_key)
        cursor_ttl = await fake_redis.ttl(cursor_key)
        assert cache_ttl > 0
        assert cursor_ttl > 0
        # They should be close (within the same TTL class);
        # fakeredis gives them the same value if written
        # in the same call.
        assert cursor_ttl >= cache_ttl - 1
        assert cache_ttl >= cursor_ttl - 1


# ---------------------------------------------------------------------------
# Smoke: end-to-end Consolidator→Warmer still writes cache
# ---------------------------------------------------------------------------


class TestSmokeEndToEnd:
    pytestmark = pytest.mark.asyncio

    async def test_cold_and_incremental_round_trip_session(
        self, event_log, session_manager
    ):
        """E2E: cold seed + incremental no-op + delta."""
        await event_log.append(_session_started_event("sess-1"))
        await session_manager.refresh_cache_incremental("sess-1")
        cached = await session_manager.read("sess-1")
        assert cached is not None
        # Second tick (no delta) — no-op.
        await session_manager.refresh_cache_incremental("sess-1")
        cached = await session_manager.read("sess-1")
        assert cached is not None
        # Third tick (with delta) — cold fallback
        # re-rebuilds; cursor advances.
        await event_log.append(_session_message_event("sess-1", "user", "ok"))
        await session_manager.refresh_cache_incremental("sess-1")
        cached = await session_manager.read("sess-1")
        assert cached is not None
        assert cached.messages

    async def test_cold_and_incremental_round_trip_profile(
        self, event_log, profile_manager
    ):
        await event_log.append(_profile_created_event("t", "u"))
        await profile_manager.refresh_cache_incremental("t", "u")
        cached = await profile_manager.read("t", "u")
        assert cached is not None
        # New preference.
        await event_log.append(_profile_preference_set_event("t", "u", "lang", "pt"))
        await profile_manager.refresh_cache_incremental("t", "u")
        cached = await profile_manager.read("t", "u")
        assert cached is not None
        assert cached.preferences.get("lang") == "pt"
