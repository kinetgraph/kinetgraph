# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
The reaper derives the tool's deadline from the tool itself
(ADR-085 §3.2, issue #83).

The timeout is a property of the **tool**: it is how long the
tool may legitimately take. The ``WorkerManager`` is the
component that knows the tools, so it holds that declaration and
the reaper — which runs inside the manager — applies it.

The reaper already reads a declared property straight off the
registered tool: ``tool_is_idempotent(manager, tool_name)``
reads ``__tool_worker_idempotent__``. The timeout is the same
pattern, one declaration further.

So the deadline is derived:

    requested_at + timeout − now

``requested_at`` is the request event's own timestamp, which
travels in the queued payload. Nothing has to be published
anywhere: the router stays blind to execution time, and no
wiring exists to get wrong.

These tests pin:

1. A tool that declares a timeout is reclaimed on **its own**
   deadline, not the global ``reaper_idle_time``.
2. The derivation needs no wiring at all — the router is not
   involved.
3. A tool that declares no timeout keeps the global fallback.
4. A message whose deadline already passed is reclaimable at
   once.
5. A slow tool is not reclaimed while it still runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.result import Ok
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.worker import tool_worker


@tool_worker(name="slow", max_concurrency=1, timeout_seconds=900)
class _SlowTool:
    """A tool that legitimately takes 15 minutes — longer than
    the global ``reaper_idle_time`` fallback (300 s)."""

    async def invoke(self, *, idempotency_key: str) -> Ok:
        return Ok({})


@tool_worker(name="quick", max_concurrency=1)
class _QuickTool:
    """A tool that declares no timeout of its own."""

    async def invoke(self, *, idempotency_key: str) -> Ok:
        return Ok({})


@pytest.fixture
def redis_mock():
    redis = MagicMock()
    redis.xgroup_create = AsyncMock()
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xack = AsyncMock(return_value=1)
    redis.xpending_range = AsyncMock(return_value=[])
    redis.xrange = AsyncMock(return_value=[])
    redis.xautoclaim = AsyncMock(return_value=("0-0", [], "0-0"))
    return redis


@pytest.fixture
def event_log_mock():
    log = MagicMock()
    log.append = AsyncMock(return_value=MagicMock(unwrap=MagicMock(return_value="0-0")))
    return log


def _payload(*, tool: str, age_seconds: float = 0.0) -> bytes:
    """A ``tool.<name>.requested`` payload aged by
    ``age_seconds``.

    The age is carried by the event's own timestamp — the
    reaper derives the deadline from it.
    """
    now = datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
    event = Event.create(
        event_type=f"tool.{tool}.requested",
        agent_id="agent-1",
        event_class="domain",
        data={"tool": tool, "params": {}},
        correlation=CorrelationContext.new(),
        timestamp=now,
    )
    return event.to_json().encode()


def _prime_pending(
    redis_mock,
    msg_id: str,
    *,
    idle_ms: int,
    payload: bytes | dict,
) -> None:
    """Prime the mocks for one pending message.

    The reaper reads the PEL (``XPENDING``) for the idle time
    and the stream (``XRANGE``) for the payload, then claims.
    """
    redis_mock.xpending_range.return_value = [
        {
            "message_id": msg_id.encode(),
            "consumer": b"w1",
            "time_since_delivered": idle_ms,
            "times_delivered": 1,
        }
    ]
    fields = payload if isinstance(payload, dict) else {b"payload": payload}
    redis_mock.xrange.return_value = [(msg_id.encode(), fields)]
    redis_mock.xautoclaim.return_value = (
        "0-0",
        [(msg_id.encode(), fields)],
        [],
    )


class TestReaperDerivesTheDeadline:
    """The reaper derives the deadline from the tool's own
    declaration."""

    pytestmark = pytest.mark.asyncio

    async def test_a_declared_timeout_replaces_the_global_fallback(
        self, redis_mock, event_log_mock
    ) -> None:
        """The tool says 900 s; the global says 300 s.

        The message has been idle for 1000 s, so it is past
        its own threshold too — but the threshold used is the
        **tool's** 900 s, not the global 300 s.
        """
        manager = WorkerManager(redis_mock, event_log_mock, reaper_idle_time=300.0)
        manager.register(_SlowTool, acl=None)
        _prime_pending(
            redis_mock,
            "1-0",
            idle_ms=1_000_000,
            payload=_payload(tool="slow", age_seconds=100.0),
        )

        await manager._reaper_loop_once("slow")  # type: ignore[attr-defined]

        threshold = redis_mock.xautoclaim.await_args.kwargs["min_idle_time"]
        # The tool's 900 s budget, less the 100 s already spent.
        assert 799_000 < threshold <= 800_000

    async def test_a_slow_tool_is_not_reclaimed_while_it_runs(
        self, redis_mock, event_log_mock
    ) -> None:
        """The global fallback is 300 s; the tool says 900 s.

        After 400 s of idleness the message is **not** ready —
        the tool may still be running. This is the exact
        failure ADR-085 exists to prevent: the same request
        executing twice, in parallel.
        """
        manager = WorkerManager(redis_mock, event_log_mock, reaper_idle_time=300.0)
        manager.register(_SlowTool, acl=None)
        _prime_pending(
            redis_mock,
            "1-0",
            idle_ms=400_000,
            payload=_payload(tool="slow", age_seconds=400.0),
        )

        await manager._reaper_loop_once("slow")  # type: ignore[attr-defined]

        redis_mock.xautoclaim.assert_not_awaited()

    async def test_no_wiring_is_required(self, redis_mock, event_log_mock) -> None:
        """The derivation needs nothing published.

        The router is not involved at any point: the tool's
        declaration is on the tool, and the reaper runs inside
        the manager that registered it.
        """
        manager = WorkerManager(redis_mock, event_log_mock, reaper_idle_time=300.0)
        manager.register(_SlowTool, acl=None)
        _prime_pending(
            redis_mock,
            "1-0",
            idle_ms=1_000_000,
            payload=_payload(tool="slow", age_seconds=100.0),
        )

        await manager._reaper_loop_once("slow")  # type: ignore[attr-defined]

        threshold = redis_mock.xautoclaim.await_args.kwargs["min_idle_time"]
        # The tool's 900 s budget, less the 100 s already spent.
        assert 790_000 < threshold <= 800_000

    async def test_a_tool_without_a_declaration_keeps_the_global_fallback(
        self, redis_mock, event_log_mock
    ) -> None:
        """A tool that declares no timeout keeps the global
        ``reaper_idle_time``."""
        manager = WorkerManager(redis_mock, event_log_mock, reaper_idle_time=300.0)
        manager.register(_QuickTool, acl=None)
        _prime_pending(
            redis_mock,
            "1-0",
            idle_ms=400_000,
            payload=_payload(tool="quick", age_seconds=0.0),
        )

        await manager._reaper_loop_once("quick")  # type: ignore[attr-defined]

        assert redis_mock.xautoclaim.await_args.kwargs["min_idle_time"] == 300_000

    async def test_an_expired_deadline_clamps_to_zero(
        self, redis_mock, event_log_mock
    ) -> None:
        """A tool that already blew its budget is reclaimable at
        once — the threshold must not go negative."""
        manager = WorkerManager(redis_mock, event_log_mock, reaper_idle_time=300.0)
        manager.register(_SlowTool, acl=None)
        _prime_pending(
            redis_mock,
            "1-0",
            idle_ms=1_000,
            payload=_payload(tool="slow", age_seconds=1000.0),
        )

        await manager._reaper_loop_once("slow")  # type: ignore[attr-defined]

        assert redis_mock.xautoclaim.await_args.kwargs["min_idle_time"] == 0
