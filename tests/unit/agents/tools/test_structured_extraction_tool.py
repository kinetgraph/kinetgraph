# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for :class:`StructuredExtractionTool` (ADR-055 §2.6).

The Tool is a thin adapter over the
:class:`SLMStructuredExtractor` facade. The unit tests
exercise the Tool's contract:

  - schema validation (regex spot-check)
  - delegation to the facade
  - typed error propagation (``invalid_schema`` and
    ``extraction_failed``)
  - happy path returns ``Ok(list[dict])``

The facade is mocked so the test is fast and CI-portable;
the real model + schema dialect is exercised in
``tests/integration/knowledge/extraction/test_gliner_structured_extraction.py``.

Coverage bar (skill: kntgraph-testing §7.2): every public
function (here, just ``invoke``) has the happy path + at
least one failure mode per failure type.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.agents.tools.protocol import Tool
from kntgraph.agents.tools.structured_extraction import (
    StructuredExtractionTool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_extractor_mock(
    *,
    return_value: "list[dict[str, object]] | None" = None,
    side_effect: "Exception | None" = None,
) -> MagicMock:
    """Return an async mock that behaves like
    ``SLMStructuredExtractor.extract``."""
    extractor = MagicMock(spec=["extract"])
    if side_effect is not None:
        extractor.extract = AsyncMock(side_effect=side_effect)
    else:
        extractor.extract = AsyncMock(return_value=return_value)
    return extractor


# ---------------------------------------------------------------------------
# Protocol & metadata
# ---------------------------------------------------------------------------


class TestToolProtocol:
    """``StructuredExtractionTool`` implements the
    framework ``Tool`` Protocol and exposes the metadata
    a Tool registry expects."""

    def test_is_a_tool(self) -> None:
        tool = StructuredExtractionTool(extractor=_make_extractor_mock())
        assert isinstance(tool, Tool)

    def test_name_and_input_schema(self) -> None:
        tool = StructuredExtractionTool(extractor=_make_extractor_mock())
        assert tool.name == "extract_structured"
        assert tool.input_schema["required"] == ["text", "schema"]
        assert "text" in tool.input_schema["properties"]
        assert "schema" in tool.input_schema["properties"]
        assert tool.input_schema["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """A well-formed call delegates to the facade and
    returns ``Ok(list[dict])``."""

    @pytest.mark.asyncio
    async def test_delegates_to_extractor(self) -> None:
        """The Tool forwards ``text`` and ``schema``
        verbatim to the facade and returns its result."""
        expected: list[dict[str, object]] = [{"nome": "Joao", "cpf": "123.456.789-00"}]
        extractor = _make_extractor_mock(return_value=expected)
        tool = StructuredExtractionTool(extractor=extractor)

        schema = {"documento": ["nome::str", "cpf::str"]}
        result = await tool.invoke(
            idempotency_key="k-1",
            text="Joao, 123.456.789-00.",
            schema=schema,
        )

        extractor.extract.assert_awaited_once_with("Joao, 123.456.789-00.", schema)
        assert result.is_ok()
        assert result.ok_value() == expected

    @pytest.mark.asyncio
    async def test_empty_result_is_ok(self) -> None:
        """An empty ``list`` is a legitimate result (the
        model found nothing). It is wrapped in ``Ok``,
        not ``Err``."""
        extractor = _make_extractor_mock(return_value=[])
        tool = StructuredExtractionTool(extractor=extractor)
        result = await tool.invoke(
            idempotency_key="k-2",
            text="nothing here",
            schema={"documento": ["nome::str"]},
        )
        assert result.is_ok()
        assert result.ok_value() == []


# ---------------------------------------------------------------------------
# Schema validation — failure mode 1
# ---------------------------------------------------------------------------


class TestInvalidSchema:
    """``Err(ToolError("invalid_schema: ..."))`` when the
    schema fails the regex spot-check. The model is NOT
    called when validation fails."""

    @pytest.mark.asyncio
    async def test_non_dict_schema(self) -> None:
        extractor = _make_extractor_mock()
        tool = StructuredExtractionTool(extractor=extractor)
        result = await tool.invoke(
            idempotency_key="k",
            text="t",
            schema=["not", "a", "dict"],  # type: ignore[arg-type]
        )
        assert result.is_err()
        assert "invalid_schema" in str(result.err_value())
        extractor.extract.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_dict_schema(self) -> None:
        extractor = _make_extractor_mock()
        tool = StructuredExtractionTool(extractor=extractor)
        result = await tool.invoke(
            idempotency_key="k",
            text="t",
            schema={},
        )
        assert result.is_err()
        assert "invalid_schema" in str(result.err_value())
        extractor.extract.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_structure_value_not_list(self) -> None:
        extractor = _make_extractor_mock()
        tool = StructuredExtractionTool(extractor=extractor)
        result = await tool.invoke(
            idempotency_key="k",
            text="t",
            schema={"documento": "not-a-list"},  # type: ignore[arg-type]
        )
        assert result.is_err()
        assert "invalid_schema" in str(result.err_value())
        extractor.extract.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_field_specs_list(self) -> None:
        extractor = _make_extractor_mock()
        tool = StructuredExtractionTool(extractor=extractor)
        result = await tool.invoke(
            idempotency_key="k",
            text="t",
            schema={"documento": []},
        )
        assert result.is_err()
        assert "invalid_schema" in str(result.err_value())
        extractor.extract.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_field_spec_shape(self) -> None:
        """A spec that does not match the regex is rejected
        upfront — typos like ``"::str"`` or whitespace
        in the field name are caught here."""
        extractor = _make_extractor_mock()
        tool = StructuredExtractionTool(extractor=extractor)
        result = await tool.invoke(
            idempotency_key="k",
            text="t",
            schema={"documento": ["::str"]},  # missing field name
        )
        assert result.is_err()
        assert "invalid_schema" in str(result.err_value())
        extractor.extract.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_valid_spec_with_anchor_passes(self) -> None:
        """The regex accepts ``field::type::anchor prose``
        — anchors are how the GLiNER2 dialect disambiguates
        fields that share a name."""
        extractor = _make_extractor_mock(return_value=[])
        tool = StructuredExtractionTool(extractor=extractor)
        result = await tool.invoke(
            idempotency_key="k",
            text="t",
            schema={
                "documento": [
                    "nome::str::Full name of the person",
                    "cpf::str",
                ]
            },
        )
        assert result.is_ok()


# ---------------------------------------------------------------------------
# Extraction failure — failure mode 2
# ---------------------------------------------------------------------------


class TestExtractionFailure:
    """``Err(ToolError("extraction_failed: ..."))`` when
    the model raises. The Tool does NOT re-raise; it
    surfaces the error as a typed ``Err``."""

    @pytest.mark.asyncio
    async def test_model_raises_returns_err(self) -> None:
        extractor = _make_extractor_mock(side_effect=RuntimeError("boom"))
        tool = StructuredExtractionTool(extractor=extractor)
        result = await tool.invoke(
            idempotency_key="k",
            text="t",
            schema={"documento": ["nome::str"]},
        )
        assert result.is_err()
        assert "extraction_failed" in str(result.err_value())
        assert "boom" in str(result.err_value())


# ---------------------------------------------------------------------------
# Tool metadata — Tool Protocol extras
# ---------------------------------------------------------------------------


class TestToolMetadata:
    """The Tool's metadata is consumed by ToolInvoker at
    registration time (input schema validation, etc.)."""

    def test_description_is_non_empty(self) -> None:
        tool = StructuredExtractionTool(extractor=_make_extractor_mock())
        assert tool.description
        assert len(tool.description) > 20
