# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
GlinerEntityAdapter — Entity extractor adapter backed by GLiNER2.

Iter 21: renamed from ``GlinerEntityExtractor`` to
``GlinerEntityAdapter`` (consistent with the Adapter
convention — ``OllamaEmbeddingAdapter``,
``FalkorDBGraphAdapter``, ``LiteLLMTransportAdapter``).
The class is still a ``Template Method`` — production
users subclass it to wire a real GLiNER2 model.

Most callers should NOT construct this directly —
use ``SLMEntityExtractor`` (the facade, in
``__init__.py``) which holds a reference to the
adapter and delegates every call. The ``SLM`` prefix
(``Small Language Model``) covers GLiNER2 today but
is **not tied** to it — a future
``TinyLLMEntityAdapter`` can be slotted in via
``SLMEntityExtractor(adapter=...)`` without changing
the facade's public API.

GLiNER2 is a zero-shot NLU model that classifies spans
against a label set. The extractor is opt-in: the `gliner2`
package is a separate dependency installed via
`uv add 'kntgraph[gliner]'`. When the package is
missing, importing this module raises `ImportError` and
the public `__init__.py` sets the symbol to `None` —
callers that try to use it see a clear error.

Why GLiNER2 (and not an LLM)
----------------------------

- Runs local (CPU or single GPU), no per-call cost, no
  rate limit.
- Deterministic for the same input + model + labels.
- The label set is configurable per call, so the same
  extractor can serve a generic Document projection and a
  specific Tool schema (ADR-010 §2.5 / Fase 4). The
  `PiiRedactionTool` (Fase 3) reuses this class with the
  PII label set.

Versus the heuristic extractor
------------------------------

`HeuristicEntityExtractor` is regex + payload-key. Fast,
dependency-free, but limited to known patterns. GLiNER2
catches semantic mentions ("Sr. João da Silva" as a
`person`, "R. das Flores, 123" as an `address`) that
regex never will.

The two are complementary. The default extraction path
is heuristic. GLiNER2 is wired in by injecting the
extractor into the projector / solution promoter.

Subclass-and-wire contract
--------------------------

The framework ships a **base implementation** that
satisfies the Protocol surface (`extract`,
`extract_with_mentions`) but does NOT call any real
GLiNER2 model — the actual model loading is application
territory. Two reasons:

  1. GLiNER2's public API is still moving (v1.0 → v1.5
     changed call signatures). The framework pins the
     label set and the conversion to `Entity`, but the
     model download + device placement is per-deployment.
  2. Applications often want a domain-specific model
     (Portuguese fiscal, healthcare, etc.). Forcing a
     default would either bloat the framework or
     constrain the application.

`GlinerEntityAdapter` is therefore a `Template Method`:
the conversion (raw span → `Entity`) is final and
shared; the inference (raw span from a model) is one
hook (`_run_model`) that subclasses override. The
default `_run_model` returns an empty list — the
extractor becomes a no-op pass-through that still
satisfies the Protocol.

To wire a real model, subclass:

    class MyGliner(GlinerEntityAdapter):
        def __init__(self, model_path):
            super().__init__()
            self._model = load_my_model(model_path)
        async def _run_model(self, text):
            spans = self._model.predict(text, self._labels)
            return [(self._span_to_entity(s), s.start) for s in spans]

The framework tests exercise the conversion path with a
fake `_run_model` that returns canned spans. Production
wiring is a deployment concern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

from kntgraph.knowledge.extraction.base import (
    Entity,
    EntityExtractorWithMentions,
    dedup_entities,
)

if TYPE_CHECKING:
    from kntgraph.knowledge.extraction.base import ExtractedValue


# ``GLiNERSpan`` is the framework-level adapter for
# the raw span emitted by ``gliner2``. The library
# has shipped multiple shapes across versions
# (dataclass, dict, object); the adapter is a
# Protocol that any of them satisfy. Duck-typed at
# runtime via :func:`_field`.
@runtime_checkable
class GLiNERSpan(Protocol):
    label: "str | None"
    text: "str | None"
    score: "float | None"
    start: "int | None"
    end: "int | None"


# Default label set for general-purpose extraction. The
# values are the entity-type constants from `base.py`; the
# GLiNER2 model is asked to return one of these for each
# span. The same strings are stored as `Entity.type` and
# therefore used as the `:Entity {type}` label in FalkorDB
# queries.
DEFAULT_LABELS: tuple[str, ...] = (
    "org",
    "person",
    "product",
    "money",
    "date",
    "id",
    "location",
    "other",
)


class GlinerEntityAdapter(EntityExtractorWithMentions):
    """
    Template-method base for GLiNER2-backed Entity extraction.

    Implements the rich `EntityExtractorWithMentions`
    contract. The minimal `EntityExtractor.extract` is
    provided as a thin wrapper that drops the offset.

    The conversion path (raw span → `Entity`) is final.
    The inference path (`_run_model`) is the subclass
    hook. The default implementation returns an empty
    list — the extractor becomes a no-op that still
    satisfies the Protocol. This is the right shape for
    the framework: the model is a per-deployment concern.

    To wire a real model, subclass and override
    `_run_model` (see module docstring).
    """

    def __init__(
        self,
        labels: tuple[str, ...] = DEFAULT_LABELS,
        *,
        threshold: float = 0.5,
    ) -> None:
        """
        Args:
          labels: the closed set of entity types the
            extractor can return. Must contain only
            non-empty strings; the framework's type
            constants in `base.py` are the canonical
            values.
          threshold: minimum confidence for a span to be
            kept. Default `0.5`; lower for recall-heavy
            applications, higher for precision-heavy.
            Subclasses that override `_run_model` are
            expected to honour this value.
        """
        if not labels:
            raise ValueError("labels must be non-empty")
        if not all(isinstance(lbl, str) and lbl for lbl in labels):
            raise ValueError("labels must be non-empty strings")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be in [0, 1], got {threshold!r}")
        self._labels = labels
        self._threshold = float(threshold)

    # ------------------------------------------------------------------ public

    async def extract(self, text: str) -> list[Entity]:
        """Extract entities from `text`. Order is not significant."""
        if not text:
            return []
        results = await self._run_model(text)
        # Tolerate two shapes from subclasses:
        #   - tuples `(Entity, offset)` (the canonical shape)
        #   - `None` (malformed span dropped by `_convert_span`)
        # We extract the Entity in both cases and drop
        # anything that is not an Entity.
        entities: list[Entity] = []
        for r in results:
            if isinstance(r, tuple) and len(r) == 2:
                e, _ = r
                if e is not None:
                    entities.append(e)
            # `None` and other shapes are silently dropped.
        return dedup_entities(entities)

    async def extract_with_mentions(
        self, text: str
    ) -> list[tuple[Entity, Optional[int]]]:
        """
        Extract entities with character offsets.

        Returns the same entities as `extract`, with the
        start offset of each span. Subclasses that wire a
        real model populate the offset from the model's
        output; the default `_run_model` returns no spans.

        Malformed spans (returning `None` from
        `_convert_span`) are filtered before the result
        is returned.
        """
        if not text:
            return []
        results = await self._run_model(text)
        out: list[tuple[Entity, Optional[int]]] = []
        for r in results:
            if isinstance(r, tuple) and len(r) == 2:
                e, o = r
                if e is not None:
                    out.append((e, o))
        return out

    # ------------------------------------------------------------------ hook

    async def _run_model(self, text: str) -> list[tuple[Entity, Optional[int]]]:
        """
        Hook for subclasses. Returns `[(Entity, offset)]`.

        The default implementation returns an empty list.
        The conversion logic that the subclass DOES NOT
        need to re-implement is in `_convert_span` below.

        A real subclass loads the model once (in its
        `__init__` or lazily on first call), then calls
        the model and feeds the result through
        `_convert_span` for each span. Example:

            async def _run_model(self, text):
                raw = await asyncio.to_thread(
                    self._model.predict_entities,
                    text, self._labels,
                    self._threshold,
                )
                return [
                    converted
                    for s in raw
                    if (converted := self._convert_span(s)) is not None
                ]
        """
        return []

    # ---------------------------------------------------------- helpers

    def _convert_span(
        self, span: "GLiNERSpan"
    ) -> Optional[tuple[Entity, Optional[int]]]:
        """
        Convert a raw model span to `(Entity, offset)`.

        Tolerates both dict and object shapes (GLiNER2
        v1.0 used dataclasses, v1.5+ uses dicts). Returns
        `None` for malformed spans (missing `label` or
        `text`); the caller filters `None` results before
        dedup.
        """
        label = _field(span, "label")
        span_text = _field(span, "text")
        start = _field(span, "start")
        score = _field(span, "score")
        if not label or not span_text:
            return None
        offset: Optional[int]
        if start is None:
            offset = None
        else:
            try:
                offset = int(start)
            except (TypeError, ValueError):
                offset = None
        attrs: dict[str, ExtractedValue] = {}
        if score is not None:
            try:
                attrs["gliner_score"] = float(score)
            except (TypeError, ValueError):
                pass
        return (
            Entity(
                name=str(span_text),
                type=str(label),
                surface=str(span_text),
                attributes=attrs,
            ),
            offset,
        )

    # ------------------------------------------------------------------ repr

    def __repr__(self) -> str:
        return (
            f"GlinerEntityAdapter("
            f"labels={len(self._labels)}, "
            f"threshold={self._threshold})"
        )


def _field(span: "GLiNERSpan | dict[str, object]", name: str) -> "object | None":
    """
    Read `name` from `span` whether it is a dict or has
    an attribute. Returns `None` if the field is absent.
    """
    if span is None:
        return None
    if isinstance(span, dict):
        return span.get(name)
    return getattr(span, name, None)


__all__ = [
    "GlinerEntityAdapter",
    "DEFAULT_LABELS",
]
