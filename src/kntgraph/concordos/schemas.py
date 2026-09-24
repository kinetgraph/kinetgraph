# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
concordos.schemas -- Pydantic v2 schemas for the bundle format (ADR-073).

The bundle format lets a vertical declare FSM/Saga configs
declaratively (YAML or JSON). These Pydantic models are
the validation layer: bundles are parsed, validated against
these schemas, and rejected loudly on any error (unknown
keys, malformed transitions, missing events, etc.).

The schemas are **strict** (extra="forbid"): typos in
field names fail the load with the dotted path. The
schemas are also **typed** (no ``Any``): the loader
resolves ``EventSchema.schema`` to a real Pydantic
model subclass via ``importlib`` (ADR-073 §4.2).

This module is implementation reference; the design lives in
[ADR-073](../docs/concordos-bundle-spec.md) and the
specification reference is
[docs/concordos-bundle-spec.md](../docs/concordos-bundle-spec.md).
"""

from __future__ import annotations

import re

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)


# Pattern vocabulary (matches docs/concordos-bundle-spec.md §3.1).

# Event names: ``domain.subdomain.name`` (e.g., ``invoice.submitted``).
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

# Bundle IDs follow Java/OSGi/Kubernetes reverse-DNS convention.
_BUNDLE_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

# Concordo IDs are namespace-prefixed: ``fsm:Name``, ``saga:Name``.
_FSM_ID = re.compile(r"^fsm:[A-Za-z_][A-Za-z0-9_]*$")
_SAGA_ID = re.compile(r"^saga:[A-Za-z_][A-Za-z0-9_]*$")

# Step / spec names are valid Python identifiers.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Semantic version: ``MAJOR.MINOR.PATCH``.
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class _StrictModel(BaseModel):
    """Base for all bundle schemas.

    ``extra='forbid'`` rejects unknown keys at every level —
    a typo in ``compensate_tool`` (e.g., ``compesate_tool``)
    fails loudly with the dotted path instead of silently
    being ignored.
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class EventSchema(_StrictModel):
    """One event in the bundle's vocabulary.

    ``schema`` is a **dotted path** to a Pydantic model
    subclass declared in code (ADR-073 §4.2). The loader
    resolves it via ``importlib`` at load time. The bundle
    declares only the event's name; the schema lives in
    code where it can be type-checked, IDE-autocompleted,
    and unit-tested.

    Example (YAML)::

        events:
          - name: invoice.submitted
            schema: acme.invoice.events.InvoiceSubmitted
    """

    name: str = Field(pattern=_EVENT_NAME.pattern)
    schema_: str = Field(
        alias="schema",
        description=(
            "Dotted path to a Pydantic model subclass. "
            "Resolved via importlib at load time."
        ),
    )


# ---------------------------------------------------------------------------
# Specifications
# ---------------------------------------------------------------------------


class SpecificationSchema(_StrictModel):
    """A named predicate (ADR-073 §4.4).

    ``expression`` is a string in the mini-language (see
    [docs/concordos-bundle-spec.md §2](../docs/concordos-bundle-spec.md)).
    It is NOT validated for semantic correctness here — only
    syntactic parsing happens at load time. Semantic evaluation
    is lazy (at guard execution).
    """

    id: str = Field(pattern=_IDENT.pattern)
    expression: str = Field(min_length=1, max_length=2048)


# ---------------------------------------------------------------------------
# FSM
# ---------------------------------------------------------------------------


class FSMTransitionSchema(_StrictModel):
    """A single FSM transition (ADR-073 §3.2).

    ``from`` (alias) → ``from_state``: the source state.
    ``to``: the target state.
    ``on_event``: the event type that triggers the transition
    (must be declared in ``events[]``).
    ``guard``: an optional mini-language expression
    (parsed syntactically; semantic evaluation happens lazily).
    """

    from_state: str = Field(alias="from", pattern=_IDENT.pattern)
    to: str = Field(pattern=_IDENT.pattern)
    on_event: str = Field(pattern=_EVENT_NAME.pattern)
    guard: str | None = Field(default=None, max_length=2048)


class FSMConfigSchema(_StrictModel):
    """The FSM configuration block (ADR-073 §3.2).

    ``id``: the Concordo id (becomes ``Concordo.name``).
    ``component``: dotted path to a ``DomainComponent`` subclass.
    ``initial_state``: starting state (must be in ``states``).
    ``states``: explicit list (validates transitions + reachability).
    ``terminal``: subset of ``states`` (no outgoing transitions).
    ``on_entry``: maps target state → event type emitted on entry.

    Example (YAML)::

        business_fsm:
          id: "fsm:KnowledgeLifecycle"
          component: "acme.components.KnowledgeComponent"
          state_field: "lifecycle_state"
          initial_state: "INGESTED"
          states: ["INGESTED", "EXTRACTING", "CONSOLIDATED"]
          terminal: ["CONSOLIDATED"]
          transitions:
            - {from: "INGESTED", to: "EXTRACTING", on_event: "document.ingested"}
            - {from: "EXTRACTING", to: "CONSOLIDATED", on_event: "knowledge.extracted"}
          on_entry:
            CONSOLIDATED: "knowledge.consolidated"
    """

    id: str = Field(pattern=_FSM_ID.pattern)
    component: str = Field(
        description=(
            "Dotted path to a DomainComponent subclass. "
            "Resolved via importlib at load time."
        )
    )
    state_field: str = Field(pattern=_IDENT.pattern)
    initial_state: str = Field(pattern=_IDENT.pattern)
    states: list[str] = Field(min_length=1)
    terminal: list[str] = Field(default_factory=list)
    transitions: list[FSMTransitionSchema] = Field(min_length=1)
    on_entry: dict[str, str] = Field(default_factory=dict)

    @field_validator("terminal")
    @classmethod
    def _terminal_subset(cls, v: list[str], info) -> list[str]:
        states: list[str] = info.data.get("states", [])
        unknown = set(v) - set(states)
        if unknown:
            raise ValueError(
                f"terminal states not declared in 'states': {sorted(unknown)}"
            )
        return v

    @field_validator("on_entry")
    @classmethod
    def _on_entry_targets(cls, v: dict[str, str]) -> dict[str, str]:
        # The KEY is the state name (an identifier).
        # The VALUE is the event name (dotted) that the FSM
        # emits on entering that state.
        for state, event_name in v.items():
            if not _EVENT_NAME.match(event_name):
                raise ValueError(
                    f"on_entry[{state!r}]={event_name!r} must be a dotted event name"
                )
        return v


# ---------------------------------------------------------------------------
# Saga
# ---------------------------------------------------------------------------


class SagaStepSchema(_StrictModel):
    """A single saga step (ADR-073 §3.2).

    ``tool``: ``None`` declares a human step (ADR-072 §4.3).
    ``input_mapping``: maps param names to path expressions
    evaluated at dispatch time.
    """

    name: str = Field(pattern=_IDENT.pattern)
    tool: str | None = Field(default=None)
    timeout_ms: int = Field(default=30_000, ge=1)
    pre_condition: str | None = Field(default=None, max_length=2048)
    skip_when: str | None = Field(default=None, max_length=2048)
    compensate_tool: str | None = Field(default=None)
    compensate_when: str | None = Field(default=None, max_length=2048)
    approval_timeout_ms: int | None = Field(default=None, ge=1)
    input_mapping: dict[str, str] = Field(default_factory=dict)

    @field_validator("input_mapping")
    @classmethod
    def _input_paths(cls, v: dict[str, str]) -> dict[str, str]:
        for param_name, path in v.items():
            if not path.startswith(("event.data.", "steps.", "agent.")):
                raise ValueError(
                    f"input_mapping[{param_name!r}]={path!r} "
                    f"must start with 'event.data.', 'steps.', or 'agent.'"
                )
        return v


class SagaConfigSchema(_StrictModel):
    """A saga configuration (ADR-073 §3.2)."""

    id: str = Field(pattern=_SAGA_ID.pattern)
    trigger_event: str = Field(pattern=_EVENT_NAME.pattern)
    saga_timeout_ms: int = Field(default=300_000, ge=1)
    fail_when: str | None = Field(default=None, max_length=2048)
    steps: tuple[SagaStepSchema, ...] = Field(min_length=1)

    @field_validator("steps")
    @classmethod
    def _unique_step_names(
        cls, v: tuple[SagaStepSchema, ...]
    ) -> tuple[SagaStepSchema, ...]:
        names = [s.name for s in v]
        if len(names) != len(set(names)):
            dupes = {n for n in names if names.count(n) > 1}
            raise ValueError(f"saga has duplicate step names: {sorted(dupes)}")
        return v


# ---------------------------------------------------------------------------
# Bundle (top-level)
# ---------------------------------------------------------------------------


class BundleSchema(_StrictModel):
    """The top-level bundle shape (ADR-073 §3.2).

    A bundle is a named, versioned, atomic package of
    behaviour. It declares its event vocabulary, named
    predicates, an FSM (optional), and a list of sagas
    (optional). Multiple bundles are loaded by the
    catalog; the last-loaded wins per ``Concordo.name``
    (ADR-073 §8 — multi-tenant overrides).

    Example (YAML)::

        bundle_id: "com.acme.knowledge_pipeline"
        version: "1.0.0"
        events:
          - name: "document.ingested"
            schema: "acme.events.DocumentIngested"
        specifications:
          - id: "ConfidencePassed"
            expression: "event.data.confidence_score >= 0.80"
        business_fsm:
          id: "fsm:KnowledgeLifecycle"
          ...
        workflow_sagas:
          - id: "saga:EntityExtraction"
            ...
    """

    bundle_id: str = Field(pattern=_BUNDLE_ID.pattern)
    version: str = Field(pattern=_SEMVER.pattern)
    events: list[EventSchema] = Field(default_factory=list)
    specifications: list[SpecificationSchema] = Field(default_factory=list)
    business_fsm: FSMConfigSchema | None = None
    workflow_sagas: list[SagaConfigSchema] = Field(default_factory=list)


__all__ = [
    "BundleSchema",
    "EventSchema",
    "FSMConfigSchema",
    "FSMTransitionSchema",
    "SagaConfigSchema",
    "SagaStepSchema",
    "SpecificationSchema",
]
