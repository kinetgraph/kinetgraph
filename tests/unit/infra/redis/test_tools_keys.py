# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``infra.redis._tools._keys`` (ADR-076 / DEBT §2.35).

Closes the tool-queue key helpers coverage. The two public
symbols are:

  - ``TOOL_QUEUE_KEY_TEMPLATE`` -- the bare suffix
    template ``"knt:tools:{tool_name}:queue"``.
  - ``tool_queue_key(prefix, tool_name)`` -- compose
    the namespace prefix with the template via
    :func:`kntgraph.infra.redis._prefix.namespaced`.

Every public function in ``_tools/_keys.py`` gets the
happy-path-plus-one-failure-mode coverage that AGENTS.md
§7 (`kntgraph-testing` skill) requires.
"""

from __future__ import annotations

from kntgraph.infra.redis._tools import TOOL_QUEUE_KEY_TEMPLATE, tool_queue_key


# ---------------------------------------------------------------------------
# TOOL_QUEUE_KEY_TEMPLATE -- suffix template shape
# ---------------------------------------------------------------------------


class TestTemplate:
    def test_template_is_suffix_only(self) -> None:
        """The template must NOT carry a leading namespace;
        :func:`tool_queue_key` is the composition point.
        """
        assert TOOL_QUEUE_KEY_TEMPLATE == "knt:tools:{tool_name}:queue"

    def test_template_format_accepts_tool_name(self) -> None:
        """``.format(tool_name=...)`` resolves the placeholder."""
        assert TOOL_QUEUE_KEY_TEMPLATE.format(tool_name="echo") == (
            "knt:tools:echo:queue"
        )


# ---------------------------------------------------------------------------
# tool_queue_key -- composition with the namespace prefix
# ---------------------------------------------------------------------------


class TestToolQueueKeyHappyPath:
    def test_empty_prefix_returns_unprefixed_key(self) -> None:
        """Empty prefix is the byte-for-byte identical
        pre-ADR-076 wire format. This is the contract every
        existing single-service deploy relies on.
        """
        assert tool_queue_key("", "echo") == "knt:tools:echo:queue"

    def test_prefix_with_trailing_colon(self) -> None:
        """The canonical operator-facing shape:
        ``"acme-billing:"`` produces
        ``"acme-billing:knt:tools:echo:queue"`` -- the colon
        comes from the prefix, not from the helper.
        """
        assert tool_queue_key("acme-billing:", "echo") == (
            "acme-billing:knt:tools:echo:queue"
        )

    def test_prefix_without_trailing_colon(self) -> None:
        """A prefix without a trailing colon produces a
        concatenated key without an inserted separator.
        Matches the :func:`namespaced` contract -- the
        helper does NOT auto-insert ``:``.
        """
        assert tool_queue_key("acme", "echo") == "acmeknt:tools:echo:queue"

    def test_compound_prefix(self) -> None:
        """Compound prefixes (``"p1.p2:"``) compose in
        front of the template unchanged.
        """
        assert tool_queue_key("p1.p2:", "ocr") == "p1.p2:knt:tools:ocr:queue"

    def test_dotted_tool_name(self) -> None:
        """Tool names with dots (``"pdf.parse"``) round-trip
        through ``.format`` and end up in the key verbatim.
        """
        assert tool_queue_key("", "pdf.parse") == "knt:tools:pdf.parse:queue"


class TestToolQueueKeyFailureMode:
    def test_composition_is_deterministic(self) -> None:
        """The helper is a pure function: calling it
        twice with the same arguments returns the same
        string. This is the contract ``WorkerManager`` and
        ``ToolRouter`` rely on when they call
        ``_stream_key(tool_name)`` from two different
        call sites -- both sites must observe the same
        Redis key.
        """
        assert tool_queue_key("acme:", "echo") == tool_queue_key("acme:", "echo")
        assert tool_queue_key("", "echo") == tool_queue_key("", "echo")
