# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the tools/protocol.py module.
"""

from __future__ import annotations

import pytest

from kntgraph.core.result import Err, Ok, ToolError
from kntgraph.agents.tools.protocol import (
    Tool,
    ToolEventType,
    ToolRegistry,
)


class _HelloTool:
    """A trivial Tool implementation for testing."""

    name = "hello.greet"
    description = "Greets someone."
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }

    async def invoke(self, *, name: str, **kwargs):
        if not name:
            return Err(ToolError("name is required"))
        return Ok({"greeting": f"hello, {name}"})


class TestToolRegistry:
    def test_register_and_get(self):
        r = ToolRegistry()
        tool = _HelloTool()
        r.register(tool)
        assert r.get("hello.greet") is tool
        assert r.get("nope") is None

    def test_duplicate_register_raises(self):
        r = ToolRegistry()
        r.register(_HelloTool())
        with pytest.raises(ValueError):
            r.register(_HelloTool())

    def test_unregister(self):
        r = ToolRegistry()
        r.register(_HelloTool())
        r.unregister("hello.greet")
        assert r.get("hello.greet") is None

    def test_names_and_tools(self):
        r = ToolRegistry()
        r.register(_HelloTool())
        assert r.names() == ["hello.greet"]
        assert len(r.tools()) == 1

    def test_contains_and_len(self):
        r = ToolRegistry()
        assert "x" not in r
        assert len(r) == 0
        r.register(_HelloTool())
        assert "hello.greet" in r
        assert len(r) == 1

    def test_protocol_satisfied(self):
        """A class with the right attributes satisfies Tool."""
        r = ToolRegistry()
        r.register(_HelloTool())
        assert isinstance(r.get("hello.greet"), Tool)


class TestToolEventType:
    def test_requested(self):
        assert (
            ToolEventType.requested("invoice.issue") == "tool.invoice.issue.requested"
        )

    def test_completed(self):
        assert (
            ToolEventType.completed("invoice.issue") == "tool.invoice.issue.completed"
        )

    def test_failed(self):
        assert ToolEventType.failed("invoice.issue") == "tool.invoice.issue.failed"

    def test_round_trip(self):
        requested = "tool.x.y.requested"
        assert ToolEventType.completed("x.y") == requested.replace(
            "requested", "completed"
        )
        assert ToolEventType.failed("x.y") == requested.replace("requested", "failed")


class TestToolInvoke:
    @pytest.mark.asyncio
    async def test_invoke_ok(self):
        tool = _HelloTool()
        r = await tool.invoke(name="world")
        assert r.is_ok()
        assert r.ok_value() == {"greeting": "hello, world"}

    @pytest.mark.asyncio
    async def test_invoke_err(self):
        tool = _HelloTool()
        r = await tool.invoke(name="")
        assert r.is_err()
        assert "name is required" in str(r.err_value())

    @pytest.mark.asyncio
    async def test_invoke_optional_kwargs(self):
        tool = _HelloTool()
        r = await tool.invoke(name="alice", extra="ignored")
        assert r.is_ok()
        # Extra kwargs are accepted (not validated strictly).
