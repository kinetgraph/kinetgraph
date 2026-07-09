# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the ``SLM*`` facades (Iter 21).

The facades decouple the public surface from GLiNER2:
``SLMEntityExtractor``, ``SLMIntentClassifier`` and
``SLMArgumentExtractor`` are IS-A Protocol and delegate
to a low-level adapter (default: the corresponding
``Gliner*Adapter``). A future TinyLLM / FastText adapter
can be slotted in via ``adapter=`` without changing the
facade's API.

The tests verify:

  - Each facade is IS-A the relevant Protocol at runtime
    (duck-typed via ``runtime_checkable``).
  - The default adapter is the corresponding
    ``Gliner*Adapter`` (no surprise replacement).
  - Explicit adapter injection wins over the default.
  - The ``SLMIntentClassifier`` facade reads Settings
    the same way the underlying ``GlinerIntentAdapter``
    does (delegation).
  - The ``SLMArgumentExtractor`` facade reads Settings
    the same way the underlying ``GlinerArgumentAdapter``
    does.

The GLiNER2 model load is mocked to avoid a real
network call; we only verify the wiring.
"""

from __future__ import annotations


from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kntgraph.infra.config import fresh_settings
from kntgraph.knowledge.extraction import (
    Entity,
    EntityExtractor,
    EntityExtractorWithMentions,
    IntentClassifier,
    SLMArgumentExtractor,
    SLMEntityExtractor,
    SLMIntentClassifier,
)
from kntgraph.knowledge.extraction.base import (
    ArgumentExtractor,
)


# ---------------------------------------------------------------------------
# Shared mocks
# ---------------------------------------------------------------------------


class _MockGLiNER2Module:
    """Mock of the ``gliner2`` module exposing
    ``GLiNER2.from_pretrained`` as a MagicMock that
    returns a sentinel."""

    def __init__(self):
        self.GLiNER2 = MagicMock()
        self.GLiNER2.from_pretrained = MagicMock(return_value="sentinel-model")


def _mocked_require_optional():
    """Returns a (context-manager) patcher that mocks
    ``require_optional`` to return an object with a
    ``GLiNER2`` attribute pointing at a mock model."""
    mock_module = _MockGLiNER2Module()
    mock_require_result = MagicMock()
    mock_require_result.GLiNER2 = mock_module.GLiNER2
    return patch(
        "kntgraph._optional.require_optional",
        return_value=mock_require_result,
    )


def _make_tool_registry():
    """Build a minimal ToolRegistry for argument-extraction
    tests. We need at least one tool registered so the
    argument extractor can resolve a schema. The actual
    tool body doesn't matter — we never call ``extract``."""
    from kntgraph.agents.tools.protocol import ToolRegistry

    reg = ToolRegistry()
    # Use a SimpleNamespace as a duck-typed Tool — the
    # registry stores it by name and only reads `name`/
    # `description`/`input_schema` from the Protocol. The
    # framework never invokes the test stub.
    from types import SimpleNamespace

    stub = SimpleNamespace(
        name="dummy",
        description="dummy tool for tests",
        input_schema={
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        },
    )
    reg.register(stub)  # type: ignore[arg-type]
    return reg


# ---------------------------------------------------------------------------
# SLMEntityExtractor
# ---------------------------------------------------------------------------


class TestSLMEntityExtractor:
    def test_is_a_entity_extractor(self) -> None:
        """The facade IS-A EntityExtractorWithMentions
        (and therefore EntityExtractor)."""
        facade = SLMEntityExtractor()
        assert isinstance(facade, EntityExtractorWithMentions)
        assert isinstance(facade, EntityExtractor)

    def test_default_adapter_is_gliner(self) -> None:
        """No ``adapter=`` arg → facade instantiates the
        default ``GlinerEntityAdapter`` (template method)."""
        facade = SLMEntityExtractor()
        from kntgraph.knowledge.extraction import (
            GlinerEntityAdapter,
        )

        assert isinstance(facade._adapter, GlinerEntityAdapter)

    def test_explicit_adapter_wins(self) -> None:
        """Passing ``adapter=`` swaps the implementation
        — a future TinyLLM adapter plugs in cleanly."""
        sentinel = MagicMock(spec=EntityExtractorWithMentions)
        facade = SLMEntityExtractor(adapter=sentinel)
        assert facade._adapter is sentinel

    @pytest.mark.asyncio
    async def test_extract_delegates(self) -> None:
        """``extract`` is a thin wrapper over the adapter."""
        sentinel = MagicMock(spec=EntityExtractorWithMentions)
        sentinel.extract = AsyncMock(
            return_value=[Entity(name="x", type="org", surface="x")]
        )
        facade = SLMEntityExtractor(adapter=sentinel)
        result = await facade.extract("text")
        sentinel.extract.assert_awaited_once_with("text")
        assert len(result) == 1 and result[0].name == "x"

    @pytest.mark.asyncio
    async def test_extract_with_mentions_delegates(self) -> None:
        """``extract_with_mentions`` is a thin wrapper
        over the adapter."""
        sentinel = MagicMock(spec=EntityExtractorWithMentions)
        sentinel.extract_with_mentions = AsyncMock(
            return_value=[(Entity(name="y", type="org", surface="y"), 0)]
        )
        facade = SLMEntityExtractor(adapter=sentinel)
        result = await facade.extract_with_mentions("text")
        sentinel.extract_with_mentions.assert_awaited_once_with("text")
        assert result[0][0].name == "y"


# ---------------------------------------------------------------------------
# SLMIntentClassifier
# ---------------------------------------------------------------------------


class TestSLMIntentClassifier:
    def test_is_a_intent_classifier(self) -> None:
        """The facade IS-A IntentClassifier."""
        with _mocked_require_optional():
            facade = SLMIntentClassifier()
        assert isinstance(facade, IntentClassifier)

    def test_default_adapter_is_gliner(self) -> None:
        """No ``adapter=`` arg → facade instantiates
        ``GlinerIntentAdapter`` (delegates model load)."""
        with _mocked_require_optional():
            facade = SLMIntentClassifier()
        from kntgraph.knowledge.extraction import (
            GlinerIntentAdapter,
        )

        assert isinstance(facade._adapter, GlinerIntentAdapter)

    def test_default_model_from_settings(self) -> None:
        """``SLMIntentClassifier()`` reads the model from
        ``Settings.arg_extractor_model_id`` (default
        ``"gliner2-base"``)."""
        fresh_settings.cache_clear()
        with _mocked_require_optional():
            facade = SLMIntentClassifier()
        assert facade.model_name == "gliner2-base"
        fresh_settings.cache_clear()

    def test_env_override_changes_model(self, monkeypatch) -> None:
        """``KNT_ARG_EXTRACTOR_MODEL_ID`` propagates
        through the facade to the adapter."""
        monkeypatch.setenv(
            "KNT_ARG_EXTRACTOR_MODEL_ID",
            "urchen/gliner-multi-pii-base",
        )
        fresh_settings.cache_clear()
        with _mocked_require_optional():
            facade = SLMIntentClassifier()
        assert facade.model_name == ("urchen/gliner-multi-pii-base")
        fresh_settings.cache_clear()

    def test_explicit_adapter_wins(self) -> None:
        """Passing ``adapter=`` swaps the implementation."""
        sentinel = MagicMock(spec=IntentClassifier)
        sentinel.model_name = "injected-model"
        facade = SLMIntentClassifier(adapter=sentinel)
        assert facade._adapter is sentinel
        assert facade.model_name == "injected-model"

    @pytest.mark.asyncio
    async def test_classify_delegates(self) -> None:
        """``classify`` is a thin wrapper over the adapter."""
        sentinel = MagicMock(spec=IntentClassifier)
        sentinel.classify = AsyncMock(return_value="classification-sentinel")
        facade = SLMIntentClassifier(adapter=sentinel)
        result = await facade.classify("text", ["a", "b"])
        sentinel.classify.assert_awaited_once_with("text", ["a", "b"], None)
        assert result == "classification-sentinel"


# ---------------------------------------------------------------------------
# SLMArgumentExtractor
# ---------------------------------------------------------------------------


class TestSLMArgumentExtractor:
    def test_is_a_argument_extractor(self) -> None:
        """The facade IS-A ArgumentExtractor."""
        reg = _make_tool_registry()
        with _mocked_require_optional():
            facade = SLMArgumentExtractor(reg)
        assert isinstance(facade, ArgumentExtractor)

    def test_default_adapter_is_gliner(self) -> None:
        """No ``adapter=`` arg → facade instantiates
        ``GlinerArgumentAdapter``.

        Iter 27: the ``GlinerArgumentAdapter`` is now
        importable from the framework (``kntgraph
        .knowledge.extraction``) directly.

        Iter 28 follow-up: the vertical re-export
        shim is GONE. ``GlinerArgumentAdapter`` is
        only available from the framework path.
        """
        reg = _make_tool_registry()
        with _mocked_require_optional():
            facade = SLMArgumentExtractor(reg)
        from kntgraph.knowledge.extraction import (
            GlinerArgumentAdapter,
        )

        assert isinstance(facade._adapter, GlinerArgumentAdapter)

    def test_default_model_from_settings(self) -> None:
        """``SLMArgumentExtractor(reg)`` reads the model
        from ``Settings.arg_extractor_model_id``."""
        reg = _make_tool_registry()
        fresh_settings.cache_clear()
        with _mocked_require_optional():
            facade = SLMArgumentExtractor(reg)
        assert facade.model_name == "gliner2-base"
        fresh_settings.cache_clear()

    def test_env_override_changes_model(self, monkeypatch) -> None:
        """``KNT_ARG_EXTRACTOR_MODEL_ID`` propagates
        through the facade to the adapter."""
        reg = _make_tool_registry()
        monkeypatch.setenv(
            "KNT_ARG_EXTRACTOR_MODEL_ID",
            "urchen/gliner-multi-pii-base",
        )
        fresh_settings.cache_clear()
        with _mocked_require_optional():
            facade = SLMArgumentExtractor(reg)
        assert facade.model_name == ("urchen/gliner-multi-pii-base")
        fresh_settings.cache_clear()

    def test_explicit_adapter_wins(self) -> None:
        """Passing ``adapter=`` swaps the implementation."""
        reg = _make_tool_registry()
        sentinel = MagicMock(spec=ArgumentExtractor)
        sentinel.model_name = "injected-model"
        facade = SLMArgumentExtractor(reg, adapter=sentinel)
        assert facade._adapter is sentinel
        assert facade.model_name == "injected-model"

    @pytest.mark.asyncio
    async def test_extract_delegates(self) -> None:
        """``extract`` is a thin wrapper over the adapter."""
        reg = _make_tool_registry()
        sentinel = MagicMock(spec=ArgumentExtractor)
        sentinel.extract = AsyncMock(return_value="arg-extraction-sentinel")
        facade = SLMArgumentExtractor(reg, adapter=sentinel)
        result = await facade.extract("text", "dummy")
        sentinel.extract.assert_awaited_once_with("text", "dummy")
        assert result == "arg-extraction-sentinel"
