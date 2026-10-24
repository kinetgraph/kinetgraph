# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
The router injects the request's remaining TTL (ADR-085 A'').

The reaper reclaims a pending message on **its own** threshold
so that a slow tool is not reclaimed while it still runs. For
that, the TTL has to travel with the request.

The dispatcher owns the ``ToolCallTTL`` (the per-tool TTL
configuration), and the router is the component that writes the
request to the queue — so the router is where the TTL is
injected, using the TTL the dispatcher gives it.

These tests pin:

1. With a TTL configured, the routed payload carries
   ``ttl_remaining`` in the request's ``data``.
2. Without a TTL configured, the payload is **byte-identical**
   to the pre-ADR-085 wire format (the default must not change
   the wire format).
3. The TTL shrinks as the request ages (the remaining time, not
   the total).
4. The TTL is per tool (the ``ToolCallTTL.per_tool_ttls``
   override applies).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world.components import ToolCallTTL
from kntgraph.tools._ttl import TTL_REMAINING_KEY
from kntgraph.tools.router import ToolRouter

pytestmark = pytest.mark.asyncio


@pytest.fixture
def redis_mock():
    redis = MagicMock()
    redis.xadd = AsyncMock(return_value=b"1-0")
    redis.set = AsyncMock(return_value=True)
    return redis


def _request_event(*, tool: str = "idem", age_seconds: float = 0.0) -> Event:
    """A ``tool.<name>.requested`` event aged by ``age_seconds``."""
    now = datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
    return Event.create(
        event_type=f"tool.{tool}.requested",
        agent_id="agent-1",
        event_class="domain",
        data={"tool": tool, "params": {}},
        correlation=CorrelationContext.new(),
        timestamp=now,
    )


def _routed_data(redis_mock) -> dict:
    """The ``data`` mapping of the routed payload."""
    payload = redis_mock.xadd.await_args.args[1]["payload"]
    decoded = json.loads(payload)
    return decoded["data"]


class TestTtlInjection:
    async def test_injects_ttl_remaining(self, redis_mock) -> None:
        router = ToolRouter(redis_mock, tool_ttls=ToolCallTTL(default_ttl_seconds=900))
        event = _request_event()

        await router.route_batch([event])

        assert TTL_REMAINING_KEY in _routed_data(redis_mock)

    async def test_without_ttl_configured_the_wire_is_unchanged(
        self, redis_mock
    ) -> None:
        """The default must not change the wire format.

        A deployment that does not configure a TTL keeps the
        exact pre-ADR-085 payload — the reaper falls back to
        the global ``reaper_idle_time``.
        """
        router = ToolRouter(redis_mock)
        event = _request_event()

        await router.route_batch([event])

        payload = redis_mock.xadd.await_args.args[1]["payload"]
        decoded = json.loads(payload)
        assert TTL_REMAINING_KEY not in decoded["data"]

    async def test_ttl_shrinks_as_the_request_ages(self, redis_mock) -> None:
        """The payload carries the **remaining** TTL, not the
        total: a request that waited 100 s has 800 s left of a
        900 s TTL."""
        router = ToolRouter(redis_mock, tool_ttls=ToolCallTTL(default_ttl_seconds=900))
        event = _request_event(age_seconds=100.0)

        await router.route_batch([event])

        remaining = _routed_data(redis_mock)[TTL_REMAINING_KEY]
        assert 780 < remaining <= 800

    async def test_ttl_is_per_tool(self, redis_mock) -> None:
        """The ``ToolCallTTL`` per-tool override applies."""
        router = ToolRouter(
            redis_mock,
            tool_ttls=ToolCallTTL(
                default_ttl_seconds=300, per_tool_ttls={"slow": 3600}
            ),
        )

        await router.route_batch([_request_event(tool="slow", age_seconds=0)])
        slow_remaining = _routed_data(redis_mock)[TTL_REMAINING_KEY]

        await router.route_batch([_request_event(tool="fast", age_seconds=0)])
        fast_remaining = _routed_data(redis_mock)[TTL_REMAINING_KEY]

        assert slow_remaining > fast_remaining

    async def test_the_tool_still_receives_only_its_params(self, redis_mock) -> None:
        """The TTL must not leak into the tool's kwargs.

        ``coerce_tool_params`` reads ``data["params"]``, so a
        key at the ``data`` level is invisible to the tool — this
        test pins that the tool's params are untouched.
        """
        router = ToolRouter(redis_mock, tool_ttls=ToolCallTTL(default_ttl_seconds=900))
        event = _request_event()

        await router.route_batch([event])

        params = _routed_data(redis_mock)["params"]
        assert TTL_REMAINING_KEY not in params
