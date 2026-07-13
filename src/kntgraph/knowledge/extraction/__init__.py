# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Entity and intent extraction for the Knowledge tier.

An :class:`EntityExtractor` turns raw text (typically
the JSON serialised payload of a ``tool.*.requested``
event) into a list of typed :class:`Entity` values.
The :class:`IntentClassifier` does the same for
intent routing. The Protocols here are the single
contracts; concrete extractors live in
:mod:`.heuristic` (regex, default, no deps) and
:mod:`.gliner` (GLiNER2, opt-in, requires the
``kntgraph[gliner]`` extra).

Public surfaces (Iter 21)
-------------------------

  - :class:`SLMEntityExtractor`   — facade over an
    ``EntityExtractorWithMentions`` adapter.
  - :class:`SLMIntentClassifier`  — facade over an
    ``IntentClassifier`` adapter.
  - :class:`SLMArgumentExtractor` — facade over an
    ``ArgumentExtractor`` adapter. The default
    backing is :class:`GlinerArgumentAdapter`
    (framework-level since Iter 27).

The ``SLM`` prefix (``Small Language Model``) decouples
the public surface from GLiNER2 — a future local model
(TinyLLM, FastText, etc) can be slotted in via
``SLM*Extractor(adapter=...)`` without breaking callers.
The default implementation is always the corresponding
``Gliner*Adapter``.

The output of an extractor is consumed by:

  - ``FalkorDBProjector`` when projecting ``Document``
    nodes with MENTIONS edges to ``Entity`` nodes. The
    MVP path; see ADR-004 §2.4.

  - The ``SolutionExtractor`` in
    :mod:`kntgraph.agents.memory.solutions` when building
    ``Problem.tags_json`` from the tool event payload.
    The Solution tier path; see ADR-010 §2.5 and §3.

  - The ``SemanticRoutingRole`` in
    :mod:`kntgraph.agents.roles.semantic_router` for intent
    dispatch (ADR-013).

The Protocol is intentionally minimal. The same Protocol
is used across tiers so that an application can plug a
single extractor and have it serve all three paths.

The argument-extraction subpackage
(:class:`ArgumentExtractor`, :func:`walk_schema`,
:class:`FieldFinder`) used to live in
:mod:`kntgraph.agents.knowledge.argument_extractor` because
the historical implementation depended on
:class:`kntgraph.agents.tools.protocol.ToolRegistry` — a
vertical concept.

Iter 27: :class:`GlinerArgumentAdapter` (the canonical
default backing of :class:`SLMArgumentExtractor`)
moved to :mod:`kntgraph.knowledge.extraction
.gliner_argument`. The adapter's
:class:`GlinerFieldFinder` and
:class:`SchemaArgumentExtractor` dependencies still
live in the vertical package (a follow-up iter will
move them too); the adapter does **lazy local
imports** so the framework module is importable
without loading the vertical.

See :mod:`base` for the type definitions.
"""

from .base import (
    ArgExtraction,
    ArgumentExtractor,
    Classification,
    Entity,
    EntityExtractor,
    EntityExtractorWithMentions,
    ExtractedArg,
    IntentClassifier,
    IntentScore,
    canonicalize,
    dedup_entities,
    parse_payload,
)

# Concrete extractors are imported lazily so the rest of
# the package is usable even when an optional dependency
# is missing.
try:
    from .heuristic import HeuristicEntityExtractor

    _HAS_HEURISTIC = True
except ImportError:  # pragma: no cover
    HeuristicEntityExtractor = None
    _HAS_HEURISTIC = False

try:
    from .gliner import GlinerEntityAdapter

    _HAS_GLINER = True
except ImportError:  # pragma: no cover
    GlinerEntityAdapter = None
    _HAS_GLINER = False

try:
    from .gliner_intent import GlinerIntentAdapter

    _HAS_GLINER_INTENT = True
except ImportError:  # pragma: no cover
    GlinerIntentAdapter = None
    _HAS_GLINER_INTENT = False

# Iter 27 + 28: ``GlinerArgumentAdapter`` is the framework-level
# adapter for argument extraction. The adapter itself is
# in ``gliner_argument.py``; the pieces it composes
# (``FieldFinder`` Protocol, ``RegexFieldFinder``,
# ``SchemaArgumentExtractor``, ``GlinerFieldFinder``,
# ``coerce``) live in the framework's ``argument``
# subpackage (Iter 28). All imports are eager; no
# ``kntgraph -> kntgraph.agents`` leak in any form.
from .gliner_argument import GlinerArgumentAdapter

# Iter 28: the argument subpackage is the canonical home
# of the building blocks (FieldFinder, RegexFieldFinder,
# coerce, SchemaArgumentExtractor, GlinerFieldFinder).
# Re-export the public surface here for callers that
# import from ``kntgraph.knowledge.extraction``
# directly.
from .argument import (
    FieldFinder,
    RegexFieldFinder,
    SchemaArgumentExtractor,
    GlinerFieldFinder,
    coerce,
    FieldValue,
    extract_first,
    field_o,
    match_to_value,
)

# SLM facades — public surfaces over the low-level
# adapters. Always importable (no lazy sentinel) so
# `SLM*()` is the canonical entry point. Construction
# may still fail with a clear error when the underlying
# optional dep is missing.
try:
    from ._slm_facades import (
        SLMArgumentExtractor,
        SLMEntityExtractor,
        SLMIntentClassifier,
    )

    _HAS_SLM = True
except ImportError:  # pragma: no cover
    SLMArgumentExtractor = None
    SLMEntityExtractor = None
    SLMIntentClassifier = None
    _HAS_SLM = False


__all__ = [
    # Types
    "Entity",
    "EntityExtractor",
    "EntityExtractorWithMentions",
    "IntentClassifier",
    "IntentScore",
    "Classification",
    "ArgumentExtractor",
    "ExtractedArg",
    "ArgExtraction",
    # Helpers
    "canonicalize",
    "dedup_entities",
    "parse_payload",
    # Implementations (None if the dep is missing)
    "HeuristicEntityExtractor",
    "GlinerEntityAdapter",
    "GlinerIntentAdapter",
    "GlinerArgumentAdapter",
    # Argument subpackage (Iter 28: framework-level)
    "FieldFinder",
    "RegexFieldFinder",
    "SchemaArgumentExtractor",
    "GlinerFieldFinder",
    "FieldValue",
    "coerce",
    "extract_first",
    "field_o",
    "match_to_value",
    # Facades (public surfaces; SLM* prefix decouples
    # from GLiNER2 specifically)
    "SLMEntityExtractor",
    "SLMIntentClassifier",
    "SLMArgumentExtractor",
]


def _raise_if_missing(name: str, available: bool, message: str) -> None:
    """Helper: raise a clear ImportError if `available` is
    False, with a consistent shape. The ``__getattr__``
    body stays flat (CC ≤ 2) by dispatching through this
    helper instead of inline ``if``/``raise`` blocks.
    """
    if not available:
        raise ImportError(message)


def __getattr__(name):  # pragma: no cover - sentinel
    """Raise a clear error if a missing optional is requested."""
    if name == "HeuristicEntityExtractor":
        _raise_if_missing(
            name,
            _HAS_HEURISTIC,
            "HeuristicEntityExtractor is unavailable (import failure). "
            "Check kntgraph.knowledge.extraction.heuristic.",
        )
    elif name == "GlinerEntityAdapter":
        _raise_if_missing(
            name,
            _HAS_GLINER,
            "GlinerEntityAdapter requires the 'gliner2' package. "
            "Install with: uv add 'kntgraph[gliner]'",
        )
    elif name == "GlinerIntentAdapter":
        _raise_if_missing(
            name,
            _HAS_GLINER_INTENT,
            "GlinerIntentAdapter requires the 'gliner2' package. "
            "Install with: uv add 'kntgraph[gliner]'",
        )
    elif name in {"SLMEntityExtractor", "SLMIntentClassifier", "SLMArgumentExtractor"}:
        _raise_if_missing(
            name,
            _HAS_SLM,
            "SLM facades are unavailable (import failure). "
            "Check kntgraph.knowledge.extraction._slm_facades.",
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
