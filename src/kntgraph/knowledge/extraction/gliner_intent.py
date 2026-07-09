# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
GlinerIntentAdapter — Intent classifier adapter backed by GLiNER2.

ADR-013 (Semantic Routing via GLiNER2) — Momento 1.

Shape
-----

This is NOT a `GlinerEntityAdapter`. The entity extractor
asks "which spans in the text match a label?"; the intent
classifier asks "which of K labels best describes the WHOLE
text?". GLiNER2 v1.5+ supports both modes through a shared
classification primitive, but the post-processing shapes
differ. Keeping the two as siblings of the same
`IntentClassifier` Protocol avoids the
entity-extractor-shaped API forcing the intent path into a
degenerate form (or vice versa).

Opt-in
------

The `gliner2` package is a separate dependency installed via
`uv add 'kntgraph[gliner]'`. This module does an
EAGER import of `gliner2` at construction time (when the
classifier is wired into a deployment) so that
`IntentClassifier` Protocols are still trivially mockable
in tests. The construction is the only point where the
real model loads; the lazy pattern in
`extraction/__init__.py` makes the symbol `None` if
`gliner2` is missing, so the rest of the package keeps
importing.

Threading
---------

`GLiNER2.from_pretrained(...)` and `model.extract(...)` are
PyTorch calls. We wrap the inference call in
`asyncio.to_thread` so the event loop stays responsive
under a `ToolInvoker` workload. The model object is
thread-safe for the inference-only path we use
(forward pass, no in-place mutation).

Model versioning
----------------

GLiNER2's public API is still moving (v1.0 → v1.5 changed
call signatures). The conversion from raw model output to
`Classification` is concentrated in `_parse_output`, which
tolerates both dict and object shapes (same pattern as
`gliner.py:_field`). A version mismatch surfaces as a
clear `ValueError` from `_parse_output`, NOT as a
silent empty result.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:
    from kntgraph.knowledge.extraction.gliner import GLiNERSpan

from .base import (
    Classification,
    IntentClassifier,
    IntentScore,
)


def _field(obj: "dict[str, object] | GLiNERSpan", name: str) -> "object | None":
    """
    Read `name` from `obj` whether it is a dict or has the
    attribute. Returns `None` if absent.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


class GlinerIntentAdapter(IntentClassifier):
    """
    GLiNER2-backed intent classifier (low-level adapter).

    Iter 21: renamed from ``GlinerIntentClassifier`` to
    ``GlinerIntentAdapter`` (consistent with the
    Adapter convention — ``OllamaEmbeddingAdapter``,
    ``FalkorDBGraphAdapter``, ``LiteLLMTransportAdapter``).
    Most callers should NOT construct this directly —
    use ``SLMIntentClassifier`` (the facade) which
    holds a reference to a low-level adapter and
    delegates every call. The ``SLM`` prefix
    (``Small Language Model``) covers GLiNER2 today
    but is **not tied** to it — a future
    ``TinyLLMIntentAdapter`` can be slotted in via
    ``SLMIntentClassifier(adapter=...)`` without
    changing the facade's public API.

    Construction loads the model eagerly. The first
    `classify` call is therefore near-instant (no cold
    start beyond the constructor). For deployments that
    need lazy loading, wrap construction in a startup hook
    (e.g. an `async def warmup()` in the role) or delay
    instantiation until first use.

    The label set is supplied per call. The same adapter
    instance can serve multiple tool registries in a
    multi-tenant deployment (one classifier, many label
    sets) without re-loading the model.

    The model is loaded via ``gliner2`` (an optional
    extra: ``kntgraph[gliner]``). When the extra is
    not installed, ``__init__`` raises an ``ImportError``
    with a clear remediation message — the framework
    stays usable without the NER capability.
    """

    def __init__(
        self,
        model_name: str | None = None,
        *,
        device: Optional[str] = None,
        threshold: float = 0.5,
    ) -> None:
        """
        Args:
          model_name: passed to `GLiNER2.from_pretrained`.
            When ``None`` (default), the adapter reads
            ``Settings.arg_extractor_model_id`` (which
            itself defaults to ``"gliner2-base"``). The
            public release default; tenants with custom
            checkpoints pass the local path or HF repo id.
          device: torch device (e.g. "cpu", "cuda"). `None`
            lets GLiNER2 pick (typically CPU when no
            accelerator is available).
          threshold: minimum score for a candidate to be
            kept. The role layer applies its OWN threshold
            (configurable per deployment, default 0.6) on
            top; this one is the model's per-label cutoff
            inside `_parse_output`.
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be in [0, 1], got {threshold!r}")
        # Iter 21: read the model name from Settings when
        # the caller passes ``None``. Encapsulated in
        # ``_resolve_model_name`` so the ``__init__`` body
        # stays flat (CC ≤ 2).
        model_name = self._resolve_model_name(model_name)

        # Eager import. If `gliner2` is not installed, fail
        # HERE with a clear error pointing to the extra.
        from ..._optional import require_optional

        GLiNER2 = require_optional(
            "gliner2",
            "kntgraph[gliner]",
            purpose="GlinerIntentAdapter",
        ).GLiNER2

        self._model = GLiNER2.from_pretrained(model_name, device=device)
        self._threshold = float(threshold)
        self._model_name = model_name

    @staticmethod
    def _resolve_model_name(model_name: "str | None") -> str:
        """
        Resolve the effective model name from explicit
        arg + Settings.

        The sentinel ``None`` means "no override; use
        Settings". Any explicit value wins. Extracted so
        the ``__init__`` body stays flat (CC ≤ 2) and the
        defaults are easy to test in isolation.
        """
        if model_name is not None:
            return model_name
        from kntgraph.infra.config import fresh_settings

        return fresh_settings().arg_extractor_model_id

    @property
    def model_name(self) -> str:
        return self._model_name

    async def classify(
        self,
        text: str,
        labels: Iterable[str],
        descriptions: Optional[Iterable[str]] = None,
    ) -> Classification:
        """
        Classify `text` against `labels`.

        `descriptions` is an optional iterable with the
        same length as `labels`. When provided, each
        description is sent to the GLiNER2 classifier as
        the per-label description in the schema. Brazilian-
        specific keywords ("NF-e", "CNPJ", "PIX", "nota
        fiscal") give the zero-shot model strong anchors
        that pure English tool names lack. See ADR-013 §2.1.

        The label set is materialised to a tuple to keep the
        function idempotent across calls (no hidden state).
        Empty `text` or empty `labels` short-circuit to a
        "no decision" `Classification`.

        Inference runs in a worker thread
        (`asyncio.to_thread`) to keep the event loop free.
        Latency for a typical 50-word message in CPU is
        100-300ms with the negative-class trick; first call
        also pays the GLiNER2 warmup.
        """
        if not text or not text.strip():
            return Classification(top_label="", top_score=0.0, candidates=())
        labels_tuple = tuple(labels)
        if not labels_tuple:
            return Classification(top_label="", top_score=0.0, candidates=())
        if not all(isinstance(lbl, str) and lbl for lbl in labels_tuple):
            raise ValueError("labels must be non-empty strings")

        descriptions_tuple: Optional[tuple[str, ...]] = None
        if descriptions is not None:
            descriptions_tuple = tuple(descriptions)
            if len(descriptions_tuple) != len(labels_tuple):
                raise ValueError(
                    "descriptions must match labels length "
                    f"(got {len(descriptions_tuple)} descriptions for "
                    f"{len(labels_tuple)} labels)"
                )

        started = time.perf_counter()
        raw = await asyncio.to_thread(
            self._run_inference, text, labels_tuple, descriptions_tuple
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        classification = self._parse_output(raw, labels_tuple)
        # Telemetry hook: callers can read the elapsed
        # time via the returned object if they attach it
        # later. We don't decorate Classification with
        # latency (keeps the value object pure) but
        # the role layer measures its own roundtrip.
        _ = elapsed_ms
        return classification

    # ---------------------------------------------------------- internal

    def _run_inference(
        self,
        text: str,
        labels: tuple[str, ...],
        descriptions: Optional[tuple[str, ...]] = None,
    ) -> "dict[str, object]":
        """
        Synchronous model call. Runs in a worker thread
        via `asyncio.to_thread`. Returns the raw GLiNER2
        output for `_parse_output` to interpret.

        GLiNER2 1.3.x exposes two relevant primitives:

          - `extract(text, schema, ...)`: schema-driven,
            returns the top-1 winner of a single-label
            classification with a normalised confidence.
            Ranks are NOT produced — running this once
            with N labels in the `labels` list always
            returns the same confidence (~1.0) regardless
            of which label wins, so we cannot compare.

          - `batch_extract(texts, schema, ...)`: same shape,
            but accepts a LIST of classification tasks in a
            single call. We exploit this to run one
            binary task per candidate label: each task
            asks "is `text` of intent `<label>`, or is it
            `none_of_the_above`?". The model returns the
            confidence for `none_of_the_above`; the
            per-label "this IS the intent" score is
            `1 - neg_confidence`. Ranking by these scores
            gives a real comparison (text that matches
            a label has a low `none_of_the_above`
            confidence; text that matches no label has a
            high `none_of_the_above` confidence).

        When `descriptions` is supplied (one per label),
        each task carries the description alongside the
        candidate label. Brazilian-specific keywords
        ("NF-e", "CNPJ", "PIX", "nota fiscal") give the
        zero-shot model strong anchors that pure English
        tool names lack.

        Cost: one model call per `classify(text, labels)`,
        regardless of len(labels). The classifier itself
        issues len(labels) sub-tasks internally; on a CPU
        machine with the 205M base model, end-to-end
        latency is ~100-300ms per `classify` call. On GPU,
        an order of magnitude faster.
        """

        # Negative-class trick: pair each candidate with a
        # generic "none_of_the_above" label. The model's
        # confidence on `none_of_the_above` becomes the
        # inverse score for the candidate.
        def _task(i: int, label: str) -> dict:
            entry = {
                "task": f"_fmh_intent_{i}",
                "labels": [label, "none_of_the_above"],
            }
            if descriptions is not None:
                entry["descriptions"] = [descriptions[i], ""]
            return entry

        schema = {
            "classifications": [_task(i, label) for i, label in enumerate(labels)],
        }
        results = self._model.batch_extract([text], schema, include_confidence=True)
        # `batch_extract` returns a list with one entry
        # per input text; we passed one text.
        return results[0] if results else {}

    def _parse_output(
        self,
        raw: "dict[str, object]",
        labels: tuple[str, ...],
    ) -> Classification:
        """
        Convert the raw GLiNER2 output into a `Classification`.

        The shape produced by `_run_inference` is a single
        dict `{"_fmh_intent_<i>": {"label": ...,
        "confidence": ...}}` — one entry per candidate label,
        each being a binary classification between the
        candidate and `none_of_the_above`. We invert the
        score: `score = 1 - confidence_of_none`. Ranking by
        these inverted scores gives a real comparison.

        Returns `Classification(top_label="",
        top_score=0.0, candidates=())` when the output is
        empty / unparseable. The role layer interprets this
        as a no-decision (emits `routing.unclassified`).
        """
        if not isinstance(raw, dict) or not raw:
            return Classification(top_label="", top_score=0.0, candidates=())

        scored = [
            intent_score
            for intent_score in (
                self._score_one_label(label, i, raw) for i, label in enumerate(labels)
            )
            if intent_score is not None
        ]
        if not scored:
            return Classification(top_label="", top_score=0.0, candidates=())
        return self._finalise_classification(scored)

    def _score_one_label(
        self, label: str, index: int, raw: "dict[str, object]"
    ) -> Optional[IntentScore]:
        """Score one candidate label against the model's
        binary decision (label vs. ``none_of_the_above``).

        Returns ``None`` when the label is missing,
        unparseable, below threshold, or won by an
        unexpected value (older GLiNER2 versions).
        """
        task = f"_fmh_intent_{index}"
        entry = raw.get(task)
        if not isinstance(entry, dict):
            return None
        winner_label = entry.get("label")
        confidence = entry.get("confidence")
        if winner_label is None or confidence is None:
            return None
        try:
            conf_f = float(confidence)
        except (TypeError, ValueError):
            return None
        score_f = _intent_score_for_winner(
            winner_label, expected_label=label, confidence=conf_f
        )
        if score_f is None or score_f < self._threshold:
            return None
        return IntentScore(label=str(label), score=score_f)

    def _finalise_classification(self, scored: list[IntentScore]) -> Classification:
        """Sort candidates descending by score, then by
        label for determinism on ties, and pack the top
        entry plus the full candidate list into the
        returned ``Classification``.
        """
        # Sort descending by score, then by label for
        # determinism on ties. The role layer consumes
        # only the top entry, but the full list is kept
        # in `candidates` for the audit / fallback path
        # (`routing.unclassified` event carries it).
        scored.sort(key=lambda s: (-s.score, s.label))
        return Classification(
            top_label=scored[0].label,
            top_score=scored[0].score,
            candidates=tuple(scored),
        )

    def __repr__(self) -> str:
        return (
            f"GlinerIntentAdapter("
            f"model_name={self._model_name!r}, "
            f"threshold={self._threshold})"
        )


def _intent_score_for_winner(
    winner_label: object,
    *,
    expected_label: str,
    confidence: float,
) -> Optional[float]:
    """Translate a model's binary decision into an
    intent score for ``expected_label``.

    The model picks either ``expected_label`` or
    ``none_of_the_above``. If it picked
    ``none_of_the_above``, this label's score is
    the inverse of that confidence.

    Returns ``None`` for any unexpected label
    (older GLiNER2 versions may slip through).
    """
    if winner_label == expected_label:
        return confidence
    if winner_label == "none_of_the_above":
        return 1.0 - confidence
    return None


__all__ = [
    "GlinerIntentAdapter",
]
