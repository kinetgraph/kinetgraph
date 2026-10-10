# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for ``ToolResult`` (ADR-079).

The frozen dataclass replaces the legacy ``result_dict``
shape (``dict[str, str | JsonValue]``) returned by every
``@tool_worker`` invocation. It carries the discriminated
``status`` field plus the wire-boundary ``to_wire()``
method.

The test below covers both branches of ``to_wire()`` and
the ``ok`` / ``err`` factories. Branch coverage of
``tools/_result.py`` is the trigger for this file: the
``else`` branch on ``to_wire()`` (the error projection)
was uncovered when the dataclass was first introduced
(ADR-079 §6.1).
"""

from __future__ import annotations

from kntgraph.tools._result import ToolResult


class TestToolResultFactories:
    """The ``ok`` and ``err`` factories build the
    discriminated union cleanly; the value/error
    attribute defaults are the right null per the
    branch.
    """

    def test_ok_sets_status_value_and_nulls_error(self) -> None:
        """``ToolResult.ok(value)`` produces
        ``status == 'ok'`` with the value carried on
        ``value`` and ``error`` defaulted to ``None``.
        """
        result: ToolResult = ToolResult.ok({"text": "hi"})

        assert result.status == "ok"
        assert result.value == {"text": "hi"}
        assert result.error is None

    def test_err_sets_status_error_and_nulls_value(self) -> None:
        """``ToolResult.err(error)`` produces
        ``status == 'err'`` with the error carried on
        ``error`` and ``value`` defaulted to ``None``.
        """
        result: ToolResult = ToolResult.err("nope")

        assert result.status == "err"
        assert result.value is None
        assert result.error == "nope"


class TestToolResultToWire:
    """``to_wire()`` is the boundary to the
    ``Event.data`` JSON shape (ADR-079 §3.3). Both
    branches must be exercised.
    """

    def test_ok_projects_status_value(self) -> None:
        """The success branch returns a mapping with
        ``status`` and ``value`` keys, no ``error`` key.
        """
        result: ToolResult = ToolResult.ok({"text": "ok"})

        wire = result.to_wire()

        assert wire == {"status": "ok", "value": {"text": "ok"}}
        # No ``error`` key on the success wire shape.
        assert "error" not in wire

    def test_err_projects_status_error(self) -> None:
        """The error branch returns a mapping with
        ``status`` and ``error`` keys, no ``value`` key.
        This is the branch that was uncovered in
        ``_result.py:84`` when the dataclass shipped.
        """
        result: ToolResult = ToolResult.err("bad input")

        wire = result.to_wire()

        assert wire == {"status": "err", "error": "bad input"}
        # No ``value`` key on the error wire shape.
        assert "value" not in wire


class TestToolResultImmutability:
    """``frozen=True`` (skill §1.4) blocks attribute
    assignment. The test guards against a regression
    that would loosen this guarantee.
    """

    def test_cannot_assign_to_status(self) -> None:
        """The dataclass is ``frozen``; assigning to any
        field raises ``FrozenInstanceError`` (subclass
        of ``AttributeError``).
        """
        import dataclasses

        result: ToolResult = ToolResult.ok({"x": 1})

        import pytest

        with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
            result.status = "err"  # type: ignore[misc]
