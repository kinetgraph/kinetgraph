# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for :class:`GlinerStructuredAdapter` (ADR-055 §2.4).

Behaviour-first: the adapter is exercised through its
public surface (construction + ``extract``). The
GLiNER2 model is **injected** via
``GlinerModelRegistry`` mocks so the unit tests stay
fast and CI-portable; the real model is exercised in
``tests/integration/knowledge/extraction/test_gliner_structured_extraction.py``
(ADR-055 §3.6).

Coverage bar (skill: kntgraph-testing §7.2):
  - every public function: happy path + at least one
    failure mode.

Coverage of this file:
  - construction resolves the model name (explicit vs.
    Settings fallback) and forwards kwargs to the
    registry.
  - ``extract`` delegates to the model, applies the
    threshold, and preserves ``__confidence_*`` keys
    when ``include_confidence=True``.
  - ``_normalise`` handles the GLiNER2 response shapes:
    dict-of-list-of-record, empty, non-dict, and the
    per-field ``{text, confidence}`` shape.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kntgraph.knowledge.extraction import GlinerStructuredAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_model() -> MagicMock:
    """Return a mock that quacks like a GLiNER2 model."""
    model = MagicMock(name="gliner2-model")
    model.extract_json = MagicMock(name="extract_json")
    return model


@pytest.fixture(autouse=True)
def _patched_registry(fake_model: MagicMock):
    """Replace ``GlinerModelRegistry.get`` with a patcher
    that returns ``fake_model``. The autouse fixture in
    ``tests/conftest.py`` already clears the cache
    between tests; this one goes a step further and
    patches ``get`` so the test does not depend on
    ``gliner2`` being importable."""
    with patch(
        "kntgraph.knowledge.extraction._gliner_model_registry.GlinerModelRegistry.get",
        return_value=fake_model,
    ) as mock_get:
        yield mock_get


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    """``GlinerStructuredAdapter.__init__`` resolves the
    model name and forwards kwargs to the registry."""

    def test_explicit_model_name_forwarded(self, _patched_registry) -> None:
        """Explicit ``model_name=`` wins over Settings."""
        adapter = GlinerStructuredAdapter(model_name="custom/gliner-x")
        assert adapter.model_name == "custom/gliner-x"
        _patched_registry.assert_called_once_with(
            "custom/gliner-x",
            device=None,
            cache_dir=None,
        )

    def test_settings_model_name_fallback(
        self, _patched_registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``None`` falls back to
        ``Settings.arg_extractor_model_id``."""
        monkeypatch.setenv("KNT_ARG_EXTRACTOR_MODEL_ID", "settings-model")
        from kntgraph.infra.config import fresh_settings

        fresh_settings.cache_clear()
        adapter = GlinerStructuredAdapter()
        assert adapter.model_name == "settings-model"
        fresh_settings.cache_clear()

    def test_device_and_cache_dir_forwarded(
        self, _patched_registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both ``device`` and ``Settings.model_cache_dir``
        propagate to the registry call."""
        monkeypatch.setenv("KNT_MODEL_CACHE_DIR", "/var/cache/models")
        from kntgraph.infra.config import fresh_settings

        fresh_settings.cache_clear()
        GlinerStructuredAdapter(
            model_name="gliner2-base",
            device="cuda",
        )
        _patched_registry.assert_called_once_with(
            "gliner2-base",
            device="cuda",
            cache_dir="/var/cache/models",
        )
        fresh_settings.cache_clear()


# ---------------------------------------------------------------------------
# extract — normalise path
# ---------------------------------------------------------------------------


class TestNormalise:
    """``extract`` flattens the GLiNER2 wrapper and
    applies the threshold."""

    @pytest.mark.asyncio
    async def test_empty_result_passes_through(self, fake_model: MagicMock) -> None:
        """``None`` from the upstream becomes ``[]``."""
        fake_model.extract_json = MagicMock(return_value=None)
        adapter = GlinerStructuredAdapter(model_name="m", include_confidence=True)
        out = await adapter.extract("hi", {"x": ["f::str"]})
        assert out == []

    @pytest.mark.asyncio
    async def test_extract_calls_model_extract_json(
        self, fake_model: MagicMock
    ) -> None:
        """``extract`` calls the model's ``extract_json``
        with ``include_confidence=True`` so the threshold
        can be applied post-hoc."""
        fake_model.extract_json = MagicMock(return_value={})
        adapter = GlinerStructuredAdapter(model_name="m")
        await adapter.extract("text", {"x": ["f::str"]})
        fake_model.extract_json.assert_called_once_with(
            "text",
            {"x": ["f::str"]},
            include_confidence=True,
        )

    @pytest.mark.asyncio
    async def test_threshold_drops_low_confidence_field(
        self, fake_model: MagicMock
    ) -> None:
        """A field whose confidence is below the threshold
        is dropped from the record."""
        fake_model.extract_json = MagicMock(
            return_value={
                "documento": [
                    {
                        "nome": {
                            "text": "Joao",
                            "confidence": 0.9,
                        },
                        "cpf": {
                            "text": "x",
                            "confidence": 0.1,  # below 0.5
                        },
                    }
                ]
            }
        )
        adapter = GlinerStructuredAdapter(
            model_name="m",
            field_threshold=0.5,
            include_confidence=True,
        )
        out = await adapter.extract("t", {"documento": ["n", "c"]})
        assert out == [
            {
                "nome": "Joao",
                "__confidence_nome": 0.9,
            }
        ]

    @pytest.mark.asyncio
    async def test_record_with_no_remaining_fields_is_dropped(
        self, fake_model: MagicMock
    ) -> None:
        """A record whose every field fell below the
        threshold is dropped entirely — the result
        stays a clean ``list[dict]`` rather than
        ``list[empty_dict]``."""
        fake_model.extract_json = MagicMock(
            return_value={
                "documento": [
                    {
                        "nome": {
                            "text": "?",
                            "confidence": 0.1,
                        },
                        "cpf": {
                            "text": "?",
                            "confidence": 0.1,
                        },
                    }
                ]
            }
        )
        adapter = GlinerStructuredAdapter(
            model_name="m", field_threshold=0.5, include_confidence=True
        )
        out = await adapter.extract("t", {"documento": ["n", "c"]})
        assert out == []

    @pytest.mark.asyncio
    async def test_multiple_structures_concatenated(
        self, fake_model: MagicMock
    ) -> None:
        """A multi-key schema returns the concatenation
        of every structure's records (defensive against a
        future backend that returns more than one key)."""
        fake_model.extract_json = MagicMock(
            return_value={
                "company": [{"name": "Acme"}],
                "person": [{"name": "Joao"}],
            }
        )
        adapter = GlinerStructuredAdapter(model_name="m")
        out = await adapter.extract(
            "t",
            {"company": ["name::str"], "person": ["name::str"]},
        )
        # Order matches the iteration order of the
        # upstream dict (preserved in Python 3.7+).
        assert out == [{"name": "Acme"}, {"name": "Joao"}]

    @pytest.mark.asyncio
    async def test_include_confidence_false_strips_siblings(
        self, fake_model: MagicMock
    ) -> None:
        """With ``include_confidence=False`` the returned
        records carry no ``__confidence_*`` keys —
        the adapter discards confidence entirely."""
        fake_model.extract_json = MagicMock(
            return_value={
                "documento": [
                    {
                        "nome": {"text": "Joao", "confidence": 0.9},
                    }
                ]
            }
        )
        adapter = GlinerStructuredAdapter(model_name="m", include_confidence=False)
        out = await adapter.extract("t", {"documento": ["nome::str"]})
        assert out == [{"nome": "Joao"}]

    @pytest.mark.asyncio
    async def test_non_dict_record_is_skipped(self, fake_model: MagicMock) -> None:
        """Defensive: a record that is not a dict (e.g.
        ``None``) is dropped, not propagated."""
        fake_model.extract_json = MagicMock(
            return_value={"documento": [None, {"nome": "Joao"}]}
        )
        adapter = GlinerStructuredAdapter(model_name="m")
        out = await adapter.extract("t", {"documento": ["nome::str"]})
        assert out == [{"nome": "Joao"}]


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    """The adapter's public properties expose the
    resolved configuration back to the facade and the
    Tool layer."""

    def test_model_name_reflects_resolution(self) -> None:
        adapter = GlinerStructuredAdapter(model_name="m")
        assert adapter.model_name == "m"

    def test_field_threshold_and_include_confidence_default(self) -> None:
        adapter = GlinerStructuredAdapter(model_name="m")
        assert adapter.field_threshold == 0.5
        assert adapter.include_confidence is False

    def test_field_threshold_and_include_confidence_overridden(self) -> None:
        adapter = GlinerStructuredAdapter(
            model_name="m",
            field_threshold=0.9,
            include_confidence=True,
        )
        assert adapter.field_threshold == 0.9
        assert adapter.include_confidence is True


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


def test_protocol_compliance() -> None:
    """``GlinerStructuredAdapter`` is a runtime-checkable
    :class:`StructuredExtractor` (the Protocol that the
    facade IS-A Protocol)."""
    from kntgraph.knowledge.extraction.base import (
        StructuredExtractor,
    )

    adapter = GlinerStructuredAdapter(model_name="m")
    assert isinstance(adapter, StructuredExtractor)


def test_uses_registry_not_direct_from_pretrained() -> None:
    """Regression guard: the source code MUST NOT call
    ``GLiNER2.from_pretrained`` directly (ADR-055 §2.2).
    It MUST go through ``GlinerModelRegistry.get``."""
    import inspect

    from kntgraph.knowledge.extraction import gliner_structured

    source = inspect.getsource(gliner_structured)
    assert ".from_pretrained(" not in source, (
        "GlinerStructuredAdapter still calls from_pretrained directly "
        "(ADR-055 regression)."
    )
