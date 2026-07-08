# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for idempotency_key injection by ToolInvoker.

These tests pin the contract that:
  - ToolInvoker passes idempotency_key=str(request.event_id)
    to every tool invoke call.
  - The tool receives it as a keyword argument (alongside
    the request's `data` fields).
  - The key is stable across calls: re-handling the same
    request produces the same idempotency_key.
  - Tools that store state keyed by idempotency_key can
    dedupe their side effect (simulated bank.transfer test).
"""

from __future__ import annotations

import uuid

import pytest

from kntgraph.core.event import Event, CorrelationContext
from kntgraph.core.result import Ok
from kntgraph.agents.tools.invoker import ToolInvoker
from kntgraph.agents.tools.protocol import ToolRegistry

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Capturing tool — records the idempotency_key it received
# ---------------------------------------------------------------------------


class _CapturingTool:
    """A test tool that records every invoke call."""

    def __init__(self, name: str = "test.echo") -> None:
        self.name = name
        self.description = "captures invoke args"
        self.input_schema: dict = {}
        self.calls: list[dict] = []

    async def invoke(self, *, idempotency_key: str, **kwargs):
        self.calls.append({"idempotency_key": idempotency_key, **kwargs})
        return Ok({"echo": kwargs, "key": idempotency_key})


class _FakeLog:
    """
    Minimal stand-in for EventLog. Records appended events
    and returns a fake stream id. Used in unit tests that
    exercise the invoker without a real Redis.
    """

    def __init__(self) -> None:
        from kntgraph.core.result import Ok

        self.appended: list[Event] = []
        self._ok_factory = lambda value: Ok(value)

    async def append(self, event: Event):
        self.appended.append(event)
        return self._ok_factory(f"fake-{len(self.appended)}-0")

    async def read(self, agent_id: str) -> list[Event]:
        return [e for e in self.appended if e.agent_id == agent_id]


class _BankTransferTool:
    """
    Simulated non-idempotent tool. Deduplicates transfers
    using a local cache keyed by idempotency_key.
    """

    def __init__(self) -> None:
        self.name = "bank.transfer"
        self.description = "PIX transfer (deduped by idempotency_key)"
        self.input_schema: dict = {}
        self._cache: dict[str, dict] = {}
        self.call_count = 0

    async def invoke(self, *, idempotency_key: str, amount: int, to: str):
        self.call_count += 1
        if idempotency_key in self._cache:
            return Ok({"status": "duplicate", "transfer": self._cache[idempotency_key]})
        transfer = {
            "id": f"tx-{idempotency_key[:8]}",
            "amount": amount,
            "to": to,
        }
        self._cache[idempotency_key] = transfer
        return Ok({"status": "ok", "transfer": transfer})


def _make_request(agent_id: str = "a-1", tool_name: str = "test.echo", **data) -> Event:
    return Event.domain_from(
        agent_id=agent_id,
        type=f"tool.{tool_name}.requested",
        data=data,
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


# ---------------------------------------------------------------------------
# P1 — Tool receives idempotency_key
# ---------------------------------------------------------------------------


class TestIdempotencyKeyInjected:
    async def test_idempotency_key_equals_request_event_id(self):
        """
        The tool receives idempotency_key = str(request.event_id).
        """
        log = _FakeLog()
        registry = ToolRegistry()
        tool = _CapturingTool()
        registry.register(tool)

        invoker = ToolInvoker(log=log, registry=registry)  # type: ignore[arg-type]
        request = _make_request(x=1, y=2)
        result = await invoker.handle_request_event(request)
        assert result.is_ok(), f"err={result.err_value()} tool_calls={tool.calls}"

        assert len(tool.calls) == 1
        call = tool.calls[0]
        assert call["idempotency_key"] == str(request.event_id)
        assert call["x"] == 1
        assert call["y"] == 2

    async def test_idempotency_key_preserved_across_replay(self):
        """
        Re-dispatching the SAME request (same event_id) produces
        the SAME idempotency_key. The tool can dedupe.
        """
        log = _FakeLog()
        registry = ToolRegistry()
        tool = _CapturingTool()
        registry.register(tool)
        invoker = ToolInvoker(log=log, registry=registry)  # type: ignore[arg-type]

        request = _make_request(payload="hello")
        await invoker.handle_request_event(request)
        await invoker.handle_request_event(request)

        assert len(tool.calls) == 2
        assert tool.calls[0]["idempotency_key"] == tool.calls[1]["idempotency_key"]
        # The completion event is the same too:
        assert tool.calls[0]["idempotency_key"] == str(request.event_id)


# ---------------------------------------------------------------------------
# P2 — Tool dedup by idempotency_key
# ---------------------------------------------------------------------------


class TestToolDedupByKey:
    async def test_bank_transfer_dedupes(self):
        """
        A tool that maintains a local cache keyed by
        idempotency_key returns the same result on repeated
        calls — the second call sees `status=duplicate`.
        """
        log = _FakeLog()
        registry = ToolRegistry()
        bank = _BankTransferTool()
        registry.register(bank)
        invoker = ToolInvoker(log=log, registry=registry)  # type: ignore[arg-type]

        request = _make_request(tool_name="bank.transfer", amount=100, to="acc-123")
        # Sanity: the request's event_id is stable across calls
        request2 = _make_request(tool_name="bank.transfer", amount=100, to="acc-123")
        assert str(request.event_id) == str(request2.event_id)
        r1 = await invoker.handle_request_event(request)
        assert r1.is_ok(), f"r1.err={r1.err_value()}"

        # Same request again — must see duplicate
        r2 = await invoker.handle_request_event(request)
        assert r2.is_ok(), f"r2.err={r2.err_value()}"

        # The tool's internal state should have one transfer
        assert len(bank._cache) == 1, (
            f"cache={bank._cache} call_count={bank.call_count}"
        )
        assert bank.call_count == 2, "tool should have been called twice"


# ---------------------------------------------------------------------------
# P3 — Tool that ignores idempotency_key still works
# ---------------------------------------------------------------------------


class _OldStyleTool:
    """A tool that does NOT accept idempotency_key (legacy)."""

    def __init__(self) -> None:
        self.name = "legacy.no_key"
        self.description = "legacy tool"
        self.input_schema: dict = {}

    async def invoke(self, **kwargs):
        # Old tool: would raise on unknown kwargs if the invoker
        # was strict. The current invoker passes idempotency_key
        # as a kwarg, so this tool needs to accept **kwargs.
        return Ok({"got": list(kwargs.keys())})


class TestBackwardCompat:
    async def test_tool_accepting_only_kwargs_still_works(self):
        """
        A tool that uses **kwargs (no explicit parameters) still
        works because idempotency_key is just another kwarg.
        """
        log = _FakeLog()
        registry = ToolRegistry()
        tool = _OldStyleTool()
        registry.register(tool)
        invoker = ToolInvoker(log=log, registry=registry)  # type: ignore[arg-type]

        request = _make_request(tool_name="legacy.no_key", x=42)
        r = await invoker.handle_request_event(request)
        assert r.is_ok()
        # Inspect the tool's captured kwargs (the .completed
        # event in `log.appended[0]` carries the tool's return
        # value under data["result"]).
        assert len(log.appended) == 1
        completion = log.appended[0]
        result = completion.data["result"]
        assert "idempotency_key" in result["got"]
        assert "x" in result["got"]


# ---------------------------------------------------------------------------
# P4 — ToolResult carries request_id (for downstream dedup)
# ---------------------------------------------------------------------------


class TestCompletionEventCarriesRequestId:
    async def test_completion_causation_id_is_request(self):
        """
        The .completed event has causation_id = request.event_id.
        Downstream consumers can join on this without needing
        to inspect the idempotency_key in the data payload.
        """
        log = _FakeLog()
        registry = ToolRegistry()
        registry.register(_CapturingTool())
        invoker = ToolInvoker(log=log, registry=registry)  # type: ignore[arg-type]

        request = _make_request(x=1)
        await invoker.handle_request_event(request)

        # The completion event was appended
        assert len(log.appended) == 1
        completion = log.appended[0]
        assert completion.event_type == "tool.test.echo.completed"
        assert completion.causation_id == request.event_id
        # The request_id is also in the data payload
        assert completion.data["request_id"] == str(request.event_id)
