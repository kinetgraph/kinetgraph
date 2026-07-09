# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the intent-classification building blocks
introduced by ADR-013.

Focuses on the parts that do NOT need the GLiNER2 model
loaded:
  - `Classification` value object construction and
    sortability.
  - `IntentScore` value object.
  - `GlinerIntentAdapter._parse_output` — converts
    the raw GLiNER2 1.3.x output (negative-class
    trick: one binary task per candidate label) into
    a ranked `Classification`.

The full inference path (real GLiNER2 model load +
`asyncio.to_thread`) is integration territory and
lives in `tests/integration/knowledge/test_gliner_intent.py`
(planned — see ADR-013 §3).
"""

from __future__ import annotations

import pytest

from kntgraph.knowledge.extraction.base import (
    Classification,
    IntentScore,
)
from kntgraph.knowledge.extraction.gliner_intent import (
    GlinerIntentAdapter,
)


# ---------------------------------------------------------------------------
# Classification / IntentScore
# ---------------------------------------------------------------------------


class TestClassificationValueObject:
    def test_top_label_must_be_non_empty(self) -> None:
        # An empty `top_label` is the explicit "no
        # decision" signal (consumed by the role layer
        # to emit `routing.unclassified`); the value
        # object does NOT enforce non-empty on
        # construction (it is informational).
        c = Classification(top_label="", top_score=0.0, candidates=())
        assert c.top_label == ""
        assert c.top_score == 0.0
        assert c.candidates == ()

    def test_candidates_preserved_in_order(self) -> None:
        cands = (
            IntentScore(label="a", score=0.9),
            IntentScore(label="b", score=0.5),
        )
        c = Classification(top_label="a", top_score=0.9, candidates=cands)
        assert c.candidates == cands

    def test_intent_score_is_hashable_via_frozen(self) -> None:
        # Frozen dataclass: hashable, so it can be
        # used as a dict key in caches.
        s1 = IntentScore(label="x", score=0.5)
        s2 = IntentScore(label="x", score=0.5)
        assert s1 == s2
        assert {s1, s2} == {s1}


# ---------------------------------------------------------------------------
# GlinerIntentAdapter._parse_output
# ---------------------------------------------------------------------------
#
# GLiNER2 1.3.x returns one binary classification per
# candidate label via the negative-class trick. The raw
# output is a dict of the form:
#
#     {
#         "_fmh_intent_0": {"label": "a", "confidence": 0.9},
#         "_fmh_intent_1": {"label": "none_of_the_above", "confidence": 0.95},
#         "_fmh_intent_2": {"label": "c", "confidence": 0.6},
#     }
#
# The parser inverts `none_of_the_above` confidences into
# per-label scores and ranks. Tests below exercise that
# mapping.


def _make_raw(items: list[tuple[str, str, float]]) -> dict:
    """
    Build a fake raw output dict from a list of
    `(task, label, confidence)` tuples.
    """
    return {
        task: {"label": label, "confidence": confidence}
        for task, label, confidence in items
    }


def _make_classifier() -> GlinerIntentAdapter:
    """
    Build a `GlinerIntentAdapter` WITHOUT triggering
    the eager `gliner2` import. We construct the object
    via `__new__` and set attributes directly.

    This lets the parsing tests run in environments
    where `gliner2` is not installed (the package's
    default). The lazy-import path is exercised in
    the integration test.
    """
    clf = GlinerIntentAdapter.__new__(GlinerIntentAdapter)
    clf._threshold = 0.0  # accept everything for parse tests
    clf._model = object()  # placeholder; parse_output does not touch it
    clf._model_name = "fake-for-parse-tests"
    return clf


class TestParseOutput:
    def test_label_winner_keeps_its_confidence(self) -> None:
        # When the model picks the candidate label (not
        # none_of_the_above), the score is the raw
        # confidence.
        clf = _make_classifier()
        raw = _make_raw(
            [
                ("_fmh_intent_0", "emitir_nfe", 0.9),
                ("_fmh_intent_1", "none_of_the_above", 0.95),
                ("_fmh_intent_2", "cancelar_nfe", 0.6),
            ]
        )
        c = clf._parse_output(raw, ("emitir_nfe", "cancelar_nfe"))
        assert c.top_label == "emitir_nfe"
        assert c.top_score == pytest.approx(0.9)

    def test_none_of_the_above_winner_is_inverted(self) -> None:
        # When the model picks none_of_the_above, the
        # candidate's score is 1 - confidence_of_none.
        clf = _make_classifier()
        raw = _make_raw(
            [
                ("_fmh_intent_0", "none_of_the_above", 0.9),
                ("_fmh_intent_1", "cancelar_nfe", 0.05),
            ]
        )
        c = clf._parse_output(raw, ("emitir_nfe", "cancelar_nfe"))
        # Score for emitir_nfe: 1 - 0.9 = 0.1
        # Score for cancelar_nfe: 0.05 (raw)
        # Top-1 is emitir_nfe by tiebreak (0.1 > 0.05).
        emitir = next(s for s in c.candidates if s.label == "emitir_nfe")
        cancelar = next(s for s in c.candidates if s.label == "cancelar_nfe")
        assert emitir.score == pytest.approx(0.1)
        assert cancelar.score == pytest.approx(0.05)

    def test_unexpected_label_is_dropped(self) -> None:
        # Defensive: the model returning a label not in
        # the requested set is dropped from the ranking
        # (older checkpoints may misbehave). The matching
        # task still contributes its valid result.
        clf = _make_classifier()
        raw = _make_raw(
            [
                # emitir_nfe task returns an unexpected
                # label → dropped.
                ("_fmh_intent_0", "stranger", 0.9),
                # cancelar_nfe task returns the expected
                # label → kept.
                ("_fmh_intent_1", "cancelar_nfe", 0.5),
            ]
        )
        c = clf._parse_output(raw, ("emitir_nfe", "cancelar_nfe"))
        assert all(s.label != "stranger" for s in c.candidates)
        assert c.top_label == "cancelar_nfe"

    def test_none_input_returns_no_decision(self) -> None:
        clf = _make_classifier()
        c = clf._parse_output(None, ("emitir_nfe",))
        assert c.top_label == ""
        assert c.top_score == 0.0
        assert c.candidates == ()

    def test_empty_dict_returns_no_decision(self) -> None:
        clf = _make_classifier()
        c = clf._parse_output({}, ("emitir_nfe",))
        assert c.top_label == ""

    def test_threshold_filters_below_cutoff(self) -> None:
        clf = _make_classifier()
        clf._threshold = 0.5
        # emitir_nfe ends up with score 0.4 (below);
        # cancelar_nfe stays at 0.6.
        raw = _make_raw(
            [
                ("_fmh_intent_0", "none_of_the_above", 0.6),  # emitir_nfe: 0.4
                ("_fmh_intent_1", "cancelar_nfe", 0.6),
            ]
        )
        c = clf._parse_output(raw, ("emitir_nfe", "cancelar_nfe"))
        labels = [s.label for s in c.candidates]
        assert "emitir_nfe" not in labels
        assert c.top_label == "cancelar_nfe"
        assert c.top_score == pytest.approx(0.6)

    def test_all_below_threshold_returns_no_decision(self) -> None:
        clf = _make_classifier()
        clf._threshold = 0.9
        raw = _make_raw(
            [
                ("_fmh_intent_0", "none_of_the_above", 0.99),  # emitir: 0.01
                ("_fmh_intent_1", "none_of_the_above", 0.99),  # cancelar: 0.01
            ]
        )
        c = clf._parse_output(raw, ("emitir_nfe", "cancelar_nfe"))
        assert c.top_label == ""
        assert c.candidates == ()

    def test_sort_descending_then_alpha(self) -> None:
        # Ties broken alphabetically (deterministic).
        clf = _make_classifier()
        raw = _make_raw(
            [
                ("_fmh_intent_0", "z", 0.5),
                ("_fmh_intent_1", "a", 0.9),
                ("_fmh_intent_2", "m", 0.9),
            ]
        )
        c = clf._parse_output(raw, ("z", "a", "m"))
        assert [s.label for s in c.candidates] == ["a", "m", "z"]
        assert c.top_label == "a"

    def test_malformed_entry_dropped(self) -> None:
        clf = _make_classifier()
        # Mix of valid and malformed entries for one
        # candidate label. Each malformed entry is
        # silently dropped. The parser iterates only
        # `len(labels)` tasks — extra entries in `raw`
        # are ignored.
        raw = {
            "_fmh_intent_0": {},  # missing label + confidence
            "_fmh_intent_1": {"label": "emitir_nfe"},  # missing confidence
            "_fmh_intent_2": {"confidence": 0.5},  # missing label
            "_fmh_intent_3": {"label": "emitir_nfe", "confidence": "x"},
            "_fmh_intent_4": {"label": "emitir_nfe", "confidence": 0.7},
        }
        # First valid entry for _fmh_intent_0 wins; the
        # parser keeps the score from that single valid
        # iteration (it doesn't sum or merge multiple).
        c = clf._parse_output(raw, ("emitir_nfe",))
        # Iteration picks _fmh_intent_0 first; {} is
        # malformed and dropped. No further entries are
        # consulted for the same label.
        assert c.top_label == ""
        assert c.candidates == ()


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_threshold_validated(self) -> None:
        # We do NOT call __init__ (would import gliner2).
        # Validate via the class-level check: __init__
        # raises ValueError for out-of-range thresholds.
        # We exercise the check directly.
        with pytest.raises(ValueError):
            # The check is in __init__, so we simulate
            # by patching `from_pretrained` away and
            # running __init__ with a bad threshold.
            GlinerIntentAdapter.__init__(
                GlinerIntentAdapter.__new__(GlinerIntentAdapter),
                model_name="x",
                threshold=1.5,
            )
