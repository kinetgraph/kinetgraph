# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``WorkerManager.register()`` is where a tool is declared — and
therefore where its timeout belongs (ADR-085).

The reaper needs to know, **per message**, when a pending
request may be reclaimed. The threshold it uses is the
request's own TTL. That TTL is a property of the *tool* (how
long the tool may legitimately take), and ``register()`` is
where the operator declares the tool.

The alternative — keeping the TTL in the dispatcher's
``ToolCallTTL.per_tool_ttls`` — splits the declaration in two:
the tool says one thing, the dispatcher says another, and a
divergence re-opens the duplicate-execution bug ADR-085 closes.

These tests pin the publication contract:

1. ``register()`` publishes the tool's declared timeout.
2. The publication is keyed by tool name (the router's key).
3. A tool that declares no timeout publishes nothing — the
   router keeps its own default.
4. The published value is what the router writes into the
   request's ``data``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.tools._ttl import TTL_REMAINING_KEY
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter
from kntgraph.tools.worker import tool_worker

pytestmark = pytest.mark.asyncio


@tool_worker(name="slow", max_concurrency=1, timeout_seconds=3600)
class _SlowTool:
    async def invoke(self, *, idempotency_key: str) -> dict:
        return {}


@tool_worker(name="quick", max_concurrency=1)
class _QuickTool:
    async def invoke(self, *, idempotency_key: str) -> dict:
        return {}


@pytest.fixture
def redis_mock():
    redis = MagicMock()
    redis.xgroup_create = AsyncMock()
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xack = AsyncMock(return_value=1)
    redis.xpending_range = AsyncMock(return_value=[])
    redis.xrange = AsyncMock(return_value=[])
    redis.xautoclaim = AsyncMock(return_value=("0-0", [], "0-0"))
    redis.xadd = AsyncMock(return_value=b"1-0")
    redis.set = AsyncMock(return_value=True)
    return redis


@pytest.fixture
def event_log_mock():
    log = MagicMock()
    log.append = AsyncMock(return_value=MagicMock(unwrap=MagicMock(return_value="0-0")))
    return log


def _request_event(*, tool: str, age_seconds: float = 0.0) -> Event:
    now = datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
    return Event.create(
        event_type=f"tool.{tool}.requested",
        agent_id="agent-1",
        event_class="domain",
        data={"tool": tool, "params": {}},
        correlation=CorrelationContext.new(),
        timestamp=now,
    )


class TestRegisterPublishesTimeout:
    async def test_register_publishes_the_declared_timeout(
        self, redis_mock, event_log_mock
    ) -> None:
        manager = WorkerManager(redis_mock, event_log_mock)
        manager.register(_SlowTool, acl=None)

        assert manager.published_timeout_for("slow") == 3600.0

    async def test_register_publishes_nothing_without_a_declaration(
        self, redis_mock, event_log_mock
    ) -> None:
        """A tool that declares no timeout publishes nothing —
        the router keeps its own default."""
        manager = WorkerManager(redis_mock, event_log_mock)
        manager.register(_QuickTool, acl=None)

        assert manager.published_timeout_for("quick") is None

    async def test_unknown_tool_publishes_nothing(
        self, redis_mock, event_log_mock
    ) -> None:
        manager = WorkerManager(redis_mock, event_log_mock)
        assert manager.published_timeout_for("nope") is None

    async def test_the_publication_reaches_the_router(
        self, redis_mock, event_log_mock
    ) -> None:
        """The end-to-end contract: what ``register()``
        publishes is what the router writes into the request.

        This is the test that would have caught the split
        declaration — the tool says 3600 s, the router writes
        3600 s, and there is no second place to keep in sync.
        """
        manager = WorkerManager(redis_mock, event_log_mock)
        manager.register(_SlowTool, acl=None)

        router = ToolRouter(redis_mock)
        router.publish_timeouts(manager.published_timeouts())

        await router.route_batch([_request_event(tool="slow", age_seconds=600)])

        payload = redis_mock.xadd.await_args.args[1]["payload"]
        remaining = json.loads(payload)["data"][TTL_REMAINING_KEY]
        # 3600 s total, 600 s already elapsed -> ~3000 s left.
        assert 2900 < remaining <= 3000
