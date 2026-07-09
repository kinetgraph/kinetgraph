# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the framework-level ``tools.protocol``
module (Iter 25).

The framework defines **three layered Protocols** for
the Tool concept:

  - ``Describable`` (identity): name, description,
    input_schema. Inspectable without invoking.
  - ``Callable[T_in, T_out]``: an object that can be
    called with a payload, returning a result.
  - ``Tool[R]`` (full orchestration): Describable +
    Callable + ``idempotency_key`` keyword + Result
    envelope. The shape the ToolInvoker consumes.

This file tests each Protocol independently and the
sub-Protocol relationship (``Tool`` IS-A both
``Describable`` and ``Callable``).
"""

from __future__ import annotations

from typing import Any

import pytest

from kntgraph.core.result import Err, Ok, ToolError
from kntgraph.tools.protocol import (
    Callable,
    Describable,
    Tool,
)


class _DescribableOnly:
    """An object that satisfies only Describable (no
    invoke, no __call__). Used to prove that the
    Describable Protocol is independent of execution."""

    name = "meta.info"
    description = "A tool that only carries metadata."
    input_schema: dict = {}


class _CallableOnly:
    """An object that satisfies only Callable — no
    identity, just execution."""

    async def __call__(self, payload: int) -> int:
        return payload * 2


class _FullTool:
    """A complete Tool: identity + idempotency_key +
    Result envelope."""

    name = "math.multiply"
    description = "Multiplies a number by 2."
    input_schema: dict = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
    }

    async def invoke(
        self,
        *,
        idempotency_key: str,
        x: int,
        **kwargs: Any,
    ):
        if x < 0:
            return Err(ToolError("x must be non-negative"))
        return Ok({"result": x * 2, "idem": idempotency_key})


class TestDescribable:
    """``Describable`` Protocol: identity, no execution."""

    def test_class_with_name_description_schema_satisfies(self):
        obj = _DescribableOnly()
        assert isinstance(obj, Describable)

    def test_tool_satisfies_describable(self):
        """A full Tool is also a Describable (sub-Protocol)."""
        assert isinstance(_FullTool(), Describable)

    def test_missing_name_fails_structural_check(self):
        """``@runtime_checkable`` Protocol checks for
        attribute presence. A class missing ``name``
        does NOT satisfy the Protocol."""

        class _NoName:
            description = "x"
            input_schema: dict = {}

        assert not isinstance(_NoName(), Describable)


class TestCallable:
    """``Callable`` Protocol: async __call__ with payload."""

    def test_async_callable_satisfies(self):
        assert isinstance(_CallableOnly(), Callable)

    def test_non_callable_fails_runtime_check(self):
        """``@runtime_checkable`` Protocol checks for
        ``__call__`` attribute presence. An object
        without ``__call__`` fails the isinstance
        check (raises TypeError)."""

        class _NotCallable:
            pass

        # ``isinstance(_NotCallable(), Callable)``
        # returns False (no exception). The contract
        # is enforced at the call site, not at the
        # Protocol check.
        assert not isinstance(_NotCallable(), Callable)


class TestToolSubProtocol:
    """``Tool`` is a sub-Protocol of ``Describable``.
    It declares ``invoke`` as its execution method
    (rather than inheriting ``Callable``'s
    ``__call__``), because ``@runtime_checkable``
    Protocol structural checks in Python 3.12 cannot
    merge the `invoke` method with a `__call__`
    declaration cleanly."""

    def test_full_tool_satisfies_tool(self):
        assert isinstance(_FullTool(), Tool)

    def test_full_tool_satisfies_describable(self):
        assert isinstance(_FullTool(), Describable)

    def test_describable_only_is_not_a_tool(self):
        """A Describable is NOT a Tool: no ``invoke``."""
        obj = _DescribableOnly()
        assert not isinstance(obj, Tool)

    def test_callable_only_is_not_a_tool(self):
        """A Callable is NOT a Tool: missing identity."""
        assert not isinstance(_CallableOnly(), Tool)


class TestToolInvokeContract:
    """The wire contract: ``invoke`` takes idempotency_key
    as a required keyword and returns Result."""

    @pytest.mark.asyncio
    async def test_invoke_returns_ok(self):
        tool = _FullTool()
        r = await tool.invoke(idempotency_key="k1", x=5)
        assert r.is_ok()
        assert r.ok_value() == {"result": 10, "idem": "k1"}

    @pytest.mark.asyncio
    async def test_invoke_returns_err(self):
        tool = _FullTool()
        r = await tool.invoke(idempotency_key="k1", x=-1)
        assert r.is_err()
        assert "non-negative" in str(r.err_value())

    @pytest.mark.asyncio
    async def test_idempotency_key_is_reachable(self):
        """The idempotency_key keyword MUST be available
        in the tool body. This is the contract that
        ``ToolInvoker`` relies on to pass the
        event_id of the .requested event."""
        tool = _FullTool()
        r = await tool.invoke(idempotency_key="evt-abc", x=3)
        assert r.ok_value()["idem"] == "evt-abc"


class TestExistingToolBackwardCompat:
    """The old single-Protocol ``Tool`` shape must still
    be importable. ``kntgraph.tools.protocol.Tool``
    is the canonical name; ``kntgraph.agents.tools.protocol``
    re-exports it."""

    def test_canonical_path_exports(self):
        # Canonical import works
        from kntgraph.tools.protocol import (
            Callable as CanonicalCallable,
        )
        from kntgraph.tools.protocol import (
            Describable as CanonicalDescribable,
        )
        from kntgraph.tools.protocol import Tool as CanonicalTool

        assert CanonicalTool is Tool
        assert CanonicalCallable is Callable
        assert CanonicalDescribable is Describable

    def test_legacy_path_re_exports(self):
        # Legacy path (kntgraph.agents.tools.protocol) is
        # a re-export, not the canonical home.
        from kntgraph.agents.tools.protocol import Callable as LegacyCallable
        from kntgraph.agents.tools.protocol import Describable as LegacyDescribable
        from kntgraph.agents.tools.protocol import Tool as LegacyTool

        assert LegacyTool is Tool
        assert LegacyCallable is Callable
        assert LegacyDescribable is Describable
