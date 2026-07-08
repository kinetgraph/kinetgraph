# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for `tools/invoker.py` — the adapter helper
that bridges pure systems and side-effecting tools via the
real `EventLog` (Redis Streams).

These tests pin the contract of the `ToolInvoker` end-to-end:

  - A `.requested` event appended to the agent's stream is
    consumed by `run_once`, dispatched to the registered tool,
    and the resulting `.completed` (or `.failed`) event is
    written back to the same stream with `causation_id` =
    the request's `event_id`.

  - The `idempotency_key` injected by the invoker equals
    `str(request.event_id)`, so a tool that dedupes by key
    does not double-execute on a re-handle.

  - A `run_once` call is idempotent: a second call with no
    new requests in the stream returns 0 and does NOT call
    the tool again.

  - The invoker never crashes the agent's stream on a bad
    request: it writes a `.failed` event for unknown tools,
    tools that raise, and tools that return `Err(...)`.

  - The `filter_fn` constructor argument allows the caller to
    skip certain requests (e.g. priority filter) without
    removing them from the log.

All tests run against a real Redis (skipped if unavailable —
see `tests/integration/conftest.py`).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from kntgraph.core.event import Event, CorrelationContext
from kntgraph.core.result import Err, Ok
from kntgraph.stream.event_log import EventLog
from kntgraph.agents.tools.invoker import ToolInvoker
from kntgraph.agents.tools.protocol import ToolRegistry

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Tool support — small, realistic tools defined here so the test
# stays self-contained (no dependency on `kntgraph.agents` adapters).
# ---------------------------------------------------------------------------


class _EchoTool:
    """
    Returns the request's `data` payload verbatim, wrapped in
    an `Ok`. Used to verify the OK path: the result on the
    `.completed` event matches the tool's return value.
    """

    def __init__(self) -> None:
        self.name = "support.echo"
        self.description = "echoes its inputs"
        self.input_schema: dict = {}
        self.call_count = 0
        self.last_idempotency_key: str | None = None

    async def invoke(self, *, idempotency_key: str, **kwargs):
        self.call_count += 1
        self.last_idempotency_key = idempotency_key
        return Ok({"echoed": dict(kwargs)})


class _IdempotentCounterTool:
    """
    Simulates a non-idempotent side effect (e.g. a bank
    transfer) by honouring the `idempotency_key`: a second
    invocation with the same key returns the cached result
    without re-running the side effect.

    Used to verify that the invoker's `idempotency_key`
    injection is stable across re-dispatches.
    """

    def __init__(self) -> None:
        self.name = "support.counter"
        self.description = "counts unique idempotency_keys"
        self.input_schema: dict = {}
        self.side_effects = 0  # number of times the "side effect" ran
        self.cache: dict[str, dict] = {}

    async def invoke(self, *, idempotency_key: str, **kwargs):
        if idempotency_key in self.cache:
            return Ok({"status": "duplicate", "value": self.cache[idempotency_key]})
        self.side_effects += 1
        value = {"n": self.side_effects, **kwargs}
        self.cache[idempotency_key] = value
        return Ok({"status": "executed", "value": value})


class _RequiresPayloadTool:
    """
    Validates that the request carries a `payload` field. If
    not present, returns `Err` so the invoker emits a
    `.failed` event with the error message in `data`.
    """

    def __init__(self) -> None:
        self.name = "support.requires_payload"
        self.description = "validates payload presence"
        self.input_schema: dict = {}

    async def invoke(self, *, idempotency_key: str, **kwargs):
        if "payload" not in kwargs:
            return Err("'payload' is required")
        return Ok({"received": kwargs["payload"]})


class _RaisingTool:
    """
    Raises from `invoke`. The invoker must catch the exception
    and emit `.failed` with `raised: <repr>` in the error
    message — never propagate the exception back to the
    caller of `run_once`.
    """

    def __init__(self) -> None:
        self.name = "support.raising"
        self.description = "raises on every call"
        self.input_schema: dict = {}

    async def invoke(self, *, idempotency_key: str, **kwargs):
        raise RuntimeError("boom from tool")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _events_by_type(events: list[Event]) -> dict[str, list[Event]]:
    out: dict[str, list[Event]] = {}
    for e in events:
        out.setdefault(e.event_type, []).append(e)
    return out


# ---------------------------------------------------------------------------
# P1 — OK path: requested → completed, result on the event
# ---------------------------------------------------------------------------


class TestOkPath:
    async def test_requested_yields_completed_with_result(self, clean_redis):
        log = EventLog(clean_redis)
        registry = ToolRegistry()
        tool = _EchoTool()
        registry.register(tool)

        invoker = ToolInvoker(log, registry)

        request = Event.domain_from(
            agent_id="agent-1",
            type="tool.support.echo.requested",
            data={"msg": "hello", "n": 7},
            correlation=CorrelationContext.new(correlation_id=uuid4()),
        )
        await log.append(request)

        handled = await invoker.run_once("agent-1")
        assert handled == 1
        assert tool.call_count == 1

        events = await log.read("agent-1")
        by_type = _events_by_type(events)
        assert "tool.support.echo.requested" in by_type
        assert "tool.support.echo.completed" in by_type
        assert "tool.support.echo.failed" not in by_type

        completed = by_type["tool.support.echo.completed"][0]
        assert completed.causation_id == request.event_id
        # The result is wrapped under data["result"] by the invoker
        assert completed.data["result"] == {"echoed": {"msg": "hello", "n": 7}}
        # Latency is recorded as a float
        assert isinstance(completed.data["latency_ms"], float)
        assert completed.data["latency_ms"] >= 0.0

    async def test_idempotency_key_equals_request_event_id(self, clean_redis):
        """
        The tool receives `idempotency_key=str(request.event_id)`.
        This is the foundation of the at-most-once guarantee
        documented in `invoker.py`.
        """
        log = EventLog(clean_redis)
        registry = ToolRegistry()
        tool = _EchoTool()
        registry.register(tool)
        invoker = ToolInvoker(log, registry)

        request = Event.domain_from(
            agent_id="agent-1",
            type="tool.support.echo.requested",
            data={},
            correlation=CorrelationContext.new(correlation_id=uuid4()),
        )
        await log.append(request)
        await invoker.run_once("agent-1")

        assert tool.last_idempotency_key == str(request.event_id)


# ---------------------------------------------------------------------------
# P2 — run_once is itself idempotent
# ---------------------------------------------------------------------------


class TestRunOnceIdempotence:
    async def test_second_run_once_does_not_re_invoke(self, clean_redis):
        log = EventLog(clean_redis)
        registry = ToolRegistry()
        tool = _EchoTool()
        registry.register(tool)
        invoker = ToolInvoker(log, registry)

        await log.append(
            Event.domain_from(
                agent_id="agent-1",
                type="tool.support.echo.requested",
                data={"k": "v"},
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )

        n1 = await invoker.run_once("agent-1")
        n2 = await invoker.run_once("agent-1")
        assert n1 == 1
        assert n2 == 0
        # The tool was called exactly once, despite two run_once
        # passes — the invoker sees the `.completed` event and
        # skips the request on the second pass.
        assert tool.call_count == 1

    async def test_handle_request_event_dedupes_via_idempotency_key(self, clean_redis):
        """
        The `idempotency_key` injected into the tool is stable,
        so a tool that honours it (e.g. a bank transfer that
        caches by key) does not double-execute the side effect
        even if the invoker is asked to handle the same request
        event again.
        """
        log = EventLog(clean_redis)
        registry = ToolRegistry()
        tool = _IdempotentCounterTool()
        registry.register(tool)
        invoker = ToolInvoker(log, registry)

        request = Event.domain_from(
            agent_id="agent-1",
            type="tool.support.counter.requested",
            data={"amount": 100},
            correlation=CorrelationContext.new(correlation_id=uuid4()),
        )
        await log.append(request)

        # First handle executes the side effect.
        r1 = await invoker.handle_request_event(request)
        assert r1.is_ok()
        # Second handle on the same request: tool returns the
        # cached result, no new side effect.
        r2 = await invoker.handle_request_event(request)
        assert r2.is_ok()
        assert tool.side_effects == 1


# ---------------------------------------------------------------------------
# P3 — Failure paths never crash the stream
# ---------------------------------------------------------------------------


class TestFailurePaths:
    async def test_tool_returns_err_emits_failed(self, clean_redis):
        log = EventLog(clean_redis)
        registry = ToolRegistry()
        registry.register(_RequiresPayloadTool())
        invoker = ToolInvoker(log, registry)

        request = Event.domain_from(
            agent_id="agent-1",
            type="tool.support.requires_payload.requested",
            data={},  # missing required `payload`,
            correlation=CorrelationContext.new(correlation_id=uuid4()),
        )
        await log.append(request)

        handled = await invoker.run_once("agent-1")
        assert handled == 1

        events = await log.read("agent-1")
        by_type = _events_by_type(events)
        assert "tool.support.requires_payload.failed" in by_type
        assert "tool.support.requires_payload.completed" not in by_type

        failed = by_type["tool.support.requires_payload.failed"][0]
        assert failed.causation_id == request.event_id
        assert "'payload' is required" in failed.data["error"]

    async def test_unknown_tool_emits_failed(self, clean_redis):
        log = EventLog(clean_redis)
        registry = ToolRegistry()  # empty
        invoker = ToolInvoker(log, registry)

        request = Event.domain_from(
            agent_id="agent-1",
            type="tool.does.not.exist.requested",
            data={},
            correlation=CorrelationContext.new(correlation_id=uuid4()),
        )
        await log.append(request)

        handled = await invoker.run_once("agent-1")
        assert handled == 1

        events = await log.read("agent-1")
        failed = [e for e in events if e.event_type == "tool.does.not.exist.failed"]
        assert len(failed) == 1
        assert "not registered" in failed[0].data["error"]

    async def test_tool_that_raises_emits_failed(self, clean_redis):
        """
        A tool that raises must not crash `run_once`. The
        invoker catches the exception and writes a `.failed`
        event with `raised: <repr>` so the dispatcher can
        continue processing the rest of the stream.
        """
        log = EventLog(clean_redis)
        registry = ToolRegistry()
        registry.register(_RaisingTool())
        invoker = ToolInvoker(log, registry)

        request = Event.domain_from(
            agent_id="agent-1",
            type="tool.support.raising.requested",
            data={},
            correlation=CorrelationContext.new(correlation_id=uuid4()),
        )
        await log.append(request)

        handled = await invoker.run_once("agent-1")
        assert handled == 1

        events = await log.read("agent-1")
        failed = [e for e in events if e.event_type == "tool.support.raising.failed"]
        assert len(failed) == 1
        assert "raised:" in failed[0].data["error"]
        assert "boom from tool" in failed[0].data["error"]

    async def test_mixed_ok_and_fail_in_same_run(self, clean_redis):
        """
        A failure on one request does not prevent the invoker
        from handling other pending requests in the same
        `run_once` pass.
        """
        log = EventLog(clean_redis)
        registry = ToolRegistry()
        registry.register(_EchoTool())
        registry.register(_RequiresPayloadTool())
        invoker = ToolInvoker(log, registry)

        # One request that will succeed, one that will fail.
        await log.append(
            Event.domain_from(
                agent_id="agent-1",
                type="tool.support.echo.requested",
                data={"msg": "ok"},
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )
        await log.append(
            Event.domain_from(
                agent_id="agent-1",
                type="tool.support.requires_payload.requested",
                data={},  # missing payload → Err,
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )

        handled = await invoker.run_once("agent-1")
        assert handled == 2

        events = await log.read("agent-1")
        by_type = _events_by_type(events)
        assert "tool.support.echo.completed" in by_type
        assert "tool.support.requires_payload.failed" in by_type


# ---------------------------------------------------------------------------
# P4 — Multi-agent and filter
# ---------------------------------------------------------------------------


class TestScopeAndFilter:
    async def test_run_once_only_processes_target_agent(self, clean_redis):
        """
        `run_once(agent_id)` only handles requests for the
        given agent. Pending requests on other agents are
        left untouched.
        """
        log = EventLog(clean_redis)
        registry = ToolRegistry()
        tool_a = _EchoTool()
        _tool_b = _EchoTool()
        # Same name, but the registry is per-invoker; we only
        # need it registered once globally.
        registry.register(tool_a)
        invoker = ToolInvoker(log, registry)

        await log.append(
            Event.domain_from(
                agent_id="agent-a",
                type="tool.support.echo.requested",
                data={"who": "a"},
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )
        await log.append(
            Event.domain_from(
                agent_id="agent-b",
                type="tool.support.echo.requested",
                data={"who": "b"},
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )

        n = await invoker.run_once("agent-a")
        assert n == 1

        # agent-a has its `.completed`; agent-b is untouched.
        a_events = await log.read("agent-a")
        b_events = await log.read("agent-b")
        assert any(e.event_type == "tool.support.echo.completed" for e in a_events)
        assert not any(e.event_type == "tool.support.echo.completed" for e in b_events)
        assert all(e.event_type == "tool.support.echo.requested" for e in b_events)

    async def test_filter_fn_skips_matching_requests(self, clean_redis):
        """
        A `filter_fn` provided to the constructor must
        suppress matching requests: the invoker does NOT call
        the tool and does NOT write a completion event.
        The original request stays in the stream.
        """
        log = EventLog(clean_redis)
        registry = ToolRegistry()
        tool = _EchoTool()
        registry.register(tool)

        # Skip requests that carry `skip=True`.
        def _skip_when_flagged(e: Event) -> bool:
            return not bool(e.data.get("skip"))

        invoker = ToolInvoker(log, registry, filter_fn=_skip_when_flagged)

        await log.append(
            Event.domain_from(
                agent_id="agent-1",
                type="tool.support.echo.requested",
                data={"skip": True},
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )
        await log.append(
            Event.domain_from(
                agent_id="agent-1",
                type="tool.support.echo.requested",
                data={"skip": False},
                correlation=CorrelationContext.new(correlation_id=uuid4()),
            )
        )

        n = await invoker.run_once("agent-1")
        assert n == 1
        # The tool was called once — for the non-skipped request.
        assert tool.call_count == 1

        events = await log.read("agent-1")
        completed = [e for e in events if e.event_type == "tool.support.echo.completed"]
        assert len(completed) == 1
        # The completed event's result is the payload of the
        # non-skipped request, not the skipped one.
        assert completed[0].data["result"] == {"echoed": {"skip": False}}
