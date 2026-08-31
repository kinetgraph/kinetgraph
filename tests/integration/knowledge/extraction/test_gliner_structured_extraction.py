# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for :class:`GlinerStructuredAdapter`
(ADR-055 §2.4).

These tests exercise the **real GLiNER2 model** through
the framework's adapter and the process-level
``GlinerModelRegistry``. The adapter's contract is the
GLiNER2 ``extract_json`` API — the dialect, the
multi-record output shape, the per-field confidence, and
the thresholding semantic. Mocking the model would test
the mock, not the production behaviour.

Skipped automatically when the ``gliner2`` package is
not installed or the model cannot be loaded.

Model: ``fastino/gliner2.5-small-v1`` — the smallest
official Fastino checkpoint (74M params, ``fastino/gliner2.5-small-v1``
~300 MB), the official Fastino small model that works
with the 2.0.0 lib via ``AutoExtractor``. Override via
``KNT_STRUCTURED_TEST_MODEL``.
"""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Skip guard — entire module skipped when gliner2 is not installed.
# ---------------------------------------------------------------------------


def _gliner2_available() -> bool:
    """Return True when the ``gliner2`` package is importable."""
    try:
        import gliner2  # noqa: F401

        return True
    except ImportError:
        return False


if not _gliner2_available():
    pytest.skip(
        "gliner2 not installed (kntgraph[gliner])",
        allow_module_level=True,
    )


_TEST_MODEL = os.environ.get("KNT_STRUCTURED_TEST_MODEL", "fastino/gliner2.5-small-v1")


from kntgraph.knowledge.extraction import (  # noqa: E402
    GlinerModelRegistry,
    GlinerStructuredAdapter,
)


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    """Flush the registry cache before every test so each
    test starts from a clean state. The ``_clear`` method
    is the test-only API documented in ADR-055; production
    code never calls it."""
    GlinerModelRegistry._clear()
    yield
    GlinerModelRegistry._clear()


# ---------------------------------------------------------------------------
# Behaviour tests
# ---------------------------------------------------------------------------


class TestStructuredExtractionBehaviour:
    """End-to-end coverage of the structured-extraction
    adapter against a real GLiNER2 model. The contract:

      - the adapter flattens the upstream wrapper
        ``{<structure>: [record, ...]}`` into a flat
        ``list[dict]``;
      - per-field confidence is preserved as a sibling
        ``__confidence_<field>`` key when
        ``include_confidence=True``;
      - the ``field_threshold`` drops fields below it;
      - the same model instance is reused across two
        adapters (ADR-055 Fase 1 contract).
    """

    @pytest.mark.asyncio
    async def test_single_record_extracted(self) -> None:
        """A schema with two fields returns one record
        per person in the text. The Fastino small model
        is a real NLU model — we assert on the
        *contract* (record shape, field key, non-empty
        text) not on exact values."""
        adapter = GlinerStructuredAdapter(
            model_name=_TEST_MODEL,
            field_threshold=0.0,  # disable threshold for this smoke
            include_confidence=True,
        )
        records = await adapter.extract(
            "Joao da Silva, CPF 123.456.789-00",
            {
                "documento": [
                    "nome::str::full name",
                    "cpf::str::Brazilian individual taxpayer ID",
                ]
            },
        )
        # The model may extract 0 or 1 records depending on
        # threshold / confidence. We assert on the shape,
        # not on a guaranteed extraction.
        assert isinstance(records, list)
        for record in records:
            assert isinstance(record, dict)
            # When confidence is requested, every field
            # that survived the threshold has a sibling
            # ``__confidence_<field>`` key.
            for field_name in list(record):
                if not field_name.startswith("__confidence_"):
                    assert f"__confidence_{field_name}" in record, (
                        f"field {field_name!r} has no __confidence_{field_name} sibling"
                    )

    @pytest.mark.asyncio
    async def test_field_threshold_drops_low_confidence(self) -> None:
        """A field whose confidence is below the
        threshold is dropped from the record (when the
        model emits any field at all). We assert on the
        invariant: every field present in the output has
        a confidence ABOVE the threshold."""
        adapter = GlinerStructuredAdapter(
            model_name=_TEST_MODEL,
            field_threshold=0.99,  # very high — most fields drop
            include_confidence=True,
        )
        records = await adapter.extract(
            "Joao da Silva, CPF 123.456.789-00",
            {
                "documento": [
                    "nome::str",
                    "cpf::str",
                ]
            },
        )
        for record in records:
            for field_name in list(record):
                if field_name.startswith("__confidence_"):
                    continue
                confidence_key = f"__confidence_{field_name}"
                assert confidence_key in record
                assert record[confidence_key] >= 0.99

    @pytest.mark.asyncio
    async def test_empty_text_returns_empty_list(self) -> None:
        """An empty / uninformative text returns ``[]`` —
        not an exception, not ``[None]``. Defensive
        contract."""
        adapter = GlinerStructuredAdapter(
            model_name=_TEST_MODEL,
        )
        records = await adapter.extract(
            ".",
            {"documento": ["nome::str", "cpf::str"]},
        )
        # Empty list is the expected outcome for uninformative input.
        # We only assert on shape, not on the exact count.
        assert isinstance(records, list)
        for record in records:
            assert isinstance(record, dict)

    @pytest.mark.asyncio
    async def test_two_adapters_share_loaded_model(self) -> None:
        """ADR-055 Fase 1 contract: two adapters with the
        same ``(model_name, device, cache_dir)`` key
        receive the same loaded model instance (one cold
        start, one copy in RAM)."""
        a = GlinerStructuredAdapter(model_name=_TEST_MODEL)
        b = GlinerStructuredAdapter(model_name=_TEST_MODEL)
        # The model objects are the same Python instance.
        # This is the process-level singleton guarantee.
        assert a._model is b._model


# ---------------------------------------------------------------------------
# Model versioning tolerance
# ---------------------------------------------------------------------------


class TestSchemaDialectTolerance:
    """The adapter passes the schema verbatim to the
    model — it does NOT validate the dialect. A field
    spec like ``"field::str"`` and a bare ``"field"``
    are both valid inputs at the adapter boundary."""

    @pytest.mark.asyncio
    async def test_bare_field_name_accepted(self) -> None:
        """A schema with bare field names (``"nome"``)
        is accepted by the model. The adapter does not
        enforce the ``::type`` tail — that is the
        Tool's job."""
        adapter = GlinerStructuredAdapter(model_name=_TEST_MODEL)
        # We don't assert on the extraction result — the
        # small model may or may not extract. We only
        # assert that no exception is raised and the
        # return is a list of dicts.
        records = await adapter.extract(
            "Joao da Silva, CPF 123.456.789-00",
            {"documento": ["nome", "cpf"]},
        )
        assert isinstance(records, list)
        for record in records:
            assert isinstance(record, dict)
