# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Reaper timeout consistency (ADR-085).

The ``WorkerManager`` reaper reclaims a stuck message with
``XAUTOCLAIM min_idle_time=reaper_idle_time``. The problem
ADR-085 fixes: ``min_idle_time`` counts from the **last
delivery**, and a message being processed right now is also in
the PEL without an XACK. A tool slower than ``reaper_idle_time``
is therefore reclaimed **while it still runs** — the same
request executes twice.

These tests pin the three contracts:

  1. **Per-message idle threshold.** The message's own
     ``ttl_remaining`` (injected by the dispatcher when the
     request was routed) decides when it may be reclaimed.
     ``reaper_idle_time`` is the fallback for messages that
     carry no TTL (pre-ADR-085 payloads, or a tool request
     emitted by a system and routed before the dispatcher could
     compute its expiry).
  2. **Non-idempotent tools are never re-executed.** A tool
     declared ``idempotent=False`` must not be re-run by the
     reaper; the reaper emits a failure instead. ADR-075 §3.5
     promised this for the sweeper; the reaper never had it.
  3. **The reaper does not reclaim a message that is currently
     executing.** A message whose tool is in flight stays with
     the worker that owns it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.worker import tool_worker


@tool_worker(name="idem", max_concurrency=1, retries=3, timeout_seconds=900)
class _IdempotentTool:
    """A tool whose side effects are safe to retry.

    It declares a 900 s budget — longer than the global
    ``reaper_idle_time`` — so the reaper must use the tool's own
    threshold.
    """

    async def invoke(self, *, idempotency_key: str) -> dict:
        return {}


@tool_worker(name="non_idem", max_concurrency=1, retries=3, idempotent=False)
class _NonIdempotentTool:
    """A tool with non-idempotent side effects (e.g. a PIX)."""

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
    return redis


@pytest.fixture
def event_log_mock():
    log = MagicMock()
    log.append = AsyncMock(return_value=MagicMock(unwrap=MagicMock(return_value="0-0")))
    return log


def _make_manager(redis_mock, event_log_mock, **kwargs) -> WorkerManager:
    manager = WorkerManager(redis_mock, event_log_mock, **kwargs)
    manager.register(_IdempotentTool, acl=None)
    return manager


class TestToolWorkerIdempotentFlag:
    """``@tool_worker(idempotent=...)`` must reach the class.

    The flag lives on the ``Tool`` Protocol (an instance
    attribute), but the reaper only has the **class**. The
    decorator is what bridges the two.
    """

    def test_decorator_injects_idempotent_default(self) -> None:
        assert _IdempotentTool.__tool_worker_idempotent__ is True

    def test_decorator_injects_idempotent_false(self) -> None:
        assert _NonIdempotentTool.__tool_worker_idempotent__ is False

    def test_default_is_idempotent(self) -> None:
        """ADR-075 §3.3: the default is ``True`` (the framework
        may freely re-invoke). Operators opt out per tool."""

        @tool_worker(name="plain")
        class _Plain:
            async def invoke(self, *, idempotency_key: str) -> dict:
                return {}

        assert _Plain.__tool_worker_idempotent__ is True


class TestReaperIdleThreshold:
    pytestmark = pytest.mark.asyncio

    """The reaper must use the **tool's** declared timeout when
    the tool declares one, and fall back to ``reaper_idle_time``
    when it does not.

    The deadline is derived from the request's own timestamp and
    the tool's declaration — the reaper runs inside the manager
    that registered the tool, so it already holds both.
    """

    async def test_uses_global_fallback_without_ttl(
        self, redis_mock, event_log_mock
    ) -> None:
        """A tool that declares no timeout uses the global
        ``reaper_idle_time`` — the pre-ADR-085 behaviour."""
        manager = _make_manager(redis_mock, event_log_mock, reaper_idle_time=300.0)
        _prime_pending(redis_mock, "1-0", idle_ms=400_000, payload={})

        await manager._reaper_loop_once("idem")  # type: ignore[attr-defined]

        assert redis_mock.xautoclaim.await_args.kwargs["min_idle_time"] == 300_000

    async def test_uses_the_tool_declared_timeout(
        self, redis_mock, event_log_mock
    ) -> None:
        """The tool says 900 s; the global says 300 s.

        The message has been idle for 1000 s — past its own
        threshold — so it is reclaimable, and the threshold used
        is the tool's 900 s.
        """
        manager = _make_manager(redis_mock, event_log_mock, reaper_idle_time=300.0)
        _prime_pending(
            redis_mock,
            "1-0",
            idle_ms=1_000_000,
            payload=_payload(age_seconds=0.0),
        )

        await manager._reaper_loop_once("idem")  # type: ignore[attr-defined]

        threshold = redis_mock.xautoclaim.await_args.kwargs["min_idle_time"]
        assert 899_000 < threshold <= 900_000

    async def test_expired_deadline_clamps_to_zero(
        self, redis_mock, event_log_mock
    ) -> None:
        """A request that already blew the tool's budget must not
        produce a negative threshold — it is reclaimable
        immediately."""
        manager = _make_manager(redis_mock, event_log_mock, reaper_idle_time=300.0)
        _prime_pending(
            redis_mock,
            "1-0",
            idle_ms=1_000,
            payload=_payload(age_seconds=1000.0),
        )

        await manager._reaper_loop_once("idem")  # type: ignore[attr-defined]

        assert redis_mock.xautoclaim.await_args.kwargs["min_idle_time"] == 0

    async def test_message_below_its_threshold_is_not_claimed(
        self, redis_mock, event_log_mock
    ) -> None:
        """A message whose idle time is below the tool's timeout is
        left alone — this is the slow-tool case.

        The global ``reaper_idle_time`` is 300 s but the tool
        says 900 s: after 400 s of idleness the message is NOT
        ready, because the tool may still be running.
        """
        manager = _make_manager(redis_mock, event_log_mock, reaper_idle_time=300.0)
        _prime_pending(
            redis_mock,
            "1-0",
            idle_ms=400_000,
            payload=_payload(age_seconds=400.0),
        )

        await manager._reaper_loop_once("idem")  # type: ignore[attr-defined]

        redis_mock.xautoclaim.assert_not_awaited()


class TestReaperSkipsNonIdempotent:
    pytestmark = pytest.mark.asyncio

    """A non-idempotent tool must never be re-executed by the
    reaper."""

    async def test_non_idempotent_tool_is_not_re_run(
        self, redis_mock, event_log_mock
    ) -> None:
        manager = WorkerManager(redis_mock, event_log_mock)
        manager.register(_NonIdempotentTool, acl=None)
        _prime_pending(
            redis_mock,
            "1-0",
            idle_ms=1_000_000,
            payload=_payload(age_seconds=0.0),
        )

        dispatched = []
        original = manager._dispatch_to_tool

        async def _spy(**kwargs):
            dispatched.append(kwargs)
            return await original(**kwargs)

        manager._dispatch_to_tool = _spy  # type: ignore[method-assign]

        await manager._reaper_loop_once("non_idem")  # type: ignore[attr-defined]
        await asyncio.sleep(0)

        assert dispatched == [], (
            "the reaper re-executed a non-idempotent tool — the "
            "side effect (a PIX, a file write) happens twice "
            "(ADR-085 B')"
        )

    async def test_non_idempotent_tool_emits_failure(
        self, redis_mock, event_log_mock
    ) -> None:
        """Instead of re-running, the reaper emits a failure and
        XACKs the message (the request is resolved, not retried)."""
        manager = WorkerManager(redis_mock, event_log_mock)
        manager.register(_NonIdempotentTool, acl=None)
        _prime_pending(
            redis_mock,
            "1-0",
            idle_ms=1_000_000,
            payload=_payload(age_seconds=0.0),
        )

        await manager._reaper_loop_once("non_idem")  # type: ignore[attr-defined]
        await asyncio.sleep(0.05)

        # The message is resolved, not retried.
        redis_mock.xack.assert_awaited()
        assert event_log_mock.append.await_count >= 1


class TestReaperSkipsInFlight:
    pytestmark = pytest.mark.asyncio

    """A message whose tool is currently executing must not be
    reclaimed."""

    async def test_in_flight_message_is_not_reclaimed(
        self, redis_mock, event_log_mock
    ) -> None:
        manager = _make_manager(redis_mock, event_log_mock)
        _prime_pending(
            redis_mock,
            "1-0",
            idle_ms=1_000_000,
            payload=_payload(age_seconds=0.0),
        )

        # The consume loop is executing this message right now.
        manager._mark_in_flight("idem", "1-0")

        dispatched = []
        original = manager._dispatch_to_tool

        async def _spy(**kwargs):
            dispatched.append(kwargs)
            return await original(**kwargs)

        manager._dispatch_to_tool = _spy  # type: ignore[method-assign]

        await manager._reaper_loop_once("idem")  # type: ignore[attr-defined]
        await asyncio.sleep(0)

        assert dispatched == [], (
            "the reaper re-executed a message that was already "
            "in flight — the same request runs twice (ADR-085 A')"
        )


def _payload(*, age_seconds: float = 0.0) -> bytes:
    """A ``tool.<name>.requested`` payload aged by
    ``age_seconds``.

    The age is carried by the event's own timestamp — the reaper
    derives the deadline from it.
    """
    from datetime import UTC, datetime, timedelta

    from kntgraph.core.event import CorrelationContext, Event

    now = datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
    event = Event.create(
        event_type="tool.idem.requested",
        agent_id="agent-1",
        event_class="domain",
        data={"tool": "idem", "params": {}},
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
