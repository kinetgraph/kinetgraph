# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration test: the TTL sweeper's failure event collapses in the
EventLog (ADR-087).

**The defect (measured in production, 2026-10-25).** The sweeper's
failure event carried ``swept_at`` — the wall-clock time of *that*
sweep — in its ``data``. The ``event_id`` is derived from the
``data``, so every tick minted a **new** ``event_id`` for the **same**
expired request. The EventLog dedupes on ``event_id``, so it could
not collapse them.

Measured in the ``soldi/backoffice`` Redis: three
``tool.backend_event_publisher.failed`` events with the same
``request_event_id``, the same ``expired_at``, and three different
``event_id``s — repeated in two agents (six duplicates in total).
The EventLog's ``eventids`` index held two entries for nine events:
the duplicated failures never reached the index.

**The invariant this pins.** One expired request produces exactly one
``tool.<name>.failed`` event in the EventLog — regardless of how many
ticks observed it, how many dispatcher processes ran, or how many
times the process restarted.

The unit test (``tests/unit/runner/test_tool_call_ttl_sweeper.py``)
pins the ``event_id`` stability. This file pins the **observable
consequence** against a real Redis: the stream length.

The system under test is the sweeper + the real ``EventLog``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world.components import ToolCallTTL
from kntgraph.core.world.projection_tool_calls import project_tool_calls
from kntgraph.infra.redis._event_log._adapter import RedisEventLogAdapter
from kntgraph.runner.tool_call_ttl_sweeper import ToolCallTTLSweeperSystem
from kntgraph.stream.event_log import AGENT_STREAM_KEY, EventLog

pytestmark = pytest.mark.asyncio

AGENT_ID = "agent-sweeper-idem"
_BASE = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)


def _request_event(tool_name: str = "stuck_tool") -> Event:
    """A tool request whose TTL is 300 s."""
    return Event.create(
        event_type=f"tool.{tool_name}.requested",
        agent_id=AGENT_ID,
        event_class="domain",
        data={"tool": tool_name, "params": {}},
        correlation=CorrelationContext.new(),
        timestamp=_BASE,
    )


def _views(request: Event):
    """The post-projection views carrying the request in the
    ``tool_requests`` slot."""
    return project_tool_calls(
        [request],
        ttl=ToolCallTTL(default_ttl_seconds=300.0),
    )


async def _stream_len(redis, agent_id: str = AGENT_ID) -> int:
    key = AGENT_STREAM_KEY.format(agent_id=agent_id)
    return int(await redis.xlen(key))


class TestSweeperFailureCollapsesInTheEventLog:
    async def test_one_expired_request_yields_one_event(self, clean_redis):
        """The baseline: a single sweep emits one failure."""
        log = EventLog(RedisEventLogAdapter(clean_redis))
        request = _request_event()
        views = _views(request)

        sweeper = ToolCallTTLSweeperSystem(now=_BASE + timedelta(seconds=600))
        events = sweeper(views)
        assert len(events) == 1
        assert (await log.append(events[0])).is_ok()

        assert await _stream_len(clean_redis) == 1

    async def test_repeated_sweeps_collapse_to_one_event(self, clean_redis):
        """THE REGRESSION: several sweeps of the same expired
        request produce **one** EventLog entry.

        Before the fix, each sweep minted a distinct ``event_id``
        and the stream grew by one per sweep — the production
        symptom.
        """
        log = EventLog(RedisEventLogAdapter(clean_redis))
        request = _request_event()
        views = _views(request)

        for offset in (600, 700, 900, 1200, 1500):
            sweeper = ToolCallTTLSweeperSystem(now=_BASE + timedelta(seconds=offset))
            events = sweeper(views)
            assert len(events) == 1
            result = await log.append(events[0])
            assert result.is_ok()

        # ONE entry — the EventLog's dedup collapsed the repeats.
        assert await _stream_len(clean_redis) == 1

    async def test_a_fresh_sweeper_instance_also_collapses(self, clean_redis):
        """The process-restart case.

        The sweeper's in-memory ``_emitted_failures`` set is
        per-instance; a restart loses it. The EventLog's
        ``event_id`` dedup is the real guarantee — this test pins
        that it holds across instances.
        """
        log = EventLog(RedisEventLogAdapter(clean_redis))
        request = _request_event()
        views = _views(request)

        for offset in (600, 700, 900):
            # A NEW instance each time — the process restarted.
            sweeper = ToolCallTTLSweeperSystem(now=_BASE + timedelta(seconds=offset))
            events = sweeper(views)
            assert len(events) == 1
            await log.append(events[0])

        assert await _stream_len(clean_redis) == 1

    async def test_the_failure_still_carries_swept_at(self, clean_redis):
        """The fix must not drop the audit information.

        ``swept_at`` leaves the **identity**, not the **payload**.
        """
        log = EventLog(RedisEventLogAdapter(clean_redis))
        request = _request_event()
        views = _views(request)

        sweeper = ToolCallTTLSweeperSystem(now=_BASE + timedelta(seconds=600))
        events = sweeper(views)
        await log.append(events[0])

        key = AGENT_STREAM_KEY.format(agent_id=AGENT_ID)
        entries = await clean_redis.xrange(key)
        assert len(entries) == 1
        fields = entries[0][1]
        assert b"swept_at" in fields[b"data"]
        assert b"ttl_expired" in fields[b"data"]

    async def test_distinct_requests_yield_distinct_events(self, clean_redis):
        """The stability must not collapse two different expired
        requests into one entry."""
        log = EventLog(RedisEventLogAdapter(clean_redis))

        for tool_name in ("alpha_tool", "beta_tool"):
            request = _request_event(tool_name)
            views = _views(request)
            sweeper = ToolCallTTLSweeperSystem(now=_BASE + timedelta(seconds=600))
            events = sweeper(views)
            assert len(events) == 1
            await log.append(events[0])

        assert await _stream_len(clean_redis) == 2
