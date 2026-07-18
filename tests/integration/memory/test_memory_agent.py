# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the discriminated MemoryAgent parser used by
Consolidator and Projector.

The previous implementation returned a heterogeneous tuple
(``tuple[str, str] | tuple[str, str, str] | None``), which
forced `cast` at every call site and gave the type checker
no help. The new implementation returns a frozen dataclass
``MemoryAgent`` with a clear shape; the call sites use
``match`` to narrow.

These tests pin the contract of the new parser and the
type-narrowed dispatch.
"""

from __future__ import annotations
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._memory import RedisProfileStorage
from kntgraph.infra.redis._memory import RedisSessionStorage

from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

# Tests in TestMemoryAgent and TestParseAgentId are sync;
# tests in TestRefreshAll and TestProjectAll are async and
# carry the @pytest.mark.asyncio decorator individually.


class TestMemoryAgent:
    def test_session_factory(self):
        from kntgraph.memory.consolidation import MemoryAgent

        a = MemoryAgent.session("s-1")
        assert a.kind == "session"
        assert a.id1 == "s-1"
        assert a.id2 == ""

    def test_profile_factory(self):
        from kntgraph.memory.consolidation import MemoryAgent

        a = MemoryAgent.profile("tenant-a", "user-b")
        assert a.kind == "profile"
        assert a.id1 == "tenant-a"
        assert a.id2 == "user-b"

    def test_is_frozen(self):
        from kntgraph.memory.consolidation import MemoryAgent

        a = MemoryAgent.session("s-1")
        with pytest.raises(FrozenInstanceError):
            a.id1 = "other"  # type: ignore[misc]

    def test_id1_id2_are_str(self):
        from kntgraph.memory.consolidation import MemoryAgent

        a = MemoryAgent.session("s")
        assert isinstance(a.id1, str)
        assert isinstance(a.id2, str)

    def test_kind_is_string(self):
        """The kind field is a plain string for easy logging."""
        from kntgraph.memory.consolidation import MemoryAgent

        assert MemoryAgent.session("s").kind == "session"
        assert MemoryAgent.profile("t", "u").kind == "profile"

    def test_repr_is_informative(self):
        from kntgraph.memory.consolidation import MemoryAgent

        a = MemoryAgent.profile("t", "u")
        text = repr(a)
        # The repr should mention the kind and the ids, so
        # log lines are useful in production.
        assert "profile" in text
        assert "t" in text
        assert "u" in text


class TestParseAgentId:
    """
    The parser is the only place that knows the agent_id
    string convention. Tests pin every form it accepts and
    the cases where it must return None.
    """

    def test_session_id(self):
        from kntgraph.memory.consolidation import parse_agent_id

        result = parse_agent_id("session:s-1")
        assert result is not None
        assert result.kind == "session"
        assert result.id1 == "s-1"

    def test_session_id_with_colons(self):
        """
        The id itself may contain colons (e.g. tenant-prefixed
        sessions). The parser must split on the FIRST colon
        only.
        """
        from kntgraph.memory.consolidation import parse_agent_id

        result = parse_agent_id("session:tenant-x:user-y:sess-1")
        assert result is not None
        assert result.kind == "session"
        assert result.id1 == "tenant-x:user-y:sess-1"

    def test_profile_id(self):
        from kntgraph.memory.consolidation import parse_agent_id

        result = parse_agent_id("profile:tenant-x:user-y")
        assert result is not None
        assert result.kind == "profile"
        assert result.id1 == "tenant-x"
        assert result.id2 == "user-y"

    def test_profile_id_with_colon_in_user_id(self):
        """
        Profile id1=tenant, id2=user. If the user id contains
        a colon (unlikely but possible), we still split only
        on the first colon after the prefix.
        """
        from kntgraph.memory.consolidation import parse_agent_id

        result = parse_agent_id("profile:tenant-x:user-y:extra")
        assert result is not None
        assert result.kind == "profile"
        assert result.id1 == "tenant-x"
        assert result.id2 == "user-y:extra"

    def test_unknown_agent_returns_none(self):
        from kntgraph.memory.consolidation import parse_agent_id

        assert parse_agent_id("fechamento:tenant-x:2026-01") is None
        assert parse_agent_id("NF-001") is None
        assert parse_agent_id("agent.spawned") is None
        assert parse_agent_id("") is None

    def test_profile_with_missing_user_returns_none(self):
        """
        A profile id MUST have a (tenant, user) pair. If the
        body has no colon, it is malformed and the parser
        skips it (matching the original behaviour).
        """
        from kntgraph.memory.consolidation import parse_agent_id

        assert parse_agent_id("profile:") is None
        assert parse_agent_id("profile:tenant-x") is None


class TestRefreshAll:
    """
    `Consolidator.refresh_all` walks the World and publishes
    a CacheRefreshRequest for every memory agent.
    """

    @pytest.mark.asyncio
    async def test_publishes_one_request_per_session(self, clean_redis):
        from kntgraph.memory.cache_warmer import (
            CacheRefreshBus,
            CacheRefreshRequest,
        )
        from kntgraph.memory.consolidation import Consolidator
        from kntgraph.memory.session import SessionManager
        from kntgraph.stream.event_log import EventLog
        from kntgraph.core.world import World
        from kntgraph.core.event import CorrelationContext, Event

        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        bus = CacheRefreshBus()
        cons = Consolidator(log, bus, sm)

        # Build a World with one session agent.
        from kntgraph.core.event import OperationalEventType

        ev = Event.operation_from(
            agent_id="session:s-1",
            type=OperationalEventType.SPAWNED,
            correlation=CorrelationContext.new(correlation_id=uuid4()),
        )
        world = World.fold([ev])
        out = cons.refresh_all(world)
        assert out == []
        assert len(bus) == 1
        req = bus.drain()[0]
        assert isinstance(req, CacheRefreshRequest)
        assert req.kind == "session"
        assert req.id1 == "s-1"

    @pytest.mark.asyncio
    async def test_publishes_one_request_per_profile(self, clean_redis):
        from kntgraph.memory.cache_warmer import (
            CacheRefreshBus,
        )
        from kntgraph.memory.consolidation import Consolidator
        from kntgraph.memory.profile import ProfileManager
        from kntgraph.stream.event_log import EventLog
        from kntgraph.core.world import World
        from kntgraph.core.event import CorrelationContext, Event
        from kntgraph.core.event import OperationalEventType

        log = EventLog(RedisEventLogAdapter(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        bus = CacheRefreshBus()
        cons = Consolidator(log, bus, None, pm)

        ev = Event.operation_from(
            agent_id="profile:tenant-x:user-y",
            type=OperationalEventType.SPAWNED,
            correlation=CorrelationContext.new(correlation_id=uuid4()),
        )
        world = World.fold([ev])
        cons.refresh_all(world)
        req = bus.drain()[0]
        assert req.kind == "profile"
        assert req.id1 == "tenant-x"
        assert req.id2 == "user-y"

    @pytest.mark.asyncio
    async def test_skips_non_memory_agents(self, clean_redis):
        from kntgraph.memory.cache_warmer import CacheRefreshBus
        from kntgraph.memory.consolidation import Consolidator
        from kntgraph.stream.event_log import EventLog
        from kntgraph.core.world import World
        from kntgraph.core.event import CorrelationContext, Event
        from kntgraph.core.event import OperationalEventType

        log = EventLog(RedisEventLogAdapter(clean_redis))
        bus = CacheRefreshBus()
        cons = Consolidator(log, bus)
        # Build a World with an agent that is NOT memory.
        ev = Event.operation_from(
            agent_id="NF-2026-001",
            type=OperationalEventType.SPAWNED,
            correlation=CorrelationContext.new(correlation_id=uuid4()),
        )
        world = World.fold([ev])
        cons.refresh_all(world)
        assert len(bus) == 0


class TestProjectAll:
    """
    `Projector.project_all` is the I/O-side counterpart of
    the Consolidator: it walks the EventLog directly and
    writes the cache.
    """

    @pytest.mark.asyncio
    async def test_returns_counts_per_kind(self, clean_redis):
        from kntgraph.memory.consolidation import Projector
        from kntgraph.memory.profile import ProfileManager
        from kntgraph.memory.session import SessionManager
        from kntgraph.stream.event_log import EventLog

        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        proj = Projector(log, sm, pm)

        await sm.start("s-1", user_id="u", tenant_id="t")
        await sm.append_message("s-1", "user", "olá")
        await pm.create("t-1", "u-1", tier="vip")
        await pm.set_preference("t-1", "u-1", "lang", "pt-BR")

        await clean_redis.delete("knt:session:s-1")
        await clean_redis.delete("knt:profile:t-1:u-1")

        result = await proj.project_all()
        # ``continuity`` always appears in the counts (ADR-014),
        # with value 0 when no continuity events exist.
        assert result == {"sessions": 1, "profiles": 1, "continuity": 0}

    @pytest.mark.asyncio
    async def test_skips_unknown_agent_ids(self, clean_redis):
        """
        The EventLog may contain agent streams that are not
        memory (e.g. NFs, fechamento). The projector must
        skip them silently.
        """
        from kntgraph.core.event import CorrelationContext, Event
        from kntgraph.memory.consolidation import Projector
        from kntgraph.memory.profile import ProfileManager
        from kntgraph.memory.session import SessionManager
        from kntgraph.stream.event_log import EventLog

        log = EventLog(RedisEventLogAdapter(clean_redis))
        sm = SessionManager(log, RedisSessionStorage(clean_redis))
        pm = ProfileManager(log, RedisProfileStorage(clean_redis))
        proj = Projector(log, sm, pm)

        # Append a non-memory event directly to a foreign agent.
        await log.append(
            Event.create(
                event_type="document.received",
                agent_id="NF-001",
                event_class="domain",
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )
        result = await proj.project_all()
        # ``continuity`` always appears in the counts (ADR-014).
        assert result == {"sessions": 0, "profiles": 0, "continuity": 0}
