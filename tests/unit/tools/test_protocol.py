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
)
from kntgraph.tools.manager import WorkerManager


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


def _make_manager() -> WorkerManager:
    """Build a ``WorkerManager`` backed by mocks."""
    from unittest.mock import AsyncMock, MagicMock

    redis_mock = MagicMock()
    redis_mock.xack = AsyncMock()
    event_log_mock = MagicMock()
    event_log_mock.append = AsyncMock()
    return WorkerManager(redis=redis_mock, event_log=event_log_mock)


class TestWorkerManagerIntrospection:
    """Tests for ``WorkerManager.get`` / ``names`` /
    ``__contains__`` / ``__len__`` (the introspection
    surface migrated from ``ToolRegistry`` in v0.18
    per ADR-066 §4.4).
    """

    def test_register_and_get(self):
        mgr = _make_manager()
        mgr.register(_HelloTool, acl=None)
        assert mgr.get("hello.greet") is _HelloTool
        assert mgr.get("nope") is None

    def test_names(self):
        mgr = _make_manager()
        mgr.register(_HelloTool, acl=None)
        assert mgr.names() == ["hello.greet"]

    def test_contains_and_len(self):
        mgr = _make_manager()
        assert "x" not in mgr
        assert len(mgr) == 0
        mgr.register(_HelloTool, acl=None)
        assert "hello.greet" in mgr
        assert len(mgr) == 1

    def test_protocol_satisfied(self):
        """A class with the right attributes satisfies Tool."""
        mgr = _make_manager()
        mgr.register(_HelloTool, acl=None)
        assert isinstance(mgr.get("hello.greet"), Tool)


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
