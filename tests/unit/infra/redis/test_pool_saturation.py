# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Pool saturation is not a transport failure (ADR-084 §2.4, §3.4).

redis-py raises ``MaxConnectionsError`` the moment the pool is
exhausted — it does not queue. The class inherits from redis-py's
``ConnectionError``, so before ADR-084 the framework classified it
as a transport failure and the consume loop tore the pool down.
That killed every sibling consumer's socket, so they all raised
``MaxConnectionsError`` too: a momentary burst became a reconnect
storm.

These tests pin the two halves of the fix:

  - the classifier distinguishes saturation from transport failure;
  - the consume loop back-pressures instead of reconnecting.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.exceptions as redis_exceptions

from kntgraph.infra.redis._transport import (
    is_saturation_error,
    is_transport_error,
)
from kntgraph.tools.manager import WorkerManager


class TestSaturationClassification:
    """``MaxConnectionsError`` must not be a transport error."""

    def test_max_connections_is_saturation(self) -> None:
        err = redis_exceptions.MaxConnectionsError("Too many connections")
        assert is_saturation_error(err) is True

    def test_max_connections_is_not_a_transport_error(self) -> None:
        err = redis_exceptions.MaxConnectionsError("Too many connections")
        assert is_transport_error(err) is False, (
            "a saturated pool is not a broken transport — reconnecting "
            "closes the pool and takes every sibling consumer down "
            "(ADR-084 §1.5)"
        )

    def test_other_connection_errors_remain_transport_errors(self) -> None:
        """The narrowing must not swallow genuine transport failures."""
        for exc in (
            redis_exceptions.ConnectionError("Connection reset"),
            redis_exceptions.TimeoutError("timed out"),
            redis_exceptions.BusyLoadingError("LOADING"),
            ConnectionError("builtin"),
            TimeoutError("builtin"),
            OSError("EPIPE"),
            RuntimeError("Transport is closed"),
        ):
            assert is_transport_error(exc) is True, exc
            assert is_saturation_error(exc) is False, exc


class TestConsumeLoopBackoff:
    """The consume loop must back-pressure on saturation."""

    pytestmark = pytest.mark.asyncio

    @pytest.fixture
    def redis_mock(self):
        redis = MagicMock()
        redis.xgroup_create = AsyncMock()
        redis.xreadgroup = AsyncMock(return_value=[])
        redis.xack = AsyncMock(return_value=1)
        redis.xpending_range = AsyncMock(return_value=[])
        redis.xautoclaim = AsyncMock(return_value=("0-0", [], "0-0"))
        redis.aclose = AsyncMock()
        return redis

    @pytest.fixture
    def event_log_mock(self):
        log = MagicMock()
        log.append = AsyncMock(
            return_value=MagicMock(unwrap=MagicMock(return_value="0-0"))
        )
        return log

    async def test_saturation_does_not_reconnect(self, redis_mock, event_log_mock):
        """A saturated pool must NOT trigger the reconnect path.

        The reconnect closes the pool's sockets; doing that on
        saturation kills the sibling consumers.
        """
        from kntgraph.tools.worker import tool_worker

        @tool_worker(name="sat_tool")
        class _SatTool:
            async def invoke(self, *, idempotency_key: str) -> dict:
                return {}

        redis_mock.xreadgroup = AsyncMock(
            side_effect=[
                redis_exceptions.MaxConnectionsError("Too many connections"),
                asyncio.CancelledError(),
            ]
        )

        manager = WorkerManager(redis_mock, event_log_mock, reaper_interval=0.01)
        manager.register(_SatTool, acl=None)

        await manager.start()
        try:
            await asyncio.sleep(0.2)
        finally:
            await manager.stop()

        redis_mock.connection_pool.disconnect.assert_not_called()
        redis_mock.aclose.assert_not_called()

    async def test_saturation_backoff_doubles(self, redis_mock, event_log_mock):
        """Consecutive saturations back off further, up to a ceiling.

        Deterministic: the back-off is driven directly rather than
        through the consume loop's timing, so the assertion does not
        depend on how many saturation events the loop managed to
        process within the sleep window.
        """
        manager = WorkerManager(redis_mock, event_log_mock, reaper_interval=0.01)

        assert manager._saturation_backoff_seconds == 0.25
        await manager._back_off_on_saturation("tool")
        assert manager._saturation_backoff_seconds == 0.50
        await manager._back_off_on_saturation("tool")
        assert manager._saturation_backoff_seconds == 1.00

        # The ceiling holds. The ceiling is lowered for the test so
        # the assertion does not pay the production sleep.
        manager._saturation_backoff_ceiling_seconds = 1.0
        await manager._back_off_on_saturation("tool")
        assert manager._saturation_backoff_seconds == 1.0

    async def test_successful_read_resets_the_backoff(self, redis_mock, event_log_mock):
        """A successful read restores the initial back-off."""
        from kntgraph.tools.worker import tool_worker

        @tool_worker(name="sat_tool3")
        class _SatTool3:
            async def invoke(self, *, idempotency_key: str) -> dict:
                return {}

        manager = WorkerManager(redis_mock, event_log_mock, reaper_interval=0.01)
        manager.register(_SatTool3, acl=None)

        # Grow the back-off, then let one read succeed.
        await manager._back_off_on_saturation("tool")
        assert manager._saturation_backoff_seconds > 0.25

        await manager.start()
        try:
            await asyncio.sleep(0.1)
        finally:
            await manager.stop()

        assert manager._saturation_backoff_seconds == 0.25
