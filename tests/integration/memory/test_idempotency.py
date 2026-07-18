# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for code review item #1.

Verifies:
  - ProfileManager.change_tier is idempotent on (tenant, user, to_tier)
    regardless of current state.
  - SessionManager.start is idempotent on session_id regardless of
    metadata variation.
  - Repeated identical start calls produce the same event_id.
  - Repeated identical change_tier calls produce the same event_id.
  - State fold is correct after the fix.

The fix ensures that the event payload (`data`) carries only
the *intent* of the operation, not derived state. Derived state
(`from_tier`, `metadata`) is computed by the fold, not stored
in the event.
"""

import pytest

from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._memory import RedisProfileStorage, RedisSessionStorage
from kntgraph.memory.profile import ProfileManager
from kntgraph.memory.session import SessionManager
from kntgraph.stream.event_log import EventLog

pytestmark = pytest.mark.asyncio


class TestChangeTierIdempotency:
    async def test_repeat_change_tier_same_target_produces_same_event_id(
        self, redis_client
    ):
        log = EventLog(RedisEventLogAdapter(redis_client))
        pm = ProfileManager(log, RedisProfileStorage(redis_client))
        await pm.create("t", "u", tier="standard")

        r1 = await pm.change_tier("t", "u", "vip")
        r2 = await pm.change_tier("t", "u", "vip")
        r3 = await pm.change_tier("t", "u", "vip")

        assert r1.is_ok() and r2.is_ok() and r3.is_ok()
        # All three should collapse to the SAME event_id.
        assert r1.unwrap().event_id == r2.unwrap().event_id == r3.unwrap().event_id

    async def test_repeat_change_tier_stores_one_event(self, redis_client):
        log = EventLog(RedisEventLogAdapter(redis_client))
        pm = ProfileManager(log, RedisProfileStorage(redis_client))
        await pm.create("t", "u", tier="standard")

        for _ in range(5):
            await pm.change_tier("t", "u", "vip")

        events = await log.read(ProfileManager.agent_id_for("t", "u"))
        tier_events = [e for e in events if e.event_type == "profile.tier_changed"]
        assert len(tier_events) == 1
        assert tier_events[0].data["to_tier"] == "vip"

    async def test_different_target_tier_stores_separate_events(self, redis_client):
        log = EventLog(RedisEventLogAdapter(redis_client))
        pm = ProfileManager(log, RedisProfileStorage(redis_client))
        await pm.create("t", "u", tier="standard")

        await pm.change_tier("t", "u", "vip")
        await pm.change_tier("t", "u", "basic")
        await pm.change_tier("t", "u", "vip")  # collapse with first

        events = await log.read(ProfileManager.agent_id_for("t", "u"))
        tier_events = [e for e in events if e.event_type == "profile.tier_changed"]
        # 2 distinct tier transitions: vip and basic; the second vip is idempotent
        assert len(tier_events) == 2
        assert {e.data["to_tier"] for e in tier_events} == {"vip", "basic"}

    async def test_final_state_after_repeat_changes(self, redis_client):
        log = EventLog(RedisEventLogAdapter(redis_client))
        pm = ProfileManager(log, RedisProfileStorage(redis_client))
        await pm.create("t", "u", tier="standard")
        for _ in range(10):
            await pm.change_tier("t", "u", "vip")

        state = await pm.read("t", "u")
        assert state is not None
        assert state.tier == "vip"


class TestSessionStartIdempotency:
    async def test_repeat_start_same_args_produces_same_event_id(self, redis_client):
        log = EventLog(RedisEventLogAdapter(redis_client))
        sm = SessionManager(log, RedisSessionStorage(redis_client))

        r1 = await sm.start("s-1", user_id="u", tenant_id="t")
        r2 = await sm.start("s-1", user_id="u", tenant_id="t")
        assert r1.unwrap().event_id == r2.unwrap().event_id

    async def test_repeat_start_with_varying_metadata_is_idempotent(self, redis_client):
        log = EventLog(RedisEventLogAdapter(redis_client))
        sm = SessionManager(log, RedisSessionStorage(redis_client))

        r1 = await sm.start("s-1", user_id="u", tenant_id="t", metadata={"a": 1})
        r2 = await sm.start("s-1", user_id="u", tenant_id="t", metadata={"b": 2})
        r3 = await sm.start("s-1", user_id="u", tenant_id="t", metadata={})

        # All three should be the SAME logical start: same event_id.
        assert r1.unwrap().event_id == r2.unwrap().event_id == r3.unwrap().event_id

        # Only one started event in the log.
        events = await log.read(SessionManager.agent_id_for("s-1"))
        started = [e for e in events if e.event_type == "session.started"]
        assert len(started) == 1
