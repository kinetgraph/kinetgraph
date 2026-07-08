# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the framework-level argument-extraction
pieces (Iter 28). These pieces used to live in a
vertical package; Iter 28 promoted them to the
framework, and the follow-up iter deleted the
vertical re-export shim.

The pieces are:

  - :class:`FieldFinder` Protocol + :class:`RegexFieldFinder`
    (low-level field-find abstraction + the pure regex
    implementation)
  - :func:`coerce` (raw value → JSON-Schema type)
  - :class:`SchemaArgumentExtractor` (orchestrator)
  - :class:`GlinerFieldFinder` (GLiNER2-backed FieldFinder)
  - :class:`GlinerArgumentAdapter` (the canonical
    framework default; updated in Iter 27 to lazy-import
    the vertical pieces; Iter 28 makes those imports
    eager)

Iter 28 closes the lazy-import shim documented in
ADR-026 §4 (Pendentes). After Iter 28, the framework
has **0 imports `kntgraph → kntgraph.agents`** in any
form (eager or lazy).
"""

from __future__ import annotations

from typing import Optional

import pytest

from kntgraph.knowledge.extraction.argument._finder import (
    FieldFinder,
    RegexFieldFinder,
)
from kntgraph.knowledge.extraction.argument._coerce import (
    coerce,
)
from kntgraph.knowledge.extraction.argument._extractor import (
    SchemaArgumentExtractor,
)
from kntgraph.knowledge.extraction import (
    SLMArgumentExtractor,
)
from kntgraph.tools.registry import ToolRegistry
from kntgraph.tools.schema import (
    FieldSpec,
)


# ---------------------------------------------------------------------------
# FieldFinder Protocol + RegexFieldFinder
# ---------------------------------------------------------------------------


class _StubFinder:
    """Test double: returns canned values. Used to verify
    that ``SchemaArgumentExtractor`` aggregates field
    finds correctly."""

    def __init__(self, mapping: dict[str, Optional[tuple]]) -> None:
        self._mapping = mapping
        self.calls: list[FieldSpec] = []

    async def find(
        self,
        text: str,
        field: FieldSpec,
    ) -> Optional[tuple]:
        self.calls.append(field)
        return self._mapping.get(field.name)


class TestFieldFinderProtocol:
    """The ``FieldFinder`` Protocol is ``@runtime_checkable``
    so ``isinstance(obj, FieldFinder)`` works."""

    def test_regex_field_finder_satisfies_protocol(self) -> None:
        assert isinstance(RegexFieldFinder(), FieldFinder)

    def test_stub_with_find_method_satisfies_protocol(self) -> None:
        # Any class with the right method satisfies.
        assert isinstance(_StubFinder({}), FieldFinder)


class TestRegexFieldFinder:
    """Pure-logic field finder. No I/O, no third-party deps."""

    @pytest.mark.asyncio
    async def test_cnpj_with_format(self) -> None:
        finder = RegexFieldFinder()
        spec = FieldSpec(
            name="cnpj",
            json_type="string",
            required=True,
            format="cnpj",
        )
        result = await finder.find("CNPJ: 12.345.678/0001-90", spec)
        assert result is not None
        assert result[0] == "12.345.678/0001-90"
        assert result[1] == 1.0

    @pytest.mark.asyncio
    async def test_cnpj_contiguous_digits(self) -> None:
        finder = RegexFieldFinder()
        spec = FieldSpec(
            name="cnpj",
            json_type="string",
            required=False,
            format="cnpj",
        )
        result = await finder.find("id=12345678000190", spec)
        assert result is not None
        assert result[0] == "12345678000190"

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self) -> None:
        finder = RegexFieldFinder()
        spec = FieldSpec(
            name="cnpj",
            json_type="string",
            required=False,
            format="cnpj",
        )
        assert await finder.find("no cnpj here", spec) is None

    @pytest.mark.asyncio
    async def test_empty_text_returns_none(self) -> None:
        finder = RegexFieldFinder()
        spec = FieldSpec(
            name="cnpj",
            json_type="string",
            required=False,
            format="cnpj",
        )
        assert await finder.find("", spec) is None

    @pytest.mark.asyncio
    async def test_name_hint_fallback(self) -> None:
        """When the schema's ``format`` is unknown, the
        finder falls back to a name-based pattern
        (e.g. a field named ``email`` matches the email
        pattern)."""
        finder = RegexFieldFinder()
        spec = FieldSpec(
            name="email",
            json_type="string",
            required=False,
            format=None,
        )
        result = await finder.find("contact: alice@example.com", spec)
        assert result is not None
        assert result[0] == "alice@example.com"


# ---------------------------------------------------------------------------
# coerce helper
# ---------------------------------------------------------------------------


class TestCoerce:
    def test_none_returns_none(self) -> None:
        spec = FieldSpec(name="x", json_type="string", required=False)
        assert coerce(None, spec) is None

    def test_string_coercion(self) -> None:
        spec = FieldSpec(name="x", json_type="string", required=False)
        assert coerce("hello", spec) == "hello"
        # Non-strings are str()'d and stripped.
        assert coerce(123, spec) == "123"
        # Empty/whitespace strings become None.
        assert coerce("  ", spec) is None

    def test_integer_coercion(self) -> None:
        spec = FieldSpec(name="x", json_type="integer", required=False)
        assert coerce(42, spec) == 42
        assert coerce(42.0, spec) == 42
        # Floats that aren't whole are dropped.
        assert coerce(42.5, spec) is None
        # String parse: int("42") succeeds; int("42.5") fails
        # because the regex match is "42" with leftover.
        assert coerce("42", spec) == 42
        assert coerce("not-a-number", spec) is None

    def test_number_coercion(self) -> None:
        spec = FieldSpec(name="x", json_type="number", required=False)
        assert coerce(42, spec) == 42
        assert coerce(42.5, spec) == 42.5
        # Brazilian-style comma decimal.
        assert coerce("42,5", spec) == 42.5

    def test_bool_is_rejected_for_numeric(self) -> None:
        """``True`` is technically an int in Python; the
        coercion treats it as a sneak boolean and
        rejects it for numeric types."""
        spec = FieldSpec(name="x", json_type="integer", required=False)
        assert coerce(True, spec) is None


# ---------------------------------------------------------------------------
# SchemaArgumentExtractor
# ---------------------------------------------------------------------------


def _make_registry_with_schema(schema: dict) -> ToolRegistry:
    """Build a ``ToolRegistry`` with a single Tool that
    has the given input_schema. Used to exercise
    ``SchemaArgumentExtractor`` end-to-end."""

    class _StubTool:
        name = "test.tool"
        description = "stub"
        input_schema = schema

        async def invoke(self, *, idempotency_key, **kwargs):
            from kntgraph.core.result import Ok

            return Ok({})

    reg = ToolRegistry()
    reg.register(_StubTool())  # type: ignore[arg-type]
    return reg


class TestSchemaArgumentExtractor:
    def test_registry_required(self) -> None:
        with pytest.raises(ValueError, match="registry is required"):
            SchemaArgumentExtractor(None, _StubFinder({}))  # type: ignore[arg-type]

    def test_finder_required(self) -> None:
        reg = _make_registry_with_schema({})
        with pytest.raises(ValueError, match="finder is required"):
            SchemaArgumentExtractor(reg, None)  # type: ignore[arg-type]

    def test_field_threshold_bounds(self) -> None:
        reg = _make_registry_with_schema({})
        with pytest.raises(ValueError, match=r"field_threshold must be in"):
            SchemaArgumentExtractor(
                reg,
                _StubFinder({}),
                field_threshold=1.5,
            )

    def test_threshold_filter_drops_low_confidence(self) -> None:
        reg = _make_registry_with_schema(
            {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            }
        )
        finder = _StubFinder({"x": ("value", 0.3)})
        ext = SchemaArgumentExtractor(reg, finder, field_threshold=0.5)
        # 0.3 < 0.5, so the field is dropped.
        import asyncio

        result = asyncio.run(ext.extract("text", "test.tool"))
        assert result.fields == {}

    def test_threshold_filter_keeps_high_confidence(self) -> None:
        reg = _make_registry_with_schema(
            {
                "type": "object",
                "properties": {"x": {"type": "string"}},
            }
        )
        finder = _StubFinder({"x": ("value", 0.9)})
        ext = SchemaArgumentExtractor(reg, finder, field_threshold=0.5)
        import asyncio

        result = asyncio.run(ext.extract("text", "test.tool"))
        assert result.fields == {"x": "value"}
        assert result.confidences == {"x": 0.9}

    def test_unknown_tool_raises(self) -> None:
        reg = _make_registry_with_schema({})
        ext = SchemaArgumentExtractor(reg, _StubFinder({}))
        import asyncio

        with pytest.raises(Exception):
            asyncio.run(ext.extract("text", "unregistered-tool"))

    def test_empty_text_returns_empty_extraction(self) -> None:
        reg = _make_registry_with_schema(
            {
                "type": "object",
                "properties": {"x": {"type": "string"}},
            }
        )
        ext = SchemaArgumentExtractor(reg, _StubFinder({}))
        import asyncio

        result = asyncio.run(ext.extract("", "test.tool"))
        assert result.fields == {}


# ---------------------------------------------------------------------------
# GlinerArgumentAdapter integration
# ---------------------------------------------------------------------------


class TestGlinerArgumentAdapterEagerImports:
    """Iter 28: the framework's ``GlinerArgumentAdapter``
    no longer uses lazy imports for its 2 vertical
    dependencies. The framework has 0 imports
    `kntgraph → kntgraph.agents` (eager OR lazy)."""

    def test_module_does_not_lazy_import_vertical(self) -> None:
        """The module is importable; the lazy-import
        shim from Iter 27 is gone (replaced by eager
        imports of the framework's own components)."""
        import kntgraph.knowledge.extraction.gliner_argument as mod

        # Inspect the module's source: there should be
        # no ``from kntgraph.agents`` import inside any
        # function (lazy) or at module level.
        import inspect

        source = inspect.getsource(mod)
        assert "from kntgraph.agents" not in source, (
            "kntgraph.knowledge.extraction.gliner_argument "
            "still imports from kntgraph.agents (Iter 28 regression):\n"
            f"{source}"
        )

    def test_class_hierarchy_includes_vertical_types_via_framework(
        self,
    ) -> None:
        """The adapter depends on the framework's
        ``SchemaArgumentExtractor`` and
        ``GlinerFieldFinder`` (both now framework
        modules). The class definitions are
        importable from the framework path."""
        from kntgraph.knowledge.extraction.argument import (
            _extractor as ext_mod,
        )
        from kntgraph.knowledge.extraction.argument import (
            _gliner_finder as finder_mod,
        )

        assert hasattr(ext_mod, "SchemaArgumentExtractor")
        assert hasattr(finder_mod, "GlinerFieldFinder")


# ---------------------------------------------------------------------------
# SLMArgumentExtractor facade
# ---------------------------------------------------------------------------


class TestSLMArgumentExtractorAfterIter28:
    """The facade still works after the canonical home
    moved. The default adapter is still
    ``GlinerArgumentAdapter``; the public API is
    unchanged."""

    def test_default_adapter_is_gliner(self) -> None:
        """The default adapter is the framework's
        ``GlinerArgumentAdapter``. We patch the
        adapter's ``__init__`` to avoid the GLiNER2
        model load (which requires the optional
        dep)."""
        from unittest.mock import patch
        from kntgraph.knowledge.extraction import (
            GlinerArgumentAdapter,
        )

        reg = _make_registry_with_schema({})
        with patch.object(GlinerArgumentAdapter, "__init__", return_value=None):
            facade = SLMArgumentExtractor(reg)
        assert isinstance(facade._adapter, GlinerArgumentAdapter)


# ---------------------------------------------------------------------------
# Top-level __all__ exports
# ---------------------------------------------------------------------------


class TestFrameworkArgumentExports:
    """The framework's argument extraction pieces are
    importable from the canonical paths."""

    def test_finder_module_exports(self) -> None:
        from kntgraph.knowledge.extraction.argument import (
            _finder as mod,
        )

        assert hasattr(mod, "FieldFinder")
        assert hasattr(mod, "RegexFieldFinder")
        assert hasattr(mod, "FieldValue")

    def test_coerce_module_exports(self) -> None:
        from kntgraph.knowledge.extraction.argument import (
            _coerce as mod,
        )

        assert hasattr(mod, "coerce")
        assert hasattr(mod, "CoercedValue")

    def test_extractor_module_exports(self) -> None:
        from kntgraph.knowledge.extraction.argument import (
            _extractor as mod,
        )

        assert hasattr(mod, "SchemaArgumentExtractor")

    def test_gliner_finder_module_exports(self) -> None:
        from kntgraph.knowledge.extraction.argument import (
            _gliner_finder as mod,
        )

        assert hasattr(mod, "GlinerFieldFinder")
        assert hasattr(mod, "extract_first")
        assert hasattr(mod, "match_to_value")
        assert hasattr(mod, "field_o")
