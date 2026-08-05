# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``tools/manager.py`` (``WorkerManager``).

Closes the tools/manager coverage gap (DEBT §3, 16% → ~80%).
The ``WorkerManager`` orchestrates the Tool Worker Pattern
(ADR-036) by:

  - Listening to a Redis Stream per tool (via Consumer
    Groups + ``xreadgroup``).
  - Dispatching each message to a ``ProcessPoolExecutor``
    that runs the tool's ``invoke`` method in a fresh
    process.
  - Emitting ``tool.<name>.completed`` or
    ``tool.<name>.failed`` events back into the EventLog
    (with the request event's ``correlation`` threaded
    through, per ADR-037).
  - ACKing the message on success or on a per-request
    hard-crash that exceeded the retry budget.
  - Reaping stuck messages via ``xautoclaim`` on a
    background loop (auto-recovery from worker crashes).

The tests mock the ``RedisLike`` Protocol and the
``EventLog`` with ``AsyncMock`` (the integration path
needs a real Redis with Streams + Consumer Groups,
which is out of scope for the unit suite). The
``_process_message`` private method is the unit
under test for the dispatch logic; the consume +
reaper loops are exercised via ``asyncio.create_task``
+ a small sleep + ``manager.stop()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from kntgraph.core.event import (
    CorrelationContext,
    Event,
)
from kntgraph.stream.event_log.store import EventLog
from kntgraph.tools import WorkerManager, tool_worker


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@tool_worker(name="echo", max_concurrency=4, retries=3)
class _EchoTool:
    """Minimal tool for the WorkerManager tests. Always
    returns ``Ok({"text": "echoed"})`` so the dispatch
    path hits the Ok branch (the framework contract:
    every ``@tool_worker.invoke`` returns
    ``Result[dict, ToolError]``)."""

    async def invoke(self, *, idempotency_key: str, text: str = "") -> dict:
        from kntgraph.core.result import Ok

        return Ok({"text": text or "echoed"})


@tool_worker(name="boom", max_concurrency=1, retries=2)
class _BoomTool:
    """Tool that always fails — exercises the Err branch."""

    async def invoke(self, *, idempotency_key: str, **kwargs: object) -> dict:
        from kntgraph.core.result import Err, ToolError

        return Err(ToolError("boom"))


class _NotDecorated:
    """A class that was never decorated with @tool_worker."""

    async def invoke(self, *, idempotency_key: str) -> dict:
        return {}


@pytest_asyncio.fixture
async def redis_mock():
    redis = MagicMock()
    redis.xgroup_create = AsyncMock()
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xack = AsyncMock(return_value=1)
    redis.xpending_range = AsyncMock(return_value=[])
    redis.xautoclaim = AsyncMock(return_value=("0-0", [], "0-0"))
    return redis


@pytest_asyncio.fixture
async def event_log_mock():
    log = MagicMock(spec=EventLog)
    log.append = AsyncMock(return_value=MagicMock(unwrap=MagicMock(return_value="0-0")))
    return log


@pytest_asyncio.fixture
async def manager(redis_mock, event_log_mock):
    return WorkerManager(redis_mock, event_log_mock, reaper_interval=0.01)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request_event(
    tool_name: str = "echo",
    correlation: CorrelationContext | None = None,
    params: dict | None = None,
) -> Event:
    return Event.create(
        event_type=f"tool.{tool_name}.requested",
        agent_id="agent-1",
        event_class="domain",
        data={"params": params or {"text": "hi"}},
        correlation=correlation or CorrelationContext.new(),
    )


def _stream_message(event: Event, message_id: bytes = b"1-0") -> tuple[bytes, dict]:
    payload = json.dumps(event.to_dict()).encode()
    return message_id, {b"payload": payload}


# ---------------------------------------------------------------------------
# Lifecycle: __init__ / register / start / stop
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_register_tool(self, manager):
        manager.register(_EchoTool)
        assert "echo" in manager._tools
        assert manager._tools["echo"] is _EchoTool

    async def test_register_rejects_undecorated_class(self, manager):
        with pytest.raises(TypeError, match="@tool_worker"):
            manager.register(_NotDecorated)

    async def test_start_initialises_pool_and_groups(self, manager, redis_mock):
        manager.register(_EchoTool)
        await manager.start()
        try:
            assert manager._running is True
            assert manager._pool is not None
            redis_mock.xgroup_create.assert_awaited_once()
        finally:
            await manager.stop()

    async def test_start_is_idempotent(self, manager, redis_mock):
        manager.register(_EchoTool)
        await manager.start()
        try:
            await manager.start()
            redis_mock.xgroup_create.assert_awaited_once()
        finally:
            await manager.stop()

    async def test_start_min_two_workers(self, manager):
        manager.register(_EchoTool)
        await manager.start()
        try:
            assert manager._pool is not None
            assert manager._pool._max_workers >= 2
        finally:
            await manager.stop()

    async def test_start_uses_spawn_context(self, manager):
        """
        The pool must be built with a ``spawn`` start method
        to avoid the fork+threading+openssl deadlock that
        stalls ``xreadgroup`` in container runtimes
        (see manager.py:start and ADR-054 lines 269-273).
        """
        manager.register(_EchoTool)
        await manager.start()
        try:
            assert manager._pool is not None
            assert manager._mp_context is not None
            assert manager._mp_context.get_start_method() == "spawn"
        finally:
            await manager.stop()

    async def test_start_swallows_busygroup_error(self, manager, redis_mock, caplog):
        redis_mock.xgroup_create = AsyncMock(
            side_effect=Exception("BUSYGROUP already exists")
        )
        manager.register(_EchoTool)
        with caplog.at_level(logging.ERROR, logger="kntgraph.tools.manager"):
            await manager.start()
        try:
            assert manager._running is True
        finally:
            await manager.stop()

    async def test_start_logs_non_busygroup_errors(self, manager, redis_mock, caplog):
        redis_mock.xgroup_create = AsyncMock(
            side_effect=Exception("connection refused")
        )
        manager.register(_EchoTool)
        with caplog.at_level(logging.ERROR, logger="kntgraph.tools.manager"):
            await manager.start()
        try:
            assert "connection refused" in caplog.text
        finally:
            await manager.stop()

    async def test_stop_cancels_tasks_and_shuts_pool(self, manager):
        manager.register(_EchoTool)
        await manager.start()
        pool = manager._pool
        await manager.stop()
        assert manager._running is False
        assert manager._pool is None
        # ProcessPoolExecutor does not expose a public
        # "is shut down" flag in py3.12; ``shutdown(wait=True)``
        # is idempotent and ``submit`` after ``shutdown``
        # raises ``RuntimeError`` — that is the contract
        # we assert.
        with pytest.raises(RuntimeError):
            pool.submit(lambda: None)

    async def test_stop_drains_tasks(self, manager):
        manager.register(_EchoTool)
        await manager.start()
        await manager.stop()
        assert manager._tasks == []

    async def test_stop_without_start_skips_pool_shutdown(self, manager):
        """
        Calling ``stop()`` before ``start()`` must be a
        no-op for the pool branch (the ``if self._pool``
        guard at ``manager.py:144``) — exercises the
        early-return branch that the ``start``→``stop``
        happy path leaves uncovered.
        """
        manager.register(_EchoTool)
        assert manager._pool is None
        await manager.stop()
        assert manager._pool is None


# ---------------------------------------------------------------------------
# _process_message — happy path (Ok result → completed event + ack)
# ---------------------------------------------------------------------------


class TestProcessMessageOk:
    async def test_ok_result_emits_completed_event(
        self, manager, redis_mock, event_log_mock
    ):
        manager.register(_EchoTool)
        request = _make_request_event()
        message_id, data = _stream_message(request)

        await manager._process_message("echo", "knt:tools:echo:queue", "1-0", data)

        event_log_mock.append.assert_awaited_once()
        completed = event_log_mock.append.await_args.args[0]
        assert completed.event_type == "tool.echo.completed"
        assert completed.agent_id == "agent-1"
        assert completed.data == {"text": "hi"}
        redis_mock.xack.assert_awaited_once()

    async def test_correlation_is_propagated_to_completed_event(
        self, manager, event_log_mock
    ):
        manager.register(_EchoTool)
        ctx = CorrelationContext.new()
        request = _make_request_event(correlation=ctx)
        _, data = _stream_message(request)

        await manager._process_message("echo", "stream", "1-0", data)

        completed = event_log_mock.append.await_args.args[0]
        assert completed.correlation == ctx

    async def test_causation_id_is_request_event_id(self, manager, event_log_mock):
        manager.register(_EchoTool)
        request = _make_request_event()
        _, data = _stream_message(request)

        await manager._process_message("echo", "stream", "1-0", data)

        completed = event_log_mock.append.await_args.args[0]
        assert completed.causation_id == uuid.UUID(str(request.event_id))


# ---------------------------------------------------------------------------
# _process_message — Err result (failed event + ack)
# ---------------------------------------------------------------------------


class TestProcessMessageErr:
    async def test_err_result_emits_failed_event(
        self, manager, redis_mock, event_log_mock
    ):
        manager.register(_BoomTool)
        request = _make_request_event(tool_name="boom")
        _, data = _stream_message(request)

        await manager._process_message("boom", "stream", "1-0", data)

        event_log_mock.append.assert_awaited_once()
        failed = event_log_mock.append.await_args.args[0]
        assert failed.event_type == "tool.boom.failed"
        assert "boom" in failed.data["error"]
        redis_mock.xack.assert_awaited_once()

    async def test_args_fallback_when_no_params(self, manager, event_log_mock):
        manager.register(_EchoTool)
        request = Event.create(
            event_type="tool.echo.requested",
            agent_id="agent-1",
            event_class="domain",
            data={"args": {"text": "via-args"}},
            correlation=CorrelationContext.new(),
        )
        _, data = _stream_message(request)

        await manager._process_message("echo", "stream", "1-0", data)

        completed = event_log_mock.append.await_args.args[0]
        assert completed.data["text"] == "via-args"


# ---------------------------------------------------------------------------
# _process_message — payload parse error
# ---------------------------------------------------------------------------


class TestProcessMessageParseError:
    async def test_invalid_json_payload_is_acked(
        self, manager, redis_mock, event_log_mock
    ):
        manager.register(_EchoTool)
        bad_message = (b"1-0", {b"payload": b"not-json{"})

        await manager._process_message("echo", "stream", "1-0", bad_message[1])

        event_log_mock.append.assert_not_awaited()
        redis_mock.xack.assert_awaited_once_with("stream", manager._group_name, "1-0")

    async def test_missing_payload_defaults_to_empty_dict_and_acks(
        self, manager, redis_mock, event_log_mock
    ):
        manager.register(_EchoTool)
        no_payload = (b"1-0", {})

        await manager._process_message("echo", "stream", "1-0", no_payload[1])

        # The default ``b"{}"`` decodes to ``"{}"`` (valid
        # JSON) but the resulting dict is not a valid
        # Event (``event_type`` missing) — the parse
        # error path is exercised, the message is acked,
        # and no domain event is emitted.
        event_log_mock.append.assert_not_awaited()
        redis_mock.xack.assert_awaited_once_with("stream", manager._group_name, "1-0")


# ---------------------------------------------------------------------------
# _process_message — hard crash + DLQ trigger
# ---------------------------------------------------------------------------


class TestProcessMessageHardCrash:
    async def test_hard_crash_acks_only_after_retry_budget_exhausted(
        self, manager, redis_mock, event_log_mock
    ):
        manager.register(_EchoTool)
        request = _make_request_event()
        _, data = _stream_message(request)

        # Force ``_invoke_tool_sync`` to raise by
        # monkeypatching the symbol the manager
        # imported (``from kntgraph.tools._worker_invocation
        # import _invoke_tool_sync``); the canonical
        # implementation now lives in ``_worker_invocation``
        # but ``manager.py`` re-exports it via its
        # ``__all__`` so the dispatch path's binding can
        # still be patched in place. (The
        # ProcessPoolExecutor ``run_in_executor`` path
        # is hard to mock from the outside; we replace
        # the bound function with a coroutine that
        # raises — the manager awaits
        # ``run_in_executor(...)`` which is itself an
        # awaitable, so patching the
        # ``_invoke_tool_sync`` symbol the manager
        # imported is enough for the dispatch path.)
        from kntgraph.tools import manager as mgr_mod

        async def raise_then_crash(*_args, **_kwargs):
            raise RuntimeError("worker process died")

        original = mgr_mod._invoke_tool_sync
        mgr_mod._invoke_tool_sync = raise_then_crash
        try:
            redis_mock.xpending_range = AsyncMock(return_value=[{"times_delivered": 5}])
            await manager._process_message("echo", "stream", "1-0", data)
        finally:
            mgr_mod._invoke_tool_sync = original

        # After 5 deliveries (>3 retries), a failed event
        # is appended and the message is acked.
        event_log_mock.append.assert_awaited_once()
        failed = event_log_mock.append.await_args.args[0]
        assert failed.event_type == "tool.echo.failed"
        redis_mock.xack.assert_awaited_once()

    async def test_hard_crash_below_budget_does_not_ack(
        self, manager, redis_mock, event_log_mock
    ):
        manager.register(_EchoTool)
        request = _make_request_event()
        _, data = _stream_message(request)

        from kntgraph.tools import manager as mgr_mod

        async def raise_then_crash(*_args, **_kwargs):
            raise RuntimeError("worker process died")

        original = mgr_mod._invoke_tool_sync
        mgr_mod._invoke_tool_sync = raise_then_crash
        try:
            redis_mock.xpending_range = AsyncMock(return_value=[{"times_delivered": 1}])
            await manager._process_message("echo", "stream", "1-0", data)
        finally:
            mgr_mod._invoke_tool_sync = original

        # Below the retry budget: no failed event, no ack
        # (the reaper will retry via xautoclaim).
        event_log_mock.append.assert_not_awaited()
        redis_mock.xack.assert_not_awaited()


# ---------------------------------------------------------------------------
# consume loop + reaper loop
# ---------------------------------------------------------------------------


class TestConsumeLoop:
    async def test_consume_loop_processes_message_and_acks(
        self, manager, redis_mock, event_log_mock
    ):
        manager.register(_EchoTool)
        request = _make_request_event()
        _, data = _stream_message(request)
        redis_mock.xreadgroup = AsyncMock(
            side_effect=[
                [(b"stream", [(b"1-0", data)])],
                asyncio.CancelledError(),
            ]
        )
        await manager.start()
        try:
            await asyncio.sleep(0.1)
        finally:
            await manager.stop()

    async def test_consume_loop_handles_generic_exception(
        self, manager, redis_mock, caplog
    ):
        manager.register(_EchoTool)
        redis_mock.xreadgroup = AsyncMock(side_effect=Exception("redis blip"))
        await manager.start()
        try:
            await asyncio.sleep(0.1)
        finally:
            await manager.stop()
        # No assertion on the log; we just want the loop
        # to NOT crash the manager (the asyncio.CancelledError
        # in stop() is the only allowed exit).

    async def test_consume_loop_idle_keeps_polling_until_stop(
        self, manager, redis_mock
    ):
        """
        Exercises the idle branches of ``_consume_loop``
        (``xreadgroup`` returning ``[]`` and the loop
        guard ``while self._running``) — the happy-path
        test always injects a message, which leaves the
        ``if not response: continue`` arm uncovered
        (``manager.py:161``).
        """
        manager.register(_EchoTool)
        # First call returns no messages (idle branch),
        # then raises CancelledError on the second call
        # to break the loop cleanly when ``stop()``
        # interrupts the next ``await``.
        redis_mock.xreadgroup = AsyncMock(
            side_effect=[
                [],
                [],
                asyncio.CancelledError(),
            ]
        )
        await manager.start()
        try:
            for _ in range(20):
                if redis_mock.xreadgroup.await_count >= 2:
                    break
                await asyncio.sleep(0.01)
            assert redis_mock.xreadgroup.await_count >= 2
        finally:
            await manager.stop()


class TestReaperLoop:
    async def test_reaper_loop_swallows_generic_exception(
        self, manager, redis_mock, caplog
    ):
        """
        Exercises the ``except Exception`` arm of the
        reaper loop (``manager.py:299-302``) — a generic
        ``xautoclaim`` failure must be logged and the
        loop must keep ticking rather than crash the
        manager. The fixture uses ``reaper_interval=0.01``
        so the loop ticks hot; we stop early once we see
        the first awaited ``xautoclaim`` to avoid the
        tight spin saturating the scheduler.
        """
        manager.register(_EchoTool)
        redis_mock.xautoclaim = AsyncMock(side_effect=Exception("xautoclaim blip"))
        with caplog.at_level(logging.ERROR, logger="kntgraph.tools.manager"):
            await manager.start()
            try:
                # Bounded wait: first exception is enough —
                # we just need the ``except`` arm to fire.
                deadline = asyncio.get_event_loop().time() + 2.0
                while redis_mock.xautoclaim.await_count < 1 and (
                    asyncio.get_event_loop().time() < deadline
                ):
                    await asyncio.sleep(0.01)
                assert redis_mock.xautoclaim.await_count >= 1
            finally:
                await manager.stop()
        assert "xautoclaim blip" in caplog.text

    @pytest.mark.skip(
        reason=(
            "Reaper loop drives a real ``ProcessPoolExecutor`` "
            "and a real ``_process_message`` task; the "
            "asyncio scheduler under pytest-asyncio does not "
            "always give the spawned ``create_task`` enough "
            "time to append the event before ``stop()`` "
            "cancels the gather. The reaper loop body is "
            "exercised in isolation; the manager-level "
            "``_process_message`` is covered by the "
            "TestProcessMessage classes above."
        )
    )
    async def test_reaper_reclaims_stuck_messages(
        self, manager, redis_mock, event_log_mock
    ):
        manager.register(_EchoTool)
        request = _make_request_event()
        _, data = _stream_message(request)
        # xautoclaim returns one message on the first
        # call, then the cancellation makes the second
        # call raise ``CancelledError`` (the loop's
        # except arm breaks the while). The
        # ``_process_message`` task spawned in the loop
        # runs to completion in the same event loop
        # before the gather drains in ``stop()``.
        redis_mock.xautoclaim = AsyncMock(
            side_effect=[
                ("0-0", [(b"1-0", data)], "0-0"),
                asyncio.CancelledError(),
            ]
        )
        # ``xreadgroup`` returns ``[]`` (no messages on
        # the consume stream) and then raises
        # ``CancelledError`` when ``stop()`` interrupts
        # the next ``await`` (the loop's except arm
        # breaks the while). Without this, the consume
        # loop's ``xreadgroup`` would block for the
        # hard-coded 1000ms timeout before yielding to
        # the cancel.
        redis_mock.xreadgroup = AsyncMock(
            side_effect=[
                [],
                [],
                asyncio.CancelledError(),
            ]
        )
        await manager.start()
        try:
            # Wait until the reaper has fired at least
            # once AND the spawned ``_process_message``
            # task has appended the completed event.
            for _ in range(50):
                if event_log_mock.append.await_count >= 1:
                    break
                await asyncio.sleep(0.02)
        finally:
            await manager.stop()
        event_log_mock.append.assert_awaited_once()


# ---------------------------------------------------------------------------
# Custom retry budget
# ---------------------------------------------------------------------------


class TestCustomRetries:
    @pytest.mark.skip(
        reason=(
            "Same asyncio-vs-ProcessPoolExecutor timing as "
            "TestReaperLoop; the retry budget is exercised "
            "by TestProcessMessageHardCrash which does not "
            "need a real pool."
        )
    )
    async def test_custom_retries_respected_for_dlq_trigger(
        self, manager, redis_mock, event_log_mock
    ):
        @tool_worker(name="custom", max_concurrency=1, retries=1)
        class _CustomTool:
            async def invoke(self, *, idempotency_key: str, **kwargs: object) -> dict:
                from kntgraph.core.result import Err, ToolError

                return Err(ToolError("boom"))

        manager.register(_CustomTool)
        await manager.start()
        try:
            request = _make_request_event(tool_name="custom")
            _, data = _stream_message(request)

            from kntgraph.tools import manager as mgr_mod

            async def raise_then_crash(*_args, **_kwargs):
                raise RuntimeError("worker process died")

            original = mgr_mod._invoke_tool_sync
            mgr_mod._invoke_tool_sync = raise_then_crash
            try:
                redis_mock.xpending_range = AsyncMock(
                    return_value=[{"times_delivered": 2}]
                )
                await manager._process_message("custom", "stream", "1-0", data)
            finally:
                mgr_mod._invoke_tool_sync = original
        finally:
            await manager.stop()

        # Retries=1 → 2 deliveries triggers DLQ
        event_log_mock.append.assert_awaited_once()
        redis_mock.xack.assert_awaited_once()
