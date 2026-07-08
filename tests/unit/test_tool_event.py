# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``core.tool_event``.

Pins the parse contract that the rest of the framework
relies on (the inverse of ``tools.protocol.ToolEventType``).
"""

from __future__ import annotations

import pytest

from kntgraph.core.tool_event import (
    ToolEvent,
    ToolEventKind,
    is_tool_event,
    parse_tool_event,
    tool_name_of,
)


class TestParseToolEvent:
    def test_requested(self):
        ev = parse_tool_event("tool.city_lookup.requested")
        assert ev == ToolEvent(tool_name="city_lookup", kind=ToolEventKind.REQUESTED)

    def test_completed(self):
        ev = parse_tool_event("tool.city_lookup.completed")
        assert ev == ToolEvent(tool_name="city_lookup", kind=ToolEventKind.COMPLETED)

    def test_failed(self):
        ev = parse_tool_event("tool.city_lookup.failed")
        assert ev == ToolEvent(tool_name="city_lookup", kind=ToolEventKind.FAILED)

    def test_args_invalid(self):
        ev = parse_tool_event("tool.city_lookup.args_invalid")
        assert ev == ToolEvent(
            tool_name="city_lookup",
            kind=ToolEventKind.ARGS_INVALID,
        )

    def test_tool_name_with_dots_preserved(self):
        """The wire format is ``tool.<name>.<kind>`` where
        ``<name>`` may contain dots (e.g. ``invoice.issue``).
        The slicing must NOT split on dots — only on the
        leading ``tool.`` and the trailing ``.<kind>``.
        """
        ev = parse_tool_event("tool.invoice.issue.requested")
        assert ev == ToolEvent(
            tool_name="invoice.issue",
            kind=ToolEventKind.REQUESTED,
        )
        ev = parse_tool_event("tool.invoice.issue.completed")
        assert ev.tool_name == "invoice.issue"
        assert ev.kind == ToolEventKind.COMPLETED

    def test_non_tool_event_returns_none(self):
        assert parse_tool_event("city.lookup.requested") is None

    def test_empty_string_returns_none(self):
        assert parse_tool_event("") is None

    def test_unrelated_prefix_returns_none(self):
        assert parse_tool_event("notool.x.requested") is None

    def test_unknown_suffix_returns_none(self):
        """``tool.<name>.something_else`` is NOT a tool event
        — only the four documented kinds count. The framework
        has no ``.started`` or ``.progress`` state.
        """
        assert parse_tool_event("tool.city_lookup.started") is None
        assert parse_tool_event("tool.city_lookup.unknown") is None

    def test_empty_tool_name_returns_none(self):
        """``tool..requested`` has no tool name; the parser
        refuses rather than emit ``ToolEvent(tool_name="")``.
        """
        assert parse_tool_event("tool..requested") is None

    def test_just_prefix_returns_none(self):
        assert parse_tool_event("tool.") is None
        assert parse_tool_event("tool") is None


class TestToolNameOf:
    def test_basic(self):
        assert tool_name_of("tool.city_lookup.completed") == "city_lookup"

    def test_dotted_name(self):
        assert tool_name_of("tool.invoice.issue.requested") == "invoice.issue"

    def test_non_tool(self):
        assert tool_name_of("city.lookup.completed") is None

    def test_unknown_suffix(self):
        assert tool_name_of("tool.city_lookup.started") is None

    def test_empty_name(self):
        assert tool_name_of("tool..requested") is None


class TestIsToolEvent:
    def test_any_kind(self):
        assert is_tool_event("tool.x.requested") is True
        assert is_tool_event("tool.x.completed") is True
        assert is_tool_event("tool.x.failed") is True
        assert is_tool_event("tool.x.args_invalid") is True

    def test_specific_kind(self):
        assert is_tool_event("tool.x.completed", ToolEventKind.COMPLETED) is True
        assert is_tool_event("tool.x.completed", ToolEventKind.FAILED) is False

    def test_multiple_kinds(self):
        """Common case: filter for terminal events only."""
        assert (
            is_tool_event(
                "tool.x.completed",
                ToolEventKind.COMPLETED,
                ToolEventKind.FAILED,
            )
            is True
        )
        assert (
            is_tool_event(
                "tool.x.failed",
                ToolEventKind.COMPLETED,
                ToolEventKind.FAILED,
            )
            is True
        )
        assert (
            is_tool_event(
                "tool.x.requested",
                ToolEventKind.COMPLETED,
                ToolEventKind.FAILED,
            )
            is False
        )

    def test_non_tool_event(self):
        assert is_tool_event("city.lookup.requested") is False
        assert is_tool_event("") is False

    def test_unknown_suffix(self):
        assert is_tool_event("tool.x.started") is False


class TestRoundTripWithToolEventTypeConstructor:
    """
    The constructor in ``tools.protocol.ToolEventType`` and
    the parser in ``core.tool_event`` are inverses — pin
    that contract so a future change to one of them that
    breaks the round-trip is caught.
    """

    @pytest.mark.parametrize("name", ["city_lookup", "invoice.issue", "a.b.c.d"])
    @pytest.mark.parametrize(
        "kind",
        [
            ToolEventKind.REQUESTED,
            ToolEventKind.COMPLETED,
            ToolEventKind.FAILED,
            ToolEventKind.ARGS_INVALID,
        ],
    )
    def test_round_trip(self, name, kind):
        from kntgraph.agents.tools.protocol import ToolEventType

        wire = getattr(ToolEventType, kind.value)(name)
        parsed = parse_tool_event(wire)
        assert parsed is not None
        assert parsed.tool_name == name
        assert parsed.kind == kind
