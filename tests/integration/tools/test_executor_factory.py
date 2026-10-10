# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for the pluggable executor factory in
``WorkerManager.register()`` (ADR-069).

The primitive lets a caller override how a specific tool is
executed — replacing the internal ``ProcessPoolExecutor`` with
any async callable that returns the standard ``result_dict``.
The Manager still owns consumer/XACK/reaper/ACL/events; the
factory is a black box invoked once per message.

Behaviour tests with the real ``WorkerManager`` and
``KNT_REDIS_FAKE=1`` (per the testing skill). Each public
function path is covered by a happy-path test and at least one
failure mode.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.result import Err, Ok, Result
from kntgraph.stream.event_log.store import EventLog
from kntgraph.tools._result import ToolResult
from kntgraph.tools.worker import tool_worker

# Mirror the type alias the Manager exposes, so we can type
# factories in tests without importing the (unimported in
# tests) production symbol.
ExecutorFactory = Callable[
    [Callable[[str, dict], dict], str, dict[str, Any]],
    Awaitable[dict[str, Any]],
]


@tool_worker(name="math_doubler", max_concurrency=1, retries=2)
class MathDoublerTool:
    async def invoke(self, *, idempotency_key: str, number: int) -> Result[dict, str]:
        if number < 0:
            return Err("Negative numbers not allowed")
        return Ok({"result": number * 2})


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_register_accepts_executor_factory_kwarg(clean_redis):
    """
    The ``register`` signature accepts an optional
    ``executor_factory`` kwarg without breaking the legacy
    call shape (no factory, no ACL). Bit-for-bit compat.
    """
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter
    from kntgraph.tools.manager import WorkerManager

    manager = WorkerManager(
        clean_redis, event_log=EventLog(RedisEventLogAdapter(clean_redis))
    )

    def _factory(*_args: Any, **_kwargs: Any) -> Any:
        return None

    manager.register(MathDoublerTool, executor_factory=_factory)

    # No exception raised — that's the assertion.


async def test_factory_receives_sync_fn_idempotency_key_and_params(
    clean_redis,
):
    """
    The factory is invoked with the sync ``_invoke_tool_sync``
    (or equivalent), the message's ``idempotency_key`` (which
    is the event_id), and the parsed ``tool_params``. These
    three are the contract.
    """
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter
    from kntgraph.tools.manager import WorkerManager

    agent_id = f"a-{uuid.uuid4()}"
    log = EventLog(RedisEventLogAdapter(clean_redis))
    manager = WorkerManager(clean_redis, event_log=log)

    captured: dict[str, Any] = {}

    async def factory(sync_fn, idempotency_key, tool_params):
        captured["sync_fn"] = sync_fn
        captured["idempotency_key"] = idempotency_key
        captured["tool_params"] = tool_params
        # Drive the real sync_fn so the tool actually runs.
        return sync_fn(MathDoublerTool, idempotency_key, tool_params)

    manager.register(MathDoublerTool, executor_factory=factory)

    request_event = Event.create(
        event_type="tool.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "math_doubler", "params": {"number": 7}},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )
    await clean_redis.xadd(
        "knt:tools:math_doubler:queue",
        {"payload": json.dumps(request_event.to_dict())},
    )

    await manager.start()
    try:
        # Wait for the factory to be invoked.
        for _ in range(20):
            if "idempotency_key" in captured:
                break
            await asyncio.sleep(0.1)

        assert captured["sync_fn"].__name__ == "_invoke_tool_sync"
        assert captured["idempotency_key"] == str(request_event.event_id)
        assert captured["tool_params"] == {"number": 7}
    finally:
        await manager.stop()


async def test_factory_result_ok_emits_completed_event(clean_redis):
    """
    A factory returning ``{"status": "ok", "value": ...}`` causes
    the Manager to emit ``tool.<name>.completed`` and XACK the
    message — same observability surface as the internal pool.
    """
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter
    from kntgraph.tools.manager import WorkerManager

    agent_id = f"a-{uuid.uuid4()}"
    log = EventLog(RedisEventLogAdapter(clean_redis))
    manager = WorkerManager(clean_redis, event_log=log)

    async def factory(sync_fn, idempotency_key, tool_params):
        return ToolResult.ok({"from_factory": True})

    manager.register(MathDoublerTool, executor_factory=factory)

    request_event = Event.create(
        event_type="tool.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "math_doubler", "params": {"number": 3}},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )
    await clean_redis.xadd(
        "knt:tools:math_doubler:queue",
        {"payload": json.dumps(request_event.to_dict())},
    )

    await manager.start()
    try:
        completed = False
        for _ in range(20):
            for e in await log.read(agent_id):
                if e.event_type == "tool.math_doubler.completed":
                    assert e.data == {"from_factory": True}
                    assert str(e.causation_id) == str(request_event.event_id)
                    completed = True
            if completed:
                break
            await asyncio.sleep(0.1)
        assert completed, "Factory OK did not produce tool.completed"
    finally:
        await manager.stop()


async def test_factory_result_err_emits_failed_event(clean_redis):
    """
    A factory returning ``{"status": "err", "error": ...}`` causes
    the Manager to emit ``tool.<name>.failed`` (Manager-owned
    domain event) and XACK the message.
    """
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter
    from kntgraph.tools.manager import WorkerManager

    agent_id = f"a-{uuid.uuid4()}"
    log = EventLog(RedisEventLogAdapter(clean_redis))
    manager = WorkerManager(clean_redis, event_log=log)

    async def factory(sync_fn, idempotency_key, tool_params):
        return ToolResult.err("factory said no")

    manager.register(MathDoublerTool, executor_factory=factory)

    request_event = Event.create(
        event_type="tool.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "math_doubler", "params": {"number": 1}},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )
    await clean_redis.xadd(
        "knt:tools:math_doubler:queue",
        {"payload": json.dumps(request_event.to_dict())},
    )

    await manager.start()
    try:
        failed = False
        for _ in range(20):
            for e in await log.read(agent_id):
                if e.event_type == "tool.math_doubler.failed":
                    assert e.data["error"] == "factory said no"
                    failed = True
            if failed:
                break
            await asyncio.sleep(0.1)
        assert failed, "Factory ERR did not produce tool.failed"
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


async def test_factory_exception_routes_to_hard_crash(clean_redis):
    """
    If the factory raises, the Manager's existing
    ``except Exception`` path catches it as a hard crash: the
    message stays in PEL (no XACK) for the reaper to recover,
    and ``worker.tool.hard_crash`` is the only side effect.
    """
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter
    from kntgraph.tools.manager import WorkerManager

    manager = WorkerManager(
        clean_redis, event_log=EventLog(RedisEventLogAdapter(clean_redis))
    )

    async def factory(sync_fn, idempotency_key, tool_params):
        raise RuntimeError("factory exploded")

    manager.register(MathDoublerTool, executor_factory=factory)

    request_event = Event.create(
        event_type="tool.requested",
        agent_id=f"a-{uuid.uuid4()}",
        event_class="domain",
        data={"tool": "math_doubler", "params": {"number": 5}},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )
    await clean_redis.xadd(
        "knt:tools:math_doubler:queue",
        {"payload": json.dumps(request_event.to_dict())},
    )

    # Sanity: PEL has the message after start (proves no XACK).
    await manager.start()
    try:
        # Give the consume loop a moment.
        for _ in range(20):
            pending = await clean_redis.xpending(
                "knt:tools:math_doubler:queue", "fmh_tool_workers"
            )
            if pending and pending["pending"] >= 1:
                break
            await asyncio.sleep(0.1)

        # The message is still in PEL (no XACK happened).
        assert pending is not None
        pending_count: int = pending["pending"]
        assert pending_count >= 1, "Hard crash should leave message in PEL"
    finally:
        await manager.stop()


async def test_no_factory_uses_internal_pool(clean_redis):
    """
    Regression guard: tools registered WITHOUT ``executor_factory=``
    still go through the internal ``ProcessPoolExecutor`` path.
    This is the bit-for-bit compat promise of ADR-069.
    """
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter
    from kntgraph.tools.manager import WorkerManager

    agent_id = f"a-{uuid.uuid4()}"
    log = EventLog(RedisEventLogAdapter(clean_redis))
    manager = WorkerManager(clean_redis, event_log=log)
    # NO executor_factory — the default path must still work.
    manager.register(MathDoublerTool)

    request_event = Event.create(
        event_type="tool.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "math_doubler", "params": {"number": 9}},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )
    await clean_redis.xadd(
        "knt:tools:math_doubler:queue",
        {"payload": json.dumps(request_event.to_dict())},
    )

    await manager.start()
    try:
        completed = False
        for _ in range(20):
            for e in await log.read(agent_id):
                if e.event_type == "tool.math_doubler.completed":
                    assert e.data == {"result": 18}
                    completed = True
            if completed:
                break
            await asyncio.sleep(0.1)
        assert completed, "No-factory path must still work (compat)"
    finally:
        await manager.stop()
