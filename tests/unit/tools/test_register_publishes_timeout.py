# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``WorkerManager.register()`` is where a tool is declared — and
therefore where its timeout belongs (ADR-085).

The reaper needs to know, **per message**, when a pending
request may be reclaimed. The threshold it uses is the request's
own deadline: the tool's declared budget, measured from the
request's timestamp.

That TTL is a property of the *tool* (how long the tool may
legitimately take), and ``register()`` is where the operator
declares the tool. So the manager holds the declaration, and the
reaper — which runs inside the manager — reads it directly.

The alternative — keeping the TTL in the dispatcher's
``ToolCallTTL.per_tool_ttls`` — splits the declaration in two:
the tool says one thing, the dispatcher says another, and a
divergence re-opens the duplicate-execution bug ADR-085 closes.

These tests pin the publication contract:

1. ``register()`` records the tool's declared timeout.
2. The record is keyed by tool name.
3. A tool that declares no timeout records nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.worker import tool_worker


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


class TestRegisterRecordsTheTimeout:
    def test_register_records_the_declared_timeout(
        self, redis_mock, event_log_mock
    ) -> None:
        manager = WorkerManager(redis_mock, event_log_mock)
        manager.register(_SlowTool, acl=None)

        assert manager.published_timeout_for("slow") == 3600.0

    def test_register_records_nothing_without_a_declaration(
        self, redis_mock, event_log_mock
    ) -> None:
        """A tool that declares no timeout records nothing — the
        reaper keeps its global default."""
        manager = WorkerManager(redis_mock, event_log_mock)
        manager.register(_QuickTool, acl=None)

        assert manager.published_timeout_for("quick") is None

    def test_unknown_tool_records_nothing(self, redis_mock, event_log_mock) -> None:
        manager = WorkerManager(redis_mock, event_log_mock)
        assert manager.published_timeout_for("nope") is None

    def test_the_record_is_keyed_by_tool_name(self, redis_mock, event_log_mock) -> None:
        """The whole declared set, keyed by tool name."""
        manager = WorkerManager(redis_mock, event_log_mock)
        manager.register(_SlowTool, acl=None)
        manager.register(_QuickTool, acl=None)

        assert manager.published_timeouts() == {"slow": 3600.0}
