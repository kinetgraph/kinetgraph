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

**Future redesign.** ``GlinerFieldFinder`` is also a
candidate for revision in a future ADR (see
``DEBT.md`` §2.34). The known inconsistencies are minor
and the class functions correctly today:

  - The constructor defaults to
    ``model_name="gliner2-base"`` instead of accepting
    ``None`` and falling back to
    ``Settings.arg_extractor_model_id`` (the contract
    the three sibling adapters follow).
  - The docstring says "GLiNER2 v1.5+" — the framework
    has since moved to ``gliner2 2.0.0``.

The redesign (open the constructor signature, refresh
the docstring) is a small follow-up to roll into the
entity-adapter redesign ADR.

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
from typing import Any, Optional, Protocol, Union, runtime_checkable

from kntgraph.core._typing import JsonScalar, ValidatorInput
from kntgraph.knowledge.extraction.argument._finder import FieldFinder
from kntgraph.tools.schema import FieldSpec


class _MatchDict(Protocol):
    """Structural shape of a GLiNER2 dict-shaped match.

    GLiNER2 1.3.x with ``include_confidence=True`` returns
    matches as ``{"text": str, "confidence": float}`` (or
    ``"score"`` / ``"surface"`` / ``"value"`` aliases; the
    helpers read these via :func:`_read`). The Protocol
    is for static typing only; the helpers duck-type at
    runtime.
    """

    text: str
    confidence: float


@runtime_checkable
class _MatchObj(Protocol):
    """Structural shape of a GLiNER2 dataclass-shaped match.

    GLiNER2 pre-1.3 returns dataclass instances with
    ``.text`` and ``.score`` attributes. The Protocol is
    for static typing only; ``@runtime_checkable`` lets
    the candidate-list helper narrow the union without
    having to import the upstream type at module level.
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
# directly. The framework reads it through :func:`_read`
# so the exact shape is tolerated; this alias exists for
# the call sites that bind the result.
GlinerRawResult = Union[dict[str, Any], list[GlinerMatch]]


# The narrow union consumed by the private ``_read`` helper.
# ``field_o`` is reserved for JSON-shaped ``ValidatorInput``
# (the framework's stream-boundary contract); the GLiNER2
# paths (which admit attribute-bearing objects) use
# ``_read`` instead.
_MatchCandidate = Union[_MatchDict, _MatchObj, dict[str, Any]]


def field_o(obj: ValidatorInput, name: str) -> Optional[JsonScalar]:
    """Read `name` from `obj` whether dict or attribute.

    Reserved for JSON-shaped ``ValidatorInput`` at the
    stream boundary. The GLiNER2 match helpers use the
    local :func:`_read` instead — ``field_o`` is not
    designed to admit attribute-bearing objects.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        value = obj.get(name)
        if value is None:
            return None
        return value  # type: ignore[return-value]
    return getattr(obj, name, None)


def _read(obj: Any, name: str) -> Any:
    """Read ``name`` from a GLiNER2-shaped match object.

    Mirrors :func:`field_o` but admits the GLiNER2
    ``_MatchDict`` / ``_MatchObj`` Protocols (objects
    with attribute access) alongside plain dicts. The
    parameter is deliberately typed ``Any`` because
    the call sites bind different shapes (the raw
    response, one match from a list, or one match
    from a nested dict); the consumers narrow via
    ``isinstance`` or ``str()``/``float()`` coercion.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def extract_first(
    raw: GlinerRawResult,
    entity_name: str,
) -> Optional[tuple[str, float]]:
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
) -> Optional[tuple[str, float]]:
    """1.3.x canonical shape: ``{"entities": {label: [...]}}``."""
    entities_dict = _read(raw, "entities")
    if not isinstance(entities_dict, dict):
        return None
    return match_to_value(entities_dict.get(entity_name))


def _extract_from_top_level_label(
    raw: GlinerRawResult, entity_name: str
) -> Optional[tuple[str, float]]:
    """Older dict shape: top-level ``{label: [match, ...]}``."""
    if not isinstance(raw, dict) or entity_name not in raw:
        return None
    if isinstance(_read(raw, "entities"), dict):
        # Already handled by the entities_dict path.
        return None
    return match_to_value(raw[entity_name])


def _extract_from_candidates(
    raw: GlinerRawResult, entity_name: str
) -> Optional[tuple[str, float]]:
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


def _as_candidate_list(raw: GlinerRawResult) -> list[_MatchCandidate]:
    """Normalise the various list-of-candidates shapes
    into a plain list of ``_MatchCandidate``.

    Bare ``str`` matches (the no-confidence form) are
    dropped here — they are handled by
    :func:`match_to_value` before the candidate walk
    would have processed them. The walk only needs the
    scored candidates.
    """
    if isinstance(raw, (list, tuple)):
        return _collect_from_sequence(raw)
    inner = _read(raw, "predictions")
    if isinstance(inner, (list, tuple)):
        return _collect_from_sequence(inner)
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, _MatchObj):
        return [raw]
    return []


def _collect_from_sequence(seq: Any) -> list[_MatchCandidate]:
    """Filter a sequence of raw candidates to keep only
    ``_MatchCandidate`` (dict or ``_MatchObj``). Bare
    strings (handled by :func:`match_to_value`) and
    anything else are dropped.
    """
    out: list[_MatchCandidate] = []
    for c in seq:
        if isinstance(c, dict):
            out.append(c)
        elif isinstance(c, _MatchObj):
            out.append(c)
    return out


def _candidate_to_text_score(
    c: _MatchCandidate, entity_name: str
) -> tuple[Optional[str], float]:
    """Pull ``(text, score)`` out of one candidate.

    Returns ``(None, 0.0)`` when the candidate's label
    doesn't match or the text/score is unusable.
    """
    label = _read(c, "label") or _read(c, "entity")
    if label is not None and label != entity_name:
        return (None, 0.0)
    text = _read(c, "text") or _read(c, "surface") or _read(c, "value")
    if text is None:
        return (None, 0.0)
    raw_score = _read(c, "score") or _read(c, "confidence") or 0.0
    try:
        return (str(text), float(raw_score))
    except (TypeError, ValueError):
        return (None, 0.0)


def match_to_value(match: Optional[GlinerMatch]) -> Optional[tuple[str, float]]:
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
    text = _read(match, "text") or _read(match, "surface") or _read(match, "value")
    if text is None:
        return None
    score = _read(match, "score") or _read(match, "confidence") or 1.0
    try:
        return (str(text), float(score))
    except (TypeError, ValueError):
        return None


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
        # ADR-055: delegate model loading to the process-level registry
        # so all adapters sharing the same (model_name, device, cache_dir)
        # key reuse one loaded instance instead of each paying the cold
        # start and holding a separate copy in RAM. The registry
        # encapsulates ``require_optional`` and raises a clear error when
        # ``gliner2`` is not installed.
        from kntgraph.infra.config import fresh_settings
        from kntgraph.knowledge.extraction._gliner_model_registry import (
            GlinerModelRegistry,
        )

        cache_dir = fresh_settings().model_cache_dir
        self._model = GlinerModelRegistry.get(
            model_name,
            device=device,
            cache_dir=cache_dir,
        )
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
