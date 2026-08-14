# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``memory/continuity/manager.py``.

Closes the manager coverage gap (DEBT §3, 70% reported
globally; 31% in isolation). The manager has 14 public
methods grouped into:

  - Identity / class-level: ``agent_id_prefix``,
    ``agent_id_for`` (inherited), ``cache_key``,
    ``hash_value``.
  - Cache: ``write_cache``, ``refresh_cache``.
  - Read API: ``read`` (inherited from base), ``list_for_tenant``,
    ``recency_suggest``.
  - Domain mutations: ``create``, ``record_tool_used``,
    ``record_entity_seen``, ``record_category_chosen``,
    ``clear``.

The tests use the real ``EventLog`` + ``RedisContinuityStorage``
against fakeredis (AGENTS.md §7.1). The
``correlation_middleware`` is scoped per-test via
``with correlation_middleware.scope():`` so the
manager's ``_build_and_emit`` (which reads
``correlation_middleware.current()``) sees a context.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio

from kntgraph.core.event import correlation_middleware
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._memory import RedisContinuityStorage
from kntgraph.memory.continuity.manager import (
    CONTINUITY_KEY_PREFIX,
    ContinuityManager,
)
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
async def manager(fake_redis, event_log):
    storage = RedisContinuityStorage(fake_redis)
    return ContinuityManager(event_log, storage, ttl_seconds=60)


@pytest_asyncio.fixture
async def correlation_ctx():
    correlation_middleware.clear()
    with correlation_middleware.scope() as ctx:
        yield ctx
    correlation_middleware.clear()


@pytest_asyncio.fixture(autouse=True)
async def _isolate_correlation():
    correlation_middleware.clear()
    yield
    correlation_middleware.clear()


# ---------------------------------------------------------------------------
# Identity / class-level helpers
# ---------------------------------------------------------------------------


class TestIdentity:
    async def test_agent_id_prefix(self):
        assert ContinuityManager.agent_id_prefix == "continuity:"

    async def test_agent_id_for(self):
        assert (
            ContinuityManager.agent_id_for("tenant-a", "user-1")
            == "continuity:tenant-a:user-1"
        )

    async def test_cache_key(self):
        assert (
            ContinuityManager.cache_key("tenant-a", "user-1")
            == f"{CONTINUITY_KEY_PREFIX}tenant-a:user-1"
        )

    async def test_hash_value_uses_sha256_prefix(self):
        h = ContinuityManager.hash_value("user@example.com")
        assert h.startswith("sha256:")
        assert len(h) > len("sha256:")

    async def test_hash_value_deterministic(self):
        a = ContinuityManager.hash_value("abc")
        b = ContinuityManager.hash_value("abc")
        assert a == b

    async def test_hash_value_distinct_for_different_inputs(self):
        a = ContinuityManager.hash_value("abc")
        b = ContinuityManager.hash_value("def")
        assert a != b


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestCreate:
    async def test_create_ok(self, manager, correlation_ctx, fake_redis):
        result = await manager.create("t-1", "u-1")
        assert result.is_ok()
        key = f"{CONTINUITY_KEY_PREFIX}t-1:u-1"
        assert await fake_redis.exists(key)

    async def test_create_writes_event_to_log(
        self, manager, correlation_ctx, event_log
    ):
        await manager.create("t-1", "u-1")
        events = await event_log.read(ContinuityManager.agent_id_for("t-1", "u-1"))
        assert len(events) == 1
        assert events[0].event_type == "continuity.created"


# ---------------------------------------------------------------------------
# record_tool_used
# ---------------------------------------------------------------------------


class TestRecordToolUsed:
    async def test_record_tool_used_ok(self, manager, correlation_ctx):
        result = await manager.record_tool_used(
            "t-1",
            "u-1",
            tool="ocr",
            params_fingerprint="sha256:abc",
            result_signature="sha256:def",
            latency_ms=120,
        )
        assert result.is_ok()

    async def test_record_tool_used_err_on_empty_tool_name(
        self, manager, correlation_ctx
    ):
        result = await manager.record_tool_used(
            "t-1",
            "u-1",
            tool="",
            params_fingerprint="sha256:abc",
            result_signature="sha256:def",
            latency_ms=120,
        )
        assert result.is_err()
        assert "Empty tool name" in str(result.err_value())

    async def test_record_tool_used_err_when_correlation_missing(self, manager):
        result = await manager.record_tool_used(
            "t-1",
            "u-1",
            tool="ocr",
            params_fingerprint="sha256:abc",
            result_signature="sha256:def",
            latency_ms=120,
        )
        assert result.is_err()


# ---------------------------------------------------------------------------
# record_entity_seen
# ---------------------------------------------------------------------------


class TestRecordEntitySeen:
    async def test_record_entity_seen_ok(self, manager, correlation_ctx):
        result = await manager.record_entity_seen(
            "t-1",
            "u-1",
            kind="document",
            value_hash="sha256:abcdef",
            source="ocr",
        )
        assert result.is_ok()

    async def test_record_entity_seen_err_on_empty_kind(self, manager, correlation_ctx):
        result = await manager.record_entity_seen(
            "t-1",
            "u-1",
            kind="",
            value_hash="sha256:abcdef",
            source="ocr",
        )
        assert result.is_err()
        assert "Empty entity kind" in str(result.err_value())

    async def test_record_entity_seen_err_on_empty_value_hash(
        self, manager, correlation_ctx
    ):
        result = await manager.record_entity_seen(
            "t-1",
            "u-1",
            kind="document",
            value_hash="",
            source="ocr",
        )
        assert result.is_err()
        assert "Empty entity value_hash" in str(result.err_value())

    async def test_record_entity_seen_rejects_raw_value_via_pii_gate(
        self, manager, correlation_ctx
    ):
        result = await manager.record_entity_seen(
            "t-1",
            "u-1",
            kind="document",
            value_hash="user@example.com",
            source="ocr",
        )
        assert result.is_err()
        assert "sha256:" in str(result.err_value())

    async def test_record_entity_seen_err_when_correlation_missing(self, manager):
        result = await manager.record_entity_seen(
            "t-1",
            "u-1",
            kind="document",
            value_hash="sha256:abcdef",
            source="ocr",
        )
        assert result.is_err()


# ---------------------------------------------------------------------------
# record_category_chosen
# ---------------------------------------------------------------------------


class TestRecordCategoryChosen:
    async def test_record_category_chosen_ok(self, manager, correlation_ctx):
        result = await manager.record_category_chosen(
            "t-1", "u-1", slot="cfop", value="6.102"
        )
        assert result.is_ok()

    async def test_record_category_chosen_err_on_empty_slot(
        self, manager, correlation_ctx
    ):
        result = await manager.record_category_chosen(
            "t-1", "u-1", slot="", value="6.102"
        )
        assert result.is_err()
        assert "Empty category slot" in str(result.err_value())

    async def test_record_category_chosen_err_on_empty_value(
        self, manager, correlation_ctx
    ):
        result = await manager.record_category_chosen(
            "t-1", "u-1", slot="cfop", value=""
        )
        assert result.is_err()
        assert "Empty category value" in str(result.err_value())

    async def test_record_category_chosen_err_when_correlation_missing(self, manager):
        result = await manager.record_category_chosen(
            "t-1", "u-1", slot="cfop", value="6.102"
        )
        assert result.is_err()


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


class TestClear:
    async def test_clear_ok(self, manager, correlation_ctx):
        result = await manager.clear("t-1", "u-1")
        assert result.is_ok()

    async def test_clear_sets_cleared_at(self, manager, correlation_ctx):
        await manager.create("t-1", "u-1")
        await manager.clear("t-1", "u-1")
        state = await manager.read("t-1", "u-1")
        assert state is not None
        assert state.is_cleared() is True

    async def test_clear_with_reason(self, manager, correlation_ctx, event_log):
        await manager.clear("t-1", "u-1", reason="lgpd_request")
        events = await event_log.read(ContinuityManager.agent_id_for("t-1", "u-1"))
        cleared = [e for e in events if e.event_type == "continuity.cleared"]
        assert len(cleared) == 1
        assert cleared[0].data == {"reason": "lgpd_request"}


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


class TestRead:
    async def test_read_none_for_unknown(self, manager):
        assert await manager.read("t-1", "u-1") is None

    async def test_read_returns_state_after_create(self, manager, correlation_ctx):
        await manager.create("t-1", "u-1")
        state = await manager.read("t-1", "u-1")
        assert state is not None
        assert state.tenant_id == "t-1"
        assert state.user_id == "u-1"
        assert state.is_cleared() is False


# ---------------------------------------------------------------------------
# list_for_tenant
# ---------------------------------------------------------------------------


class TestListForTenant:
    async def test_list_empty_for_unknown_tenant(self, manager):
        result = await manager.list_for_tenant("no-such-tenant")
        assert result == []

    async def test_list_returns_states_for_tenant(self, manager, correlation_ctx):
        await manager.create("t-1", "u-1")
        await manager.create("t-1", "u-2")
        await manager.create("t-2", "u-1")

        result = await manager.list_for_tenant("t-1")

        assert len(result) == 2
        user_ids = {s.user_id for s in result}
        assert user_ids == {"u-1", "u-2"}

    async def test_list_respects_limit(self, manager, correlation_ctx):
        for i in range(5):
            await manager.create("t-1", f"u-{i}")

        result = await manager.list_for_tenant("t-1", limit=2)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# recency_suggest
# ---------------------------------------------------------------------------


class TestRecencySuggest:
    async def test_suggest_none_for_unknown(self, manager):
        assert await manager.recency_suggest("t-1", "u-1", "cfop") is None

    async def test_suggest_returns_last_category(self, manager, correlation_ctx):
        await manager.create("t-1", "u-1")
        await manager.record_category_chosen("t-1", "u-1", slot="cfop", value="6.102")
        result = await manager.recency_suggest("t-1", "u-1", "cfop")
        assert result is not None
        assert result.startswith("6.102")

    async def test_suggest_none_after_clear(self, manager, correlation_ctx):
        await manager.create("t-1", "u-1")
        await manager.record_category_chosen("t-1", "u-1", slot="cfop", value="6.102")
        await manager.clear("t-1", "u-1")
        assert await manager.recency_suggest("t-1", "u-1", "cfop") is None

    async def test_suggest_none_for_unknown_slot(self, manager, correlation_ctx):
        await manager.create("t-1", "u-1")
        await manager.record_category_chosen("t-1", "u-1", slot="cfop", value="6.102")
        assert await manager.recency_suggest("t-1", "u-1", "cst") is None


# ---------------------------------------------------------------------------
# write_cache / refresh_cache
# ---------------------------------------------------------------------------


class TestWriteCacheAndRefresh:
    async def test_write_cache_round_trip(self, manager, correlation_ctx):
        from kntgraph.memory.continuity.state import ContinuityState

        state = ContinuityState(
            tenant_id="t-1",
            user_id="u-1",
            created_at=100.0,
            updated_at=200.0,
        )
        await manager.write_cache("t-1", "u-1", state)
        read_back = await manager.read("t-1", "u-1")
        assert read_back is not None
        assert read_back.created_at == 100.0

    async def test_refresh_cache_rebuilds_from_log(self, manager, correlation_ctx):
        await manager.create("t-1", "u-1")
        await manager.refresh_cache("t-1", "u-1")
        state = await manager.read("t-1", "u-1")
        assert state is not None
        assert state.tenant_id == "t-1"
        assert state.user_id == "u-1"


# ---------------------------------------------------------------------------
# Error paths (mocked)
# ---------------------------------------------------------------------------


class TestErrorPaths:
    async def test_emit_and_refresh_returns_err_when_log_fails(
        self, manager, correlation_ctx, monkeypatch
    ):
        from kntgraph.core.result import Err, PersistenceError

        async def failing_append(event):
            return Err(PersistenceError("redis down"))

        monkeypatch.setattr(manager._log, "append", failing_append)

        result = await manager.create("t-1", "u-1")
        assert result.is_err()
        assert "redis down" in str(result.err_value())

    async def test_read_cache_returns_err_on_storage_error(
        self, manager, correlation_ctx, fake_redis
    ):
        from kntgraph.core.result import Err, PersistenceError

        class FakeStorage:
            def __init__(self, real):
                self._real = real

            async def get_record(self, key):
                return Err(PersistenceError("boom"))

            def __getattr__(self, name):
                return getattr(self._real, name)

        manager._storage = FakeStorage(manager._storage)  # type: ignore[assignment]
        state = await manager.read("t-1", "u-1")
        assert state is None  # base.read falls back to fold, which returns None
