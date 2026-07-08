# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Additional unit tests for `ToolInvoker` covering malformed events,
non-request event types, tool exceptions and EventLog append failures.

These follow the project's test patterns in `tests/unit/tools/test_invoker.py`.
"""

from __future__ import annotations

import uuid

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.result import Err, Ok
from kntgraph.agents.tools.invoker import ToolInvoker, tool_name_from_request
from kntgraph.agents.tools.protocol import ToolRegistry

pytestmark = pytest.mark.asyncio


class _BadLog:
    """EventLog that fails to append (returns Err)."""

    async def append(self, event: Event):
        return Err(Exception("disk full"))


class _RaisingTool:
    """Tool that raises when invoked."""

    def __init__(self) -> None:
        self.name = "boom.tool"
        self.description = "raises"
        self.input_schema = {}

    async def invoke(self, *, idempotency_key: str, **kwargs):
        raise RuntimeError("boom!")


class _GoodTool:
    def __init__(self) -> None:
        self.name = "good.tool"
        self.description = "succeeds"
        self.input_schema = {}

    async def invoke(self, *, idempotency_key: str, **kwargs):
        return Ok({"ok": True})


def _make_event(
    agent_id: str = "A1", et: str = "tool.good.tool.requested", **data
) -> Event:
    return Event.domain_from(
        agent_id=agent_id,
        type=et,
        data=data,
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


class TestMalformedAndHelpers:
    async def test_tool_name_extraction(self):
        e = _make_event(et="tool.foo.bar.requested")
        assert tool_name_from_request(e) == "foo.bar"

    async def test_non_request_event_returns_err(self):
        log = _BadLog()
        registry = ToolRegistry()
        invoker = ToolInvoker(log=log, registry=registry)

        e = Event.domain_from(
            agent_id="a",
            type="not.a.request",
            data={},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        r = await invoker.handle_request_event(e)
        assert r.is_err()
        assert isinstance(r.err_value(), ValueError)

    async def test_malformed_event_type_returns_err(self):
        log = _BadLog()
        registry = ToolRegistry()
        invoker = ToolInvoker(log=log, registry=registry)

        e = Event.domain_from(
            agent_id="a",
            type="tool.badformat",
            data={},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        r = await invoker.handle_request_event(e)
        assert r.is_err()
        assert isinstance(r.err_value(), ValueError)

    async def test_empty_tool_name_returns_err(self):
        log = _BadLog()
        registry = ToolRegistry()
        invoker = ToolInvoker(log=log, registry=registry)

        e = Event.domain_from(
            agent_id="a",
            type="tool..requested",
            data={},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        r = await invoker.handle_request_event(e)
        assert r.is_err()
        assert isinstance(r.err_value(), ValueError)


class TestInvokeRaisesAndAppendFailure:
    async def test_invoke_raises_emits_failed_event(self, fake_log):
        # Use a Fake append that records events so we can assert failure was appended
        log = fake_log
        registry = ToolRegistry()
        registry.register(_RaisingTool())
        invoker = ToolInvoker(log=log, registry=registry)  # type: ignore[arg-type]

        req = Event.domain_from(
            agent_id="X",
            type="tool.boom.tool.requested",
            data={},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        await log.append(req)
        r = await invoker.handle_request_event(req)
        # handle_request_event returns Ok(Event) for the failure append
        assert r.is_ok()
        events = await log.read("X")
        assert any(e.event_type.endswith(".failed") for e in events)
        failed = next(e for e in events if e.event_type.endswith(".failed"))
        assert "raised:" in failed.data.get("error", "")

    async def test_append_failure_causes_err_return_on_completion(self):
        # When append fails during completion, the invoker returns Err
        bad_log = _BadLog()
        registry = ToolRegistry()
        registry.register(_GoodTool())
        invoker = ToolInvoker(log=bad_log, registry=registry)  # type: ignore[arg-type]

        req = Event.domain_from(
            agent_id="A",
            type="tool.good.tool.requested",
            data={},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        r = await invoker.handle_request_event(req)
        assert r.is_err()
        assert "Failed to append completion" in str(r.err_value())
