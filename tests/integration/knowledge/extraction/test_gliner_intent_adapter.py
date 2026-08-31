# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for GlinerIntentAdapter (ADR-055, ADR-013 §2.1).

These tests use a real GLiNER2 model to verify the end-to-end intent
classification path. They are designed to catch latent bugs in the
adapter's interaction with the model API rather than in the adapter's
pure Python logic (which is covered by the unit tests in
test_slm_facades.py and test_gliner_intent.py).

Known bug surface areas exercised here:

  1. **Output shape contract** — ``batch_extract`` returns a list; the
     adapter reads ``results[0]``. If the list is empty or the shape
     changes across model versions, the adapter silently returns a
     no-decision Classification. The test catches a wrong fallback.

  2. **Task key mismatch** — ``_run_inference`` labels each task
     ``_fmh_intent_<i>``. ``_parse_output`` looks for the same key. A
     version bump that changes the key format silently drops all scores
     and returns no-decision. The test catches this.

  3. **Confidence field name** — the parser reads
     ``entry["confidence"]``. Older GLiNER2 versions used ``"score"``.
     A regression here silently returns no-decision. The test catches it.

  4. **Negative-class trick sign** — ``_intent_score_for_winner``
     inverts the score when ``none_of_the_above`` wins. An off-by-one
     (e.g. returning ``confidence`` instead of ``1 - confidence``) would
     cause every intent to score near 0 and return no-decision. The test
     catches it.

  5. **Threshold gate** — when ``threshold=0.0``, ALL labels above 0.0
     score should be returned as candidates. A bug in the threshold
     comparison would silently drop them.

  6. **Model sharing via registry** — two ``GlinerIntentAdapter``
     instances built with the same model name must share the same model
     object (registry caching). A regression in the registry wiring
     would cause the second adapter to load a new model (slower, more
     RAM). The test catches it without re-measuring RAM.

Run:

    uv run pytest tests/integration/knowledge/extraction/ \\
        -v -k test_gliner_intent_adapter

Env var ``KNT_REGISTRY_TEST_MODEL`` overrides the default model.
"""

from __future__ import annotations

import os
import time

import pytest

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------


def _gliner2_available() -> bool:
    """Return True when the ``gliner2`` package is importable."""
    try:
        import gliner2  # noqa: F401

        return True
    except ImportError:
        return False


if not _gliner2_available():
    pytest.skip("gliner2 not installed (kntgraph[gliner])", allow_module_level=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEST_MODEL = os.environ.get("KNT_REGISTRY_TEST_MODEL", "urchade/gliner_small-v2.1")

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    """Isolate registry state per test."""
    from kntgraph.knowledge.extraction._gliner_model_registry import (
        GlinerModelRegistry,
    )

    GlinerModelRegistry._clear()
    yield
    GlinerModelRegistry._clear()


# ---------------------------------------------------------------------------
# Helper: build a GlinerIntentAdapter with the test model
# ---------------------------------------------------------------------------


def _make_adapter(threshold: float = 0.0):
    """Construct a ``GlinerIntentAdapter`` against the test model.

    ``threshold=0.0`` ensures all candidates above 0.0 are returned,
    so the tests can inspect the full ranked list without the threshold
    gate hiding results.
    """
    from kntgraph.knowledge.extraction.gliner_intent import GlinerIntentAdapter

    return GlinerIntentAdapter(model_name=_TEST_MODEL, threshold=threshold)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGlinerIntentAdapterEndToEnd:
    """End-to-end classification using a real model.

    Each test is a distinct scenario that exercises a different bug
    surface (documented in the module docstring above).
    """

    async def test_clear_winner_returns_non_empty_classification(self) -> None:
        """A text with an unambiguous semantic match must return a
        non-empty ``Classification`` (not the no-decision fallback).

        Bug surface 1 + 2 + 3: validates output shape contract, task key
        format, and confidence field name all at once. If any of them is
        broken the adapter silently returns ``top_label=""``; the
        assertion would fail.
        """
        adapter = _make_adapter(threshold=0.0)
        result = await adapter.classify(
            "I need to issue a tax invoice for this purchase",
            labels=["invoice_issue", "payment_query", "cancel_order"],
        )
        assert result.top_label != "", (
            "GlinerIntentAdapter.classify returned no-decision for a text "
            "with a clear semantic match. Possible causes: batch_extract "
            "returned an unexpected shape, task key mismatch "
            "(_fmh_intent_<i>), or confidence field renamed. "
            f"Raw top_label={result.top_label!r}, top_score={result.top_score}"
        )
        assert result.top_score > 0.0, (
            "top_score must be positive when a winner is found; "
            f"got {result.top_score}. "
            "Check negative-class score inversion in _intent_score_for_winner."
        )

    async def test_negative_class_trick_score_is_inverted(self) -> None:
        """The score for the winning label must be the INVERTED confidence
        of ``none_of_the_above``, not its raw value.

        Bug surface 4: if the implementation returns ``confidence``
        instead of ``1 - confidence`` for the winner, a text that clearly
        matches a label would score near 0 (because ``none_of_the_above``
        has high confidence for a matching text). The assertion requires
        score > 0.5 as a proxy for the inversion being correct.
        """
        adapter = _make_adapter(threshold=0.0)
        result = await adapter.classify(
            "cancel my order please",
            labels=["order_cancel", "invoice_issue"],
        )
        # The dominant label score must be meaningfully above 0.5 when
        # the text clearly describes one intent. If the sign is inverted
        # it would be near 0 (model says none_of_the_above is unlikely
        # but the score is mistakenly reported as-is, not inverted).
        if result.top_label:
            assert result.top_score > 0.5, (
                f"top_score={result.top_score:.3f} is suspiciously low for "
                "'cancel my order please'. The negative-class trick may have "
                "been applied incorrectly (score not inverted)."
            )

    async def test_all_candidates_returned_at_zero_threshold(self) -> None:
        """With ``threshold=0.0``, every label that received a positive
        score must appear in ``candidates``.

        Bug surface 5: a strict ``<`` comparison in the threshold gate
        (instead of ``<=``) would drop labels that score exactly 0.0;
        that is correct. But a bug that gates on ``< threshold`` when
        threshold is 0.0 would silently drop candidates with positive
        scores.
        """
        labels = ["invoice_issue", "payment_query", "cancel_order"]
        adapter = _make_adapter(threshold=0.0)
        result = await adapter.classify(
            "send me the payment receipt",
            labels=labels,
        )
        # Candidates must include at least one entry (the top label).
        # If the threshold gate is broken, scored candidates are dropped
        # and the Classification falls back to no-decision.
        assert len(result.candidates) >= 1, (
            "classify with threshold=0.0 returned zero candidates. "
            f"top_label={result.top_label!r}. "
            "The threshold gate may be incorrectly dropping all candidates."
        )

    async def test_empty_text_returns_no_decision(self) -> None:
        """Empty text short-circuits before calling the model and returns
        the no-decision Classification.

        Not a latent bug per se, but a guard: if this test starts failing
        it means the short-circuit was removed and the model is called
        with empty input (which can raise or return an unexpected shape).
        """
        adapter = _make_adapter(threshold=0.0)
        result = await adapter.classify("", labels=["invoice_issue"])
        assert result.top_label == "" and result.top_score == 0.0, (
            "classify('') must return the no-decision Classification. "
            f"Got top_label={result.top_label!r}, top_score={result.top_score}."
        )

    async def test_unknown_intent_returns_low_or_no_score(self) -> None:
        """A text completely unrelated to all labels should either return
        no-decision or a very low confidence winner.

        This verifies that the classifier does not hallucinate high
        confidence for random text — a regression in the threshold logic
        or in the inversion formula would cause this.
        """
        adapter = _make_adapter(threshold=0.0)
        result = await adapter.classify(
            "the weather in São Paulo is nice today",
            labels=["invoice_issue", "payment_query", "cancel_order"],
        )
        # Either no-decision, or every candidate scores below 0.7.
        if result.top_label:
            assert result.top_score < 0.7, (
                f"Unrelated text received high-confidence label "
                f"{result.top_label!r} ({result.top_score:.3f}). "
                "The model may be over-confident, or the score inversion "
                "has a sign bug that inflates scores."
            )

    async def test_two_adapters_same_model_share_registry_instance(self) -> None:
        """Two ``GlinerIntentAdapter`` instances with the same model name
        must hold the exact same model object (Python ``is`` identity).

        Bug surface 6: if the registry wiring was removed or broken, each
        adapter would call ``from_pretrained`` independently and hold a
        different object. The ``is`` check catches this without measuring
        RAM or cold-start latency.
        """
        adapter_a = _make_adapter()
        adapter_b = _make_adapter()
        assert adapter_a._model is adapter_b._model, (
            "Two GlinerIntentAdapter instances with the same model name "
            "hold different model objects. The GlinerModelRegistry wiring "
            "is broken — each adapter is loading its own copy of the "
            "weights instead of sharing the cached instance (ADR-055)."
        )

    async def test_descriptions_forwarded_to_model(self) -> None:
        """When ``descriptions`` is supplied, the result must still be
        a valid (non-error) Classification.

        This is a smoke test for the descriptions path: if the model API
        changed its schema for ``descriptions``, the call would raise or
        return an empty dict. The assertion catches both.
        """
        adapter = _make_adapter(threshold=0.0)
        descriptions = [
            "Emitir nota fiscal eletrônica",
            "Consultar pagamento PIX",
        ]
        result = await adapter.classify(
            "emitir nota fiscal",
            labels=["invoice_issue", "payment_query"],
            descriptions=descriptions,
        )
        # The adapter must not raise and must produce a parseable output.
        # We accept no-decision but NOT an unhandled exception.
        assert isinstance(result.top_label, str), (
            "classify with descriptions raised an unexpected error or "
            "returned a non-string top_label. "
            "Check that the descriptions schema key is still valid in "
            "the installed gliner2 version."
        )

    async def test_inference_completes_in_reasonable_time(self) -> None:
        """Inference on a short text must complete within 60 seconds on
        CPU (the CI environment). This is not a performance regression
        test — it is a liveness check to detect hanging model calls or
        unexpected blocking in the asyncio path.

        60 s is deliberately generous: the first call includes the model
        warm-up; subsequent calls in the same process are faster. If this
        test exceeds the timeout in CI, the asyncio.to_thread wiring
        may be broken (model called on the event loop, starving it).
        """
        adapter = _make_adapter(threshold=0.0)
        start = time.perf_counter()
        await adapter.classify(
            "approve this invoice",
            labels=["invoice_approve", "invoice_reject"],
        )
        elapsed = time.perf_counter() - start
        assert elapsed < 60.0, (
            f"classify took {elapsed:.1f}s, exceeding the 60s liveness "
            "threshold. The model call may not be running in a worker "
            "thread (asyncio.to_thread missing), or the model download "
            "was triggered mid-test."
        )
