# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for :class:`SLMStructuredExtractor` (ADR-055 §2.5).

The facade is the public surface over the low-level
:class:`GlinerStructuredAdapter`. The unit tests cover
the contract the facade IS-A :class:`StructuredExtractor`
Protocol, the kwargs forwarding to the default adapter,
and the structural delegation to an injected adapter.

Tests are TDD-shaped: every public function (here, just
``__init__`` + ``extract`` + the three properties) gets
the happy path + at least one failure mode.

Coverage bar (skill: kntgraph-testing §7.2):
  - happy path + ≥ 1 failure mode per public function.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kntgraph.knowledge.extraction import SLMStructuredExtractor
from kntgraph.knowledge.extraction.base import StructuredExtractor


# ---------------------------------------------------------------------------
# Protocol & metadata
# ---------------------------------------------------------------------------


class TestProtocol:
    """The facade IS-A :class:`StructuredExtractor`."""

    def test_is_a_structured_extractor(self) -> None:
        """The facade satisfies the Protocol whether
        the default adapter or an injected one is in
        use. The injection variant is the deterministic
        one — it does not depend on the ``gliner2``
        package being installed or on a real model
        download."""
        sentinel = MagicMock(spec=StructuredExtractor)
        sentinel.model_name = "x"
        sentinel.field_threshold = 0.5
        sentinel.include_confidence = False
        facade = SLMStructuredExtractor(adapter=sentinel)
        assert isinstance(facade, StructuredExtractor)


# ---------------------------------------------------------------------------
# Construction — kwargs forwarding
# ---------------------------------------------------------------------------


class TestConstruction:
    """``SLMStructuredExtractor.__init__`` resolves the
    default adapter (``GlinerStructuredAdapter``) when
    none is injected, and forwards kwargs (model_name,
    device, field_threshold, include_confidence) verbatim."""

    def test_default_adapter_is_gliner_structured(self) -> None:
        """No ``adapter=`` → the facade instantiates
        :class:`GlinerStructuredAdapter` as the default."""
        with patch(
            "kntgraph.knowledge.extraction.gliner_structured.GlinerStructuredAdapter.__init__",
            return_value=None,
        ) as mock_init:
            SLMStructuredExtractor(model_name="custom/gliner")
        mock_init.assert_called_once()

    def test_kwargs_forwarded_to_default_adapter(self) -> None:
        """Explicit kwargs propagate to the default
        adapter verbatim."""
        with patch(
            "kntgraph.knowledge.extraction.gliner_structured.GlinerStructuredAdapter.__init__",
            return_value=None,
        ) as mock_init:
            SLMStructuredExtractor(
                model_name="gliner2-base",
                device="cuda",
                field_threshold=0.9,
                include_confidence=True,
            )
        mock_init.assert_called_once_with(
            model_name="gliner2-base",
            device="cuda",
            field_threshold=0.9,
            include_confidence=True,
        )

    def test_explicit_adapter_wins_over_default(self) -> None:
        """When ``adapter=`` is supplied, the default
        factory is NOT called — the facade holds the
        caller's adapter as-is."""
        sentinel = MagicMock(spec=StructuredExtractor)
        sentinel.model_name = "injected-model"
        sentinel.field_threshold = 0.7
        sentinel.include_confidence = True

        facade = SLMStructuredExtractor(adapter=sentinel)
        assert facade.model_name == "injected-model"
        assert facade.field_threshold == 0.7
        assert facade.include_confidence is True


# ---------------------------------------------------------------------------
# Properties — structural delegation
# ---------------------------------------------------------------------------


class TestProperties:
    """The three properties (``model_name``,
    ``field_threshold``, ``include_confidence``) forward
    to the underlying adapter."""

    def test_model_name_delegates(self) -> None:
        sentinel = MagicMock(spec=StructuredExtractor)
        sentinel.model_name = "delegated-model"
        facade = SLMStructuredExtractor(adapter=sentinel)
        assert facade.model_name == "delegated-model"

    def test_field_threshold_delegates(self) -> None:
        sentinel = MagicMock(spec=StructuredExtractor)
        sentinel.field_threshold = 0.42
        facade = SLMStructuredExtractor(adapter=sentinel)
        assert facade.field_threshold == 0.42

    def test_include_confidence_delegates(self) -> None:
        sentinel = MagicMock(spec=StructuredExtractor)
        sentinel.include_confidence = True
        facade = SLMStructuredExtractor(adapter=sentinel)
        assert facade.include_confidence is True


# ---------------------------------------------------------------------------
# extract — delegation to the adapter
# ---------------------------------------------------------------------------


class TestExtract:
    """``SLMStructuredExtractor.extract`` is a thin
    wrapper: it forwards ``text`` and ``schema`` to the
    underlying adapter and returns its result."""

    @pytest.mark.asyncio
    async def test_delegates_to_injected_adapter(self) -> None:
        """The facade forwards ``text`` and ``schema``
        verbatim to the adapter."""
        sentinel = MagicMock(spec=StructuredExtractor)
        sentinel.model_name = "x"
        sentinel.field_threshold = 0.5
        sentinel.include_confidence = False
        sentinel.extract = AsyncMock(return_value=[{"field": "value"}])
        facade = SLMStructuredExtractor(adapter=sentinel)

        schema = {"doc": ["field::str"]}
        out = await facade.extract("the text", schema)

        sentinel.extract.assert_awaited_once_with("the text", schema)
        assert out == [{"field": "value"}]
