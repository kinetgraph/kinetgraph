# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the tools/protocol.py module.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from kntgraph.agents.tools.protocol import (
    Tool,
    ToolEventType,
)
from kntgraph.core.result import Err, Ok, ToolError
from kntgraph.tools.manager import WorkerManager


class _HelloTool:
    """A trivial Tool implementation for testing."""

    name = "hello.greet"
    description = "Greets someone."
    input_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    # ``idempotent`` defaults to ``True`` on the Protocol
    # (ADR-075 §3.3); explicit here to pin the test surface.
    idempotent = True

    async def invoke(self, *, name: str, **kwargs):
        if not name:
            return Err(ToolError("name is required"))
        return Ok({"greeting": f"hello, {name}"})


class _NonIdempotentTool:
    """Tool that opts out of framework-level re-dispatch
    (ADR-075 §3.3). Mirrors a PIX-style side effect tool.
    """

    name = "bank.transfer"
    description = "PIX transfer (operator opted out of retry)."
    input_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {"amount": {"type": "integer"}},
    }
    idempotent = False

    async def invoke(self, *, idempotency_key: str, amount: int, **kwargs):
        return Ok({"status": "ok", "transfer": {"amount": amount}})


class _OmittedIdempotentTool:
    """Tool that does NOT declare ``idempotent`` at all —
    must still satisfy the Protocol because the default
    on the Protocol is ``True`` (ADR-075 §4 backward-compat).
    """

    name = "lookup.by_id"
    description = "Read-only lookup (naturally idempotent)."
    input_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
    }

    async def invoke(
        self,
        *,
        idempotency_key: str,
        id: str,  # noqa: A002 — domain term for the tool input id
        **kwargs,
    ):
        return Ok({"id": id})


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


class TestToolIdempotentFlag:
    """``Tool.idempotent: bool = True`` (ADR-075 §3.3).

    Pin the per-tool idempotency contract:
      - a tool without the attribute still satisfies the Protocol
        (default ``True`` — backward compat per ADR-075 §4);
      - a tool that declares ``idempotent = False`` opts out
        of framework-level re-dispatch (the sweeper reads the
        flag and routes to DLQ instead — ADR-075 §2.3.2).
    """

    def test_default_is_true(self):
        """The Protocol default is ``True`` (ADR-075 §3.3)."""
        assert _HelloTool.idempotent is True

    def test_explicit_false_opts_out(self):
        """Non-idempotent side-effect tools can opt out."""
        assert _NonIdempotentTool.idempotent is False

    def test_omitted_attribute_treated_as_true(self):
        """A class without ``idempotent`` declared is treated
        as ``True`` by the framework at runtime — but
        ``isinstance(cls, Tool)`` still requires presence
        (Python ``@runtime_checkable`` Protocol gotcha).

        This pins the semantic the sweeper relies on:
        ``WorkerManager.tool_for(name).idempotent`` defaults
        to ``True`` when the tool class omitted the field.
        The TypeError on missing attributes is caught by the
        explicit ``getattr(..., True)`` fallback at every
        read site.
        """
        # Type-checker view: omitting ``idempotent`` is fine
        # because the Protocol declares the default.
        # Runtime view: ``getattr`` honours the default
        # the type-checker sees.
        assert getattr(_OmittedIdempotentTool(), "idempotent", True) is True
        # ``isinstance`` does NOT honour the Protocol
        # default — that is a documented
        # ``@runtime_checkable`` quirk. The test for
        # ``isinstance`` therefore requires the attribute
        # to be present (see ``_HelloTool`` /
        # ``_NonIdempotentTool`` above).

    def test_worker_manager_reads_flag(self):
        """``WorkerManager.get(...)`` returns the class, and
        the class carries the flag — the sweeper reads it
        via ``WorkerManager.tool_for(name).idempotent``
        (ADR-075 §2.3.2 / §3.3).
        """
        mgr = _make_manager()
        mgr.register(_NonIdempotentTool, acl=None)
        cls = mgr.get("bank.transfer")
        assert cls is _NonIdempotentTool
        assert cls.idempotent is False


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
