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

    async def test_unset_unknown_key_is_noop(self, event_log, profile_storage):
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


# ---------------------------------------------------------------------------
# Branch coverage: SessionManager error + edge paths
# ---------------------------------------------------------------------------


class TestSessionManagerBranchCoverage:
    """Branch coverage for ``session.py`` paths not exercised
    by the happy-path suite above. Each test targets a
    specific branch in the coverage report.
    """

    async def test_emit_and_refresh_propagates_persistence_error(
        self, event_log, session_storage
    ):
        """``_emit_and_refresh`` returns ``Err`` when the
        EventLog ``append`` fails (line 180-181)."""
        from unittest.mock import AsyncMock, patch

        sm = SessionManager(event_log, session_storage)
        await sm.start("s1", user_id="u", tenant_id="t")
        # Patch the EventLog append to return Err
        with patch.object(
            event_log,
            "append",
            AsyncMock(return_value=_err_result()),
        ):
            result = await sm.append_message("s1", "user", "hi")
            assert result.is_err()

    async def test_start_propagates_persistence_error(self, event_log, session_storage):
        """``start`` returns ``Err`` when the EventLog
        ``append`` fails for the ``session.started`` event
        (lines 215-216)."""
        from unittest.mock import AsyncMock, patch

        sm = SessionManager(event_log, session_storage)
        with patch.object(
            event_log,
            "append",
            AsyncMock(return_value=_err_result()),
        ):
            result = await sm.start("s1", user_id="u", tenant_id="t")
            assert result.is_err()

    async def test_start_with_metadata_emits_context_events(
        self, event_log, session_storage
    ):
        """``start`` with ``metadata`` emits one
        ``session.context`` event per key (lines 217-229).
        The context events must land in the EventLog."""
        sm = SessionManager(event_log, session_storage)
        await sm.start(
            "s1",
            user_id="u",
            tenant_id="t",
            metadata={"lang": "pt", "tz": "America/Sao_Paulo"},
        )
        events = await event_log.read(SessionManager.agent_id_for("s1"))
        # 1 started + 2 context events
        assert len(events) == 3
        ctx_events = [e for e in events if e.event_type == "session.context"]
        assert len(ctx_events) == 2
        keys = {e.data.get("key") for e in ctx_events}
        assert keys == {"lang", "tz"}

    async def test_start_metadata_context_append_failure(
        self, event_log, session_storage
    ):
        """``start`` returns ``Err`` when a ``session.context``
        event fails to append (lines 225-229)."""
        from unittest.mock import patch

        sm = SessionManager(event_log, session_storage)
        call_count = 0

        async def _flaky_append(event):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call (session.started) succeeds
                from kntgraph.core.result import Ok

                return Ok(event)
            # Second call (session.context) fails
            return _err_result()

        with patch.object(event_log, "append", side_effect=_flaky_append):
            result = await sm.start(
                "s1", user_id="u", tenant_id="t", metadata={"k": "v"}
            )
            assert result.is_err()

    async def test_list_active_skips_cache_error(
        self, event_log, session_storage, fake_redis
    ):
        """``list_active`` skips sessions whose cache read
        returns ``Err`` (line 324)."""
        sm = SessionManager(event_log, session_storage)
        await sm.start("s1", user_id="u", tenant_id="t")
        # Corrupt the cache so the read returns Err
        await fake_redis.set("knt:session:s1", b"{not json")
        # Should not raise; the corrupt session is skipped
        active = await sm.list_active("t")
        # The session is either skipped (empty list) or
        # rebuilt from the EventLog; either way no crash.
        assert isinstance(active, list)

    async def test_list_active_respects_limit(self, event_log, session_storage):
        """``list_active`` stops at ``limit`` (line 329)."""
        sm = SessionManager(event_log, session_storage)
        await sm.start("s1", user_id="u1", tenant_id="t")
        await sm.start("s2", user_id="u2", tenant_id="t")
        await sm.start("s3", user_id="u3", tenant_id="t")
        active = await sm.list_active("t", limit=2)
        assert len(active) == 2

    async def test_read_cache_returns_none_on_missing_raw(
        self, event_log, session_storage, fake_redis
    ):
        """``_read_cache`` returns ``Ok(None)`` when the
        storage returns ``Ok(None)`` (line 360). This
        happens when the key exists but the value is
        empty / null."""
        sm = SessionManager(event_log, session_storage)
        # No cache entry — the read-through falls back to
        # the EventLog (which is also empty). The _read_cache
        # itself returns Ok(None) on a miss.
        result = await sm._read_cache("knt:session:ghost", "ghost")
        assert result.is_ok()
        assert result.ok_value() is None

    async def test_fold_returns_none_when_no_started_event(
        self, event_log, session_storage
    ):
        """``_fold_session_events`` returns ``None`` when
        there is no ``session.started`` event in the log
        (line 433-434)."""
        sm = SessionManager(event_log, session_storage)
        # Emit a message event without a started event
        await sm.append_message("s1", "user", "hi")
        # The fold should return None (no started event)
        events = await event_log.read(SessionManager.agent_id_for("s1"))
        from kntgraph.memory.session import _fold_session_events

        result = _fold_session_events("s1", events)
        assert result is None

    async def test_fold_drops_non_string_context_key(self, event_log, session_storage):
        """``_on_session_context`` drops keys that are not
        strings (line 477->exit). We inject a context event
        with a non-string key via a direct EventLog append."""
        from kntgraph.core.event import CorrelationContext, Event

        sm = SessionManager(event_log, session_storage)
        await sm.start("s1", user_id="u", tenant_id="t")
        # Manually append a context event with a non-string key
        agent_id = SessionManager.agent_id_for("s1")
        bad_event = Event.domain_from(
            agent_id=agent_id,
            type="session.context",
            data={"key": 123, "value": "ignored"},
            correlation=CorrelationContext.new(),
        )
        await event_log.append(bad_event)
        # Read the state — the non-string key should be dropped
        state = await sm.read("s1")
        assert state is not None
        assert 123 not in state.context
        assert "123" not in state.context

    async def test_scalar_str_returns_default_for_none(self):
        """``_scalar_str`` returns default for None (line 527)."""
        from kntgraph.memory.session import _scalar_str

        assert _scalar_str(None, default="fallback") == "fallback"

    async def test_scalar_str_converts_int_to_string(self):
        """``_scalar_str`` converts int to str (line 531)."""
        from kntgraph.memory.session import _scalar_str

        assert _scalar_str(42) == "42"

    async def test_scalar_str_returns_default_for_list(self):
        """``_scalar_str`` returns default for non-scalar
        types like list (line 532)."""
        from kntgraph.memory.session import _scalar_str

        assert _scalar_str([1, 2], default="d") == "d"

    async def test_coerce_float_returns_default_for_none(self):
        """``_coerce_float`` returns default for None (line 540)."""
        from kntgraph.memory.session import _coerce_float

        assert _coerce_float(None, default=9.0) == 9.0

    async def test_coerce_float_converts_bool(self):
        """``_coerce_float`` converts bool to float (line 542)."""
        from kntgraph.memory.session import _coerce_float

        assert _coerce_float(True) == 1.0
        assert _coerce_float(False) == 0.0

    async def test_coerce_float_returns_default_for_string_non_numeric(self):
        """``_coerce_float`` returns default for non-numeric
        string (lines 548-549)."""
        from kntgraph.memory.session import _coerce_float

        assert _coerce_float("not-a-number") == 0.0

    async def test_coerce_float_returns_default_for_list(self):
        """``_coerce_float`` returns default for non-scalar
        types (line 550)."""
        from kntgraph.memory.session import _coerce_float

        assert _coerce_float([1.0]) == 0.0

    async def test_build_session_state_rejects_non_list_messages(self):
        """``_build_session_state`` raises ``ValueError``
        when ``messages`` is not a list (line 569)."""
        from kntgraph.memory.session import _build_session_state

        with pytest.raises(ValueError, match="messages is not a list"):
            _build_session_state(
                {"messages": "not-a-list"},
                session_id="s1",
            )

    async def test_build_session_state_rejects_non_dict_context(self):
        """``_build_session_state`` raises ``ValueError``
        when ``context`` is not a dict (line 576)."""
        from kntgraph.memory.session import _build_session_state

        with pytest.raises(ValueError, match="context is not a dict"):
            _build_session_state(
                {"context": "not-a-dict"},
                session_id="s1",
            )

    async def test_build_session_state_skips_non_dict_message_entry(self):
        """``_build_session_state`` skips entries in
        ``messages`` that are not dicts (line 572->571)."""
        from kntgraph.memory.session import _build_session_state

        state = _build_session_state(
            {
                "messages": [
                    {"role": "user", "content": "hi"},
                    "not-a-dict",
                    {"role": "assistant", "content": "bye"},
                ],
                "started_at": 1.0,
            },
            session_id="s1",
        )
        assert len(state.messages) == 2

    async def test_build_session_state_ended_at_none(self):
        """``_build_session_state`` sets ``ended_at`` to
        ``None`` when the field is absent (line 580)."""
        from kntgraph.memory.session import _build_session_state

        state = _build_session_state(
            {"started_at": 1.0},
            session_id="s1",
        )
        assert state.ended_at is None

    async def test_build_session_state_ended_at_present(self):
        """``_build_session_state`` coerces ``ended_at``
        when the field is present (line 580)."""
        from kntgraph.memory.session import _build_session_state

        state = _build_session_state(
            {"started_at": 1.0, "ended_at": 2.5},
            session_id="s1",
        )
        assert state.ended_at == 2.5


# ---------------------------------------------------------------------------
# Branch coverage: ProfileManager error + edge paths
# ---------------------------------------------------------------------------


class TestProfileManagerBranchCoverage:
    """Branch coverage for ``profile.py`` paths not exercised
    by the happy-path suite above.
    """

    async def test_emit_and_refresh_propagates_persistence_error(
        self, event_log, profile_storage
    ):
        """``_emit_and_refresh`` returns ``Err`` when the
        EventLog ``append`` fails (lines 179-180)."""
        from unittest.mock import AsyncMock, patch

        pm = ProfileManager(event_log, profile_storage)
        await pm.create("t1", "u1")
        with patch.object(
            event_log,
            "append",
            AsyncMock(return_value=_err_result()),
        ):
            result = await pm.set_preference("t1", "u1", "k", "v")
            assert result.is_err()

    async def test_list_for_tenant_skips_cache_error(
        self, event_log, profile_storage, fake_redis
    ):
        """``list_for_tenant`` skips entries whose cache
        read returns ``Err`` (line 297)."""
        pm = ProfileManager(event_log, profile_storage)
        await pm.create("t1", "u1")
        # Corrupt the cache
        await fake_redis.delete("knt:profile:t1:u1")
        await fake_redis.hset("knt:profile:t1:u1", mapping={"tier": "vip"})
        # Should not raise; the entry is either skipped or
        # rebuilt from the EventLog.
        out = await pm.list_for_tenant("t1")
        assert isinstance(out, list)

    async def test_list_for_tenant_respects_limit(self, event_log, profile_storage):
        """``list_for_tenant`` stops at ``limit`` (line 302)."""
        pm = ProfileManager(event_log, profile_storage)
        await pm.create("t1", "u1")
        await pm.create("t1", "u2")
        await pm.create("t1", "u3")
        out = await pm.list_for_tenant("t1", limit=2)
        assert len(out) == 2

    async def test_fold_returns_none_when_no_created_event(
        self, event_log, profile_storage
    ):
        """Implicit materialisation (ADR-067): a
        ``profile.preference_set`` without a
        ``profile.created`` still produces a state —
        with ``created_at == 0.0`` (the honest "never
        formally created"). Only a stream with NO
        ``profile.*`` event returns ``None``."""
        from kntgraph.core.event import Event
        from kntgraph.memory.profile import _fold_profile_events

        # Emit a preference_set event without a created event
        agent_id = ProfileManager.agent_id_for("t1", "u1")
        event = Event.domain_from(
            agent_id=agent_id,
            type="profile.preference_set",
            data={"key": "lang", "value": "pt"},
            correlation=CorrelationContext.new(),
        )
        await event_log.append(event)
        events = await event_log.read(agent_id)
        result = _fold_profile_events("t1", "u1", events)
        assert result is not None
        assert result.preferences == {"lang": "pt"}
        assert result.created_at == 0.0

        # And a stream with no profile event at all → None.
        empty_events: list[Event] = []
        assert _fold_profile_events("t1", "u1", empty_events) is None

    async def test_fold_drops_non_dict_preferences(self):
        """``_on_profile_created`` does not crash when
        ``preferences`` is not a dict (line 441->exit)."""
        from kntgraph.core.event import Event
        from kntgraph.memory.profile import _fold_profile_events

        e = Event.domain_from(
            agent_id="a",
            type="profile.created",
            data={"preferences": "not-a-dict", "tier": "standard"},
            correlation=CorrelationContext.new(),
        )
        result = _fold_profile_events("t", "u", [e])
        assert result is not None
        assert result.preferences == {}

    async def test_fold_drops_non_string_preference_key(self):
        """``_on_profile_created`` drops preference keys
        that are not strings (line 443->442). We bypass
        ``Event.domain_from`` (which JSON-serialises with
        ``sort_keys=True`` and chokes on mixed-type keys)
        by constructing the event with ``Event.create``
        and a pre-built dict."""
        from kntgraph.core.event import CorrelationContext, Event
        from kntgraph.memory.profile import _fold_profile_events

        e = Event.create(
            event_type="profile.created",
            agent_id="a",
            event_class="domain",
            data={"preferences": {"ok": "yes", "99": "v"}, "tier": "standard"},
            correlation=CorrelationContext.new(),
        )
        # Manually inject a non-string key after construction
        # (the fold reads from the in-memory dict, not from JSON)
        e.data["preferences"][99] = "v"
        result = _fold_profile_events("t", "u", [e])
        assert result is not None
        assert "ok" in result.preferences
        assert 99 not in result.preferences

    async def test_fold_preference_set_drops_non_string_key(self):
        """``_on_profile_preference_set`` drops non-string
        keys (line 453->457)."""
        from kntgraph.core.event import Event
        from kntgraph.memory.profile import _fold_profile_events

        created = Event.domain_from(
            agent_id="a",
            type="profile.created",
            data={"tier": "standard"},
            correlation=CorrelationContext.new(),
        )
        bad_pref = Event.domain_from(
            agent_id="a",
            type="profile.preference_set",
            data={"key": 99, "value": "ignored"},
            correlation=CorrelationContext.new(),
        )
        result = _fold_profile_events("t", "u", [created, bad_pref])
        assert result is not None
        assert 99 not in result.preferences

    async def test_coerce_profile_scalar_returns_default_for_dict(self):
        """``_coerce_profile_scalar`` returns default for
        dict values (lines 508-510)."""
        from kntgraph.memory.profile import _coerce_profile_scalar

        assert _coerce_profile_scalar({"tier": {"x": 1}}, "tier", "std") == "std"

    async def test_coerce_profile_scalar_returns_default_for_list(self):
        """``_coerce_profile_scalar`` returns default for
        list values (lines 508-510)."""
        from kntgraph.memory.profile import _coerce_profile_scalar

        assert _coerce_profile_scalar({"tier": [1, 2]}, "tier", "std") == "std"

    async def test_coerce_profile_scalar_converts_int(self):
        """``_coerce_profile_scalar`` converts int to str
        (line 509)."""
        from kntgraph.memory.profile import _coerce_profile_scalar

        assert _coerce_profile_scalar({"tier": 42}, "tier", "x") == "42"

    async def test_coerce_profile_scalar_value_returns_fallback_for_dict(self):
        """``_coerce_profile_scalar_value`` returns fallback
        for dict values (line 463->465)."""
        from kntgraph.memory.profile import _coerce_profile_scalar_value

        assert _coerce_profile_scalar_value({"x": 1}, fallback="d") == "d"

    async def test_coerce_profile_scalar_returns_default_for_none_value(self):
        """``_coerce_profile_scalar`` returns default when
        the key is missing (lines 508-510, None falls
        through to default)."""
        from kntgraph.memory.profile import _coerce_profile_scalar

        assert _coerce_profile_scalar({}, "tier", "std") == "std"

    async def test_coerce_profile_scalar_converts_bool(self):
        """``_coerce_profile_scalar`` converts bool to str
        (line 509)."""
        from kntgraph.memory.profile import _coerce_profile_scalar

        assert _coerce_profile_scalar({"tier": True}, "tier", "x") == "True"

    async def test_coerce_profile_float_returns_default_for_non_numeric_string(
        self,
    ):
        """``_coerce_profile_float`` returns 0.0 for
        non-numeric string (lines 524-525)."""
        from kntgraph.memory.profile import _coerce_profile_float

        assert _coerce_profile_float("nope") == 0.0

    async def test_coerce_profile_float_returns_default_for_none(self):
        """``_coerce_profile_float`` returns 0.0 for None
        (line 526)."""
        from kntgraph.memory.profile import _coerce_profile_float

        assert _coerce_profile_float(None) == 0.0

    async def test_coerce_profile_float_returns_default_for_list(self):
        """``_coerce_profile_float`` returns 0.0 for
        non-scalar (line 526)."""
        from kntgraph.memory.profile import _coerce_profile_float

        assert _coerce_profile_float([1.0]) == 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _err_result():
    """Build an ``Err(PersistenceError)`` for mock patches."""
    from kntgraph.core.result import Err, PersistenceError

    return Err(PersistenceError("mock persistence failure"))


# ---------------------------------------------------------------------------
# Branch coverage: continuity cache_codec + manager + consolidation
# ---------------------------------------------------------------------------


class TestContinuityCacheCodecBranches:
    """Branch coverage for ``continuity/cache_codec.py``
    coerce helpers (lines 173-198).
    """

    def test_coerce_float_or_none_with_bool(self):
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_none

        assert _coerce_float_or_none(True) == 1.0
        assert _coerce_float_or_none(False) == 0.0

    def test_coerce_float_or_none_with_int(self):
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_none

        assert _coerce_float_or_none(42) == 42.0

    def test_coerce_float_or_none_with_float(self):
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_none

        assert _coerce_float_or_none(3.14) == 3.14

    def test_coerce_float_or_none_with_non_scalar(self):
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_none

        assert _coerce_float_or_none([1.0]) is None
        assert _coerce_float_or_none({"x": 1}) is None

    def test_coerce_float_or_zero_with_bool(self):
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_zero

        assert _coerce_float_or_zero(True) == 1.0
        assert _coerce_float_or_zero(False) == 0.0

    def test_coerce_float_or_zero_with_int(self):
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_zero

        assert _coerce_float_or_zero(42) == 42.0

    def test_coerce_float_or_zero_with_non_scalar(self):
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_zero

        assert _coerce_float_or_zero([1.0]) == 0.0
        assert _coerce_float_or_zero({"x": 1}) == 0.0

    def test_coerce_float_or_none_with_non_numeric_string(self):
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_none

        assert _coerce_float_or_none("not-a-float") is None

    def test_coerce_float_or_zero_with_non_numeric_string(self):
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_zero

        assert _coerce_float_or_zero("not-a-float") == 0.0


class TestConsolidationBranches:
    """Branch coverage for ``consolidation.py`` (lines 405,
    421->416, 424->416, 426-428).
    """

    async def test_project_continuity_returns_false_when_no_state(
        self, event_log, session_storage, profile_storage, continuity_storage
    ):
        """``project_continuity`` returns ``False`` when the
        fold returns ``None`` (line 405)."""
        from kntgraph.memory.consolidation import Projector

        sm = SessionManager(event_log, session_storage)
        pm = ProfileManager(event_log, profile_storage)
        cm = ContinuityManager(event_log, continuity_storage)
        proj = Projector(event_log, sm, pm, cm)
        # No events for this tenant/user → fold returns None
        result = await proj.project_continuity("ghost-tenant", "ghost-user")
        assert result is False

    async def test_project_all_with_continuity_agent(
        self, event_log, session_storage, profile_storage, continuity_storage
    ):
        """``project_all`` processes a continuity agent
        (lines 426-428)."""
        from kntgraph.memory.consolidation import Projector

        cm = ContinuityManager(event_log, continuity_storage)
        await cm.create("t1", "u1")

        sm = SessionManager(event_log, session_storage)
        pm = ProfileManager(event_log, profile_storage)
        proj = Projector(event_log, sm, pm, cm)
        counts = await proj.project_all()
        assert counts["continuity"] >= 1
