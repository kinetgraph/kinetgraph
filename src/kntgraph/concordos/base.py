# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos.base -- the Specification Pattern (ADR-069 §2).

A *Concordo* is a named, versioned, composable behavioral
pattern that wires existing framework modules into a
coherent end-to-end behavior. Both Concordos in ADR-069
(BusinessFSM, WorkflowSaga) express business rules as
conditions over a shared context. This module defines the
condition language:

  - ``StepContext`` -- the read-only view a Specification
    evaluates against.
  - ``Specification`` -- a named, composable predicate
    (Evans, DDD §9).
  - ``Composable`` -- the mixin providing ``and_`` /
    ``or_`` / ``not_`` combinators.
  - ``AndSpec`` / ``OrSpec`` / ``NotSpec`` -- the concrete
    combinators.
  - ``ViewTrigger`` -- the read-only trigger surface the
    FSM/Saga systems derive from the view (ADR-069 §11.16).

The module is framework-owned (``concordos/`` imports only
from ``core/`` and stdlib, per the type-discipline skill
§1.2). Specifications are pure, immutable, and composable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Mapping
from uuid import UUID

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue
    from kntgraph.core.components.memory import (
        ContinuityComponent,
        ProfileComponent,
    )
    from kntgraph.core.event.correlation import CorrelationContext
    from kntgraph.core.world.component import DomainComponent
    from kntgraph.core.world.view import AgentView

__all__ = [
    "AndSpec",
    "Composable",
    "NotSpec",
    "OrSpec",
    "Specification",
    "StepContext",
    "ViewTrigger",
]


@dataclass(frozen=True, slots=True)
class StepContext:
    """
    Read-only view passed to Specifications for evaluation.

    Contains the framework components that might be
    relevant to a business rule. All component fields are
    optional because not every agent has every component
    populated.

    ``now`` is the dispatcher's current tick timestamp
    (injected by the system that builds the context).
    Specifications MUST treat ``now`` as the only clock
    source; reading ``datetime.now()`` inside
    ``is_satisfied_by`` would break replay determinism.

    ``trigger_data`` is the data payload of the event
    that triggered the current evaluation (e.g., the
    transition event for an FSM guard, the trigger event
    for a saga). The mini-language's ``event.data.*``
    paths resolve via this field. ``None`` when no
    trigger is in scope (e.g., for a saga step-result
    evaluation that only cares about ``step_results``).

    **Cross-agent access is opt-in.** A Specification
    that needs to read another agent's view (e.g.
    "proceed iff the financial-control agent's tier
    is VIP") declares an opt-in by accepting a
    ``cross_agent_resolver`` callable and calling it
    inside ``is_satisfied_by``. The resolver is built by
    the system that constructs the ``StepContext``
    (typically a closure over the post-fold ``World``).

    The ``StepContext`` itself does NOT carry the
    ``World``: that would make every guard FSM an O(N)
    scan over ``world.agents`` and break the "FSM
    guard is cheap" contract. Specifications that do
    NOT need cross-agent access receive
    ``cross_agent_resolver=None`` and cannot reach
    the World.

    Specifications MUST NOT mutate any agent view,
    emit events, or perform I/O. The
    ``is_satisfied_by`` method is pure; the emitted
    events belong to the system that called the
    Specification, not to the Specification itself.
    """

    step_results: MappingProxyType[str, "JsonValue"]
    step_states: MappingProxyType[str, str]
    domain: "DomainComponent | None"
    continuity: "ContinuityComponent | None"
    profile: "ProfileComponent | None"
    agent_id: str
    now: datetime
    trigger_data: "MappingProxyType[str, JsonValue] | None" = None
    cross_agent_resolver: "Callable[[str], AgentView | None] | None" = None


class Composable:
    """
    Mixin providing ``and_``, ``or_``, ``not_`` combinators
    as default implementations.

    Concrete Specifications inherit this mixin and only
    implement ``is_satisfied_by``. The combinators return
    typed ``AndSpec`` / ``OrSpec`` / ``NotSpec`` instances,
    which themselves compose further via the same mixin.
    The mixin is intentionally NOT a Protocol with
    ``runtime_checkable``: every concrete Specification must
    explicitly inherit ``Composable`` (a Protocol would let
    implementations silently forget to include it).
    """

    def and_(self: "Specification", other: "Specification") -> "AndSpec":
        return AndSpec(self, other)

    def or_(self: "Specification", other: "Specification") -> "OrSpec":
        return OrSpec(self, other)

    def not_(self: "Specification") -> "NotSpec":
        return NotSpec(self)


class Specification(ABC, Composable):
    """
    Composable predicate over ``StepContext``.

    Inspired by Evans DDD §9 (Specification Pattern). All
    implementations must be:

    - Pure (no I/O, no side effects)
    - Immutable (frozen dataclass or equivalent)
    - Compositional via the ``Composable`` mixin
    """

    @abstractmethod
    def is_satisfied_by(self, ctx: StepContext) -> bool: ...


@dataclass(frozen=True, slots=True)
class AndSpec(Specification):
    """True when both ``left`` and ``right`` are satisfied."""

    left: Specification
    right: Specification

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return self.left.is_satisfied_by(ctx) and self.right.is_satisfied_by(ctx)


@dataclass(frozen=True, slots=True)
class OrSpec(Specification):
    """True when either ``left`` or ``right`` is satisfied."""

    left: Specification
    right: Specification

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return self.left.is_satisfied_by(ctx) or self.right.is_satisfied_by(ctx)


@dataclass(frozen=True, slots=True)
class NotSpec(Specification):
    """True when ``inner`` is NOT satisfied."""

    inner: Specification

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return not self.inner.is_satisfied_by(ctx)


@dataclass(frozen=True, slots=True)
class ViewTrigger:
    """
    Read-only carrier of the trigger surface the FSM/Saga
    emit helpers consume. Built by the system from the view
    (``domain_phase`` / ``last_event_id`` / the component
    keyed by ``domain_phase``) and
    ``correlation_middleware.current()``.

    ``data`` mirrors the last domain event's payload (the
    default fold installs it under the component keyed by
    ``event_type``), so ``_dispatch_step`` can read
    ``trigger.data["saga_id"]`` and the ``enrich_from``
    enrichment reads ``trigger.data["step_results"]``.

    ``causation_id`` is the id of the event that caused this
    trigger. The view does not carry it for tool
    completions; ``SagaSystem._match_step`` recovers it from
    the ``tool_completions`` slot instead (the request's
    ``event_id``). For saga-start / timeout / compensation
    events it equals ``event_id``.

    NOT a framework type and holds no event history.
    """

    agent_id: str
    event_type: str
    event_id: UUID | None
    data: Mapping[str, "JsonValue"]
    correlation: "CorrelationContext | None"
    causation_id: UUID | None = None
