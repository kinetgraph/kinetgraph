# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for argument extraction (ADR-013, Momento 2).

Covers:
  - `walk_schema` — schema→FieldSpec.
  - `compute_schema_version` — stable hash.
  - `_coerce` — type coercion (string, integer, number,
    bool rejection, date preservation).
  - `RegexFieldFinder` — built-in patterns.
  - `SchemaArgumentExtractor` with a fake `FieldFinder`:
      - happy path (extracts all fields);
      - threshold filtering (low-confidence drop);
      - coercion failure (regex returned "dozens" for a
        number field → dropped);
      - unregistered tool → ToolError;
      - empty text → empty fields;
      - schema with no scalar properties → empty fields.
  - `GlinerArgumentAdapter` does an eager import of
    `gliner2` (integration territory; we test the shape
    of the error when the dep is missing).
"""

from __future__ import annotations

import pytest
from typing import Optional

from kntgraph.core.result import ToolError
from kntgraph.knowledge.extraction.argument import (
    FieldFinder,
    RegexFieldFinder,
    SchemaArgumentExtractor,
)
from kntgraph.knowledge.extraction import GlinerArgumentAdapter
from kntgraph.tools.registry import ToolRegistry
from kntgraph.tools.schema import (
    FieldSpec,
    compute_schema_version,
    walk_schema,
)


# Async tests are marked individually; sync tests
# (TestWalkSchema, TestSchemaVersion,
# TestGlinerArgumentAdapterConstruction) are left
# unmarked to satisfy the kntgraph pyproject's
# `asyncio_mode = "strict"`.


# ---------------------------------------------------------------------------
# walk_schema / compute_schema_version
# ---------------------------------------------------------------------------


class TestWalkSchema:
    def test_none_returns_empty(self) -> None:
        assert walk_schema(None) == []

    def test_empty_dict_returns_empty(self) -> None:
        assert walk_schema({}) == []

    def test_top_level_scalars(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "cnpj": {"type": "string", "format": "cnpj"},
                "amount": {"type": "number"},
                "qtd": {"type": "integer"},
                "ativo": {"type": "boolean"},
                "tags": {"type": "array"},
                "nested": {"type": "object"},
            },
            "required": ["cnpj", "amount"],
        }
        specs = walk_schema(schema)
        # Booleans, arrays, objects are skipped in V1.
        names = {s.name for s in specs}
        assert names == {"cnpj", "amount", "qtd"}
        by_name = {s.name: s for s in specs}
        assert by_name["cnpj"].json_type == "string"
        assert by_name["cnpj"].format == "cnpj"
        assert by_name["cnpj"].required is True
        assert by_name["amount"].required is True
        assert by_name["qtd"].required is False

    def test_malformed_property_is_skipped(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "ok": {"type": "string"},
                "": {"type": "string"},  # empty key
                "bad": "not-a-dict",  # type: ignore[dict-item]
            },
        }
        specs = walk_schema(schema)
        assert [s.name for s in specs] == ["ok"]

    def test_unknown_type_is_skipped(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "x": {"type": "string"},
                "y": {"type": "weird-type"},
            },
        }
        assert [s.name for s in walk_schema(schema)] == ["x"]


class TestSchemaVersion:
    def test_none_and_empty_have_versions(self) -> None:
        a = compute_schema_version(None)
        b = compute_schema_version({})
        assert a == b
        assert len(a) == 16

    def test_stable_across_unrelated_metadata(self) -> None:
        a = compute_schema_version(
            {
                "type": "object",
                "title": "x",
                "properties": {"cnpj": {"type": "string"}},
                "required": ["cnpj"],
            }
        )
        b = compute_schema_version(
            {
                "type": "object",
                "title": "y",  # different
                "description": "z",  # different
                "properties": {"cnpj": {"type": "string"}},
                "required": ["cnpj"],
            }
        )
        assert a == b

    def test_changes_with_properties(self) -> None:
        a = compute_schema_version(
            {"properties": {"a": {"type": "string"}}, "required": []}
        )
        b = compute_schema_version(
            {"properties": {"b": {"type": "string"}}, "required": []}
        )
        assert a != b


# ---------------------------------------------------------------------------
# RegexFieldFinder
# ---------------------------------------------------------------------------


class TestRegexFieldFinder:
    @pytest.mark.asyncio
    async def test_cnpj_format_match(self) -> None:
        f = RegexFieldFinder()
        spec = FieldSpec(name="cnpj", json_type="string", required=True, format="cnpj")
        r = await f.find("Emitir para 12.345.678/0001-90", spec)
        assert r is not None
        assert r[0] == "12.345.678/0001-90"
        assert r[1] == 1.0

    @pytest.mark.asyncio
    async def test_name_hint_match(self) -> None:
        # No explicit format; the field name `cpf`
        # should be enough to pick the cpf pattern.
        f = RegexFieldFinder()
        spec = FieldSpec(name="user_cpf", json_type="string", required=False)
        r = await f.find("cpf 123.456.789-00 ok", spec)
        assert r is not None
        assert r[0] == "123.456.789-00"

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self) -> None:
        f = RegexFieldFinder()
        spec = FieldSpec(name="cnpj", json_type="string", required=False, format="cnpj")
        r = await f.find("nada relevante aqui", spec)
        assert r is None

    @pytest.mark.asyncio
    async def test_empty_text_returns_none(self) -> None:
        f = RegexFieldFinder()
        spec = FieldSpec(name="cnpj", json_type="string", required=False, format="cnpj")
        assert await f.find("", spec) is None


# ---------------------------------------------------------------------------
# SchemaArgumentExtractor
# ---------------------------------------------------------------------------


class _FakeFinder(FieldFinder):
    """
    In-memory field finder for tests. The `responses` map
    mirrors the FieldSpec → optional (value, confidence).
    Missing keys return `None`. Set `raise_for` to a set
    of field names to make the finder raise (exercises
    the per-field error isolation).
    """

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str, str], tuple] = {}
        self.calls: list[FieldSpec] = []
        self.raise_for: set[str] = set()

    def queue(
        self,
        spec: FieldSpec,
        value,
        confidence: float = 0.9,
    ) -> None:
        self.responses[(spec.name, spec.json_type, spec.format or "")] = (
            value,
            confidence,
        )

    def raise_for_field(self, name: str) -> None:
        self.raise_for.add(name)

    async def find(
        self,
        text: str,
        field: FieldSpec,
    ) -> Optional[tuple]:
        self.calls.append(field)
        if field.name in self.raise_for:
            raise RuntimeError(f"fake finder boom for {field.name}")
        key = (field.name, field.json_type, field.format or "")
        return self.responses.get(key)


def _make_registry_with_tool(
    name: str = "tool.x",
    schema: dict | None = None,
) -> tuple[ToolRegistry, object]:
    class _T:
        # Annotations live on the class body so pyright
        # accepts them (type annotations in an assignment
        # statement are flagged as ``reportInvalidTypeForm``).
        invocations: list[dict]

    t = _T()
    t.name = name
    t.description = "test"
    t.input_schema = schema or {}
    t.invocations = []

    async def _invoke(*, idempotency_key, **kwargs):
        t.invocations.append({"idempotency_key": idempotency_key, **kwargs})
        from kntgraph.core.result import Ok

        return Ok({"ok": True})

    t.invoke = _invoke
    reg = ToolRegistry()
    reg.register(t)  # type: ignore[arg-type]
    return reg, t


class TestSchemaArgumentExtractor:
    @pytest.mark.asyncio
    async def test_happy_path_extracts_all_fields(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "cnpj": {"type": "string", "format": "cnpj"},
                "amount": {"type": "number"},
            },
            "required": ["cnpj", "amount"],
        }
        reg, _ = _make_registry_with_tool(schema=schema)
        finder = _FakeFinder()
        cnpj_spec = FieldSpec(
            name="cnpj", json_type="string", required=True, format="cnpj"
        )
        amount_spec = FieldSpec(name="amount", json_type="number", required=True)
        finder.queue(cnpj_spec, "12.345.678/0001-90", confidence=0.9)
        finder.queue(amount_spec, "1500,50", confidence=0.95)
        ex = SchemaArgumentExtractor(reg, finder, field_threshold=0.5)
        r = await ex.extract("text", "tool.x")
        assert r.tool_name == "tool.x"
        assert r.fields == {"cnpj": "12.345.678/0001-90", "amount": 1500.5}
        assert r.confidences["cnpj"] == pytest.approx(0.9)
        assert r.confidences["amount"] == pytest.approx(0.95)
        assert len(r.schema_version) == 16

    @pytest.mark.asyncio
    async def test_threshold_filters_low_confidence(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "cnpj": {"type": "string"},
                "amount": {"type": "number"},
            },
        }
        reg, _ = _make_registry_with_tool(schema=schema)
        finder = _FakeFinder()
        cnpj_spec = FieldSpec(name="cnpj", json_type="string", required=False)
        amount_spec = FieldSpec(name="amount", json_type="number", required=False)
        finder.queue(cnpj_spec, "ok", confidence=0.2)  # below threshold
        finder.queue(amount_spec, 100, confidence=0.9)
        ex = SchemaArgumentExtractor(reg, finder, field_threshold=0.5)
        r = await ex.extract("text", "tool.x")
        assert "cnpj" not in r.fields
        assert r.fields == {"amount": 100}

    @pytest.mark.asyncio
    async def test_coercion_failure_drops_field(self) -> None:
        # "dozens" cannot be coerced to number; field
        # must be dropped, NOT raise.
        schema = {
            "type": "object",
            "properties": {"amount": {"type": "number"}},
        }
        reg, _ = _make_registry_with_tool(schema=schema)
        finder = _FakeFinder()
        amount_spec = FieldSpec(name="amount", json_type="number", required=True)
        finder.queue(amount_spec, "dozens", confidence=0.99)
        ex = SchemaArgumentExtractor(reg, finder, field_threshold=0.5)
        r = await ex.extract("text", "tool.x")
        assert r.fields == {}
        assert r.confidences == {}

    @pytest.mark.asyncio
    async def test_bool_is_not_coerced_to_number(self) -> None:
        # `True` is an int in Python; the coercier must
        # reject it as a sneaky boolean.
        schema = {
            "type": "object",
            "properties": {"qtd": {"type": "integer"}},
        }
        reg, _ = _make_registry_with_tool(schema=schema)
        finder = _FakeFinder()
        qtd_spec = FieldSpec(name="qtd", json_type="integer", required=True)
        finder.queue(qtd_spec, True, confidence=0.9)
        ex = SchemaArgumentExtractor(reg, finder, field_threshold=0.5)
        r = await ex.extract("text", "tool.x")
        assert r.fields == {}

    @pytest.mark.asyncio
    async def test_unregistered_tool_raises(self) -> None:
        reg = ToolRegistry()
        finder = _FakeFinder()
        ex = SchemaArgumentExtractor(reg, finder)
        with pytest.raises(ToolError, match="not registered"):
            await ex.extract("text", "ghost")

    @pytest.mark.asyncio
    async def test_empty_text_returns_empty_fields(self) -> None:
        reg, _ = _make_registry_with_tool(
            schema={"properties": {"x": {"type": "string"}}}
        )
        finder = _FakeFinder()
        ex = SchemaArgumentExtractor(reg, finder)
        r = await ex.extract("", "tool.x")
        assert r.fields == {}
        assert r.confidences == {}

    @pytest.mark.asyncio
    async def test_no_scalar_fields_returns_empty(self) -> None:
        reg, _ = _make_registry_with_tool(
            schema={"properties": {"flag": {"type": "boolean"}}}
        )
        finder = _FakeFinder()
        ex = SchemaArgumentExtractor(reg, finder)
        r = await ex.extract("any", "tool.x")
        assert r.fields == {}
        # The version is still computed (from the
        # schema) for cache key purposes.
        assert r.schema_version

    @pytest.mark.asyncio
    async def test_no_schema_returns_empty(self) -> None:
        reg, _ = _make_registry_with_tool(schema=None)
        finder = _FakeFinder()
        ex = SchemaArgumentExtractor(reg, finder)
        r = await ex.extract("any", "tool.x")
        assert r.fields == {}

    @pytest.mark.asyncio
    async def test_finder_exception_isolated(self) -> None:
        # One field raising must NOT kill the others.
        schema = {
            "type": "object",
            "properties": {
                "a": {"type": "string"},
                "b": {"type": "string"},
            },
        }
        reg, _ = _make_registry_with_tool(schema=schema)
        finder = _FakeFinder()
        b_spec = FieldSpec(name="b", json_type="string", required=False)
        finder.queue(b_spec, "found", confidence=0.9)
        finder.raise_for_field("a")
        ex = SchemaArgumentExtractor(reg, finder, field_threshold=0.5)
        r = await ex.extract("any", "tool.x")
        assert r.fields == {"b": "found"}

    @pytest.mark.asyncio
    async def test_validates_field_threshold(self) -> None:
        reg, _ = _make_registry_with_tool()
        finder = _FakeFinder()
        with pytest.raises(ValueError, match="field_threshold"):
            SchemaArgumentExtractor(reg, finder, field_threshold=1.5)
        with pytest.raises(ValueError, match="registry is required"):
            SchemaArgumentExtractor(None, finder)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="finder is required"):
            SchemaArgumentExtractor(reg, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# GlinerArgumentAdapter — eager-load behaviour
# ---------------------------------------------------------------------------


class TestGlinerArgumentAdapterConstruction:
    def test_imports_gliner2_eagerly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Without the real `gliner2` package installed,
        # construction should raise ImportError pointing
        # to the extra. We don't depend on the package
        # being installed in the test env.
        import sys

        # Make sure `gliner2` cannot be imported in this
        # scope by hiding it.
        monkeypatch.setitem(sys.modules, "gliner2", None)
        reg, _ = _make_registry_with_tool()
        with pytest.raises(ImportError):
            GlinerArgumentAdapter(reg)
