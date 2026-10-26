# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
The router stays blind to execution time (ADR-085 §3.2).

A tool's latency budget belongs to the tool. The
``WorkerManager`` holds that declaration and the reaper — which
runs inside the manager — applies it. The router's only job is
to write the request to the queue, byte-for-byte as the event
was emitted.

These tests pin:

1. The routed payload is the event's own JSON — the router adds
   nothing.
2. The request's ``timestamp`` survives, so the reaper can
   derive the deadline from it.
3. The tool's own parameters are untouched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.core.event import CorrelationContext, Event
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


def _routed(redis_mock) -> dict:
    """The decoded routed payload."""
    payload = redis_mock.xadd.await_args.args[1]["payload"]
    return json.loads(payload)


class TestRouterAddsNothing:
    async def test_the_payload_is_the_event_json(self, redis_mock) -> None:
        """The router writes the request, nothing more.

        The deadline is derived by the reaper from the request's
        own timestamp, so the router has no deadline to carry.
        """
        router = ToolRouter(redis_mock)
        event = _request_event()

        await router.route_batch([event])

        payload = redis_mock.xadd.await_args.args[1]["payload"]
        assert json.loads(payload) == json.loads(event.to_json())

    async def test_the_timestamp_survives(self, redis_mock) -> None:
        """The reaper needs the request's age — the timestamp must
        reach the queue."""
        router = ToolRouter(redis_mock)
        event = _request_event(age_seconds=42.0)

        await router.route_batch([event])

        routed = _routed(redis_mock)
        assert routed["timestamp"] == event.timestamp.isoformat()

    async def test_the_tool_still_receives_only_its_params(self, redis_mock) -> None:
        """The router must not leak anything into the tool's
        kwargs.

        ``coerce_tool_params`` reads ``data["params"]``, so a
        key at the ``data`` level is invisible to the tool — this
        test pins that the tool's params are untouched.
        """
        router = ToolRouter(redis_mock)

        await router.route_batch([_request_event()])

        params = _routed(redis_mock)["data"]["params"]
        assert params == {}
