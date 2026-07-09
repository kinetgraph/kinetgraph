# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for memory/continuity.py (ADR-014).
Real Redis.

Covers:
  - ContinuityManager CRUD (create, record_*, clear, read).
  - Hash cache format + sliding TTL.
  - PII gate (record_entity_seen rejects raw values).
  - Cache rebuilt from log on miss.
  - Idempotency of create() and clear().
  - CacheWarmer dispatch for the ``continuity`` kind.
  - Projector.project_continuity + project_all counts.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from kntgraph.memory.cache_warmer import (
    CacheRefreshBus,
    CacheRefreshRequest,
    CacheWarmer,
)
from kntgraph.memory.consolidation import Consolidator, Projector
from kntgraph.memory.continuity import (
    CONTINUITY_KEY_PREFIX,
    ContinuityManager,
    ContinuityState,
)
from kntgraph.memory.profile import ProfileManager
from kntgraph.memory.session import SessionManager
from kntgraph.stream.event_log import EventLog

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# ContinuityManager
# ---------------------------------------------------------------------------


class TestContinuityManager:
    async def test_create(self, clean_redis):
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis, ttl_seconds=60)
        result = await cm.create("t-1", "u-1")
        assert result.is_ok()

        state = await cm.read("t-1", "u-1")
        assert state is not None
        assert state.tenant_id == "t-1"
        assert state.user_id == "u-1"
        assert state.last_tools == {}
        assert state.last_entities == {}
        assert state.last_categories == {}
        assert state.cleared_at is None

    async def test_cache_uses_hash(self, clean_redis):
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis, ttl_seconds=60)
        await cm.create("t-1", "u-1")

        assert await clean_redis.exists(f"{CONTINUITY_KEY_PREFIX}t-1:u-1")
        key_type = await clean_redis.type(f"{CONTINUITY_KEY_PREFIX}t-1:u-1")
        assert key_type == b"hash"

    async def test_ttl_is_set(self, clean_redis):
        """
        Sliding TTL: after a write, the key has a positive TTL.
        The TTL is renewed on every subsequent write.
        """
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis, ttl_seconds=120)
        await cm.create("t-1", "u-1")

        ttl = await clean_redis.ttl(f"{CONTINUITY_KEY_PREFIX}t-1:u-1")
        assert 0 < ttl <= 120

    async def test_record_tool_used(self, clean_redis):
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        await cm.create("t-1", "u-1")
        r = await cm.record_tool_used(
            "t-1",
            "u-1",
            tool="invoice.issue",
            params_fingerprint="sha256:abc",
            result_signature="sha256:def",
            latency_ms=312,
        )
        assert r.is_ok()

        state = await cm.read("t-1", "u-1")
        assert state is not None
        assert "invoice.issue" in state.last_tools

    async def test_record_entity_seen_stores_hash(self, clean_redis):
        """
        The PII gate: callers MUST pass a sha256:... value
        hash. Raw values are rejected before reaching the
        EventLog.
        """
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        await cm.create("t-1", "u-1")

        good = await cm.record_entity_seen(
            "t-1",
            "u-1",
            kind="cnpj",
            value_hash=ContinuityManager.hash_value("12.345.678/0001-90"),
            source="tool_result",
        )
        assert good.is_ok()

        bad = await cm.record_entity_seen(
            "t-1",
            "u-1",
            kind="cnpj",
            value_hash="12.345.678/0001-90",  # raw, not a hash
            source="tool_result",
        )
        assert bad.is_err()
        # Nothing should have been appended for the bad call.
        events = await log.read(ContinuityManager.agent_id_for("t-1", "u-1"))
        # 1 created + 1 good entity_seen = 2 events.
        assert len(events) == 2

    async def test_record_category_chosen(self, clean_redis):
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        await cm.create("t-1", "u-1")
        r = await cm.record_category_chosen("t-1", "u-1", "cfop", "6102")
        assert r.is_ok()

        state = await cm.read("t-1", "u-1")
        assert state is not None
        assert "cfop" in state.last_categories

    async def test_recency_suggest(self, clean_redis):
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        await cm.create("t-1", "u-1")
        await cm.record_category_chosen("t-1", "u-1", "cfop", "6102")
        await cm.record_category_chosen("t-1", "u-1", "cfop", "7102")

        last = await cm.recency_suggest("t-1", "u-1", "cfop")
        assert last is not None
        assert last.startswith("7102|")

    async def test_recency_suggest_unknown_returns_none(self, clean_redis):
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        await cm.create("t-1", "u-1")
        last = await cm.recency_suggest("t-1", "u-1", "cfop")
        assert last is None

    async def test_clear_resets_state(self, clean_redis):
        """
        LGPD right-to-erasure: after ``clear``, the cache is
        wiped (state has empty dicts and ``cleared_at`` set),
        and ``recency_suggest`` returns None.
        """
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        await cm.create("t-1", "u-1")
        await cm.record_category_chosen("t-1", "u-1", "cfop", "6102")
        r = await cm.clear("t-1", "u-1", reason="lgpd_erasure")
        assert r.is_ok()

        state = await cm.read("t-1", "u-1")
        assert state is not None
        assert state.is_cleared()
        assert state.last_categories == {}
        assert state.last_tools == {}

        last = await cm.recency_suggest("t-1", "u-1", "cfop")
        assert last is None

    async def test_post_clear_recording_starts_fresh(self, clean_redis):
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        await cm.create("t-1", "u-1")
        await cm.record_category_chosen("t-1", "u-1", "cfop", "5102")
        await cm.clear("t-1", "u-1")
        await cm.record_category_chosen("t-1", "u-1", "cfop", "6102")

        state = await cm.read("t-1", "u-1")
        assert state is not None
        assert state.is_cleared() is False
        assert state.last_categories["cfop"].startswith("6102|")

    async def test_cache_rebuilt_from_log(self, clean_redis):
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        await cm.create("t-1", "u-1")
        await cm.record_tool_used(
            "t-1",
            "u-1",
            tool="invoice.issue",
            params_fingerprint="sha256:abc",
            result_signature="sha256:def",
            latency_ms=312,
        )

        # Wipe the cache
        await clean_redis.delete(f"{CONTINUITY_KEY_PREFIX}t-1:u-1")

        # Read rebuilds
        state = await cm.read("t-1", "u-1")
        assert state is not None
        assert "invoice.issue" in state.last_tools

        # Cache is back
        assert await clean_redis.exists(f"{CONTINUITY_KEY_PREFIX}t-1:u-1")

    async def test_read_unknown_returns_none(self, clean_redis):
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        state = await cm.read("t-x", "u-x")
        assert state is None

    async def test_idempotent_create(self, clean_redis):
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        r1 = await cm.create("t", "u")
        r2 = await cm.create("t", "u")
        assert r1.unwrap().event_id == r2.unwrap().event_id

    async def test_idempotent_clear(self, clean_redis):
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        await cm.create("t", "u")
        r1 = await cm.clear("t", "u", reason="user_request")
        r2 = await cm.clear("t", "u", reason="user_request")
        assert r1.unwrap().event_id == r2.unwrap().event_id


# ---------------------------------------------------------------------------
# Consolidator + CacheWarmer dispatch for the continuity kind
# ---------------------------------------------------------------------------


class TestCacheWarmerContinuityDispatch:
    async def test_warmer_dispatches_continuity(self, clean_redis):
        """
        The CacheWarmer must accept a ``continuity_manager``
        and dispatch ``kind="continuity"`` requests to it.
        """
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        await cm.create("t-1", "u-1")
        await cm.record_tool_used(
            "t-1",
            "u-1",
            tool="invoice.issue",
            params_fingerprint="sha256:abc",
            result_signature="sha256:def",
            latency_ms=100,
        )
        # Wipe cache
        await clean_redis.delete(f"{CONTINUITY_KEY_PREFIX}t-1:u-1")

        bus = CacheRefreshBus()
        bus.publish(CacheRefreshRequest(kind="continuity", id1="t-1", id2="u-1"))

        # No session/profile needed for this branch.
        warmer = CacheWarmer(
            bus,
            session_manager=None,
            profile_manager=None,  # type: ignore[arg-type]
            continuity_manager=cm,
        )
        applied = await warmer.pump_once()
        assert applied == 1
        assert await clean_redis.exists(f"{CONTINUITY_KEY_PREFIX}t-1:u-1")

    async def test_warmer_logs_when_continuity_unconfigured(self, clean_redis):
        """
        If a ``kind="continuity"`` request arrives but no
        continuity manager was wired, the warmer logs and
        continues (does not abort the batch).
        """
        bus = CacheRefreshBus()
        bus.publish(CacheRefreshRequest(kind="continuity", id1="t", id2="u"))

        # Construct warmer without continuity_manager.
        # We still need session/profile for the type signature.
        log = EventLog(clean_redis)
        sm = SessionManager(log, clean_redis)
        pm = ProfileManager(log, clean_redis)
        warmer = CacheWarmer(bus, sm, pm)

        applied = await warmer.pump_once()
        # 1 request drained; not applied but the batch
        # survives.
        assert applied == 1


# ---------------------------------------------------------------------------
# Projector: project_continuity + project_all counts continuity
# ---------------------------------------------------------------------------


class TestProjectorContinuity:
    async def test_project_all_includes_continuity(self, clean_redis):
        log = EventLog(clean_redis)
        sm = SessionManager(log, clean_redis)
        pm = ProfileManager(log, clean_redis)
        cm = ContinuityManager(log, clean_redis)
        proj = Projector(log, sm, pm, cm)

        await sm.start("s-1", user_id="u", tenant_id="t")
        await pm.create("t-1", "u-1", preferences={"lang": "pt-BR"})
        await cm.create("t-1", "u-1")
        await cm.record_tool_used(
            "t-1",
            "u-1",
            tool="invoice.issue",
            params_fingerprint="sha256:abc",
            result_signature="sha256:def",
            latency_ms=100,
        )

        # Wipe all caches
        await clean_redis.delete("fmh:session:s-1")
        await clean_redis.delete("fmh:profile:t-1:u-1")
        await clean_redis.delete(f"{CONTINUITY_KEY_PREFIX}t-1:u-1")

        result = await proj.project_all()
        assert result == {
            "sessions": 1,
            "profiles": 1,
            "continuity": 1,
        }

    async def test_project_continuity_no_manager_returns_false(self, clean_redis):
        log = EventLog(clean_redis)
        sm = SessionManager(log, clean_redis)
        pm = ProfileManager(log, clean_redis)
        # No continuity_manager passed.
        proj = Projector(log, sm, pm)
        assert await proj.project_continuity("t", "u") is False


# ---------------------------------------------------------------------------
# Consolidator: publish continuity requests via parse_agent_id
# ---------------------------------------------------------------------------


class TestConsolidatorContinuity:
    async def test_consolidator_publishes_continuity_request(self, clean_redis):
        """
        When the World contains a ``continuity:`` agent,
        the Consolidator must publish a
        ``CacheRefreshRequest(kind="continuity", ...)``.
        """
        from kntgraph.core.event import (
            CorrelationContext,
            Event,
            OperationalEventType,
        )
        from kntgraph.core.world import World

        log = EventLog(clean_redis)
        bus = CacheRefreshBus()
        cons = Consolidator(log, bus)

        ev = Event.operation_from(
            agent_id="continuity:tenant-x:user-y",
            type=OperationalEventType.SPAWNED,
            correlation=CorrelationContext.new(correlation_id=uuid4()),
        )
        world = World.fold([ev])
        cons.refresh_all(world)
        reqs = bus.drain()
        assert len(reqs) == 1
        assert reqs[0].kind == "continuity"
        assert reqs[0].id1 == "tenant-x"
        assert reqs[0].id2 == "user-y"


# ---------------------------------------------------------------------------
# Public contract: write_cache / refresh_cache
# ---------------------------------------------------------------------------


class TestPublicCacheContract:
    async def test_continuity_write_cache_is_public(self, clean_redis):
        """
        ``ContinuityManager.write_cache`` is a public method
        that the Projector relies on (parallel contract to
        SessionManager and ProfileManager).
        """
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        state = ContinuityState(
            tenant_id="t",
            user_id="u",
            last_tools={"invoice.issue": "sha256:abc|100|1.0"},
            last_entities={},
            last_categories={"cfop": "6102|1.0"},
            created_at=1.0,
            updated_at=2.0,
        )
        await cm.write_cache("t", "u", state)
        cached = await cm.read("t", "u")
        assert cached is not None
        assert cached.last_categories == {"cfop": "6102|1.0"}

    async def test_continuity_refresh_cache_is_public(self, clean_redis):
        log = EventLog(clean_redis)
        cm = ContinuityManager(log, clean_redis)
        await cm.create("t", "u")
        await cm.record_category_chosen("t", "u", "cfop", "6102")
        await clean_redis.delete(f"{CONTINUITY_KEY_PREFIX}t:u")
        await cm.refresh_cache("t", "u")
        state = await cm.read("t", "u")
        assert state is not None
        assert "cfop" in state.last_categories


# ---------------------------------------------------------------------------
# Parsing the agent_id convention
# ---------------------------------------------------------------------------


class TestParseAgentIdContinuity:
    @pytest.mark.asyncio
    async def test_continuity_round_trip(self):
        from kntgraph.memory.consolidation import parse_agent_id

        aid = ContinuityManager.agent_id_for("tenant-x", "user-y")
        result = parse_agent_id(aid)
        assert result is not None
        assert result.kind == "continuity"
        assert result.id1 == "tenant-x"
        assert result.id2 == "user-y"
