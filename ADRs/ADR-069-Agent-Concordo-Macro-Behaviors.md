<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-069: Agent Concordo — BusinessFSM and WorkflowSaga

- **Status:** Proposed
- **Date:** 2026-09-07
- **Author:** kinetgraph architecture team
- **Supersedes:** initial draft (changes from review tracked in §11)
- **Related to:**
  - [ADR-001](./ADR-001-Arquitetura.md) — pure ECS, Event Sourcing, `World = fold(events)`
  - [ADR-003](./ADR-003-Ciclo-Dual.md) — operational × domain dual lifecycle
  - [ADR-016](./ADR-016-Event-Signing.md) — Ed25519 event signing
  - [ADR-017](./ADR-017-Identity-Authorization.md) — `ToolACL`, `Principal`, authorization
  - [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md) — `WorldSystem` + incremental `ReactiveDispatcher`
  - [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — `ToolCallRequest` / `ToolCallCompletion`
  - [ADR-035](./ADR-035-sharding-and-dispatcher-coordination-for-horizontal-scaling.md) — horizontal sharding
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — `@tool_worker` + `WorkerManager`
  - [ADR-037](./ADR-037-Mandatory-Correlation-Propagation.md) — mandatory `CorrelationContext`
  - [ADR-039](./ADR-039-Role-rethinking-and-intentions-routing.md) — `RoleComponent` + `IntentResolutionSystem`
  - [ADR-042](./ADR-042-Agents-Memory-Model-usage.md) — `SessionComponent` / `ProfileComponent` / `ContinuityComponent`
  - [ADR-044](./ADR-044-Tool-call-Overlay-Accumulation.md) — tool-call overlay accumulation
  - [ADR-045](./ADR-045-Tool-Call-Request-TTL.md) — Tool Call TTL
  - [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) — `DomainComponent`
  - [ADR-068](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md) — wakeup stream (referenced by ADR-070 follow-up)
  - [ADR-070 — Worker-Level Back-Pressure *(proposed, follow-up)*](./ADR-070-Worker-Level-Back-Pressure.md)

---

## 1. Context

### 1.1 What the framework already provides

The framework delivers a correct mechanical substrate.
Before proposing new abstractions, the existing
capabilities must be stated explicitly to avoid
duplication:

| Capability | Primitive | ADR |
|---|---|---|
| Business process state | `DomainComponent` | ADR-059 |
| Volatile conversation memory | `SessionComponent` | ADR-042 |
| Sliding-window interaction context | `ContinuityComponent` | ADR-042 |
| User preferences and billing tier | `ProfileComponent` | ADR-042 |
| Tool execution (cross-process) | `@tool_worker` + `WorkerManager` | ADR-036 |
| In-flight tool call tracking | `ToolCallRequest` / `ToolCallCompletion` | ADR-034 |
| Tool call timeout | `tool.<name>.timed_out` event | ADR-045 |
| Intent routing | `IntentResolutionSystem` | ADR-039 |
| Authorization | `ToolACL` + `Principal` | ADR-017 |
| Fault isolation | lifecycle events + DLQ | ADR-001 |
| Liveness detection | `CyclicSystem` + `Runner` | ADR-001 |
| Replay and idempotency | `World.fold` + `event_id` dedup | ADR-001 |

### 1.2 The actual gap

The gap is not at the primitive level. It is at the
level of **recognisable, reusable behavioral
compositions** that wire multiple framework modules
into a coherent whole.

Three patterns emerge in every business vertical:

1. **Business object lifecycle** — a document, an
   invoice, a task has a defined set of states and
   allowed transitions. Today each vertical
   reimplements this with ad-hoc `DomainComponent`
   fields and unchecked events.

2. **Multi-step orchestration with compensation** —
   a business operation requires a sequence of tool
   calls where (a) each step may fail, (b) context
   from previous steps feeds into subsequent ones,
   and (c) failures must trigger compensating
   actions. Today each vertical reimplements this
   with bespoke coroutines that also reinvent what
   `ToolCallRequest`, `ToolCallCompletion`, and the
   TTL system already provide.

3. **Data flowing through specialised agents** — a
   document must pass through a fixed sequence of
   agent roles (intake → validator → emitter), with
   back-pressure between stages to avoid overwhelming
   slow consumers. Today each vertical routes events
   manually between agents with no shared
   back-pressure model.

### 1.3 What a Concordo is

> **Definition.** A *Concordo* is a **named,
> versioned, composable behavioral pattern** that
> wires together existing framework modules into a
> coherent end-to-end behavior. It is expressed as:
>
> 1. **Typed configuration** — parameters that drive
>    the wiring (states, transitions, timeouts,
>    routing rules)
> 2. **Specification objects** — composable
>    predicates (AND, OR, NOT) that express
>    business rules over framework components
> 3. **Pure WorldSystems** — read existing framework
>    components (`DomainComponent`, `ProfileComponent`,
>    `ToolCallCompletion`, etc.); emit events
> 4. **CyclicSystems** — where liveness or timeout
>    detection requires scanning the World on every
>    tick (not just reacting to events)
> 5. **WorkerAdapters** — optional; only when the
>    pattern requires I/O not covered by existing
>    `@tool_worker`s
>
> A Concordo does **not** introduce new runtime
> infrastructure. It does **not** duplicate
> `ToolCallRequest`, `ToolCallCompletion`, TTL, or
> any existing component.

#### 1.3.1 The `Concordo` Protocol

Every Concordo in this ADR implements the same
public surface — defined here so the type-checker
catches drift and the `app_runner.py` (see §6.1)
reads as a typed composition:

```python
from typing import Protocol, runtime_checkable


@runtime_checkable
class Concordo(Protocol):
    """
    The public surface every Concordo exposes.

    ``name``    -- stable identifier (``fsm:Invoice``,
                   ``saga:nfe_emission``,
                   ``pipeline:fiscal_document_processing``).
                   Used by the framework's `ConcordoCatalog`
                   (§6.3) and by log/metrics tagging.
    ``version`` -- semver string. Bumping it is the
                   recommended migration signal when a
                   Concordo's emitted event schema or
                   state semantics change.
    ``install`` -- idempotent registration against the
                   ``ReactiveDispatcher``. Each Concordo
                   registers one or more ``WorldSystem``s
                   (the post-ADR-018 shape) via
                   ``dispatcher.add_system(...)``.
                   Install may be called multiple times
                   safely; the dispatcher keeps a list
                   and a second ``add_system`` call would
                   duplicate the system. The
                   ``ConcordoCatalog.install_all``
                   (§6.3) de-duplicates by name.
    """

    name: str
    version: str

    def install(self, dispatcher: "ReactiveDispatcher") -> None: ...
```

The two Concordos proposed by this ADR
(`BusinessFSMConcordo`, `WorkflowSagaConcordo`)
all conform. A third candidate — the Pipeline
Concordo — was considered and **removed from this
ADR** (see §11.17). Back-pressure between stages
is a real concern; it is being addressed as a
separate ADR (ADR-070, Worker-Level
Back-Pressure) that extends `@tool_worker` /
`WorkerManager` rather than introducing a new
Concordo.

#### 1.3.2 System shape — what ADR-018 commits us to

The framework's reactive dispatcher (ADR-018)
already commits to a single system shape:

```python
System: (World) -> list[Event]
```

The legacy `(world, event)` shape was deprecated
by ADR-018 §3 (see the historical note in
`src/kntgraph/core/system.py`). All systems in
this ADR follow the post-ADR-018 shape: they read
from the post-fold `World` via
`world.query_agents(...)` or
`world.get_agent(agent_id).components.get(...)`
and emit events. No system receives a triggering
event parameter.

This ADR defines two Concordos:
**C-01 BusinessFSM** and **C-02 WorkflowSaga**.

---

## 2. The Specification Pattern

Before describing the Concordos, this section
establishes the shared condition language used by
both BusinessFSM and WorkflowSaga.

### 2.1 Motivation

Both Concordos need to express business rules as
conditions: "should this transition be allowed?",
"should this step be skipped?", "should this step
be compensated?". Using enum strings (`REQUIRED`,
`OPTIONAL`) or ad-hoc lambdas produces rules that
are not composable, not named, and not testable in
isolation.

The Specification Pattern (Evans, *Domain-Driven
Design*, 2003) solves this: a specification is a
**named, composable predicate** over a domain
object. Two specifications can be combined into a
third using AND, OR, NOT — yielding a rule algebra
that reads like a business requirements document.

### 2.2 Protocol

The protocol is a single-method ABC plus a
`Composable` mixin that provides `and_/or_/not_`
as default implementations. This eliminates the
~30 lines of boilerplate per concrete Specification
that the previous draft required and removes the
LSP inconsistency between Protocol and concrete
`AndSpec`/`OrSpec`/`NotSpec` shapes.

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue
    from kntgraph.core.world.component import DomainComponent
    from kntgraph.core.world.world import World
    from kntgraph.core.world.view import AgentView
    from kntgraph.core.components.memory import (
        ContinuityComponent,
        ProfileComponent,
    )


@dataclass(frozen=True, slots=True)
class StepContext:
    """
    Read-only view passed to Specifications for evaluation.

    Contains the framework components that might be
    relevant to a business rule. All component
    fields are optional because not every agent has
    every component populated.

    ``now`` is the dispatcher's current tick
    timestamp (injected by the system that builds
    the context; see §3.4 and §4.5 for the
    injection pattern). Specifications MUST treat
    ``now`` as the only clock source; reading
    ``datetime.now()`` inside ``is_satisfied_by``
    would break replay determinism.

    **World access policy.** ``world`` is the
    full post-fold ``World``. Specifications MAY
    read any agent's view via
    ``world.get_agent(agent_id)`` or iterate via
    ``world.agents``. This is required for
    cross-agent rules (e.g. "only proceed if the
    financial-control agent's tier is VIP") and
    is the documented escape hatch for
    Specifications that need global state. The
    precedent is set by
    ``MemoryConsolidationSystem`` and
    ``SolutionExtractorSystem`` (both iterate
    ``world.agents`` to reason about other
    agents in the same tick).

    Specifications MUST NOT mutate the World,
    emit events, or perform I/O. The
    ``is_satisfied_by`` method is pure; the
    emitted events belong to the system that
    called the Specification, not to the
    Specification itself.
    """
    step_results: MappingProxyType[str, "JsonValue"]
    step_states: MappingProxyType[str, str]
    domain: "DomainComponent | None"
    continuity: "ContinuityComponent | None"
    profile: "ProfileComponent | None"
    world: "World"
    agent_id: str
    now: datetime


class Composable:
    """
    Mixin providing ``and_``, ``or_``, ``not_``
    combinators as default implementations.

    Concrete Specifications inherit this mixin and
    only implement ``is_satisfied_by``. The combinators
    return typed ``AndSpec`` / ``OrSpec`` / ``NotSpec``
    instances, which themselves compose further via the
    same mixin (see §2.2.1). The mixin is intentionally
    NOT a Protocol with ``runtime_checkable``: every
    concrete Specification must explicitly inherit
    ``Composable`` (a Protocol would let implementations
    silently forget to include it).
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

    Inspired by Evans DDD §9 (Specification Pattern).
    All implementations must be:

    - Pure (no I/O, no side effects)
    - Immutable (frozen dataclass or equivalent)
    - Compositional via the ``Composable`` mixin
    """

    @abstractmethod
    def is_satisfied_by(self, ctx: StepContext) -> bool: ...


# ---------------------------------------------------------------------
# 2.2.1 Combinator concrete classes
#
# They inherit ``Specification`` AND ``Composable`` so
# that ``AndSpec.and_(...)`` returns another ``AndSpec``
# (not a generic ``Specification``). This makes the
# fluent chain ``A.and_(B).and_(C)`` type as
# ``AndSpec`` end-to-end, which is what the LSP expects
# from the original draft.
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AndSpec(Specification):
    left: Specification
    right: Specification

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return (
            self.left.is_satisfied_by(ctx)
            and self.right.is_satisfied_by(ctx)
        )


@dataclass(frozen=True, slots=True)
class OrSpec(Specification):
    left: Specification
    right: Specification

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return (
            self.left.is_satisfied_by(ctx)
            or self.right.is_satisfied_by(ctx)
        )


@dataclass(frozen=True, slots=True)
class NotSpec(Specification):
    inner: Specification

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return not self.inner.is_satisfied_by(ctx)
```

### 2.3 Built-in Specifications

The framework ships a standard library of
Specifications that cover the most common conditions.
Each inherits ``Specification`` (which provides the
``Composable`` mixin's ``and_/or_/not_``); concrete
classes only declare their data fields and implement
``is_satisfied_by``.

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue


@dataclass(frozen=True, slots=True)
class StepCompleted(Specification):
    """True when the named step completed successfully."""
    step_name: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return ctx.step_states.get(self.step_name) == "completed"


@dataclass(frozen=True, slots=True)
class StepFailed(Specification):
    """True when the named step failed (any reason)."""
    step_name: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return ctx.step_states.get(self.step_name) in ("failed", "timed_out")


@dataclass(frozen=True, slots=True)
class StepTimedOut(Specification):
    """True when the named step timed out (ADR-045)."""
    step_name: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return ctx.step_states.get(self.step_name) == "timed_out"


@dataclass(frozen=True, slots=True)
class StepResultEquals(Specification):
    """
    True when a field in a step result equals a value.

    ``value`` is ``JsonValue`` (not ``object``): the
    framework's type-discipline skill forbids bare
    ``object`` in framework code. Tool workers return
    ``Mapping[str, JsonValue]`` payloads; comparing
    against an arbitrary Python object would silently
    always be False and mask bugs.
    """
    step_name: str
    field: str
    value: "JsonValue"

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        result = ctx.step_results.get(self.step_name)
        if not isinstance(result, Mapping):
            return False
        return result.get(self.field) == self.value


@dataclass(frozen=True, slots=True)
class DomainStateIs(Specification):
    """True when the DomainComponent has a specific state value."""
    field: str
    value: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        if ctx.domain is None:
            return False
        return getattr(ctx.domain, self.field, None) == self.value


@dataclass(frozen=True, slots=True)
class ProfileTierIs(Specification):
    """True when ProfileComponent.tier matches."""
    tier: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        if ctx.profile is None:
            return False
        return ctx.profile.tier == self.tier


@dataclass(frozen=True, slots=True)
class ContinuityToolUsed(Specification):
    """
    True when the named tool appears in
    ``ContinuityComponent.last_tools`` (i.e. it was the
    last tool invoked in the recent window).

    Note: the previous draft called this
    ``ContinuityUsageBelow`` and assumed a numeric
    ``last_tools_used`` field. The actual component
    (ADR-042 §2.3) carries ``last_tools: dict[str, str]``
    where the value is the tool's last invocation
    timestamp, not a usage count. Quota enforcement is
    a different concern (it lives on ``ProfileComponent``
    or a dedicated QuotaComponent) and is out of scope
    for this ADR.
    """
    tool_name: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        if ctx.continuity is None:
            return False
        return self.tool_name in ctx.continuity.last_tools
```

### 2.4 Domain-specific Specifications

Business verticals define their own Specifications
using the same protocol. Vertical Specifications
**must** be zero-argument instantiable unless they
genuinely need parameters — the FSM/Saga examples in
§3.6 and §4.8 assume ``NfeRequired()`` /
``TaxRegimeIs("simples")`` syntax. A Specification
that requires runtime configuration (e.g. an injected
clock) is broken by construction: configuration
belongs in the Concordo config, not in the rule.

```python
# In fmh_office/concordos/specs.py

from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class NfeRequired(Specification):
    """
    True when fiscal validation indicates NF-e is required.

    Reads ``ctx.step_results["validate_fiscal"]["nfe_required"]``.
    Defaults to ``True`` (assume NF-e is required unless
    the validator explicitly says otherwise — a safe
    default for Brazilian fiscal documents).
    """

    default: bool = True

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        result = ctx.step_results.get("validate_fiscal")
        if not isinstance(result, Mapping):
            return self.default
        return bool(result.get("nfe_required", self.default))


@dataclass(frozen=True, slots=True)
class TaxRegimeIs(Specification):
    """True when the domain component declares the given tax regime."""
    regime: str  # "simples" | "lucro_real" | "lucro_presumido"

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return DomainStateIs("tax_regime", self.regime).is_satisfied_by(ctx)
```

---

## 3. C-01 — BusinessFSM

### 3.1 What it is

A **BusinessFSM** (Business Finite State Machine)
declares the lifecycle of a business object as an
explicit state machine over a `DomainComponent`.

It is a **pure reactive system**: no I/O, no tool
calls. It reacts to domain events, validates
transitions, and emits `fsm.transitioned` or
`fsm.transition_rejected`. State is carried by the
`DomainComponent` — the FSM does not duplicate it.

**Conceptual difference from WorkflowSaga:**

| | BusinessFSM | WorkflowSaga |
|---|---|---|
| Purpose | Guard and record state | Drive and compensate operations |
| I/O | None (pure) | Tool workers per step |
| Duration | One fold/tick | Multiple ticks (async) |
| Compensation | None (rejections, not rollbacks) | Explicit per step |
| Reads from | `DomainComponent` | `DomainComponent` + `ContinuityComponent` + `ToolCallCompletion` |
| Emits | `fsm.transitioned` / `fsm.transition_rejected` | `tool.<name>.requested` / `saga.<name>.completed` |

They compose naturally: the Saga drives execution;
upon completion it emits an event that the FSM uses
to transition state.

### 3.2 Configuration

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from kntgraph.core.world.component import DomainComponent


@dataclass(frozen=True, slots=True)
class FSMTransition:
    """
    A single declared transition.

    ``to``    -- target state.
    ``guard`` -- optional Specification evaluated before
                 allowing the transition. If the guard is
                 not satisfied, ``fsm.transition_rejected``
                 is emitted with reason="guard_failed".
    """
    to: str
    guard: Specification | None = None


@dataclass(frozen=True, slots=True)
class FSMConfig:
    """
    Declares a finite state machine over a DomainComponent.

    ``component_type`` -- the DomainComponent subclass whose
                          ``state_field`` holds the current state.
    ``state_field``    -- attribute name on the component (str).
    ``transitions``    -- dict[from_state, dict[event_type, FSMTransition]]
    ``on_entry``       -- event_type to emit when entering a state
                          (optional; dict[to_state, event_type]).
    ``terminal``       -- states from which no transition is allowed.
    """
    component_type: type["DomainComponent"]
    state_field: str
    transitions: Mapping[str, Mapping[str, FSMTransition]]
    on_entry: Mapping[str, str] = field(default_factory=dict)
    terminal: frozenset[str] = field(default_factory=frozenset)
```

### 3.3 ECS Component

The FSM does not introduce a new component for
state — state is in the existing `DomainComponent`
(in `kntgraph.core.world.component`). It introduces
one component for audit:

```python
@dataclass(frozen=True, slots=True)
class FSMAuditComponent:
    """
    Last transition record for this agent.

    Materialised from ``fsm.transitioned`` events.
    Read-only for external systems.
    """
    from_state: str
    to_state: str
    trigger_event_type: str
    trigger_event_id: str
    transitioned_at: datetime
    guard_evaluated: bool
```

### 3.4 WorldSystem implementation

```python
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from kntgraph.core.event.constants import EventClass  # "domain" | "lifecycle"
from kntgraph.core.event.correlation import correlation_middleware
from kntgraph.core.clock import injectable_clock
from kntgraph.core.world.component import DomainComponent

if TYPE_CHECKING:
    from kntgraph.core.components.memory import (
        ContinuityComponent,
        ProfileComponent,
    )
    from kntgraph.core.clock import Clock
    from kntgraph.core.event.correlation import CorrelationContext
    from kntgraph.core._typing import JsonValue
    from kntgraph.core.event.event import Event
    from kntgraph.core.world.world import World
    from kntgraph.core.world.view import AgentView
    from .base import ViewTrigger


@dataclass(frozen=True, slots=True)
class FSMSystem:
    """
    C-01: BusinessFSM — WorldSystem (post-ADR-018 shape).

    Reads the DomainComponent state from the
    post-fold ``World``, scans every agent whose
    archetype carries the configured component,
    and validates each incoming event against the
    declared transition table.

    Emits (per matching event):

    - ``fsm.transitioned``         on success
    - ``fsm.transition_rejected``  on invalid transition,
      failed guard, or terminal-state violation

    The system is **pure**: same ``World`` ⇒ same
    ``list[Event]``. ``now`` is injected (see
    ``__init__``); it defaults to the framework clock so
    tests inject a fixed ``datetime`` for deterministic
    replay.
    """

    def __init__(
        self,
        config: FSMConfig,
        *,
        now: "Clock | None" = None,
    ) -> None:
        self._cfg = config
        self._now = injectable_clock(now)

    def __call__(self, world: "World") -> list["Event"]:
        out: list[Event] = []
        for view in world.query_agents(self._cfg.component_type):
            for event in self._events_for_agent(view, world):
                out.append(event)
        return out

    def _events_for_agent(
        self, view: "AgentView", world: "World"
    ) -> list["Event"]:
        component = view.get_component(self._cfg.component_type)
        if component is None:
            return []
        current_state: str = getattr(component, self._cfg.state_field)

        # The trigger (the event that MAY justify a
        # transition) is derived from the existing view
        # fields — the framework does NOT add a
        # ``view.last_event`` envelope (see §11.16 for
        # why that extension was dropped). The default
        # projection already records the trigger's
        # event_type in ``view.domain_phase`` (the last
        # domain event) and its id in ``view.last_event_id``
        # (see ``core/world/projection.py``). This mirrors
        # the ``_BaseRoleSystem`` precedent
        # (``agents/role_systems/_base.py``), which reads
        # ``view.last_event_id`` + ``view.components`` and
        # never needs the envelope.
        #
        # The correlation for events this system emits is
        # taken from ``correlation_middleware.current()``
        # (ADR-037): the Runner / ReactiveDispatcher wrap
        # every tick in ``correlation_middleware.scope()``,
        # so inside a tick a non-None context is guaranteed.
        # A ``ViewTrigger`` is a tiny read-only carrier of
        # the trigger surface the emit helpers need; it is
        # NOT a framework type and holds no event history.
        # The FSM leaves ``data``/``causation_id`` at their
        # defaults — only the Saga reads them.
        trigger_type = view.domain_phase
        if trigger_type is None:
            # No domain event yet for this agent — the
            # FSM has nothing to react to.
            return []

        # Single-slot caveat (see §11.18.1): when the fold
        # surfaces more than one domain event in the same
        # tick, ``view.last_event_id`` is the LAST one. The
        # FSM follows the ``ToolCallTTLSweeperSystem``
        # precedent: it keeps a ``last_processed_event_id``
        # cursor on ``FSMAuditComponent`` and re-derives
        # missed triggers from the EventLog between the
        # cursor and ``view.last_event_id``. On the happy
        # path (one event per tick, the common case) the
        # cursor matches and no scan runs. The scan is
        # sketched at the end of §11.16.
        trigger = ViewTrigger(
            agent_id=view.agent_id,
            event_type=trigger_type,
            event_id=UUID(str(view.last_event_id))
            if view.last_event_id is not None
            else None,
            correlation=correlation_middleware.current(),
        )

        if current_state in self._cfg.terminal:
            return [self._rejected(
                trigger, current_state, reason="terminal_state"
            )]

        allowed = self._cfg.transitions.get(current_state, {})
        transition = allowed.get(trigger.event_type)
        if transition is None:
            return [self._rejected(
                trigger, current_state, reason="transition_not_declared"
            )]

        if transition.guard is not None:
            ctx = StepContext(
                step_results=MappingProxyType({}),
                step_states=MappingProxyType({}),
                domain=component,
                continuity=view.get_component(ContinuityComponent),
                profile=view.get_component(ProfileComponent),
                world=world,
                agent_id=view.agent_id,
                now=self._now(),
            )
            if not transition.guard.is_satisfied_by(ctx):
                return [self._rejected(
                    trigger, current_state, reason="guard_failed"
                )]

        out = [self._transitioned(trigger, current_state, transition.to)]

        entry_type = self._cfg.on_entry.get(transition.to)
        if entry_type is not None:
            out.append(self._entry_event(
                trigger, transition.to, entry_type
            ))

        return out

    def _transitioned(
        self,
        trigger: "ViewTrigger",
        from_state: str,
        to_state: str,
    ) -> "Event":
        return Event.create(
            agent_id=trigger.agent_id,
            event_type="fsm.transitioned",
            event_class="domain",
            data={
                "from": from_state,
                "to": to_state,
                "trigger": trigger.event_type,
                "trigger_event_id": str(trigger.event_id),
            },
            causation_id=trigger.event_id,
            correlation=trigger.correlation,
        )

    def _rejected(
        self,
        trigger: "ViewTrigger",
        current_state: str,
        reason: str,
    ) -> "Event":
        return Event.create(
            agent_id=trigger.agent_id,
            event_type="fsm.transition_rejected",
            event_class="domain",
            data={
                "current_state": current_state,
                "trigger": trigger.event_type,
                "reason": reason,
            },
            causation_id=trigger.event_id,
            correlation=trigger.correlation,
        )

    def _entry_event(
        self,
        trigger: "ViewTrigger",
        state: str,
        entry_type: str,
    ) -> "Event":
        return Event.create(
            agent_id=trigger.agent_id,
            event_type=entry_type,
            event_class="domain",
            data={"state": state},
            causation_id=trigger.event_id,
            correlation=trigger.correlation,
        )
```

**Why `now` is injected, not called inline.**
The previous draft read ``datetime.now()`` inside
``__call__``. A replayed log would then re-evaluate
guards with a different ``now`` than the original
run, breaking the "same World ⇒ same list[Event]"
contract the FSM claims to honour. Injecting the
clock follows the precedent of
``ToolCallTTLSweeperSystem.__init__(now=...)`` in
``src/kntgraph/runner/tool_call_ttl_sweeper.py``.

**Clock module.** The ``now: Callable[[], datetime] |
None`` injection, the ``now or utcnow`` fallback, and the
``utcnow`` default are defined once in a new framework
module, ``src/kntgraph/core/clock.py`` (see §12). All
Concordo systems import their clock source from there;
they do not re-declare the type or the fallback inline.

**`ViewTrigger`.** The FSM never reads the full event
envelope; it needs only ``agent_id``, ``event_type``,
``event_id`` and ``correlation``. These are carried by a
tiny read-only dataclass:

```python
@dataclass(frozen=True, slots=True)
class ViewTrigger:
    """Read-only carrier of the trigger surface the
    FSM/Saga emit helpers consume. Built by the system
    from the view (``domain_phase`` / ``last_event_id`` /
    the component keyed by ``domain_phase``) and
    ``correlation_middleware.current()``.

    ``data`` mirrors the last domain event's payload (the
    default fold installs it under the component keyed by
    ``event_type``), so ``_dispatch_step`` can read
    ``trigger.data["saga_id"]`` and the ``enrich_from``
    enrichment reads ``trigger.data["step_results"]``.

    ``causation_id`` is the id of the event that caused
    this trigger. The view does not carry it for tool
    completions; ``SagaSystem._match_step`` recovers it
    from the ``tool_completions`` slot instead (the
    request's ``event_id``). For saga-start /
    timeout / compensation events it equals ``event_id``.

    NOT a framework type and holds no event history.
    """
    agent_id: str
    event_type: str
    event_id: UUID | None
    data: Mapping[str, "JsonValue"]
    correlation: "CorrelationContext"
    causation_id: UUID | None = None
```

The type is private to ``concordos/base.py``; the FSM and
Saga systems share it. It exists so the emit helpers take
one small argument instead of four, and so the ADR does
not couple the systems to ``Event`` for reading a trigger
that never came from a live envelope.

### 3.5 Concordo class

```python
class BusinessFSMConcordo:
    """
    C-01: BusinessFSM Concordo (Concordo Protocol §1.3.1).

    Registers a single ``FSMSystem`` on the
    dispatcher. The dispatcher invokes ``install``
    idempotently: ``ConcordoCatalog.install_all``
    (§6.3) dedupes by ``name`` before calling it.
    """

    def __init__(self, config: FSMConfig) -> None:
        self._config = config
        self.name = f"fsm:{config.component_type.__name__}"
        self.version = "1.0.0"

    def install(self, dispatcher: "ReactiveDispatcher") -> None:
        dispatcher.add_system(FSMSystem(self._config))
```

The trigger set from the previous draft is gone:
the post-ADR-018 dispatcher runs every registered
system on every tick and lets each system filter
via ``world.query_agents`` and the per-view event
walk. Trigger-based dispatch is a pre-ADR-018
optimisation; the new dispatcher no longer needs
it (see ``src/kntgraph/runner/reactive.py``).
```

### 3.6 Example — invoice lifecycle

```python
# fmh_office/concordos/invoice_fsm.py

from kntgraph.concordos.fsm import (
    BusinessFSMConcordo, FSMConfig, FSMTransition
)
from fmh_office.components import InvoiceDomainComponent
from fmh_office.concordos.specs import NfeRequired
from kntgraph.concordos.specs import ContinuityToolUsed

invoice_fsm = BusinessFSMConcordo(FSMConfig(
    component_type=InvoiceDomainComponent,
    state_field="status",
    transitions={
        "draft": {
            "invoice.submitted": FSMTransition(to="validating"),
        },
        "validating": {
            "invoice.approved": FSMTransition(
                to="issued",
                guard=NfeRequired().and_(
                    ContinuityToolUsed("nfe_emitter").not_()
                ),
            ),
            "invoice.approved_bypass": FSMTransition(
                to="issued",
                guard=NfeRequired().not_(),
            ),
            "invoice.rejected": FSMTransition(to="draft"),
        },
        "issued": {
            "payment.received":  FSMTransition(to="paid"),
            "invoice.cancelled": FSMTransition(to="cancelled"),
            "invoice.overdue":   FSMTransition(to="overdue"),
        },
        "overdue": {
            "payment.received":  FSMTransition(to="paid"),
            "invoice.cancelled": FSMTransition(to="cancelled"),
        },
    },
    on_entry={
        "issued":    "invoice.issuance_confirmed",
        "paid":      "invoice.payment_confirmed",
        "cancelled": "invoice.cancellation_confirmed",
    },
    terminal=frozenset({"paid", "cancelled"}),
))
```

The `guard=NfeRequired().and_(ContinuityToolUsed("nfe_emitter").not_())`
reads as: "approve the issuance iff NF-e is required
AND the nfe_emitter tool was NOT the last tool used
in the recent continuity window." The latter rule
prevents re-emitting NF-e immediately after a previous
emission that has not yet aged out of the window.
The previous draft conflated this with quota
enforcement; see §2.3 `ContinuityToolUsed` for the
reasoning.

### 3.7 Unit tests

```python
# tests/unit/concordos/test_invoice_fsm.py
#
# These tests follow the project's behaviour-test
# convention (AGENTS.md §7): they construct a real
# ``World`` via ``project_tool_calls`` (or its
# composition with ``MemoryHydrationProjection``)
# and call the system against it. No mocks on
# ``ReactiveDispatcher``. They run with
# ``KNT_REDIS_FAKE=1``.

from datetime import datetime, timezone
from kntgraph.core.world.component import DomainComponent
from kntgraph.core.world.world import World
from kntgraph.runner.world_test_helpers import make_world_with_components


@dataclass(frozen=True, slots=True)
class InvoiceDomainComponent(DomainComponent):
    status: str = "draft"
    tax_regime: str = "lucro_real"


FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def test_fsm_allows_valid_transition() -> None:
    """
    Given:  InvoiceDomainComponent.status = "validating".
    When:   the last domain event is invoice.approved;
            NfeRequired satisfied and the nfe_emitter tool
            is NOT in the recent-continuity window.
    Then:   fsm.transitioned + invoice.issuance_confirmed.
    """
    view = make_world_with_components(
        agent_id="inv-1",
        components={
            InvoiceDomainComponent: InvoiceDomainComponent(
                status="validating", tax_regime="lucro_real"
            ),
        },
        # The trigger is derived from ``domain_phase``
        # (the last domain event's type); no envelope is
        # needed. ``last_event_id`` seeds the cursor.
        last_event_type="invoice.approved",
    )
    world = World.empty().with_agent(view)
    out = FSMSystem(invoice_fsm._config, now=lambda: FIXED_NOW)(world)
    types = [e.event_type for e in out]
    assert "fsm.transitioned" in types
    assert "invoice.issuance_confirmed" in types


def test_fsm_rejects_terminal_state() -> None:
    """
    Given:  InvoiceDomainComponent.status = "paid" (terminal).
    When:   the last domain event is invoice.submitted.
    Then:   fsm.transition_rejected with reason="terminal_state".
    """
    view = make_world_with_components(
        agent_id="inv-1",
        components={
            InvoiceDomainComponent: InvoiceDomainComponent(status="paid"),
        },
        last_event_type="invoice.submitted",
    )
    world = World.empty().with_agent(view)
    out = FSMSystem(invoice_fsm._config, now=lambda: FIXED_NOW)(world)
    assert len(out) == 1
    assert out[0].event_type == "fsm.transition_rejected"
    assert out[0].data["reason"] == "terminal_state"


def test_fsm_guard_blocks_when_nfe_emitter_was_last() -> None:
    """
    Given:  ContinuityComponent.last_tools contains "nfe_emitter"
            (i.e. it was the most recent tool invoked).
    When:   the last domain event is invoice.approved.
    Then:   fsm.transition_rejected with reason="guard_failed".
    """
    from kntgraph.core.components.memory import ContinuityComponent

    view = make_world_with_components(
        agent_id="inv-1",
        components={
            InvoiceDomainComponent: InvoiceDomainComponent(
                status="validating"
            ),
            ContinuityComponent: ContinuityComponent(
                last_tools={"nfe_emitter": "2026-09-07T11:59:00Z"},
            ),
        },
        last_event_type="invoice.approved",
    )
    world = World.empty().with_agent(view)
    out = FSMSystem(invoice_fsm._config, now=lambda: FIXED_NOW)(world)
    assert out[0].event_type == "fsm.transition_rejected"
    assert out[0].data["reason"] == "guard_failed"
```

The ``make_world_with_components`` helper is introduced
in the same PR as the FSM/Saga systems
(`src/kntgraph/runner/world_test_helpers.py`); it
builds a ``World`` with one agent and attaches the given
components plus the trigger surface directly on the
view — without going through Redis or building an event
envelope (see §11.16):

  - ``last_event_type``: seeds ``view.domain_phase``
    (the last domain event's type, which the FSM/Saga
    use as the trigger predicate).
  - ``last_event_data``: optional dict attached as the
    component keyed by ``last_event_type`` (the default
    fold installs the event payload there); the Saga
    reads it for ``data.get("saga_id")`` and
    ``enrich_from``.
  - ``tool_completions``: optional mapping of
    ``request_event_id -> ToolCallCompletion`` installed
    in the ``tool_completions`` slot so the Saga's
    ``_match_step`` / ``_completion_for_step`` can join.

A unique ``last_event_id`` is generated when
``last_event_type`` is set, so a cursor-aware system
sees a move and processes the trigger once. The
``now=lambda: FIXED_NOW`` injection ensures the guard's
``now`` is deterministic.

---

## 4. C-02 — WorkflowSaga

### 4.1 What it is

A **WorkflowSaga** orchestrates a **sequence of tool
calls** with context enrichment, skip conditions,
failure policies, and compensation.

**Critical design principle:** the Saga does **not**
reinvent the tool call lifecycle. It is built
entirely on top of existing framework primitives:

| Lifecycle aspect | Existing primitive | Saga's role |
|---|---|---|
| In-flight tracking | `ToolCallRequest` (ADR-034) | None — reads from AgentView |
| Resolution tracking | `ToolCallCompletion` (ADR-034) | Reacts to `.completed` / `.failed` events |
| Step timeout | `tool.<name>.timed_out` (ADR-045) | Treats `timed_out` as failure variant |
| Saga-level timeout | None | **New: `SagaTimeoutSystem` CyclicSystem** |
| Context enrichment | `ContinuityComponent` (ADR-042) | Reads before dispatching each step |
| Sequencing + compensation | None | **New: `SagaProgressComponent` + `SagaSystem`** |

### 4.2 Supervision model

The Saga requires two supervision layers:

```
Layer 1 — Step timeout (ADR-045 already covers this):
  tool.<name>.requested emitted
  → TTL clock starts (ADR-045)
  → if no completion within step TTL:
      tool.<name>.timed_out emitted
  → SagaSystem reacts: treats as step failure

Layer 2 — Saga-level timeout (Saga adds this):
  saga.<name>.started emitted
  → SagaTimeoutSystem (CyclicSystem) scans every tick
  → if elapsed > saga_timeout_ms:
      saga.<name>.timed_out emitted
  → SagaSystem reacts: initiates compensation

Layer 3 — Agent liveness (Runner already covers this):
  existing CyclicSystem in the Runner
  → detects agents stuck without events
  → outside Saga scope
```

### 4.3 Configuration

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import Specification


@dataclass(frozen=True, slots=True)
class SagaStepConfig:
    """
    Configuration for a single saga step.

    ``name``             -- unique step identifier within the saga.
    ``tool_name``        -- registered @tool_worker name (ADR-036).
                            ``None`` declares a "human step"
                            (ADR-069 §9.2 item 3): the saga
                            blocks until an external
                            ``saga.<step>.approved`` event
                            arrives. Such steps get NO
                            ADR-045 TTL registration.
    ``compensate_tool``  -- @tool_worker called on rollback; None if
                            the step produces no compensable effect.
    ``skip_when``        -- Specification; step is skipped (not
                            dispatched) when satisfied.
    ``proceed_when``     -- Specification evaluated after step
                            completes; if not satisfied, treated as
                            failure even if tool returned success.
    ``compensate_when``  -- Specification evaluated when deciding
                            whether to compensate; None means always
                            compensate when rolling back.
    ``enrich_from``      -- field names to inject into tool params
                            before dispatch. Read from the previous
                            step's ``step_results``; if missing, the
                            field is omitted (no implicit
                            ``None``).
    ``timeout_ms``       -- step-level timeout (passed to ADR-045
                            TTL registration on dispatch).
                            Ignored for human steps.
    """
    name: str
    tool_name: str | None
    compensate_tool: str | None = None
    skip_when: "Specification | None" = None
    proceed_when: "Specification | None" = None
    compensate_when: "Specification | None" = None
    enrich_from: tuple[str, ...] = ()
    timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        # Tool name must be non-empty when present.
        # ``None`` is the explicit signal for a human
        # step; an empty string is a typo.
        if self.tool_name is not None and not self.tool_name:
            raise ValueError(
                f"SagaStepConfig.tool_name must be a non-empty "
                f"string or None (human step); got empty string "
                f"for step {self.name!r}."
            )


@dataclass(frozen=True, slots=True)
class SagaConfig:
    """
    Configuration for a WorkflowSaga.

    ``name``            -- unique saga identifier.
    ``steps``           -- ordered tuple of step configs.
    ``fail_when``       -- Specification evaluated after every step
                           failure; saga fails when satisfied.
                           Default: fail on first REQUIRED step failure.
    ``saga_timeout_ms`` -- wall-clock timeout for the entire saga;
                           enforced by SagaTimeoutSystem (CyclicSystem).
    """
    name: str
    steps: tuple[SagaStepConfig, ...]
    fail_when: "Specification | None" = None   # None = fail on first failure
    saga_timeout_ms: int = 300_000
```

### 4.4 ECS Component

```python
from types import MappingProxyType
from typing import TYPE_CHECKING

from kntgraph.core.world.component import DomainComponent

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue


@dataclass(frozen=True, slots=True)
class SagaProgressComponent(DomainComponent):
    """
    C-02: WorkflowSaga — saga execution state.

    Source of truth for the *execution* fields
    (``saga_id``, ``saga_name``, ``current_step``,
    ``direction``, ``started_at``). These are written
    when the corresponding ``saga.<name>.started`` /
    ``saga.<name>.compensating`` / etc. events are
    appended and the saga-system projection
    (registered alongside the saga itself) materialises
    the component.

    Source of truth for the *history* fields
    (``step_states``, ``step_results``,
    ``compensate_stack``) is the EventLog. The
    component carries them as a CACHE for system
    reads; a re-fold of the EventLog MUST reconstruct
    them deterministically via the projection. See
    §9.2 item 6 for the reconciliation rule.

    Tool call state (in-flight, completed, failed)
    lives in ``ToolCallRequest`` / ``ToolCallCompletion``
    (ADR-034) and is read from the agent's view —
    NOT duplicated here.

    Archetype evolution:
      Running:     {SagaProgressComponent}
      Completed:   {SagaProgressComponent}  (direction="done")
      Compensated: {SagaProgressComponent}  (direction="compensated")
      Failed:      {SagaProgressComponent}  (direction="compensation_failed")
    """
    saga_id: str
    saga_name: str
    current_step: str
    direction: str
    # "forward" | "compensating" | "done" | "compensated" |
    # "compensation_failed"
    step_order: tuple[str, ...]            # declared order (immutable)
    step_states: MappingProxyType[str, str]
    # step_name -> "pending" | "skipped" | "in_flight"
    #              "completed" | "failed" | "timed_out"
    #              "compensated" | "compensation_failed"
    step_results: MappingProxyType[str, "JsonValue"]
    # step_name -> result dict from ToolCallCompletion.result
    compensate_stack: tuple[str, ...]      # LIFO; steps pending compensation
    started_at: datetime
```

### 4.5 SagaSystem (WorldSystem)

```python
from __future__ import annotations
import dataclasses
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable

from kntgraph.core.event.event import Event
from kntgraph.core.clock import injectable_clock
from kntgraph.core.world.component import DomainComponent
from kntgraph.core.world.components import ToolCallCompletion

from .base import StepContext, ViewTrigger
from ._components import SagaProgressComponent

if TYPE_CHECKING:
    from kntgraph.core.clock import Clock
    from kntgraph.core.components.memory import (
        ContinuityComponent,
        ProfileComponent,
    )
    from kntgraph.core._typing import JsonValue
    from kntgraph.core.world.world import World
    from kntgraph.core.world.view import AgentView


@dataclass(frozen=True, slots=True)
class SagaSystem:
    """
    C-02: WorkflowSaga — WorldSystem (post-ADR-018).

    Reads from the post-fold ``World``. For every
    agent whose archetype carries a
    ``SagaProgressComponent``, walks the agent's
    recent events and reacts to:

    - ``saga.<name>.started``             → dispatch first step
    - ``tool.<name>.completed``           → advance or compensate
    - ``tool.<name>.failed``              → evaluate fail_when; compensate
    - ``tool.<name>.timed_out``           → treat as failed (ADR-045)
    - ``saga.<name>.timed_out``           → force compensation
                                            (SagaTimeoutSystem)
    - ``saga.<name>.compensation_failed`` → DLQ + alert (§4.5.1)

    Reads ``ToolCallCompletion`` from the
    ``tool_completions`` slot (already materialised
    by ``project_tool_calls``, ADR-034). Does NOT
    re-implement in-flight or resolution tracking.

    Pure: same ``World`` ⇒ same ``list[Event]``.
    ``now`` is injected (see ``__init__``) so
    replays are deterministic.
    """

    def __init__(
        self,
        config: "SagaConfig",
        *,
        now: "Clock | None" = None,
    ) -> None:
        self._cfg = config
        self._step_map = {s.name: s for s in config.steps}
        self._now = injectable_clock(now)

    def __call__(self, world: "World") -> list["Event"]:
        out: list[Event] = []
        for view in world.query_agents(SagaProgressComponent):
            out.extend(self._events_for_agent(view, world))
        return out

    def _events_for_agent(
        self, view: "AgentView", world: "World"
    ) -> list["Event"]:
        saga = view.get_component(SagaProgressComponent)
        if saga is None:
            return []

        # The trigger is derived from the view, exactly as
        # the FSM does (§3.4): ``view.domain_phase`` is the
        # last domain event's type, ``view.last_event_id``
        # its id, and ``view.components[trigger_type]`` its
        # data (the default fold installs the event payload
        # under a component keyed by the event_type —
        # ``core/world/projection.py``). The correlation is
        # taken from ``correlation_middleware.current()``
        # (non-None inside a tick, ADR-037). No
        # ``view.last_event`` envelope is required (see
        # §11.16 for why that extension was dropped).
        #
        # The saga's ``_match_step`` needs the trigger's
        # ``causation_id`` (the join key against the
        # ``tool_completions`` slot). The view does not
        # carry causation; for a completion event the
        # ``causation_id`` is the request's event_id, which
        # we recover from the ``tool_completions`` slot
        # when available (see ``_match_step``). For the
        # start/timeout events it is the trigger itself.
        trigger_type = view.domain_phase
        if trigger_type is None:
            return []
        trigger = ViewTrigger(
            agent_id=view.agent_id,
            event_type=trigger_type,
            event_id=UUID(str(view.last_event_id))
            if view.last_event_id is not None
            else None,
            data=view.components.get(trigger_type, {}),
            correlation=correlation_middleware.current(),
        )

        # Saga start
        if trigger.event_type == f"saga.{self._cfg.name}.started":
            return self._start(view, trigger, saga)

        # Saga-level timeout (from SagaTimeoutSystem)
        if trigger.event_type == f"saga.{self._cfg.name}.timed_out":
            if saga.direction == "forward":
                return self._begin_compensation(
                    world, view, saga, trigger, reason="saga_timeout"
                )
            return []

        # Compensation failure (§4.5.1): escalate to DLQ.
        if trigger.event_type == (
            f"saga.{self._cfg.name}.compensation_failed"
        ):
            return [self._dlq_event(saga, trigger)]

        # Tool completion / failure / timeout
        if not (
            trigger.event_type.startswith("tool.")
            and trigger.event_type.endswith(
                (".completed", ".failed", ".timed_out")
            )
        ):
            return []

        step_config = self._match_step(view, trigger, saga)
        if step_config is None:
            return []

        return self._handle_completion(view, world, saga, step_config, trigger)

    # ------------------------------------------------------------------
    # _match_step — the join key for saga ↔ tool completion.
    # ------------------------------------------------------------------
    def _match_step(
        self,
        view: "AgentView",
        trigger: "ViewTrigger",
        saga: SagaProgressComponent,
    ) -> "SagaStepConfig | None":
        """
        Find the saga step that the incoming tool-completion
        trigger belongs to.

        The join key is the trigger's ``causation_id``
        (== the originating ``tool.<name>.requested``
        event's ``event_id``). The view does not carry
        causation on the trigger; we recover it from the
        ``tool_completions`` slot by matching the step
        currently in flight. The dispatcher's
        ``project_tool_calls`` (ADR-034) materialises
        the resulting ``ToolCallCompletion`` in the
        agent's ``tool_completions`` slot, keyed by
        ``request_event_id``. We therefore:

          1. Find the completion whose ``tool_name``
             matches the saga step currently in
             flight (per ``saga.current_step``).
          2. Return that step's config; ``_handle_completion``
             reads the same slot for the result.

        If the completion is not in the slot (it has
        not yet been folded) the system emits no
        events; the next tick will re-run and pick it
        up. This is idempotent.
        """
        completions: "Mapping[str, ToolCallCompletion]" = (
            view.components.get("tool_completions", {})
        )
        step_cfg = self._step_map.get(saga.current_step)
        if step_cfg is None:
            return None
        for completion in completions.values():
            if completion.tool_name == step_cfg.tool_name:
                return step_cfg
        # Completion not yet folded into the view.
        # Wait for the next tick; do nothing this
        # tick to avoid double-dispatch on races.
        return None

    # ------------------------------------------------------------------
    # _start / _handle_completion / _advance / _handle_failure
    # ------------------------------------------------------------------
    def _start(
        self,
        view: "AgentView",
        trigger: "ViewTrigger",
        saga: SagaProgressComponent,
    ) -> list[Event]:
        """Dispatch the first non-skipped step."""
        step_config = self._first_non_skipped_step(saga, trigger)
        if step_config is None:
            # All steps skipped: saga completes immediately
            return [self._saga_completed(trigger, saga)]
        return [
            self._record_start(trigger, saga, step_config),
            self._dispatch_step(step_config, trigger),
        ]

    def _handle_completion(
        self,
        view: "AgentView",
        world: "World",
        saga: SagaProgressComponent,
        step_config: "SagaStepConfig",
        trigger: "ViewTrigger",
    ) -> list[Event]:
        status = trigger.event_type.rsplit(".", 1)[-1]
        # ToolCallCompletion already in AgentView (ADR-034).
        # The completion for this step's dispatch is found
        # by matching the step's tool_name against the
        # ``tool_completions`` slot (see ``_match_step``).
        completion = self._completion_for_step(view, step_config)
        result: dict = dict(completion.result or {}) if completion else {}

        new_states = dict(saga.step_states)
        new_states[step_config.name] = status

        new_results = dict(saga.step_results)
        new_results[step_config.name] = result

        ctx = StepContext(
            step_results=MappingProxyType(new_results),
            step_states=MappingProxyType(new_states),
            domain=view.get_component(DomainComponent),
            continuity=view.get_component(ContinuityComponent),
            profile=view.get_component(ProfileComponent),
            world=world,
            agent_id=view.agent_id,
            now=self._now(),
        )

        # Check proceed_when on success
        if status == "completed":
            if (
                step_config.proceed_when is not None
                and not step_config.proceed_when.is_satisfied_by(ctx)
            ):
                # Treat as failure: proceed condition not met
                new_states[step_config.name] = "failed"
                ctx = dataclasses.replace(
                    ctx,
                    step_states=MappingProxyType(new_states),
                )
                return self._handle_failure(
                    world, view, saga, step_config, trigger, ctx,
                    new_states, new_results,
                )
            return self._advance(
                saga, step_config, trigger, ctx, new_states, new_results
            )

        # Failure or timeout
        return self._handle_failure(
            world, view, saga, step_config, trigger, ctx,
            new_states, new_results,
        )

    def _completion_for_step(
        self,
        view: "AgentView",
        step_config: "SagaStepConfig",
    ) -> "ToolCallCompletion | None":
        """Return the ``ToolCallCompletion`` whose
        ``tool_name`` matches ``step_config.tool_name``
        and whose ``request_event_id`` is still in the
        ``tool_completions`` slot. ``None`` when the
        completion has not yet been folded (the next
        tick re-runs and picks it up)."""
        if step_config.tool_name is None:
            return None
        completions: "Mapping[str, ToolCallCompletion]" = (
            view.components.get("tool_completions", {})
        )
        for completion in completions.values():
            if completion.tool_name == step_config.tool_name:
                return completion
        return None

    def _advance(
        self,
        saga: SagaProgressComponent,
        current_step: "SagaStepConfig",
        trigger: "ViewTrigger",
        ctx: StepContext,
        new_states: dict,
        new_results: dict,
    ) -> list[Event]:
        """Move to the next non-skipped step or complete the saga."""
        next_step = self._next_non_skipped_step(current_step, ctx)
        record = self._record_step_completed(
            saga, current_step, trigger, new_states, new_results
        )
        if next_step is None:
            return [record, self._saga_completed(trigger, saga)]
        return [record, self._dispatch_step(next_step, trigger)]

    def _handle_failure(
        self,
        world: "World",
        view: "AgentView",
        saga: SagaProgressComponent,
        step_config: "SagaStepConfig",
        trigger: "ViewTrigger",
        ctx: StepContext,
        new_states: dict,
        new_results: dict,
    ) -> list[Event]:
        """Evaluate fail_when; begin compensation or continue."""
        fail_spec = self._cfg.fail_when
        should_fail = (
            fail_spec.is_satisfied_by(ctx)
            if fail_spec is not None
            else True  # default: fail on first failure
        )
        record = self._record_step_failed(
            saga, step_config, trigger, new_states, new_results
        )
        if should_fail:
            return [record] + self._begin_compensation(
                world, view, saga, trigger, reason="step_failure"
            )
        # continue to next step despite this step's failure
        next_step = self._next_non_skipped_step(step_config, ctx)
        if next_step is None:
            return [record, self._saga_completed(trigger, saga)]
        return [record, self._dispatch_step(next_step, trigger)]

    # ------------------------------------------------------------------
    # _begin_compensation — LIFO with per-step compensate_when
    # ------------------------------------------------------------------
    def _begin_compensation(
        self,
        world: "World",
        view: "AgentView",
        saga: SagaProgressComponent,
        trigger: "ViewTrigger",
        reason: str,
    ) -> list[Event]:
        """
        Emit compensation events in LIFO order.

        For each step on the compensate_stack (the
        steps that already produced an external
        effect and need to be rolled back), we check
        the step's ``compensate_when`` Specification.
        A step whose compensation would be a no-op
        (e.g. a timed-out NF-e emission that never
        landed) is skipped — see §4.8 example.

        If a compensation tool itself fails, the
        saga emits ``saga.<name>.compensation_failed``
        and the system routes the agent to the DLQ
        on the next tick (§4.5.1).
        """
        out: list[Event] = [
            Event.create(
                agent_id=trigger.agent_id,
                event_type=f"saga.{self._cfg.name}.compensating",
                event_class="domain",
                data={"reason": reason, "saga_id": saga.saga_id},
                causation_id=trigger.event_id,
                correlation=trigger.correlation,
            )
        ]
        ctx = StepContext(
            step_results=saga.step_results,
            step_states=saga.step_states,
            domain=None,
            continuity=None,
            profile=None,
            world=world,
            agent_id=view.agent_id,
            now=self._now(),
        )
        for step_name in reversed(saga.compensate_stack):
            step_cfg = self._step_map.get(step_name)
            if step_cfg is None or step_cfg.compensate_tool is None:
                continue
            if (
                step_cfg.compensate_when is not None
                and not step_cfg.compensate_when.is_satisfied_by(ctx)
            ):
                continue
            out.append(Event.create(
                agent_id=trigger.agent_id,
                event_type=f"tool.{step_cfg.compensate_tool}.requested",
                event_class="domain",
                data={
                    "saga_id": saga.saga_id,
                    "compensating_step": step_name,
                    **dict(saga.step_results.get(step_name, {})),
                },
                causation_id=trigger.event_id,
                correlation=trigger.correlation,
            ))
        return out

    # ------------------------------------------------------------------
    # _dispatch_step — emit tool.<name>.requested
    # ------------------------------------------------------------------
    def _dispatch_step(
        self,
        step_config: "SagaStepConfig",
        trigger: "ViewTrigger",
    ) -> "Event":
        """
        Emit ``tool.<name>.requested`` for the step.

        Human steps (``tool_name is None``) are NOT
        dispatched via ``tool.<name>.requested``.
        Instead they emit
        ``saga.<step_name>.awaiting_approval`` and the
        saga blocks until a corresponding
        ``saga.<step_name>.approved`` /
        ``saga.<step_name>.rejected`` event arrives
        (see §9.2 item 3 for the open question on
        human-step timeouts).
        """
        if step_config.tool_name is None:
            return Event.create(
                agent_id=trigger.agent_id,
                event_type=(
                    f"saga.{self._cfg.name}."
                    f"{step_config.name}.awaiting_approval"
                ),
                event_class="domain",
                data={"step_name": step_config.name},
                causation_id=trigger.event_id,
                correlation=trigger.correlation,
            )
        params: dict[str, "JsonValue"] = {
            "saga_id": trigger.data.get("saga_id", ""),
        }
        # Enrich from previous step results (read via the
        # trigger's data envelope — the saga-system
        # projection attaches the latest step_results to
        # the saga-component clone carried on the
        # dispatch event; see _record_step_completed).
        previous = trigger.data.get("step_results", {})
        if isinstance(previous, Mapping):
            for field in step_config.enrich_from:
                for prev_result in previous.values():
                    if (
                        isinstance(prev_result, Mapping)
                        and field in prev_result
                    ):
                        params.setdefault(
                            field, prev_result[field]
                        )
        return Event.create(
            agent_id=trigger.agent_id,
            event_type=f"tool.{step_config.tool_name}.requested",
            event_class="domain",
            data=params,
            causation_id=trigger.event_id,
            correlation=trigger.correlation,
        )

    # ------------------------------------------------------------------
    # _record_* helpers, _saga_completed, _first/_next_non_skipped_step
    # ------------------------------------------------------------------
    # The full implementations live in
    # ``src/kntgraph/concordos/saga/_state.py``
    # and ``_dispatch.py``; the layout follows the
    # 500-line guideline from AGENTS.md §3.

    # ------------------------------------------------------------------
    # 4.5.1 DLQ on compensation failure
    # ------------------------------------------------------------------
    def _dlq_event(
        self,
        saga: SagaProgressComponent,
        trigger: "ViewTrigger",
    ) -> "Event":
        """
        Build the DLQ-emission domain event for a saga
        whose compensation could not be completed.

        The actual DLQ insertion is performed by an
        adapter system that reads this event and
        appends to ``knt:dlq:saga:<name>`` (see
        ``src/kntgraph/infra/dlq.py``); the saga
        system only emits the typed event so the DLQ
        adapter stays out of the saga's dependency
        graph.
        """
        return Event.create(
            agent_id=trigger.agent_id,
            event_type=f"saga.{self._cfg.name}.dlq",
            event_class="domain",
            data={
                "saga_id": saga.saga_id,
                "stuck_step": saga.current_step,
                "step_states": dict(saga.step_states),
            },
            causation_id=trigger.event_id,
            correlation=trigger.correlation,
        )
```

### 4.6 SagaTimeoutSystem (WorldSystem)

```python
from kntgraph.core.event.id_helpers import (
    generate_deterministic_event_id,
)
from kntgraph.core.clock import injectable_clock
from kntgraph.core.event.correlation import correlation_middleware

if TYPE_CHECKING:
    from kntgraph.core.clock import Clock


@dataclass(frozen=True, slots=True)
class SagaTimeoutSystem:
    """
    C-02: WorkflowSaga — WorldSystem for saga-level timeout.

    Runs on every dispatcher tick. Detects sagas that
    have exceeded ``saga_timeout_ms`` and emits
    ``saga.<name>.timed_out``.

    **Determinism.** The emitted event's ``event_id``
    is computed by ``generate_deterministic_event_id``
    from ``(causation_id="root", agent_id,
    event_type, data)``. The data envelope includes
    the saga's ``started_at`` ISO string — so a tick
    that re-derives the same timeout produces the
    same ``event_id`` and is deduped by the
    EventLog's idempotency check.

    Individual step timeouts are handled by ADR-045
    (Tool Call TTL) and do not need to be checked
    here.
    """

    def __init__(
        self,
        configs: "Mapping[str, SagaConfig]",
        *,
        now: "Clock | None" = None,
    ) -> None:
        self._configs = configs
        self._now = injectable_clock(now)

    def __call__(self, world: "World") -> list["Event"]:
        now = self._now()
        out: list[Event] = []
        for view in world.query_agents(SagaProgressComponent):
            saga = view.get_component(SagaProgressComponent)
            if saga is None or saga.direction != "forward":
                continue
            config = self._configs.get(saga.saga_name)
            if config is None:
                continue
            elapsed_ms = (now - saga.started_at).total_seconds() * 1000
            if elapsed_ms <= config.saga_timeout_ms:
                continue
            data = {
                "saga_id": saga.saga_id,
                "elapsed_ms": elapsed_ms,
                "stuck_at_step": saga.current_step,
                "timeout_ms": config.saga_timeout_ms,
            }
            event_type = f"saga.{saga.saga_name}.timed_out"
            # Deterministic event_id — see §4.6 docstring.
            # The EventLog dedupes on this id, so a
            # second tick that derives the same data
            # produces the same event and is dropped.
            # We pass an explicit ``event_id`` because
            # ``generate_deterministic_event_id`` is
            # the framework's canonical helper for this
            # case (see
            # ``src/kntgraph/core/event/id_helpers.py``).
            eid = generate_deterministic_event_id(
                causation_id="root",
                event_type=event_type,
                data=data,
                agent_id=view.agent_id,
            )
            # The correlation is the tick-scoped context
            # set up by the dispatcher (or by the
            # ``correlation_middleware.scope()`` wrapper
            # in the ``Runner`` tick path; see
            # ``src/kntgraph/runner/runner.py:166``).
            # The middleware guarantees a non-None
            # ``CorrelationContext`` is available
            # whenever a system runs (ADR-037).
            correlation = correlation_middleware.current()
            out.append(Event.create(
                event_id=eid,
                agent_id=view.agent_id,
                event_type=event_type,
                event_class="domain",
                data=data,
                correlation=correlation,
            ))
        return out
```

The system does NOT add a ``view.last_correlation``
field to ``AgentView``. The earlier draft
introduced that field as if it were free; the
canonical way to get a ``CorrelationContext`` for
a freshly-built event is
``correlation_middleware.current()`` (see
``src/kntgraph/core/event/correlation.py``). The
``Runner`` (and ``ReactiveDispatcher``) wrap each
tick in ``correlation_middleware.scope()``; the
system just calls ``current()``.

### 4.7 Concordo class

```python
class WorkflowSagaConcordo:
    """
    C-02: WorkflowSaga Concordo (Concordo Protocol §1.3.1).

    Registers two ``WorldSystem``s on the dispatcher:

    - ``SagaSystem`` — reads the post-fold ``World`` and
      drives saga execution forward (or compensation).
    - ``SagaTimeoutSystem`` — scans agents whose
      archetype carries ``SagaProgressComponent`` and
      emits ``saga.<name>.timed_out`` on deadline.

    Both systems share the same ``now`` injection
    (the dispatcher supplies one clock for the whole
    tick to keep the systems aligned).
    """

    def __init__(self, config: "SagaConfig") -> None:
        self._config = config
        self.name = f"saga:{config.name}"
        self.version = "1.0.0"

    def install(self, dispatcher: "ReactiveDispatcher") -> None:
        # ``injectable_clock()`` defaults both systems to
        # the framework's canonical ``utcnow``. A vertical
        # that wants a shared fixed clock across the two
        # systems (e.g. for replay tests) constructs both
        # from the same injected ``now``.
        dispatcher.add_system(SagaSystem(self._config))
        dispatcher.add_system(SagaTimeoutSystem(
            {self._config.name: self._config},
        ))
```

There is **no** ``dispatcher.clock`` attribute. The
earlier draft invented a shared tick clock on the
``ReactiveDispatcher``; that surface is dropped in
favour of `core/clock` (see §12). ``SagaSystem`` and
``SagaTimeoutSystem`` each default to the framework's
canonical ``utcnow`` via ``injectable_clock()``; a
vertical that needs them aligned passes the same
``now`` callable to both constructors explicitly. This
matches the framework precedent —
``ToolCallTTLSweeperSystem.__init__(now=...)`` — and
keeps the dispatcher's constructor unchanged.

### 4.8 Example — NF-e emission saga

```python
# fmh_office/concordos/nfe_emission_saga.py

from kntgraph.concordos.saga import (
    WorkflowSagaConcordo, SagaConfig, SagaStepConfig
)
from kntgraph.concordos.specs import StepFailed, StepTimedOut
from fmh_office.concordos.specs import NfeRequired, TaxRegimeIs

nfe_emission_saga = WorkflowSagaConcordo(SagaConfig(
    name="nfe_emission",
    saga_timeout_ms=300_000,  # 5 minutes total

    # Fail the saga only when BOTH emission paths failed
    fail_when=StepFailed("emit_nfe").and_(StepFailed("emit_nfce")),

    steps=(
        SagaStepConfig(
            name="validate_fiscal",
            tool_name="sefaz_validator",
            # No compensation: validation produces no external effect
            timeout_ms=10_000,
        ),
        SagaStepConfig(
            name="emit_nfe",
            tool_name="nfe_emitter",
            compensate_tool="nfe_canceller",
            # Skip if fiscal validation says NF-e is not required
            skip_when=NfeRequired().not_(),
            # Do NOT compensate if step timed out:
            # the NF-e was never created, nothing to cancel
            compensate_when=StepTimedOut("emit_nfe").not_(),
            # Enrich params from previous step result
            enrich_from=("cfop", "tax_amount", "series"),
            timeout_ms=30_000,
        ),
        SagaStepConfig(
            name="emit_nfce",
            tool_name="nfce_emitter",
            compensate_tool="nfce_canceller",
            # Only execute for Simples Nacional regime
            skip_when=TaxRegimeIs("simples").not_(),
            compensate_when=StepTimedOut("emit_nfce").not_(),
            enrich_from=("cfop", "tax_amount"),
            timeout_ms=30_000,
        ),
        SagaStepConfig(
            name="register_receivable",
            tool_name="erp_receivable_tool",
            compensate_tool="erp_reversal_tool",
            # Optional: failure here does not fail the saga
            # (override fail_when at saga level to exclude this step)
            timeout_ms=15_000,
        ),
    ),
))
```

### 4.9 Unit tests

```python
from datetime import datetime, timedelta, timezone
from uuid import uuid4


FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def test_saga_dispatches_first_step_on_start() -> None:
    """
    Given:  World with one agent; SagaProgressComponent set,
            the last domain event is saga.nfe_emission.started.
    When:   SagaSystem runs.
    Then:   tool.sefaz_validator.requested is emitted.
    """
    view = make_world_with_components(
        agent_id="agent-1",
        components={
            SagaProgressComponent: SagaProgressComponent(
                saga_id="saga-001",
                saga_name="nfe_emission",
                current_step="validate_fiscal",
                direction="forward",
                step_order=("validate_fiscal", "emit_nfe"),
                step_states=MappingProxyType({"validate_fiscal": "pending"}),
                step_results=MappingProxyType({}),
                compensate_stack=(),
                started_at=FIXED_NOW,
            ),
        },
        # The trigger's data is read from the component
        # keyed by the trigger's event_type (the default
        # fold installs the event payload under that key).
        last_event_type="saga.nfe_emission.started",
        last_event_data={"saga_id": "saga-001"},
    )
    world = World.empty().with_agent(view)
    out = SagaSystem(
        nfe_emission_saga._config, now=lambda: FIXED_NOW
    )(world)
    assert any(
        e.event_type == "tool.sefaz_validator.requested" for e in out
    )


def test_saga_skips_nfe_when_not_required() -> None:
    """
    Given:  validate_fiscal completed with nfe_required=False;
            tool_completions contains the sefaz_validator
            completion keyed by request_event_id.
    When:   the last domain event is tool.sefaz_validator.completed.
    Then:   tool.nfe_emitter.requested is NOT emitted.
    """
    req_eid = str(uuid4())
    view = make_world_with_components(
        agent_id="agent-1",
        components={
            SagaProgressComponent: SagaProgressComponent(
                saga_id="saga-001",
                saga_name="nfe_emission",
                current_step="emit_nfe",
                direction="forward",
                step_order=("validate_fiscal", "emit_nfe"),
                step_states=MappingProxyType(
                    {"validate_fiscal": "completed", "emit_nfe": "in_flight"}
                ),
                step_results=MappingProxyType({
                    "validate_fiscal": {"nfe_required": False},
                }),
                compensate_stack=(),
                started_at=FIXED_NOW,
            ),
        },
        tool_completions={
            req_eid: ToolCallCompletion(
                request_event_id=req_eid,
                tool_name="sefaz_validator",
                status="completed",
                result={"nfe_required": False},
            ),
        },
        last_event_type="tool.sefaz_validator.completed",
    )
    world = World.empty().with_agent(view)
    out = SagaSystem(
        nfe_emission_saga._config, now=lambda: FIXED_NOW
    )(world)
    assert not any(
        e.event_type == "tool.nfe_emitter.requested" for e in out
    )


def test_saga_compensates_on_timeout_except_timed_out_steps() -> None:
    """
    Given:  emit_nfe step timed out.
    When:   saga-level fail_when is satisfied.
    Then:   nfe_canceller is NOT dispatched (compensate_when blocks it).
    """
    req_eid = str(uuid4())
    view = make_world_with_components(
        agent_id="agent-1",
        components={
            SagaProgressComponent: SagaProgressComponent(
                saga_id="saga-001",
                saga_name="nfe_emission",
                current_step="emit_nfe",
                direction="forward",
                step_order=("validate_fiscal", "emit_nfe"),
                step_states=MappingProxyType(
                    {"validate_fiscal": "completed", "emit_nfe": "timed_out"}
                ),
                step_results=MappingProxyType({
                    "validate_fiscal": {"nfe_required": True},
                    "emit_nfe": {},
                }),
                compensate_stack=("validate_fiscal", "emit_nfe"),
                started_at=FIXED_NOW,
            ),
        },
        tool_completions={
            req_eid: ToolCallCompletion(
                request_event_id=req_eid,
                tool_name="nfe_emitter",
                status="timed_out",
                error="ttl_expired",
            ),
        },
        last_event_type="tool.nfe_emitter.timed_out",
    )
    world = World.empty().with_agent(view)
    out = SagaSystem(
        nfe_emission_saga._config, now=lambda: FIXED_NOW
    )(world)
    assert not any(
        e.event_type == "tool.nfe_canceller.requested" for e in out
    )


def test_saga_timeout_system_emits_timed_out() -> None:
    """
    Given:  SagaProgressComponent started 6 minutes ago; timeout=5min.
    When:   SagaTimeoutSystem tick runs.
    Then:   saga.nfe_emission.timed_out is emitted.
    """
    past = FIXED_NOW - timedelta(minutes=6)
    view = make_world_with_components(
        agent_id="agent-1",
        components={
            SagaProgressComponent: SagaProgressComponent(
                saga_id="saga-001",
                saga_name="nfe_emission",
                current_step="emit_nfe",
                direction="forward",
                step_order=("validate_fiscal", "emit_nfe"),
                step_states=MappingProxyType({"emit_nfe": "in_flight"}),
                step_results=MappingProxyType({}),
                compensate_stack=("validate_fiscal",),
                started_at=past,
            ),
        },
        last_event_type="saga.nfe_emission.started",
    )
    world = World.empty().with_agent(view)
    system = SagaTimeoutSystem(
        {"nfe_emission": nfe_emission_saga._config},
        now=lambda: FIXED_NOW,
    )
    out = system(world)
    assert any(
        e.event_type == "saga.nfe_emission.timed_out" for e in out
    )


def test_saga_dlq_event_emitted_on_compensation_failure() -> None:
    """
    Given:  SagaProgressComponent.direction == "compensating";
            the last domain event is
            ``tool.nfe_canceller.failed``.
    When:   SagaSystem runs.
    Then:   ``saga.nfe_emission.dlq`` is emitted so the
            DLQ adapter picks it up on the next tick.
    """
    req_eid = str(uuid4())
    view = make_world_with_components(
        agent_id="agent-1",
        components={
            SagaProgressComponent: SagaProgressComponent(
                saga_id="saga-001",
                saga_name="nfe_emission",
                current_step="emit_nfe",
                direction="compensating",
                step_order=("validate_fiscal", "emit_nfe"),
                step_states=MappingProxyType(
                    {"emit_nfe": "compensation_failed"}
                ),
                step_results=MappingProxyType({}),
                compensate_stack=("emit_nfe",),
                started_at=FIXED_NOW,
            ),
        },
        tool_completions={
            req_eid: ToolCallCompletion(
                request_event_id=req_eid,
                tool_name="nfe_canceller",
                status="failed",
                error="se_faz_offline",
            ),
        },
        last_event_type="tool.nfe_canceller.failed",
    )
    world = World.empty().with_agent(view)
    out = SagaSystem(
        nfe_emission_saga._config, now=lambda: FIXED_NOW
    )(world)
    assert any(
        e.event_type == "saga.nfe_emission.dlq" for e in out
    )
```

### 4.10 FSM + Saga composition

The natural composition: the Saga drives execution;
upon completion it emits an event that the FSM uses
to advance state. They share no internal state —
they communicate only through the EventLog.

```
[FSMSystem]                        [SagaSystem]
invoice in "validating"
        ←  invoice.approved
fsm.transitioned (→ "issued")
invoice.issuance_confirmed         ←  triggers saga
                                   saga.nfe_emission.started
                                   tool.sefaz_validator.requested
                                   tool.sefaz_validator.completed
                                   tool.nfe_emitter.requested
                                   tool.nfe_emitter.completed
                                   saga.nfe_emission.completed
        ← saga.nfe_emission.completed
[FSMSystem] scans the agent on the next tick:
  no transition declared for this trigger — the
  FSM reads ``view.domain_phase`` (which is now
  ``saga.nfe_emission.completed``) and emits no
  events; business systems may react
```

Note that the FSM is **idempotent under repeated
runs**: when ``SagaSystem`` dispatches
``saga.nfe_emission.completed`` and ``FSMSystem``
runs on the next tick, the FSM reads the same
``view.domain_phase`` and either emits a transition
(declared) or emits nothing (no transition
declared). There is no "subscribe to event_type X"
filter — every system reads the post-fold view and
the dispatcher does not double-invoke them on the
same tick.

If the business needs the saga completion to trigger
an FSM transition, it is declared explicitly:

```python
FSMConfig(
    transitions={
        "issued": {
            "saga.nfe_emission.completed": FSMTransition(to="transmitted"),
            "saga.nfe_emission.compensated": FSMTransition(
                to="emission_failed"
            ),
        },
        ...
    },
)
```

---

## 6. Composition and installation

### 6.1 Declarative app_runner

```python
# fmh_office/app_runner.py

from kntgraph.runner import ReactiveDispatcher
from fmh_office.concordos.invoice_fsm import invoice_fsm
from fmh_office.concordos.nfe_emission_saga import nfe_emission_saga


def build_dispatcher(log, redis) -> ReactiveDispatcher:
    dispatcher = ReactiveDispatcher(log=log, redis=redis)
    ConcordoCatalog(
        invoice_fsm,             # C-01
        nfe_emission_saga,       # C-02
    ).install_all(dispatcher)
    return dispatcher
```

The ``app_runner.py`` reads like a specification of
the vertical's behavior. ``ConcordoCatalog.install_all``
(§6.3) dedupes by ``Concordo.name`` before calling
each ``install``, so a misconfiguration that imports
the same Concordo twice does not double-register
its systems on the dispatcher. The order of
installation does not affect correctness (Concordos
communicate only through events).

### 6.3 ConcordoCatalog

```python
from collections.abc import Iterable


class ConcordoCatalog:
    """
    Bag of ``Concordo`` instances with idempotent
    installation.

    ``install_all`` iterates the catalog and calls
    ``concordo.install(dispatcher)`` once per unique
    ``name``. A second pass with the same name is
    a no-op (logged at INFO level). This keeps
    ``app_runner.py`` free of dedup logic and
    makes double-imports of the same vertical
    module safe.
    """

    def __init__(self, *concordos: Concordo) -> None:
        self._concordos: dict[str, Concordo] = {}
        for c in concordos:
            if c.name in self._concordos:
                # Idempotent; first one wins. A vertical
                # that genuinely needs two different
                # versions of the same Concordo must
                # give them different names.
                continue
            self._concordos[c.name] = c

    def install_all(self, dispatcher: "ReactiveDispatcher") -> None:
        for concordo in self._concordos.values():
            concordo.install(dispatcher)
```

### 6.2 Full event trace — invoice issuance

```
invoice.submitted
  → [FSMSystem] fsm.transitioned: draft → validating
  → [FSMSystem] (on_entry): (none for validating)

invoice.approved   (validating → issued, guard: NfeRequired + quota)
  → [FSMSystem] fsm.transitioned: validating → issued
  → [FSMSystem] invoice.issuance_confirmed (on_entry)

invoice.issuance_confirmed
  → [SagaSystem] saga.nfe_emission.started triggers:
      → saga.nfe_emission.started
  → [SagaSystem] tool.sefaz_validator.requested

tool.sefaz_validator.completed  {nfe_required: true, cfop: "5102"}
  → [SagaSystem] emit_nfe not skipped (NfeRequired satisfied)
  → tool.nfe_emitter.requested  {cfop: "5102", ...}

tool.nfe_emitter.completed  {nfe_key: "43260..."}
  → [SagaSystem] emit_nfce skipped (TaxRegimeIs("simples") not satisfied)
  → tool.erp_receivable_tool.requested

tool.erp_receivable_tool.completed
  → [SagaSystem] saga.nfe_emission.completed

saga.nfe_emission.completed
  → [FSMSystem] no transition declared for this event (ignored)
  → [business ReactiveSystem] may react here
```

---

## 7. Source code layout

The layout follows AGENTS.md §3 (500-line guideline)
from the start. Files that contain logic are split
into private sub-modules; the public ``__init__.py``
re-exports the API.

```
src/kntgraph/concordos/
+-- __init__.py              # Concordo Protocol; ConcordoCatalog
+-- _private.py              # Module-private helpers (nothing exported)
+-- base.py                  # Specification, StepContext,
│                            # ViewTrigger, Composable mixin (≤ 200 lines)
+-- specs.py                 # Built-in Specifications (StepCompleted,
│                            #   StepFailed, StepTimedOut, StepResultEquals,
│                            #   DomainStateIs, ProfileTierIs, ContinuityToolUsed)
+-- fsm/
│   +-- __init__.py          # BusinessFSMConcordo (public)
│   +-- _config.py           # FSMConfig, FSMTransition (private; re-exported)
│   +-- _components.py       # FSMAuditComponent
│   +-- _system.py           # FSMSystem (WorldSystem)
+-- saga/
│   +-- __init__.py          # WorkflowSagaConcordo (public)
│   +-- _config.py           # SagaConfig, SagaStepConfig
│   +-- _components.py       # SagaProgressComponent
│   +-- _state.py            # step_states / step_results mutation helpers
│   +-- _system.py           # SagaSystem (WorldSystem)
│   +-- _timeout_system.py   # SagaTimeoutSystem (WorldSystem)
```

The split keeps each file under the 500-line
ceiling (AGENTS.md §3.1). The split rationale for
the saga sub-module:

- ``_state.py`` carries the step-states /
  step-results reconciliation helpers
  (``_record_step_completed``, ``_record_step_failed``,
  ``_saga_completed``, the projection function that
  re-derives the saga component from the EventLog).
- ``_system.py`` carries the ``WorldSystem`` shape
  (``__call__``, ``_events_for_agent``,
  ``_handle_completion``, ``_advance``,
  ``_handle_failure``, ``_begin_compensation``,
  ``_dispatch_step``). It depends on ``_state.py``.
- ``_timeout_system.py`` is a single-purpose
  ``WorldSystem`` and lives on its own.

Dependency rule (no inversions):

```
concordos/fsm/_system.py
  -> kntgraph.core.world              (World, AgentView)
  -> kntgraph.core.world.component    (DomainComponent)
  -> kntgraph.core.components.memory  (SessionComponent, ProfileComponent,
                                       ContinuityComponent)
  -> kntgraph.core.world.components   (ToolCallRequest, ToolCallCompletion)
  -> concordos/base.py                (Specification, StepContext)

concordos/saga/_system.py
  -> kntgraph.core.world              (World, AgentView)
  -> kntgraph.core.world.component    (DomainComponent)
  -> kntgraph.core.components.memory  (ProfileComponent, ContinuityComponent)
  -> kntgraph.core.world.components   (ToolCallRequest, ToolCallCompletion)
  -> kntgraph.core.event              (Event, generate_deterministic_event_id)
  -> concordos/base.py                (Specification, StepContext)
  -> concordos/saga/_state.py         (reconciliation helpers)
  -> concordos/saga/_components.py    (SagaProgressComponent)
  -> concordos/saga/_config.py        (SagaConfig, SagaStepConfig)

concordos/_components/saga_components.py
  -> dataclasses, datetime, typing    (stdlib only)
  -> kntgraph.core._typing            (JsonValue)
  -> kntgraph.core.world.component    (DomainComponent base — TYPE_CHECKING)
```

The boundary is the type-discipline skill (§1.2):
``concordos/`` does NOT import from ``agents/``,
``memory/``, ``api/``, ``cli/``, ``events/``, or
``knowledge/``. The imports above are limited to
``core/`` (framework) and ``stdlib``. The
``DomainComponent`` base lives in
``kntgraph.core.world.component`` (not in
``kntgraph.agents.knowledge`` as the previous draft
assumed — see §11 review item 1).

---

## 8. CLI scaffold

```bash
# C-01: add an FSM for an existing DomainComponent
uv run knt concordo add fsm InvoiceFSM \
  --component InvoiceDomainComponent \
  --state-field status

# C-02: generate a WorkflowSaga
uv run knt concordo new saga NfeEmission \
  --steps validate_fiscal:sefaz_validator,\
          emit_nfe:nfe_emitter,\
          register_receivable:erp_tool \
  --timeout-ms 300000
```

Each command generates:
1. A `concordos/<name>_config.py` with typed configuration
2. Wiring in `app_runner.py` (`concordo.install(dispatcher)`)
3. A stub test file in `tests/unit/concordos/`

---

## 9. Consequences

### 9.1 Positive

- **No duplication of framework primitives:** the
  Saga reads `ToolCallRequest`/`ToolCallCompletion`
  from `AgentView`; it does not re-implement
  in-flight tracking. TTL is delegated to ADR-045.
  The only new infrastructure is `SagaProgressComponent`
  (sequencing state) and `SagaTimeoutSystem`
  (saga-level timeout).
- **Specification Pattern:** business rules are named,
  composable, reusable across FSM guards and Saga
  conditions. A single `NfeRequired()` spec is
  declared once and used in both Concordos.
- **Declarative vertical configuration:** `app_runner.py`
  lists Concordos; developers read it as a
  business specification.
- **Cyclic + Reactive supervision model explicit:**
  the two supervision layers (step TTL via ADR-045,
  saga timeout via `SagaTimeoutSystem`) are declared
  explicitly. The existing Runner liveness check
  covers the agent level without any Saga-specific
  code.
- **Testable in isolation:** each system reads from
  `AgentView.components` (already hydrated); tests
  only need to construct an `AgentView` with the
  right components — no Redis, no HTTP, no LLM.

### 9.2 Open questions

1. **FSM state update and DomainComponent projection.**
   The FSM emits `fsm.transitioned`; the
   `DomainComponent` projection (ADR-059) must know
   to update `state_field` when it sees that event.
   Two options:
   - (a) FSMSystem also emits a standard
     `domain.state_updated` event in the
     `DomainComponent` vocabulary; the existing
     projection handles it.
   - (b) The `FSMConfig.state_field` becomes a key
     in a new `FSMProjection` that overlays the
     default projection.
   Option (a) is simpler and avoids a new projection;
   option (b) keeps the FSM namespace clean.

2. **Saga enrichment from ContinuityComponent.**
   `enrich_from` in `SagaStepConfig` lists field
   names to inject. Currently this reads from
   previous step results. Should it also read
   directly from `ContinuityComponent`? If so,
   the `SagaSystem` needs access to the full
   `AgentView` at dispatch time — which it already
   has (it receives `world` and reads `view`).

3. **Long-running Sagas and the Scheduler.**
   A saga waiting for human approval between steps
   may idle for hours. The `SagaTimeoutSystem`
   handles the overall deadline, but per-step
   timeouts for human-approval steps should be much
   longer than tool call TTLs. Proposal: a step
   declared with `tool_name=None` (human step)
   gets no ADR-045 TTL registration; its timeout
   is managed by a dedicated step in the
   `SagaStepConfig` with an explicit timer.

4. **Worker-level back-pressure (deferred to ADR-070).**
   The back-pressure concern that motivated the
   removed C-03 Pipeline (§11.17) is real and
   needs an answer — but not as a Concordo.
   The follow-up ADR extends `@tool_worker` /
   `WorkerManager` with a `max_in_flight` knob
   backed by a Redis counter; when the worker
   counter saturates, dispatch is delayed via the
   wakeup stream (ADR-068) until the gate clears.
   The full design lives in ADR-070 and is out of
   scope here.

5. **Per-agent recent-events buffer for the FSM.**
   The FSM needs to react to the agent's most recent
   domain event (the trigger for a potential state
   transition). The trigger is derived from
   ``view.domain_phase`` (the very last domain event
   observed; see §11.16). For agents that
   receive multiple events per tick (e.g. a saga
   completion + an FSM transition in the same fold),
   the FSM may need to scan more than one event. The
   dispatcher's incremental fold currently exposes
   only the last one. Options:
   - (a) Extend ``AgentView`` with a ``recent_events``
     tuple (last N domain events). Cheap for N=8.
   - (b) Have FSMs re-derive triggers from the
     EventLog on every tick (no World change; cost is
     N queries per tick per agent). Slow but simple.
   - (c) Keep the FSM as the only transition owner
     and forbid it from coexisting with systems that
     emit multiple events per agent per tick.
   The current ADR does NOT extend ``AgentView``; the
   FSM/Saga use the single-slot ``domain_phase`` plus a
   ``last_processed_event_id`` cursor and the delta-scan
   pattern (§11.16, §11.18.1). Option (a) remains a
   low-cost future resolution if the cursor alone proves
   insufficient; the tuple can default to empty and the
   projection fills it on the base fold.

6. **Reconciling ``SagaProgressComponent`` from the log.**
   The component carries both execution fields
   (saga_id, current_step, started_at — written by
   saga events) and history fields (step_states,
   step_results, compensate_stack — derived from the
   EventLog). On replay the projection must reconstruct
   the history deterministically. The reconciliation
   rule is documented in §4.4 but the implementation
   lives in ``concordos/saga/_state.py`` and is not
   yet drafted. The unit tests in §4.9 cover only
   the forward path; a reconciliation test against a
   re-folded EventLog is the next gate.

---

## 10. References

- Evans, E. *Domain-Driven Design*, 2003 — Chapter 9, Specification Pattern
- Richardson, C. *Microservices Patterns*, 2018 — Chapter 4, Saga Pattern
- [Akka FSM](https://doc.akka.io/docs/akka/current/fsm.html)
- [Akka Streams back-pressure model](https://doc.akka.io/docs/akka/current/stream/stream-flows-and-basics.html)
- [ADR-001 — Pure ECS + Event Sourcing](./ADR-001-Arquitetura.md)
- [ADR-003 — Dual Lifecycle](./ADR-003-Ciclo-Dual.md)
- [ADR-016 — Event Signing](./ADR-016-Event-Signing.md)
- [ADR-018 — WorldSystem + ReactiveDispatcher](./ADR-018-WorldIncremental-WorldSystem.md)
- [ADR-034 — ToolCall ECS Components](./ADR-034-ToolCall-ECS-Components.md)
- [ADR-035 — Sharding and horizontal coordination](./ADR-035-sharding-and-dispatcher-coordination-for-horizontal-scaling.md)
- [ADR-037 — Mandatory Correlation Propagation](./ADR-037-Mandatory-Correlation-Propagation.md)
- [ADR-039 — Role rethinking and intent routing](./ADR-039-Role-rethinking-and-intentions-routing.md)
- [ADR-042 — Memory Model](./ADR-042-Agents-Memory-Model-usage.md)
- [ADR-044 — Tool-call Overlay Accumulation](./ADR-044-Tool-call-Overlay-Accumulation.md)
- [ADR-045 — Tool Call TTL](./ADR-045-Tool-Call-Request-TTL.md)
- [ADR-059 — Domain Memory ECS Components](./ADR-059-Domain-Memory-ECS-Components.md)
- [ADR-068 — Idle Redis traffic and EventLog subscribe](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md)
- [ADR-070 — Worker-Level Back-Pressure *(proposed, follow-up)*](./ADR-070-Worker-Level-Back-Pressure.md)

---

## 11. Changes from review

This revision incorporates the architectural
review (2026-09-07). Each item below maps to a
specific class of issue and the resolution chosen
here. Items are tracked in `DEBT.md` as either
resolved (this ADR) or open (§9.2).

### 11.1 Framework/vertical boundary (§1, §7)

- **Issue.** The previous draft imported
  `DomainComponent` from `kntgraph.agents.knowledge`
  and `ContinuityComponent` / `ProfileComponent`
  from `kntgraph.memory`. Both paths cross the
  framework/vertical line that
  `kntgraph-type-discipline §1.2` forbids.
- **Resolution.** Imports now use
  `kntgraph.core.world.component.DomainComponent`
  (which is where the framework's `DomainComponent`
  base actually lives, see
  `src/kntgraph/core/world/component.py`) and
  `kntgraph.core.components.memory.{SessionComponent,
  ProfileComponent, ContinuityComponent}` (the
  framework's frozen-dataclass projections, ADR-042).
  The dependency diagram in §7 makes the rule
  explicit.

### 11.2 Type discipline (§2, §4)

- **Issue.** `StepContext.step_results`,
  `SagaProgressComponent.step_results`, and
  `StepResultEquals.value` were typed as
  `Mapping[..., object]`. The skill forbids bare
  `object` in framework code; three `# type:
  ignore[union-attr]` comments silenced the
  downstream errors.
- **Resolution.** All three are now `Mapping[str,
  JsonValue]`. `StepResultEquals` reads via
  `isinstance(result, Mapping)` so the runtime
  check matches the static type. The
  `# type: ignore` lines are gone.

### 11.3 Specification protocol shape (§2)

- **Issue.** `Specification` was a `Protocol` with
  `runtime_checkable`. Each concrete Specification
  reimplemented `and_/or_/not_` (≈30 lines per
  class), and the LSP between `Protocol` and the
  combinators was inconsistent.
- **Resolution.** `Specification` is now an `ABC`
  plus a `Composable` mixin with default
  implementations of `and_/or_/not_`. The combinator
  classes (`AndSpec`/`OrSpec`/`NotSpec`) inherit
  both, so fluent chains stay typed end-to-end.
  `runtime_checkable` is gone; the framework's type
  checker enforces the contract statically.

### 11.4 WorldSystem signature (§1.3.2, §3.4, §4.5)

- **Issue.** All systems in the previous draft used
  the legacy `(world, event)` signature. ADR-018
  deprecated that signature; the framework's
  canonical shape is `(world) -> list[Event]`.
- **Resolution.** Every system now follows the
  post-ADR-018 shape. The `trigger` is derived from the
  existing view fields (`domain_phase` + `last_event_id`
  + the component keyed by `domain_phase`) rather than
  being passed as a parameter or read from a new
  `view.last_event` envelope (see §11.16 for the
  reworked trigger derivation). The legacy aliases
  `ReactiveSystem`/`CyclicSystem` from
  `src/kntgraph/core/system.py` are kept for
  historical imports but the new code uses
  `WorldSystem` only.

### 11.5 Dispatcher API (§3.5, §4.7)

- **Issue.** The previous draft called
  `dispatcher.add_reactive_system(system, triggers=...)`
  and `dispatcher.add_cyclic_system(system)`. Neither
  method exists; the dispatcher exposes
  `add_system(system)` and a constructor
  `systems=[...]` parameter.
- **Resolution.** All `.install(...)` methods now
  use `dispatcher.add_system(...)`. The trigger
  set is gone — the post-ADR-018 dispatcher runs
  every system on every tick and lets each system
  filter via `world.query_agents` and the per-view
  event walk.

### 11.6 `Event.create` real signature (§3.4, §4.5)

- **Issue.** The previous draft called `Event.create(...)`
  without `correlation=` (the framework requires it,
  ADR-037) and used `event_class="domain"` directly,
  bypassing the validator that ensures namespace
  alignment (`domain_from` is the canonical builder).
- **Resolution.** Every event build now passes an
  explicit `correlation=` (typically
  `event.correlation`, propagated through the
  causal chain). `event_class="domain"` is passed
  explicitly because some events carry the saga /
  FSM semantics that the validator's `validate_event_type`
  namespace mapping does not know about; the
  comment in §3.4 explains why the framework's
  builder is acceptable for those namespaced
  events.

### 11.7 Determinism of `now` (§3.4, §4.5, §4.6)

- **Issue.** The previous draft read `datetime.now(...)`
  inline in systems declared "pure". A replayed
  log would re-evaluate guards / timeouts with a
  different `now`, breaking the "same World ⇒ same
  list[Event]" contract.
- **Resolution.** Every system accepts an optional
  `now: Clock | None` in the constructor, which
  defaults to the framework's canonical `utcnow` via
  the `injectable_clock()` helper in `core/clock`
  (§12). Tests inject a fixed clock; production uses
  the default. This follows the precedent of
  `ToolCallTTLSweeperSystem.__init__(now=...)`.

### 11.8 `SagaTimeoutSystem` deterministic `event_id` (§4.6)

- **Issue.** The previous draft claimed the emitted
  `event_id` was `uuid5(saga_id, "timed_out")`, but
  the implementation called `Event.create(...)`
  without passing an `event_id`. Two ticks that
  derived the same timeout would emit two distinct
  events, breaking idempotency.
- **Resolution.** The system now calls
  `generate_deterministic_event_id(causation_id,
  event_type, data, agent_id=...)` (the canonical
  helper from `src/kntgraph/core/event/id_helpers.py`)
  and passes the resulting UUID to `Event.create`.
  The data envelope includes `started_at` and
  `elapsed_ms`, so the hash is stable across
  repeated ticks on the same state.

### 11.9 `compensate_failed` and DLQ (§4.5.1, §4.9)

- **Issue.** The previous draft had no provision for
  a compensation tool that itself fails. A saga
  could terminate in `direction="compensated"` with
  an external effect still in flight. The follow-up
  reviewer flag also noted that the draft did not
  define the operator-side recovery path: an
  agent left in `direction="compensation_failed"`
  without a documented way out becomes a permanent
  ghost in the World, neither advancing nor
  being collected by garbage collection.
- **Resolution.** A compensation failure is detected
  via the `tool.<compensate_tool>.failed` event;
  the saga system then emits
  `saga.<name>.compensation_failed` followed by
  `saga.<name>.dlq`. The DLQ event is consumed by
  the existing ``DeadLetterActions`` adapter
  (`src/kntgraph/events/dlq/actions.py`), which
  appends to ``knt:dlq:events`` and indexes the
  entry by ``event_id`` and ``agent_id``. A unit
  test in §4.9 covers the path.

  **Operator Override — `saga.<name>.manual_resolved`.**
  When the operator decides that a DLQ'd saga is
  safe to abandon or has been corrected out of
  band, they emit ``saga.<name>.manual_resolved``
  via the standard principal-authorised path
  (ADR-017). The saga system reacts on the next
  tick and transitions the agent to
  ``direction="done"`` (skipping the compensation
  trail) or ``direction="manual_aborted"`` (a
  terminal state for audit). The event's data
  envelope carries ``{"resolution": "abandoned" |
  "external_correction", "operator_id": "..."}``
  for audit.

  **Operator Retry — `saga.<name>.retry_compensation`.**
  When the operator wants the saga to re-attempt
  compensation (e.g. after the external failure
  cause is fixed), they emit
  ``saga.<name>.retry_compensation``. The saga
  system reads the cached ``compensate_stack``
  from the component and re-dispatches the
  remaining compensations in LIFO order,
  exactly as if the saga had just transitioned
  to ``direction="compensating"`` for the first
  time. The EventLog is the source of truth
  (per §11.10); the operator may issue as many
  retries as needed, and the saga keeps trying
  until either compensation succeeds, the saga
  fails again (back to the DLQ), or the operator
  aborts.

  Both override events are **privileged**:
  emission requires a principal with the
  ``saga.override`` permission (ADR-017). The
  ``RoleComponent.allowed_tools`` gate (ADR-060
  gate 2) is bypassed for these events; the
  authorization is purely principal-based because
  the events do not trigger any tool calls
  themselves — they only re-aim the saga's
  existing compensation stack.

### 11.10 `compensate_stack` source-of-truth (§4.4, §9.2.6)

- **Issue.** `SagaProgressComponent.compensate_stack`
  is a runtime state but lives in a component that
  is "materialised from events". The two views
  contradict each other (which one is the source?).
  Worse: if the saga process crashes in the middle
  of compensating a multi-step saga (e.g. two of
  three compensations have completed and the third
  has just been dispatched), the
  ``compensate_stack`` at fold time is ambiguous —
  the projection cannot tell which compensations
  finished without a granular log of events.
- **Resolution.** §4.4 already distinguishes the
  *execution* fields (saga_id, current_step,
  started_at — written by saga events) from the
  *history* fields (step_states, step_results,
  compensate_stack — derived from the EventLog via
  a projection). The reconciliation logic lives
  in `concordos/saga/_state.py`; the unit test
  for the reconciliation is open (§9.2 item 6).

  The reconciliation is only **crash-safe** if
  the saga system emits two granular events per
  compensating step:

  - ``saga.<name>.<step>.compensation_started`` —
    emitted when the saga system dispatches the
    compensation tool. The event carries
    ``{"step_name": "emit_nfe"}``.
  - ``saga.<name>.<step>.compensated`` — emitted
    when the compensation tool's
    ``tool.<compensate_tool>.completed`` arrives.
    The event carries ``{"step_name": "emit_nfe"}``
    and is causally linked to the corresponding
    ``compensation_started``.

  The fold projection reads the sequence of these
  events and builds the precise list of
  compensated steps from the EventLog alone. A
  crash between ``compensation_started`` and
  ``compensated`` is recovered by re-dispatching
  the compensation on the next tick (the
  ``compensation_started`` is the durable signal;
  the system is idempotent because the worker
  receives the same ``compensate_when`` context
  on replay).

  Without these granular events the
  ``compensate_stack`` field is necessary but not
  sufficient: it is a cache that helps the system
  avoid re-scanning the log on every tick, but the
  log is the only authoritative record. §4.4 is
  amended to add these two event types to the
  saga vocabulary; the saga system implementation
  emits them at the boundaries
  ``_begin_compensation`` and
  ``_handle_completion`` (when the completion
  matches a compensating tool).

### 11.11 Concordo Protocol (§1.3.1, §6.3)

- **Issue.** The previous draft referenced
  `Concordo Protocol` and `ConcordoCatalog` in §7
  without defining either.
- **Resolution.** `Concordo` is now a `Protocol`
  with `name: str`, `version: str`,
  `install(dispatcher)`. `ConcordoCatalog` is a
  `dict[name, Concordo]` that dedupes by `name`
  before calling `install`. The `app_runner.py`
  example in §6.1 uses the catalog.

### 11.12 Layout: 500-line guideline (§7)

- **Issue.** The previous layout would have produced
  `concordos/saga/system.py` and
  `concordos/saga/timeout_system.py` well over the
  500-line ceiling (the saga system alone has
  `_start`, `_handle_completion`, `_advance`,
  `_handle_failure`, `_begin_compensation`,
  `_dispatch_step`, `_match_step`, the
  `_record_*` helpers, and the `_dlq_event` builder).
- **Resolution.** The saga sub-module is split into
  `_state.py` (reconciliation), `_system.py`
  (`WorldSystem`), `_timeout_system.py` (the
  timeout sweep), and `_components.py`. Each file
  is expected to stay under 500 lines. The FSM
  sub-module follows the same pattern
  (`_config.py` / `_components.py` / `_system.py`).

### 11.13 Spec parameter rules (§2.4)

- **Issue.** `NfeRequired` was a zero-argument class
  with `result.get("nfe_required", True)` as the
  fallback. The rule "all Specifications must be
  zero-argument instantiable" was implicit.
- **Resolution.** `NfeRequired(default: bool = True)`
  makes the rule explicit. A Specification that
  needs runtime configuration must surface its
  parameters in `__init__` (e.g.
  `TaxRegimeIs("simples")`); configuration that
  varies per evaluation belongs in the `StepContext`
  (e.g. `now`), not in the Specification.

### 11.14 `view.last_correlation` and the dispatcher clock (§4.6, §4.7)

- **Issue.** The previous draft referenced
  `view.last_correlation` (a field on `AgentView`)
  and the dispatcher's shared clock without
  defining either. They were invented at the ADR
  level with no plan for the supporting framework
  changes.
- **Resolution.** Three changes, one kept, two
  reversed:
  - `view.last_correlation` — **rejected**. The
    canonical way to obtain a ``CorrelationContext``
    for a freshly-built event is
    ``correlation_middleware.current()`` (see
    ``src/kntgraph/core/event/correlation.py``).
    The ``Runner`` and ``ReactiveDispatcher`` already
    wrap every tick in
    ``correlation_middleware.scope()``; systems
    just call ``current()``. No new field on
    ``AgentView`` is added.
  - `dispatcher.clock` — **reversed** in this
    revision. The earlier draft kept a shared tick
    clock on the ``ReactiveDispatcher``. That surface
    is dropped in favour of ``core/clock`` (§12);
    each system injects its ``now`` independently and
    defaults to the framework's canonical ``utcnow``
    via ``injectable_clock()``. See §4.7.
  - `core/clock` — **added**. The framework now
    centralises the wall-clock source (``utcnow``),
    the ``Clock`` type alias, and the
    ``injectable_clock`` fallback in
    ``src/kntgraph/core/clock.py`` (§12).

### 11.15 Items still open

- Item 5 (per-agent recent-events buffer) and
  item 6 (saga reconciliation test) are tracked
  in §9.2 and gate the first PR.
- Items 1, 2, 3, 4 are answered in §9.2 with
  proposals that need a follow-up ADR before
  implementation.

### 11.16 Trigger derivation — NO `AgentView` extension (§3.4, §4.5)

- **Issue.** Both systems (FSM and Saga) need to know
  *which* domain event arrived for the agent (the
  trigger). The earliest draft proposed extending
  ``AgentView`` with ``last_event`` (an event envelope)
  and ``last_event_type`` (a string). Both were
  redundant: the default projection already surfaces
  exactly the trigger surface the systems need, without
  touching the ``AgentView`` schema.

- **Resolution.** No change to ``AgentView``. The FSM
  and Saga derive the trigger from the existing view
  fields:

  1. ``view.domain_phase`` — the **type** of the most
     recent domain event (populated by
     ``project_default`` / ``projection.py:198``). This
     is the trigger predicate the FSM/Saga branch on.
  2. ``view.last_event_id`` — the **id** of that event,
     used as the ``causation_id`` of the events the
     systems emit.
  3. ``view.components[domain_phase]`` — the **data**
     of that event (the default fold installs the event
     payload under a component keyed by ``event_type``;
     ``projection.py:334``). The Saga reads
     ``data["saga_id"]`` and ``data["step_results"]``
     here.
  4. ``correlation_middleware.current()`` — the
     correlation for freshly-emitted events (ADR-037).
     The ``Runner`` / ``ReactiveDispatcher`` already
     scope the tick, so inside a tick this is non-None.

  These four are carried on a tiny private dataclass,
  ``ViewTrigger`` (see §3.4), so the emit helpers take
  one small argument instead of four and the systems do
  not couple to the ``Event`` envelope.

  This mirrors the framework's existing
  ``_BaseRoleSystem`` precedent
  (`agents/role_systems/_base.py`), which routes on
  ``view.last_event_id`` + ``view.components`` and never
  needs the envelope. The "recent events buffer"
  question is unrelated to the trigger derivation; it
  remains open (§9.2 item 5).

  **Single-slot caveat.** ``view.domain_phase`` is a
  single slot. When an agent produces more than one
  domain event in a single tick (e.g. an external
  adapter emits both ``invoice.created`` and
  ``invoice.validated`` for the same agent in the same
  fold), only the last event survives the fold; any FSM
  or Saga trigger that would have matched the earlier
  event is silently dropped. The triggering system
  **must not** rely on the fold to surface every event —
  it must maintain a ``last_processed_event_id`` cursor
  in its own component and detect deltas against the
  EventLog.

  **Precedent in the framework.**
  ``ToolCallTTLSweeperSystem`` already follows
  exactly this pattern: it keeps an in-memory
  ``_emitted_failures`` set keyed by
  ``request_event_id`` (see
  `src/kntgraph/runner/tool_call_ttl_sweeper.py:128-141`)
  and on a process restart re-derives the set
  from the EventLog via the ``causation_id`` on
  subsequent events. The FSM and Saga components
  must do the same. The recommended shape is:

  ```python
  @dataclass(frozen=True, slots=True)
  class FSMAuditComponent:
      # existing fields …
      last_processed_event_id: Optional[str] = None
      # Set to ``event.event_id`` every time the
      # FSM emits a ``fsm.transitioned`` for this
      # agent. On the next tick, the FSM compares
      # against the EventLog to detect transitions
      # that ``view.domain_phase`` would have hidden.
  ```

  The FSM and Saga systems compare
  ``view.last_event_id`` against
  ``component.last_processed_event_id``; if they
  diverge by more than the number of
  in-flight transitions, the system scans the
  EventLog for missed triggers between the two
  cursors. This is the "delta scan" pattern; the
  EventLog is replayable so the scan is
  deterministic.

### 11.17 C-03 Pipeline — removed (§5)

- **Issue.** The previous revision proposed a
  `PipelineConcordo` (§5) as the third Concordo,
  with `PipelineRouterSystem` + `PipelineBackpressureSystem`
  orchestrating "Source → Flow → Sink" stages via a
  one-agent-per-item model. Three architectural
  problems made the design untenable:

  1. **The routing the Pipeline proposes already
     exists.** `RoleComponent` (`core/components/role.py`)
     and the `_BaseRoleSystem` family
     (`agents/role_systems/_base.py:113`) already
     implement per-event-type routing from `view.last_event_id`
     to a tool request. The "declarative topology"
     of `PipelineStageConfig.input_event_type` is the
     same shape as `REQUEST_EVENT_TYPE` on each role
     system, with no added expressiveness.

  2. **The cardinalidade of "one agent per item" is
     unsupportable in production.** A fiscal pipeline
     that processes 100k NF-e/day would create 100k
     agents in the EventLog, materialize 100k
     `AgentView`s per tick, and run `O(N)` work in
     every system's `query_agents`. The
     back-pressure counter (the only piece of value
     Pipeline adds) does not require a per-item agent
     and is cheaper as a Redis counter checked by
     `WorkerManager`.

  3. **The "recent events buffer" does not exist.**
     The trigger slot is a single value. A stage that
     emits two events per item (e.g. `document.validated`
     + `document.rejected`) overwrites itself; the
     next stage never sees the second event. The
     "recent events buffer" needed to fix this
     (open §9.2 item 5) is a substantial change to
     `AgentView` and breaks the purity invariant the
     Pipeline depends on.

- **Resolution.** §5 is removed entirely. The
  Pipeline module (~400 lines, 5 components, 2
  systems, 3 configs) is **not** introduced in this
  ADR. The legitimate concern the Pipeline was
  trying to address — back-pressure between tool
  invocations — is deferred to a follow-up ADR
  (ADR-070, Worker-Level Back-Pressure) that
  extends `@tool_worker` / `WorkerManager` with
  `max_in_flight` and a Redis counter. The follow-up
  delivers the only piece of value the Pipeline had
  with ~5% of the code, no agent cardinality cost,
  and no `AgentView` schema change.

  §6.1 (app_runner) now imports only the two
  retained Concordos. §7 (layout) loses the
  `pipeline/` sub-module. §8 (CLI scaffold) loses
  the `concordo new pipeline` command. §9.2 item 4
  is rewritten to point at ADR-070.

### 11.18 Architectural-review feedback (2026-09-07)

Four follow-up concerns raised in the second-pass
review of the revision above. Each is addressed
in the corresponding section; the summary below
maps feedback → resolution.

#### 11.18.1 Single-slot trigger slot (§11.16)

- **Concern.** ``AgentView.domain_phase`` (the trigger
  predicate) is a single slot. An agent that produces
  two domain events in the same tick (e.g.
  `invoice.created` then `invoice.validated`) leaves
  only the second visible to the trigger predicate; the
  FSM / Saga that would have matched the first event
  silently drops it.
- **Resolution.** Systems MUST maintain a
  ``last_processed_event_id`` cursor in their
  own component (``FSMAuditComponent`` for FSM,
  ``SagaProgressComponent`` for Saga) and detect
  deltas against the EventLog when the fold
  surfaces more than one event. The pattern
  follows the precedent of
  ``ToolCallTTLSweeperSystem._emitted_failures``
  in `src/kntgraph/runner/tool_call_ttl_sweeper.py:128-141`
  (in-memory dedup with re-derivation from the
  EventLog on restart). §11.16 has been amended
  with this caveat and a recommended
  ``FSMAuditComponent`` extension. The full
  "recent events buffer" question remains open
  (§9.2 item 5).

#### 11.18.2 Crash-safe compensation reconstruction (§11.10)

- **Concern.** If the saga process crashes in the
  middle of a multi-step compensation (e.g. two
  of three compensations completed, third
  dispatched), the projection cannot tell which
  compensations succeeded without granular
  per-step events in the EventLog.
- **Resolution.** §11.10 has been amended to
  introduce two granular event types:

  - ``saga.<name>.<step>.compensation_started``
    — emitted on dispatch of the compensation
    tool.
  - ``saga.<name>.<step>.compensated`` —
    emitted on the matching completion.

  The fold projection reads the sequence of these
  events and reconstructs the compensated-step
  list from the EventLog alone. The
  ``compensate_stack`` field on
  ``SagaProgressComponent`` is demoted to a
  cache (still useful for hot path; not the
  source of truth). A crash between
  ``compensation_started`` and ``compensated``
  is recovered by re-dispatching on the next
  tick (idempotent via the worker's
  ``compensate_when`` context).

#### 11.18.3 Operator Override for DLQ'd sagas (§11.9)

- **Concern.** §11.9 emits ``saga.<name>.dlq``
  but the draft did not define the operator-side
  recovery path; an agent left in
  ``direction="compensation_failed"`` becomes a
  permanent ghost.
- **Resolution.** §11.9 now defines two
  privileged operator events:

  - ``saga.<name>.manual_resolved`` — the
    operator declares the saga abandoned or
    corrected out of band. The saga system
    transitions the agent to ``done`` or
    ``manual_aborted`` (terminal audit state).
    Carries ``{"resolution", "operator_id"}``.
  - ``saga.<name>.retry_compensation`` — the
    operator requests a fresh compensation
    attempt. The saga system re-dispatches the
    cached ``compensate_stack`` in LIFO order.

  Both events are **principal-gated** via
  ADR-017 (the ``saga.override`` permission).
  They bypass ``RoleComponent.allowed_tools``
  (gate 2 of ADR-060) because they do not
  trigger tool calls — they only re-aim the
  saga's existing compensation stack.

  The DLQ integration rides on the existing
  ``DeadLetterActions.reprocess(event_id)``
  API in `src/kntgraph/events/dlq/actions.py`.
  The DLQ entry is the audit artefact; the
  saga system is the actor that consumes the
  override events.

#### 11.18.4 Cross-agent read in `StepContext` (§2.2)

- **Concern.** The original ``StepContext``
  carries only ``domain`` / ``continuity`` /
  ``profile`` for the agent under evaluation.
  Specifications that need to read global state
  (e.g. "proceed iff the financial-control
  agent's tier is VIP") had no documented
  escape hatch.
- **Resolution.** ``StepContext`` now carries
  ``world: World`` and ``agent_id: str``. The
  docstring on §2.2 documents the read-only
  cross-agent policy: Specifications MAY read
  any agent's view via
  ``world.get_agent(agent_id)`` or iterate via
  ``world.agents``; MUST NOT mutate the World,
  emit events, or perform I/O. The precedent
  is set by ``MemoryConsolidationSystem``
  (`src/kntgraph/memory/consolidation.py:325`)
  and ``SolutionExtractorSystem``
  (`src/kntgraph/agents/memory/solution_extractor.py:84,155`),
  which already iterate ``world.agents`` for
  cross-agent reasoning. The
  ``FSMSystem._events_for_agent`` and
  ``SagaSystem._handle_completion`` /
  ``_begin_compensation`` signatures have been
  updated to thread the ``world`` through to
  ``StepContext``.

### 11.19 Clock unification and `AgentView` non-enrichment (2026-09-07)

Architectural review feedback on this revision. Two
changes were made, both additive to the framework and
both simplifying the Concordo systems.

#### 11.19.1 The dual clock rule (§3.4, §4.5, §12)

- **Concern.** The ADR now has five systems injecting
  `now`; the framework already has two `utcnow`
  definitions (`core/event/validators.py:28` and
  `infra/checkpoint.py:245`) and three distinct clock
  usages (wall-clock `Event.create`, injected
  `ToolCallTTLSweeperSystem`, and `time.monotonic()` in
  `resilience/circuit_breaker`). Without a single home,
  the `now: Callable[[], datetime] | None` type and the
  `now or utcnow` fallback would be re-declared by hand
  in every Concordo system.
- **Resolution.** A new framework module
  `src/kntgraph/core/clock.py` (§12) centralises:
  - `utcnow()` — the canonical wall-clock (the single
    source; `infra/checkpoint.utcnow` becomes a
    re-export).
  - `Clock = Callable[[], datetime]` — the typed
    injectable.
  - `injectable_clock(now: Clock | None) -> Clock` — the
    `now or utcnow` fallback, defined once.
  - `monotonic()` — re-export of `time.monotonic()`, with
    a docstring stating the rule: **wall-clock** for
    absolute instants that are persisted or compared
    against domain values; **monotonic** only for pure
    durations on a hot path (resilience), where it is not
    injected and never persisted.
  The `dispatcher.clock` attribute from earlier drafts is
  dropped (§11.14, §4.7); systems inject their own clock
  and default via `injectable_clock()`. The
  `circuit_breaker` is NOT migrated — it is already
  correct with monotonic inline, and its determinism is
  by construction, not by injection.

#### 11.19.2 No `AgentView` enrichment (§11.16)

- **Concern.** The earlier revision enriched `AgentView`
  with `last_event` / `last_event_type` so the FSM/Saga
  could read the trigger envelope. That touches a frozen
  framework dataclass, the default projection, the
  dispatcher tick, and every test that builds a `World`
  by hand — for information the default fold already
  provides.
- **Resolution.** The trigger is derived from the
  existing view surface (`domain_phase` + `last_event_id`
  + the component keyed by `domain_phase`) carried on a
  private `ViewTrigger`, with correlation from
  `correlation_middleware.current()`. No `AgentView`
  field is added. §3.4, §4.5, §3.7, §4.9, §9.2 item 5,
  §11.16, §11.17 item 3 and §11.18.1 are amended
  accordingly. The `make_last_event` test helper is
  dropped; `make_world_with_components` seeds the view
  surface directly.

---

## 12. Clock module (`src/kntgraph/core/clock.py`)

The specification-pattern and systems sections reference
a single clock source. This section defines it.

```python
# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
core.clock -- the framework clock.

Centralises the clock sources used across systems so the
"now" type, the canonical ``utcnow``, and the
"inject a clock or default to utcnow" fallback are
declared once. Before this module the framework had two
``utcnow`` definitions (``core/event/validators.py`` and
``infra/checkpoint.py``) and systems re-declared the
``now: Callable[[], datetime] | None = None`` signature
and the ``now or utcnow`` fallback by hand.

Clock rule
----------

| Use | Clock | Injected? | Persisted? |
|-----|-------|-----------|------------|
| Absolute instants, event timestamps, guards, saga deadlines | ``utcnow`` (wall-clock) | Yes | Yes |
| Pure duration on a hot path (resilience) | ``monotonic()`` | No | No |

Wall-clock (``utcnow``) is injectable so a replayed log
re-evaluates guards / timeouts with the same ``now`` as
the original run ("same World ⇒ same list[Event]").
Monotonic (``time.monotonic()``) is NEVER injected and
NEVER persisted: it measures a pure elapsed duration that
would be meaningless across a restart or a replay. The
``resilience/circuit_breaker`` is the precedent — its
``recovery_timeout`` is measured against monotonic and it
calls it inline (deterministic by construction, not by
injection).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from time import monotonic

Clock = Callable[[], datetime]

__all__ = ["Clock", "injectable_clock", "monotonic", "utcnow"]


def utcnow() -> datetime:
    """Timezone-aware UTC ``datetime`` — the framework's
    canonical wall-clock source. The single definition;
    ``infra.checkpoint.utcnow`` re-exports this."""
    return datetime.now(timezone.utc)


def injectable_clock(now: Clock | None) -> Clock:
    """Return ``now`` if given, else the canonical
    ``utcnow``. The one-line "inject or default" helper
    that Concordo systems call in ``__init__``:
    ``self._now = injectable_clock(now)``."""
    return now or utcnow
```

The module is framework-owned (`core/`), has no I/O and
no dependencies beyond stdlib, so it is trivially
unit-testable. `infra.checkpoint.utcnow` is changed to a
re-export of the definition here (kept as a name for
compatibility; no behaviour change).


