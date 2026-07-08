# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
GLiNER2-backed ``FieldFinder`` and match-extraction helpers.

This module is the only one that touches ``gliner2``. It
eager-loads the model in the constructor (via
:func:`require_optional`) and runs inference in a worker
thread (``asyncio.to_thread``) so the event loop stays
responsive.

Iter 28: moved from
``kntgraph.agents.knowledge.argument_extractor._gliner_finder``
to the framework. The module is framework-level because
``GlinerFieldFinder`` is the canonical default
``FieldFinder`` implementation (alongside
``RegexFieldFinder``); a future ``TinyLLMFieldFinder``
or ``FastTextFieldFinder`` would land here too.

The helpers :func:`extract_first` and :func:`match_to_value`
tolerate multiple GLiNER2 output shapes -- the model has
changed its return shape across versions (1.3.x canonical
dict, pre-1.3 dataclass list, plain dict at the top
level). :func:`field_o` is a tiny ``dict-or-attribute``
reader that lets the helpers work uniformly across the
shapes.
"""

from __future__ import annotations

import asyncio
from typing import Optional, Protocol, Union

from kntgraph.core._typing import JsonScalar, ValidatorInput
from kntgraph.knowledge.extraction.argument._finder import FieldFinder
from kntgraph.tools.schema import FieldSpec


class _MatchDict(Protocol):
    """Structural shape of a GLiNER2 dict-shaped match.

    GLiNER2 1.3.x with ``include_confidence=True`` returns
    matches as ``{"text": str, "confidence": float}`` (or
    ``"score"`` / ``"surface"`` / ``"value"`` aliases; the
    helpers read these via :func:`field_o`). The Protocol
    is for static typing only; the helpers duck-type at
    runtime.
    """

    text: str
    confidence: float


class _MatchObj(Protocol):
    """Structural shape of a GLiNER2 dataclass-shaped match.

    GLiNER2 pre-1.3 returns dataclass instances with
    ``.text`` and ``.score`` attributes. The Protocol is
    for static typing only.
    """

    text: str
    score: float


# A single match from a GLiNER2 entities result is one of:
#   - a bare string (the default, with ``include_confidence=False``);
#   - a dict with ``text`` / ``confidence`` (1.3.x canonical);
#   - a dataclass with ``.text`` / ``.score`` (pre-1.3).
GlinerMatch = Union[str, _MatchDict, _MatchObj]


# The raw GLiNER2 ``.extract_entities(...)`` response is
# a nested dict: ``{"entities": {label: [match, ...]}}``
# (1.3.x). Older versions return a list of dataclasses
# directly. The framework reads it through :func:`field_o`
# so the exact shape is tolerated; this alias exists for
# the call sites that bind the result.
GlinerRawResult = Union[dict[str, dict[str, list[GlinerMatch]]], list[GlinerMatch]]


def field_o(obj: ValidatorInput, name: str) -> Optional[JsonScalar]:
    """Read `name` from `obj` whether dict or attribute."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        value = obj.get(name)
        if value is None:
            return None
        return value  # type: ignore[return-value]
    return getattr(obj, name, None)


def extract_first(
    raw: GlinerRawResult,
    entity_name: str,
) -> Optional[tuple[GlinerMatch, float]]:
    """
    Pull the first match for `entity_name` from the raw
    GLiNER2 output.

    GLiNER2 1.3.x returns one of these shapes (depending on
    the primitive called and the version):

      - `{"entities": {label: [{"text", "confidence"}, ...]}}`
        -- produced by `extract(text, {"entities": [...]})` and
        `extract_entities(text, [...])`. The canonical 1.3.x
        shape; preferred by this code path.

      - `[candidate, ...]` where each `candidate` has
        `label`/`text`/`score` attributes or keys.
        Older (pre-1.3) dataclass shape. Kept for backward
        compatibility with earlier checkpoints.

      - `{label: [match, ...]}` or `{label: match}` at the
        top level. Older dict shape; tolerated as a
        last-resort fallback.

    Returns `None` when nothing matches `entity_name`.
    """
    if raw is None:
        return None
    return (
        _extract_from_entities_dict(raw, entity_name)
        or _extract_from_top_level_label(raw, entity_name)
        or _extract_from_candidates(raw, entity_name)
    )


def _extract_from_entities_dict(
    raw: GlinerRawResult, entity_name: str
) -> Optional[tuple[GlinerMatch, float]]:
    """1.3.x canonical shape: ``{"entities": {label: [...]}}``."""
    entities_dict = field_o(raw, "entities")
    if not isinstance(entities_dict, dict):
        return None
    return match_to_value(entities_dict.get(entity_name))


def _extract_from_top_level_label(
    raw: GlinerRawResult, entity_name: str
) -> Optional[tuple[GlinerMatch, float]]:
    """Older dict shape: top-level ``{label: [match, ...]}``."""
    if not isinstance(raw, dict) or entity_name not in raw:
        return None
    if isinstance(field_o(raw, entity_name), dict):
        return None
    return match_to_value(raw[entity_name])


def _extract_from_candidates(
    raw: GlinerRawResult, entity_name: str
) -> Optional[tuple[GlinerMatch, float]]:
    """Older list-of-candidates shape.

    Walks ``raw`` (or its ``"predictions"`` field) and
    returns the first candidate whose label matches
    ``entity_name``.
    """
    candidates = _as_candidate_list(raw)
    for c in candidates:
        text, score = _candidate_to_text_score(c, entity_name)
        if text is None:
            continue
        return (text, score)
    return None


def _as_candidate_list(raw: GlinerRawResult) -> list[object]:
    """Normalise the various list-of-candidates shapes
    into a plain list.
    """
    if isinstance(raw, (list, tuple)):
        return list(raw)
    inner = field_o(raw, "predictions")
    if isinstance(inner, (list, tuple)):
        return list(inner)
    return [raw]


def _candidate_to_text_score(
    c: object, entity_name: str
) -> tuple[Optional[str], float]:
    """Pull ``(text, score)`` out of one candidate.

    Returns ``(None, 0.0)`` when the candidate's label
    doesn't match or the text/score is unusable.
    """
    label = field_o(c, "label") or field_o(c, "entity")
    if label is not None and label != entity_name:
        return (None, 0.0)
    text = field_o(c, "text") or field_o(c, "surface") or field_o(c, "value")
    if text is None:
        return (None, 0.0)
    raw_score = field_o(c, "score") or field_o(c, "confidence") or 0.0
    try:
        return (text, float(raw_score))
    except (TypeError, ValueError):
        return (None, 0.0)


def match_to_value(match: GlinerMatch) -> Optional[tuple[str, float]]:
    """
    Convert one match from a GLiNER2 entities result into a
    `(text, confidence)` tuple. The framework treats the
    score as confidence; callers apply the
    `field_threshold` filter downstream.

    Tolerates two shapes:

      - **Bare string**: GLiNER2 1.3.x with default
        `include_confidence=False` returns matches as
        plain strings: `{"entities": {"cnpj": ["12..."]}}`.
        No confidence available; we return `1.0` so the
        downstream threshold filter doesn't drop them.
        (Operators wanting calibrated scores should pass
        `include_confidence=True`; we don't, to keep the
        per-field inference call lightweight.)

      - **Dict / dataclass**: with
        `include_confidence=True` returns
        `{"entities": {"cnpj": [{"text": "...", "confidence": 0.99}]}}`.
        The dict shape carries both text and confidence.
    """
    if match is None:
        return None
    # Bare string: GLiNER2 default (no confidence).
    if isinstance(match, str):
        return (match, 1.0)
    text = (
        field_o(match, "text") or field_o(match, "surface") or field_o(match, "value")
    )
    if text is None:
        return None
    score = field_o(match, "score") or field_o(match, "confidence") or 1.0
    try:
        score_f = float(score)
    except (TypeError, ValueError):
        return None
    return (text, score_f)


class GlinerFieldFinder(FieldFinder):
    """
    GLiNER2-backed field finder.

    Eager-loads the model in `__init__`. Inference runs
    in a worker thread (`asyncio.to_thread`) so the
    event loop stays responsive.

    Schema mapping
    --------------

    For a `FieldSpec(name="cnpj", json_type="string",
    format="cnpj")`, the finder asks GLiNER2 for the
    entity type `cnpj` in `text`. The model's
    confidence is returned as-is (downstream threshold
    filtering happens in the orchestrator).

    The mapping from `FieldSpec` to GLiNER2 entity
    name is intentionally trivial (`field.name`): the
    caller is expected to choose field names that read
    well as entity types. If a tenant needs a
    different name (e.g. a Portuguese schema where the
    field is `cnpj` but the model is trained on
    `company_tax_id`), subclass and override
    `_entity_name_for`.
    """

    def __init__(
        self,
        model_name: str = "gliner2-base",
        *,
        device: Optional[str] = None,
    ) -> None:
        from kntgraph._optional import require_optional

        GLiNER2 = require_optional(
            "gliner2",
            "kntgraph[gliner]",
            purpose=("GlinerFieldFinder and GlinerArgumentAdapter"),
        ).GLiNER2

        self._model = GLiNER2.from_pretrained(model_name, device=device)
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    def _entity_name_for(self, field: FieldSpec) -> str:
        """
        Map a `FieldSpec` to a GLiNER2 entity type.

        Default: use the field name verbatim. Subclasses
        override for tenant-specific mappings.
        """
        return field.name

    async def find(
        self,
        text: str,
        field: FieldSpec,
    ) -> Optional[tuple[str, float]]:
        if not text or not text.strip():
            return None
        entity_name = self._entity_name_for(field)
        # GLiNER2 v1.5+ accepts a single label and
        # returns a list of (text, label, score) triples
        # (or a richer object -- see `extract_first`).
        raw = await asyncio.to_thread(self._run_inference, text, entity_name)
        return extract_first(raw, entity_name)

    def _run_inference(self, text: str, entity_name: str) -> GlinerRawResult:
        """
        Synchronous model call. Runs in a worker thread
        via `asyncio.to_thread`.
        """
        return self._model.extract_entities(
            text,
            [entity_name],
            include_confidence=True,
        )

    def __repr__(self) -> str:
        return f"GlinerFieldFinder(model_name={self._model_name!r})"


__all__ = [
    "GlinerFieldFinder",
    "GlinerMatch",
    "GlinerRawResult",
    "extract_first",
    "field_o",
    "match_to_value",
]
