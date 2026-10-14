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
        ``status``, ``error`` and ``error_class`` keys,
        no ``value`` key. This is the branch that was
        uncovered in ``_result.py:84`` when the
        dataclass shipped.
        """
        result: ToolResult = ToolResult.err("bad input")

        wire = result.to_wire()

        # ``error_class`` defaults to ``"unknown"`` for
        # legacy callers (no classification specified);
        # the framework / sweeper consume it on the
        # ``tool.<name>.failed`` event's ``data``.
        assert wire == {
            "status": "err",
            "error": "bad input",
            "error_class": "unknown",
        }
        # No ``value`` key on the error wire shape.
        assert "value" not in wire


class TestToolResultErrorClass:
    """``ToolResult.error_class`` (item 1 of the backoffice
    2026-10-07 retry-storm post-mortem).

    The framework-level classification slot lets the
    WorkerManager, TTL sweeper, and circuit breaker
    distinguish retryable failures (transient) from
    deterministic ones (permanent) without parsing the
    free-form ``error`` string. Default ``"unknown"``
    preserves the legacy behaviour for tools that have
    not adopted the classification.
    """

    def test_default_is_unknown(self) -> None:
        """Legacy callers (``ToolResult.err("msg")``)
        get ``error_class == "unknown"`` — no
        behavioural change.
        """
        result = ToolResult.err("boom")
        assert result.error_class == "unknown"

    def test_explicit_transient(self) -> None:
        result = ToolResult.err("timeout", error_class="transient")
        assert result.error_class == "transient"

    def test_explicit_permanent(self) -> None:
        result = ToolResult.err("no_such_bucket", error_class="permanent")
        assert result.error_class == "permanent"

    def test_wire_carries_error_class(self) -> None:
        """The wire shape (consumed by
        ``build_failure_event`` and projected to the
        ``tool.<name>.failed`` event's ``data``) carries
        ``error_class`` so downstream consumers can
        route on it.
        """
        wire = ToolResult.err("timeout", error_class="transient").to_wire()
        assert wire["error_class"] == "transient"
        wire_p = ToolResult.err("nsb", error_class="permanent").to_wire()
        assert wire_p["error_class"] == "permanent"

    def test_ok_branch_omits_error_class(self) -> None:
        """The success wire shape stays unchanged —
        ``error_class`` is failure-only (ADR-079
        §3.3 minimal-surface rule).
        """
        wire = ToolResult.ok({"x": 1}).to_wire()
        assert wire == {"status": "ok", "value": {"x": 1}}
        assert "error_class" not in wire


class TestToolErrorClassification:
    """``ToolError.transient/permanent`` classmethods
    (the ``unknown`` classmethod was consolidated with the
    legacy constructor to keep ``ToolError`` class CC at
    1 — see ratchet rule §8).

    Tools raise / return these so the framework can
    classify the failure before it leaves the
    tool-process boundary (``_worker_invocation.py`` and
    ``executors._result_to_tool_result`` extract the
    classification when wrapping the ``Result`` into a
    ``ToolResult``).
    """

    def test_transient_factory(self) -> None:
        from kntgraph.core.result.errors import ToolError

        e = ToolError.transient("rate_limited")
        assert e.error_class == "transient"
        assert str(e) == "rate_limited"

    def test_permanent_factory(self) -> None:
        from kntgraph.core.result.errors import ToolError

        e = ToolError.permanent("no_such_bucket")
        assert e.error_class == "permanent"
        assert str(e) == "no_such_bucket"

    def test_explicit_unknown_kwarg(self) -> None:
        """Tools that explicitly classify as ``"unknown"``
        can pass the kwarg directly (the default is the
        same, but the kwarg is allowed for symmetry).
        """
        from kntgraph.core.result.errors import ToolError

        e = ToolError("some failure", error_class="unknown")
        assert e.error_class == "unknown"

    def test_result_envelope_propagates_to_tool_result(self) -> None:
        """A tool that returns ``Err(ToolError.permanent(...))``
        gets a ``ToolResult`` carrying ``error_class ==
        "permanent"`` — the classification survives the
        ``Result`` → ``ToolResult`` conversion in
        ``_worker_invocation.py``.
        """
        from kntgraph.core.result import Err
        from kntgraph.core.result.errors import ToolError

        # The test exercises the public contract that
        # ``_invoke_tool_sync`` uses internally; we
        # mirror the conversion here so the test does not
        # depend on pickling machinery.
        result = Err(ToolError.permanent("no_such_bucket"))
        assert result.is_err()
        err_value = result.err_value_or_raise()
        assert isinstance(err_value, ToolError)
        assert err_value.error_class == "permanent"
        # ``_invoke_tool_sync`` then wraps as:
        #   ToolResult.err(str(err_value), error_class=err_value.error_class)
        wrapped = ToolResult.err(str(err_value), error_class=err_value.error_class)
        assert wrapped.error_class == "permanent"
        assert wrapped.status == "err"

    def test_non_toolerror_envelope_defaults_to_unknown(self) -> None:
        """A tool that raises a non-``ToolError``
        falls back to ``error_class == "unknown"`` —
        the framework cannot introspect the type, so it
        preserves the legacy retry behaviour for this
        case.
        """
        from kntgraph.core.result import Err
        from kntgraph.core.result.errors import ToolError

        # ``ValueError`` is a non-ToolError exception;
        # the framework cannot extract classification,
        # so the wrap falls back to ``"unknown"``.
        result = Err(ValueError("some other failure"))
        assert result.is_err()
        err_value = result.err_value_or_raise()
        assert not isinstance(err_value, ToolError)
        # The legacy wrap:
        wrapped = ToolResult.err(str(err_value))
        assert wrapped.error_class == "unknown"


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
