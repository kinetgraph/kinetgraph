# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Entity — the named things in a Document or tool event
that the graph remembers.

In GraphRAG (ADR-004) and in the Solution tier (ADR-010),
an Entity is a canonical, typed mention extracted from a
piece of text. The canonical identity is the tuple
``(name, type)`` — `name` is lower-cased and stripped,
`type` is one of a closed set of labels (extensible but
stable per deployment). Two mentions of "ACME S/A" and
"acme s/a" merge into the same Entity node via `MERGE` on
the canonical key.

Two protocols
-------------

`EntityExtractor` is the minimal contract. Implementations
return a list of `Entity` with the canonical name already
applied (the `__post_init__` enforces it).

`EntityExtractorWithMentions` extends the contract with
character offsets. Consumers that need to render the
original surface form in a prompt or audit log can opt
into the richer contract; consumers that just want a
canonical name use the minimal one. The default
`HeuristicEntityExtractor` does NOT implement the rich
contract (offsets add bookkeeping cost without value for
the default path). LLM-based extractors and GLiNER2-based
ones typically do — see `gliner.py`.

Why "name" lowercase
--------------------

Entity identity in the graph is case-insensitive but
whitespace-sensitive. The canonical key strips leading /
trailing whitespace, lowercases the name and collapses
internal whitespace runs. This makes the `MERGE` predicate
match the human expectation ("the same company, regardless
of how the document wrote it"). The original surface form
is preserved in `Entity.surface` for auditing and for
prompts that need to show the user the exact form that was
seen.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Union, runtime_checkable


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


# Framework-level recursive type for values that flow
# through the extraction pipeline. Strings, ints,
# floats, bools, ``None`` and dict / list compositions
# are the common shapes — anything else is coerced to
# ``str(v)`` (see ``coerce_to_json`` below).
ExtractedScalar = Union[str, int, float, bool, None]
ExtractedValue = Union[
    ExtractedScalar,
    dict[str, "ExtractedValue"],
    list["ExtractedValue"],
]


# ---------------------------------------------------------------------------
# Entity types
# ---------------------------------------------------------------------------

# The closed set of well-known types. Applications are free
# to use other strings (the projector stores `type` as-is and
# the retriever matches on it) but the framework ships these
# as named constants for self-documentation. The values
# match the constants used in `gliner.py` so a tenant can
# switch between extractors without renaming labels.

ENTITY_TYPE_ORG = "org"
ENTITY_TYPE_PERSON = "person"
ENTITY_TYPE_PRODUCT = "product"
ENTITY_TYPE_MONEY = "money"
ENTITY_TYPE_DATE = "date"
ENTITY_TYPE_ID = "id"
ENTITY_TYPE_LOCATION = "location"
ENTITY_TYPE_OTHER = "other"


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entity:
    """
    An extracted entity, ready to be projected as a graph
    node.

    `name` is the CANONICAL form (lowercased, trimmed,
    internal whitespace collapsed). Two surface forms
    ("ACME S/A" and "acme s/a") yield the same canonical
    name and therefore merge on the graph.

    `type` is a free-form string; the framework recognises
    the constants in this module but does not enforce them.

    `surface` is the original form as it appeared in the
    text. Stored for auditing and prompt rendering.

    `attributes` is a free-form dict. Applications may use
    it to store extracted attributes (e.g. CNPJ digits for
    an org, normalised amount for money). Empty by default.
    """

    name: str
    type: str
    surface: str = ""
    attributes: dict[str, ExtractedValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Entity.name must be non-empty")
        if not self.type:
            raise ValueError("Entity.type must be non-empty")
        # Enforce the canonical form (idempotent). If the
        # caller already passed a canonical name, this is a
        # no-op; if not, the canonical form is the new
        # `name`. `surface` is preserved verbatim.
        canon = canonicalize(self.name)
        if canon != self.name:
            object.__setattr__(self, "name", canon)

    @property
    def canonical_key(self) -> tuple[str, str]:
        """The graph-level identity used in `MERGE`."""
        return (self.name, self.type)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class EntityExtractor(Protocol):
    """
    Extracts a list of `Entity` from a piece of text.

    Implementations are PURE: given the same text, they MUST
    return the same list of Entities. Side effects (LLM
    calls, network I/O) belong in the Role that wraps the
    extractor, not here.

    The Protocol is intentionally minimal. A more capable
    extractor can also return `Mention`s (with offsets) by
    implementing `EntityExtractorWithMentions` instead
    (sub-Protocol, not sub-class). The consumers
    (`FalkorDBProjector`, `SolutionExtractor`) check
    `isinstance(extractor, EntityExtractorWithMentions)`
    and use the richer path when available.
    """

    async def extract(self, text: str) -> list[Entity]:
        """Extract entities from `text`. Order is not significant."""
        ...


@runtime_checkable
class EntityExtractorWithMentions(Protocol):
    """
    Optional richer contract: extract with character offsets.

    Implementations preserve the original span (offset +
    surface form) of each mention. Consumers that need
    character positions for prompt rendering, audit logs or
    UI highlight consult this contract.

    A class that implements this Protocol MUST also satisfy
    `EntityExtractor` (callers can rely on `extract` being
    available). The two methods can return the same
    entities in different shapes.
    """

    async def extract_with_mentions(
        self, text: str
    ) -> list[tuple[Entity, Optional[int]]]:
        """Extract entities with their character offset in `text`."""
        ...


# ---------------------------------------------------------------------------
# Intent classification (ADR-013)
# ---------------------------------------------------------------------------
#
# Intent classification is a DIFFERENT shape from entity
# extraction: the question is "which of these K labels best
# describes the WHOLE text?" rather than "which spans in the
# text match a label?". Putting it on the same Protocol as
# `EntityExtractor` would force one of the two to be a
# degenerate case; we keep it separate.
#
# The label set is supplied per call (not baked into the
# classifier) so the same instance can serve multiple
# deployments / tool registries. The classifier itself
# stays stateless w.r.t. labels.


@dataclass(frozen=True, slots=True)
class IntentScore:
    """
    One (label, score) pair in a classification result.

    `label` is the candidate (typically a tool name in the
    semantic-routing use case — ADR-013 §2.1). `score` is
    the model's confidence, in [0, 1]. Order in the parent
    `Classification.candidates` is descending by score
    (the first entry is the top-1 prediction).
    """

    label: str
    score: float


@dataclass(frozen=True, slots=True)
class Classification:
    """
    Output of an `IntentClassifier.classify` call.

    `top_label` is the highest-scoring label (convenience;
    equals `candidates[0].label` when `candidates` is
    non-empty). `candidates` is the full ranked list, useful
    for the routing.unclassified path (where we keep the
    top-k for audit / fallback).

    `top_score` mirrors `candidates[0].score`. When the
    classifier returns no candidates at all (model failure,
    empty input), both `top_label` and `top_score` are
    placeholders: `top_label=""` and `top_score=0.0`. The
    `RoutingDecision` constructor and the role layer treat
    this as a "no decision" case and emit
    `routing.unclassified`.
    """

    top_label: str
    top_score: float
    candidates: tuple[IntentScore, ...] = ()


@runtime_checkable
class IntentClassifier(Protocol):
    """
    Classify a piece of text into one of a closed label set.

    The label set is supplied per call, not baked into the
    classifier. This lets the same instance serve multiple
    deployments and tool registries without re-instantiation.

    Implementations MUST be PURE for the same (text, labels,
    model): deterministic output, no side effects. The
    `SemanticRoutingRole` (ADR-013) relies on this for replay
    dedup via the deterministic `event_id`.

    Implementations SHOULD be non-blocking: the async
    signature exists precisely so GLiNER2 inference can be
    wrapped in `asyncio.to_thread` without callers having
    to know.
    """

    async def classify(
        self,
        text: str,
        labels: Sequence[str],
    ) -> Classification:
        """
        Classify `text` into one of `labels`.

        `labels` MUST be non-empty and contain only
        non-empty strings; the role layer validates this
        before calling.

        Implementations MUST return at least one candidate
        when they are able to process the input. Returning
        `Classification(top_label="", top_score=0.0,
        candidates=())` is reserved for "I cannot decide
        (empty input, model error, etc.)" — the role layer
        treats it as unclassified.
        """
        ...


# ---------------------------------------------------------------------------
# Argument extraction (ADR-013, Momento 2)
# ---------------------------------------------------------------------------
#
# After the role decides WHICH tool to call (Momento 1),
# the framework still needs to fill the tool's
# `input_schema` slots from the user's text. This is a
# span-typed problem: for each schema field, find the
# span that best answers it. Different backends solve
# it differently (regex for known formats, GLiNER2 for
# arbitrary fields, LLM for fuzzy cases). The Protocol
# below keeps the ToolInvoker agnostic to the choice.
#
# The flow:
#
#   ToolInvoker.handle_request_event(request)
#       → if pre_invoke_args_extractor is set:
#           merged = {**extracted_fields, **request.data["args"]}
#           (caller's args win; extractor fills the gaps)
#           validate(merged, tool.input_schema)
#           if invalid → emit tool.{name}.args_invalid → DLQ
#           if valid   → tool.invoke(idempotency_key=..., **merged)
#       → else:
#           tool.invoke(idempotency_key=..., **request.data)


@dataclass(frozen=True, slots=True)
class ExtractedArg:
    """
    One field extracted from the user's text.

    `value` is the raw value produced by the extractor;
    the type is enforced by the schema validator (a
    `str` for a `type: string` field, an `int`/`float`
    for a `type: number`/`integer` field, etc.). The
    extractor returns the most natural representation
    for its backend; the conversion is the
    `SchemaArgumentExtractor`'s job.

    `confidence` is in [0, 1]. Below the field-level
    threshold (configured on the extractor) the field
    is dropped from the result, regardless of its raw
    value — the role does NOT want to forward a
    low-confidence guess to a side-effecting Tool.
    """

    field_name: str
    value: ExtractedValue
    confidence: float


@dataclass(frozen=True, slots=True)
class ArgExtraction:
    """
    Output of an `ArgumentExtractor.extract` call.

    `tool_name` is repeated here (the extractor already
    knew it) so the `ToolInvoker` can route the result
    without keeping a separate mapping. `fields` maps
    field name → value. `confidences` is the parallel
    map for the per-field confidence (dropped fields
    are absent from both).

    `schema_version` is a hash of the input_schema
    that produced the extraction. Used as a cache key
    suffix: same (text, schema_version) → same result.
    """

    tool_name: str
    fields: Mapping[str, ExtractedValue]
    confidences: Mapping[str, float]
    schema_version: str


@runtime_checkable
class ArgumentExtractor(Protocol):
    """
    Populate a Tool's `input_schema` from the user's text.

    The extractor takes the target tool name (so it can
    look up the schema in the `ToolRegistry`) and the
    text to extract from. The schema is the source of
    truth for which fields to extract and of which type
    — the extractor must NOT guess at fields that are
    not in the schema.

    Implementations MUST be PURE for the same (text,
    tool_name, schema_version): deterministic output,
    no side effects. This is what makes the result
    safe to cache and replay.

    Implementations SHOULD be non-blocking. The async
    signature exists so GLiNER2 inference can be
    wrapped in `asyncio.to_thread` without callers
    having to know.
    """

    async def extract(
        self,
        text: str,
        tool_name: str,
    ) -> ArgExtraction:
        """
        Extract arguments for `tool_name` from `text`.

        `tool_name` MUST be registered in the
        `ToolRegistry`; the extractor looks up the
        schema internally. Unregistered names raise
        `ToolError` (caller routes to DLQ).

        Empty `text` returns an `ArgExtraction` with
        `fields={}` (nothing to extract). The caller
        then merges with `request.data["args"]` and
        may still have all the slots it needs.
        """
        ...


# ---------------------------------------------------------------------------
# canonicalize
# ---------------------------------------------------------------------------


def canonicalize(name: str) -> str:
    """
    Build the canonical form of an entity name.

    Rules (applied in order):
      1. Strip leading and trailing whitespace.
      2. Collapse internal whitespace runs to a single space.
      3. Lowercase (the graph is case-insensitive on names).

    Punctuation is NOT stripped — "ACME S/A" and "ACME SA"
    are intentionally different (they ARE different legal
    names). Numbers, slashes, hyphens are kept verbatim.

    Empty / whitespace-only inputs return "" (and the Entity
    constructor rejects them downstream).
    """
    s = name.strip()
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s)
    return s.lower()


# ---------------------------------------------------------------------------
# dedup_entities — shared helper
# ---------------------------------------------------------------------------


def dedup_entities(
    entities: Sequence[Entity],
) -> list[Entity]:
    """
    Deduplicate by canonical key, preserving first-seen order.

    The projector relies on the canonical key for graph
    MERGE; this dedup is a small optimisation that avoids
    sending duplicate Cypher statements when the same
    entity is matched by two passes (e.g. "NF-12345"
    matches an ID regex and also appears in a payload key).

    The first `Entity` seen for a given `(name, type)` wins
    (its `surface` and `attributes` are the ones the graph
    will store). Later duplicates with different surface
    forms are dropped — the auditor can recover the
    surface from the `Entity` it produced directly, not
    from a merged view.
    """
    seen: set[tuple[str, str]] = set()
    out: list[Entity] = []
    for e in entities:
        k = e.canonical_key
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# parse_payload — extract dict from JSON-encoded payload
# ---------------------------------------------------------------------------


def parse_payload(text: str) -> Optional[Mapping[str, ExtractedValue]]:
    """
    If `text` is a JSON-encoded dict, return it; otherwise None.

    Used by the heuristic extractor to ALSO scan the
    structured payload (e.g. CNPJ inside `data.cnpj`)
    in addition to the free-text scan. Detection is
    intentionally permissive: a leading `{` is enough to
    trigger a parse attempt.
    """
    if not text:
        return None
    import json as _json

    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        maybe = _json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(maybe, dict):
        return None
    return maybe


__all__ = [
    # Type constants
    "ENTITY_TYPE_ORG",
    "ENTITY_TYPE_PERSON",
    "ENTITY_TYPE_PRODUCT",
    "ENTITY_TYPE_MONEY",
    "ENTITY_TYPE_DATE",
    "ENTITY_TYPE_ID",
    "ENTITY_TYPE_LOCATION",
    "ENTITY_TYPE_OTHER",
    # Value object
    "Entity",
    # Protocols
    "EntityExtractor",
    "EntityExtractorWithMentions",
    "IntentClassifier",
    "ArgumentExtractor",
    # Value objects (intent classification — ADR-013)
    "IntentScore",
    "Classification",
    # Value objects (argument extraction — ADR-013 M2)
    "ExtractedArg",
    "ArgExtraction",
    # Helpers
    "canonicalize",
    "dedup_entities",
    "parse_payload",
]
