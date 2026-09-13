<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-069: Agent Concordo — BusinessFSM and WorkflowSaga

- **Status:** Accepted
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
from typing import Protocol


class Concordo(Protocol):
    """
    Public surface every Concordo bundle exposes.

    A Concordo is a **frozen bundle** of ``(name,
    systems, projections)``. It does NOT mutate the
    dispatcher — the framework's pattern is that the
    caller constructs systems/projections with their
    config and passes them to ``dispatcher.add_system``
    / ``dispatcher.add_projection`` (see
    ``runner/reactive.py:360,363``). The catalog
    (§6.3) follows the same pattern: it iterates the
    bundle and calls the dispatcher's registration
    methods.

    ``name``       -- stable identifier
                      (``fsm:Invoice``, ``saga:nfe_emission``).
                      Used by the catalog's dedup and by
                      log/metrics tagging.
    ``systems``    -- tuple of ``WorldSystem`` instances
                      the dispatcher should register.
    ``projections`` -- tuple of ``WorldProjection``
                      instances the dispatcher should
                      register.

    The Protocol is structural — concrete bundles
    (FSM, Saga) are frozen dataclasses that satisfy
    it without explicit inheritance. There is no
    ``install()`` method: the dispatcher is the
    only thing that registers systems, and the
    registration API is the same for everyone
    (Concordos, role systems, TTL sweeper, custom
    vertical systems).

    **No ``version`` field.** Earlier drafts included
    a semver string on the Protocol as the
    "recommended migration signal" when a Concordo's
    emitted event schema or state semantics changed.
    That field was decorative — no consumer read it,
    and no migration registry exists. The field is
    reintroduced only when a consumer (migration
    registry, schema catalog, dead-event rejector)
    actually exists.
    """

    name: str
    systems: tuple["WorldSystem", ...]
    projections: tuple["WorldProjection", ...]
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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue
    from kntgraph.core.world.component import DomainComponent
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

    **Cross-agent access is opt-in.** A Specification
    that needs to read another agent's view (e.g.
    "proceed iff the financial-control agent's tier
    is VIP") declares an opt-in by accepting a
    ``cross_agent_resolver`` at construction time and
    reading from it inside ``is_satisfied_by``. The
    resolver is built by the application at
    Concordo-install time (typically a closure over
    the post-fold ``World``). The ``StepContext``
    itself does NOT carry the ``World`` — that would
    make every guard FSM an O(N) scan over
    ``world.agents`` and break the "FSM guard is
    cheap" contract (§3.1).

    The earlier draft (§11.18.4) granted
    ``world.get_agent(...)`` and ``world.agents``
    directly inside the context. That made the
    escape hatch too easy to reach: a Spec author
    would not realise the per-tick cost until
    production. PR 5 of the refactor plan replaces
    the bare ``world`` field with the explicit
    resolver hook, so the cost is visible at the
    call site.

    Specifications MUST NOT mutate any agent view,
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
    agent_id: str
    now: datetime
    # Optional hook for cross-agent reads. ``None``
    # means "this rule does not read other agents".
    # A resolver accepts an ``agent_id`` and returns
    # the corresponding ``AgentView`` (or ``None``).
    # It is built by the application — typically a
    # closure over the dispatcher's post-fold World —
    # so a Specification stays testable in isolation
    # (the test passes a dict-backed resolver).
    cross_agent_resolver: "Callable[[str], AgentView | None] | None" = None


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
using the same protocol. A vertical Specification must
honour three rules:

1. **No globals, no I/O.** The Specification must not
   read module-level state, the file system, or the
   network. ``is_satisfied_by`` is a pure function of
   its constructor parameters and the ``StepContext``.
2. **No clock injection.** Time-dependent rules must
   read ``ctx.now`` (the dispatcher-injected clock),
   not ``datetime.now()``. Otherwise the rule is not
   deterministic on replay.
3. **Parameters via constructor, not via the
   Concordo config.** A Specification that varies per
   deployment (e.g. ``TaxRegimeIs("simples")``) takes
   the parameter in its constructor; a Specification
   that varies per evaluation (e.g. the step result)
   reads it from ``ctx``. Configuration that is
   *constant* across the vertical's lifetime belongs
   on the ``Concordo`` (or the YAML config, §14), not
   in the Specification.

The earlier draft phrased rule 3 as "Specifications
must be zero-argument instantiable". That was
incoherent (``TaxRegimeIs`` already takes a parameter
in the very next sentence). The corrected rule is
"parameters belong in the constructor; runtime context
belongs in ``StepContext``".

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
one component for audit AND for the FSM's own
delta-scan cursor:

```python
@dataclass(frozen=True, slots=True)
class FSMAuditComponent:
    """
    Last transition record for this agent PLUS the
    delta-scan cursor the FSM uses to detect
    transitions that ``view.domain_phase`` (a
    single slot) would have hidden when an agent
    produces more than one domain event in a tick.

    **Audit fields.** ``from_state``, ``to_state``,
    ``trigger_event_type``, ``trigger_event_id``,
    ``transitioned_at``, ``guard_evaluated`` —
    materialised from the latest ``fsm.transitioned``
    event. Read-only for external systems.

    **Cursor field.** ``last_processed_event_id`` is
    the FSM's per-agent cursor (§11.16, §11.18.1).
    It is the ``event_id`` of the most recent domain
    event the FSM has processed for this agent. On
    the next tick, the FSM compares it against
    ``view.last_event_id``: when they diverge, the
    FSM re-derives the missed triggers from the
    EventLog between the cursor and the new
    ``last_event_id``. This closes the single-slot
    gap without extending ``AgentView`` (§11.16).
    The cursor is updated every time the FSM emits
    a ``fsm.transitioned`` for the agent.
    """
    from_state: str
    to_state: str
    trigger_event_type: str
    trigger_event_id: str
    transitioned_at: datetime
    guard_evaluated: bool
    last_processed_event_id: str | None = None
```

The cursor field was declared but never written
by the earlier draft (§11.18.1); PR 2 of the
refactor plan implements the writer and the
delta-scan reader in ``FSMSystem``.

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

        # The trigger's ``data`` is read from the component
        # keyed by the trigger event type — the default fold
        # installs the event payload under that component
        # (§11.16). The saga reads it the same way (see
        # §4.5); the FSM must too, otherwise a guard built
        # on ``StepResultEquals`` or any payload-dependent
        # Specification runs against an empty context.
        trigger_data = view.components.get(trigger_type, {})
        if not isinstance(trigger_data, Mapping):
            trigger_data = MappingProxyType({})
        else:
            trigger_data = MappingProxyType(dict(trigger_data))

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
        # sketched at the end of §11.16 and implemented in
        # PR 2 of the refactor plan.
        trigger = ViewTrigger(
            agent_id=view.agent_id,
            event_type=trigger_type,
            event_id=UUID(str(view.last_event_id))
            if view.last_event_id is not None
            else None,
            data=trigger_data,
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

### 3.4.1 Why `FSMProjection` and `SagaProjection` are necessary

Both the FSM and the Saga require a post-fold
projection that materialises a ``DomainComponent``
or ``SagaProgressComponent`` from the events the
saga system emitted. Two existing mechanisms were
considered and rejected:

1. **Existing overlay projections.** The framework
   ships two ``WorldProjection`` implementations
   today: ``MemoryHydrationProjection`` (which
   reads ``session.*`` / ``profile.*`` /
   ``continuity.*`` events) and the tool-call
   overlay (which reads ``tool.*`` events via
   ``overlay_tool_projection``). Neither knows
   about ``fsm.transitioned`` or ``saga.<name>.*``
   namespaces — extending them would couple
   unrelated concerns.

2. **``@domain_component`` decorator.** The
   decorator in ``core/world/component.py``
   auto-hydrates a component from the payload of
   its own event type (``core/world/projection.py:332-336``
   builds it via ``cls(**event.data)``). This works
   for components whose full state fits in one
   event's payload; it does **not** work for FSM
   state advance (``fsm.transitioned`` carries only
   ``from`` / ``to`` / ``trigger`` /
   ``trigger_event_id`` — not the rest of the
   ``InvoiceDomainComponent`` fields, which must
   be preserved across the transition) or for the
   saga (whose ``SagaProgressComponent`` is built
   from multiple event types over time).

Therefore both FSM and Saga ship a dedicated
projection that follows the existing
``WorldProjection`` Protocol
(``runner/reactive_extensions.py:62``). They are
**implementations of an existing extension
point**, not new abstractions. They are
registered via ``dispatcher.add_projection(...)``
— the same API the application uses to register
its own custom projections.

### 3.5 Concordo class

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kntgraph.core.system import WorldSystem
    from kntgraph.runner.reactive_extensions import WorldProjection
    from ._config import FSMConfig
    from ._state import FSMProjection
    from ._system import FSMSystem


@dataclass(frozen=True, slots=True)
class BusinessFSMConcordo:
    """
    C-01: BusinessFSM Concordo bundle (§1.3.1).

    A frozen bundle of ``(config, name, systems,
    projections)``. The Concordo is pure data — it
    does NOT call ``dispatcher.add_system`` /
    ``add_projection``. The catalog (§6.3) iterates
    the bundle and registers the systems / projections
    on the dispatcher. The application can also
    iterate ``concordo.systems`` directly when it
    wants finer control over the order of registration.

    The trigger set from the previous draft is gone:
    the post-ADR-018 dispatcher runs every registered
    system on every tick and lets each system filter
    via ``world.query_agents`` and the per-view event
    walk. Trigger-based dispatch is a pre-ADR-018
    optimisation; the new dispatcher no longer needs
    it (see ``src/kntgraph/runner/reactive.py``).
    """

    config: FSMConfig
    name: str = field(init=False)
    systems: tuple["WorldSystem", ...] = field(init=False)
    projections: tuple["WorldProjection", ...] = field(init=False)

    def __post_init__(self) -> None:
        # Frozen dataclasses cannot assign attributes
        # normally; ``object.__setattr__`` is the
        # documented escape hatch for derived fields
        # in ``__post_init__``. This pattern is also
        # used by ``core/event/event.py`` for the
        # ``event_id`` derivation in ``Event.create``.
        object.__setattr__(
            self,
            "name",
            f"fsm:{self.config.component_type.__name__}",
        )
        object.__setattr__(self, "systems", (FSMSystem(self.config),))
        object.__setattr__(
            self, "projections", (FSMProjection(self.config),)
        )
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
# ``World`` via the SUT builders in ``kntgraph.testing``
# and call the system against it. No mocks on
# ``ReactiveDispatcher``. They run with
# ``KNT_REDIS_FAKE=1``.

from datetime import datetime, timezone
from kntgraph.core.world.component import DomainComponent
from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system


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
    view = (
        AgentViewBuilder("inv-1")
        .with_component(
            InvoiceDomainComponent(status="validating", tax_regime="lucro_real")
        )
        # The trigger is derived from ``domain_phase``
        # (the last domain event's type); no envelope is
        # needed. ``last_event_id`` seeds the cursor.
        .with_trigger("invoice.approved")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm.config, now=lambda: FIXED_NOW), world)
    types = [e.event_type for e in out]
    assert "fsm.transitioned" in types
    assert "invoice.issuance_confirmed" in types


def test_fsm_rejects_terminal_state() -> None:
    """
    Given:  InvoiceDomainComponent.status = "paid" (terminal).
    When:   the last domain event is invoice.submitted.
    Then:   fsm.transition_rejected with reason="terminal_state".
    """
    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="paid"))
        .with_trigger("invoice.submitted")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm.config, now=lambda: FIXED_NOW), world)
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

    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="validating"))
        .with_component(
            ContinuityComponent(
                tenant_id="t-1",
                user_id="u-1",
                last_tools={"nfe_emitter": "2026-09-07T11:59:00Z"},
            )
        )
        .with_trigger("invoice.approved")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(FSMSystem(invoice_fsm.config, now=lambda: FIXED_NOW), world)
    assert out[0].event_type == "fsm.transition_rejected"
    assert out[0].data["reason"] == "guard_failed"
```

The ``AgentViewBuilder`` / ``WorldBuilder`` / ``run_system``
helpers are the framework's SUT builders
(`src/kntgraph/testing/world_builder.py`). They assemble a
``World`` (and its ``AgentView``s) without mocks, Redis, or
fabricated ``Event`` envelopes: ``with_trigger`` seeds
``domain_phase`` + ``last_event_id`` together (the trigger
surface the FSM reads, §11.16), ``with_component`` attaches
typed ECS components, and ``run_system`` invokes the system
inside a correlation scope (ADR-037). The
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
                           failure; saga fails when satisfied. When
                           ``None`` (the default), the saga fails on
                           the first failure of any step, regardless
                           of ``skip_when`` outcome. To skip a step
                           entirely, use ``skip_when`` on the step
                           config; ``fail_when`` is for the
                           "continue on failure" pattern (§4.5,
                           ``_handle_failure`` path).
    ``saga_timeout_ms`` -- wall-clock timeout for the entire saga;
                           enforced by SagaTimeoutSystem (CyclicSystem).
    """
    name: str
    steps: tuple[SagaStepConfig, ...]
    fail_when: "Specification | None" = None   # None = fail on first failure
    saga_timeout_ms: int = 300_000
```

**Note on the "REQUIRED step" concept.** The
earlier draft said the default behaviour was "fail
on first REQUIRED step failure". ``REQUIRED`` was
never defined as a step attribute — there is no
``required: bool`` field on ``SagaStepConfig`` — so
the comment was aspirational. The corrected default
is "fail on the first failure of any step", which
is what ``fail_when=None`` means in the implementation.
A "best-effort" saga (continue past failures) is
expressed by setting ``fail_when`` to a Specification
that is never satisfied, or to one that gates on a
specific step name (see §4.8 example).

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
    # 4.5.1 Compensation failure — DLQ ingestion (uses existing DLQ)
    # ------------------------------------------------------------------
    def _compensation_failed_event(
        self,
        saga: SagaProgressComponent,
        trigger: "ViewTrigger",
    ) -> "Event":
        """
        Emit ``saga.<name>.compensation_failed`` when a
        compensation tool itself fails. The event is the
        **only** saga-side artefact: a domain event that
        downstream business systems can react to (notify
        operators, kick off a recovery workflow, etc.).

        **No ``saga.<name>.dlq`` event.** An earlier
        draft introduced a separate ``saga.<name>.dlq``
        event that an ``SagaDLQAdapterSystem`` would
        forward to ``DeadLetterQueue``. That was
        overengineering: the framework already has
        ``DeadLetterQueue.append`` /
        ``DeadLetterEvent`` / ``DLQReason``, and a
        saga-specific adapter duplicates the wiring
        any other consumer would need. The DLQ ingestion
        is delegated to a generic
        ``DLQIngestSystem`` (§4.5.2) registered by the
        application when it wants DLQ persistence —
        exactly the pattern ``ToolCallTTLSweeperSystem``
        follows for tool-call TTL failures.
        """
        return Event.create(
            agent_id=trigger.agent_id,
            event_type=f"saga.{self._cfg.name}.compensation_failed",
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

### 4.5.2 DLQ ingestion — application concern, framework-agnostic

The framework already exposes
``DeadLetterQueue`` (``events/dlq/store.py``) and
``DeadLetterEvent`` / ``DLQReason``
(``events/dlq/values.py``). The saga does **not**
ship a saga-specific DLQ adapter; doing so would
duplicate the wiring any other consumer would need
and would couple the saga to the DLQ store's
evolution.

A vertical that wants DLQ persistence wires it
once at the dispatcher, using the existing
``EventLog.subscribe`` mechanism (ADR-068):

```python
# fmh_office/app_runner.py
from datetime import datetime, timezone
from kntgraph.events.dlq import DeadLetterQueue
from kntgraph.events.dlq.values import DLQReason, DeadLetterEvent
from kntgraph.infra.redis._dlq import RedisDLQStorage


def build_dispatcher(log, redis):
    dlq = DeadLetterQueue(RedisDLQStorage(redis))

    async def ingest_compensation_failures(event):
        if not event.event_type.endswith(".compensation_failed"):
            return
        dl_event = DeadLetterEvent(
            event=event,
            reason=DLQReason.PROCESSING_FAILED,
            error_message="compensation_failed",
            original_timestamp=event.timestamp,
            dlq_timestamp=datetime.now(tz=timezone.utc),
            metadata=event.data,  # saga_id, stuck_step, etc.
        )
        await dlq.append(dl_event)

    # The dispatcher's ``subscribe`` API takes a list
    # of agent_ids and a callback; ADR-068 §3.2 covers
    # the wake-up / fallback-poll semantics.
    dispatcher = ReactiveDispatcher(log=log, redis=redis)
    dispatcher.subscribe(["*"], ingest_compensation_failures)
    ConcordoCatalog(invoice_fsm, nfe_emission_saga).install_all(
        dispatcher
    )
    return dispatcher
```

The pattern matches what the framework already
does for tool-call TTL failures: the
``ToolCallTTLSweeperSystem`` emits
``tool.<name>.failed`` events; the application
decides whether to forward them to the DLQ (or to
metrics, or to a webhook, or to ignore them). The
saga follows the same convention: emit the typed
event, let the application wire the side effects.

**Why no new ``DLQReason`` value.** The
``DLQReason`` enum is a closed vocabulary used by
metrics and dashboards. Adding a
``SAGA_COMPENSATION_FAILED`` value would force every
metric that groups by reason to handle the new
value; the alternative is to put the saga context
in ``DeadLetterEvent.metadata`` (which is
forward-compatible — existing dashboards ignore it,
new dashboards filter on it). The framework
already has a precedent for this: ``error_message``
is a free-form string, ``metadata`` is a free-form
dict, and the reason enum is reserved for the
broad category of failure.

Tests that do not care about DLQ persistence
skip the ``subscribe`` registration entirely — the
``saga.<name>.compensation_failed`` events still
land in the EventLog and remain inspectable via the
standard log tools.

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
    event_type, data)``. The ``data`` envelope is
    restricted to **stable fields** (``saga_id``,
    ``saga_name``, ``stuck_at_step``, ``timeout_ms``)
    so the hash is identical across ticks that derive
    the same timeout — the EventLog's idempotency
    check then dedupes repeated emissions. ``elapsed_ms``
    is reported for observability but is NOT part of the
    hash envelope: it grows on every tick and would defeat
    idempotency. (Earlier drafts placed ``elapsed_ms``
    inside the hash; that was wrong.)

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
            # Hash envelope (stable, idempotent): fields
            # that do not change between ticks on the
            # same stuck saga. ``elapsed_ms`` is excluded
            # on purpose (it grows every tick and would
            # defeat dedup).
            hash_data = {
                "saga_id": saga.saga_id,
                "saga_name": saga.saga_name,
                "stuck_at_step": saga.current_step,
                "timeout_ms": config.saga_timeout_ms,
            }
            # Wire envelope (what observers see): adds
            # the per-tick ``elapsed_ms`` for metrics.
            data = {**hash_data, "elapsed_ms": elapsed_ms}
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
                data=hash_data,
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
from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kntgraph.core.system import WorldSystem
    from kntgraph.runner.reactive_extensions import WorldProjection
    from ._config import SagaConfig
    from ._state import SagaProjection
    from ._system import SagaSystem
    from ._timeout_system import SagaTimeoutSystem


@dataclass(frozen=True, slots=True)
class WorkflowSagaConcordo:
    """
    C-02: WorkflowSaga Concordo bundle (§1.3.1).

    A frozen bundle of ``(config, name, systems,
    projections)``. The bundle exposes two systems:

    - ``SagaSystem`` — reads the post-fold ``World`` and
      drives saga execution forward (or compensation).
    - ``SagaTimeoutSystem`` — scans agents whose
      archetype carries ``SagaProgressComponent`` and
      emits ``saga.<name>.timed_out`` on deadline.

    And one projection:

    - ``SagaProjection`` — materialises
      ``SagaProgressComponent`` from saga events
      (§9.2 item 6).

    Both systems default to the framework's canonical
    ``utcnow`` via ``injectable_clock()``; a vertical
    that needs them aligned (e.g. for replay tests)
    constructs the bundle, then accesses
    ``concordo.systems`` and passes the same ``now``
    callable to each system explicitly. The catalog
    (§6.3) does not need a shared clock because it
    does not construct the systems — it only registers
    them.
    """

    config: SagaConfig
    name: str = field(init=False)
    systems: tuple["WorldSystem", ...] = field(init=False)
    projections: tuple["WorldProjection", ...] = field(init=False)

    def __post_init__(self) -> None:
        # See ``BusinessFSMConcordo.__post_init__`` for
        # the frozen-dataclass escape hatch rationale.
        object.__setattr__(self, "name", f"saga:{self.config.name}")
        object.__setattr__(
            self,
            "systems",
            (
                SagaSystem(self.config),
                SagaTimeoutSystem({self.config.name: self.config}),
            ),
        )
        object.__setattr__(
            self, "projections", (SagaProjection(self.config),)
        )
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

    # Fail the saga when ANY emission path failed. The
    # earlier draft composed ``StepFailed("emit_nfe").and_(
    # StepFailed("emit_nfce"))`` — but the two steps are
    # mutually exclusive at runtime (one is skipped via
    # ``skip_when=NfeRequired().not_()`` exactly when the
    # other runs), so the AND was logically unreachable.
    # A failing saga must be triggered by EITHER branch.
    fail_when=StepFailed("emit_nfe").or_(StepFailed("emit_nfce")),

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

from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system


FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def test_saga_dispatches_first_step_on_start() -> None:
    """
    Given:  World with one agent; SagaProgressComponent set,
            the last domain event is saga.nfe_emission.started.
    When:   SagaSystem runs.
    Then:   tool.sefaz_validator.requested is emitted.
    """
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            SagaProgressComponent(
                saga_id="saga-001",
                saga_name="nfe_emission",
                current_step="validate_fiscal",
                direction="forward",
                step_order=("validate_fiscal", "emit_nfe"),
                step_states=MappingProxyType({"validate_fiscal": "pending"}),
                step_results=MappingProxyType({}),
                compensate_stack=(),
                started_at=FIXED_NOW,
            )
        )
        # The trigger's data is read from the component
        # keyed by the trigger's event_type (the default
        # fold installs the event payload under that key).
        .with_trigger(
            "saga.nfe_emission.started", data={"saga_id": "saga-001"}
        )
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(
        SagaSystem(nfe_emission_saga.config, now=lambda: FIXED_NOW),
        world,
    )
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
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            SagaProgressComponent(
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
            )
        )
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                tool_name="sefaz_validator",
                status="completed",
                result={"nfe_required": False},
            ),
        )
        .with_trigger("tool.sefaz_validator.completed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(
        SagaSystem(nfe_emission_saga.config, now=lambda: FIXED_NOW),
        world,
    )
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
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            SagaProgressComponent(
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
            )
        )
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                tool_name="nfe_emitter",
                status="timed_out",
                error="ttl_expired",
            ),
        )
        .with_trigger("tool.nfe_emitter.timed_out")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(
        SagaSystem(nfe_emission_saga.config, now=lambda: FIXED_NOW),
        world,
    )
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
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            SagaProgressComponent(
                saga_id="saga-001",
                saga_name="nfe_emission",
                current_step="emit_nfe",
                direction="forward",
                step_order=("validate_fiscal", "emit_nfe"),
                step_states=MappingProxyType({"emit_nfe": "in_flight"}),
                step_results=MappingProxyType({}),
                compensate_stack=("validate_fiscal",),
                started_at=past,
            )
        )
        .with_trigger("saga.nfe_emission.started")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    system = SagaTimeoutSystem(
        {"nfe_emission": nfe_emission_saga.config},
        now=lambda: FIXED_NOW,
    )
    out = run_system(system, world)
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
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            SagaProgressComponent(
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
            )
        )
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                tool_name="nfe_canceller",
                status="failed",
                error="se_faz_offline",
            ),
        )
        .with_trigger("tool.nfe_canceller.failed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(
        SagaSystem(nfe_emission_saga.config, now=lambda: FIXED_NOW),
        world,
    )
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

Three equivalent entry points — programmatic,
YAML-loaded, and hybrid — so the vertical picks the
shape that fits its lifecycle (tests stay
programmatic; production reads a bundle YAML).

#### 6.1.1 Programmatic

```python
# fmh_office/app_runner.py

from pathlib import Path
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

DLQ ingestion (if needed) is wired separately by
the application — see §4.5.2. The catalog does not
carry a DLQ handle.

#### 6.1.2 YAML-loaded (bundle)

```python
# fmh_office/app_runner.py (production variant)

from pathlib import Path
from kntgraph.concordos import ConcordoCatalog


def build_dispatcher(log, redis, bundle_path: Path) -> ReactiveDispatcher:
    catalog = ConcordoCatalog.from_yaml(bundle_path)
    # ``catalog`` now contains one Concordo per
    # ``business_fsm`` and one per ``workflow_sagas``
    # entry in the bundle YAML (§14.2).
    dispatcher = ReactiveDispatcher(log=log, redis=redis)
    catalog.install_all(dispatcher)
    return dispatcher
```

The bundle schema is described in §14. The loader
detects the file extension (``*.yaml`` / ``*.yml`` /
``*.json``) and routes to ``yaml.safe_load` (via
``pyyaml``) or ``json.loads`. Schema validation is
Pydantic — errors raise ``ConcordoValidationError``
with the dotted path of the failing field annotated
(e.g. ``workflow_sagas[0].steps[1].compensate_tool``).

A vertical that needs multiple bundles
(e.g. a shared ``knowledge_pipeline`` bundle plus
a tenant-specific override) loads each one and
either calls ``catalog.install_all`` on each
separately or composes them:

```python
shared = ConcordoCatalog.from_yaml("bundles/knowledge_pipeline.yaml")
tenant = ConcordoCatalog.from_yaml(f"tenants/{tenant_id}.yaml")
shared.install_all(dispatcher)
tenant.install_all(dispatcher)  # shadows by Concordo.name (§6.3)
```

#### 6.1.3 Hybrid

```python
# Mix Python-defined and YAML-defined Concordos
catalog = ConcordoCatalog.from_yaml("app.yaml")
catalog.add(invoice_fsm)  # override the YAML one with a Python-defined instance
```

The catalog is a dict by ``Concordo.name``; later
``add(...)`` calls with the same name replace the
earlier entry (the catalog is dict-of-name, not
list-of-pairs). This pattern is useful when one
Concordo's config is data-driven (YAML) and
another's is logic-driven (Python registered via
``SpecRegistry``, §14.6).

The ``app_runner.py`` reads like a specification of
the vertical's behavior. ``ConcordoCatalog.install_all``
(§6.3) dedupes by ``Concordo.name`` and then iterates
each bundle's ``systems`` and ``projections`` calling
the dispatcher's registration API. A misconfiguration
that imports the same Concordo twice does not
double-register on the dispatcher. Concordos are
pure bundles; the catalog does no I/O.

The order of registration does not affect
correctness (Concordos communicate only through
events). When the order matters — e.g. a saga that
must observe FSM transitions before timeout — the
application iterates ``concordo.systems`` directly
instead of going through the catalog.

### 6.3 ConcordoCatalog

```python
from collections.abc import Iterable


class ConcordoCatalog:
    """
    Bag of ``Concordo`` bundles with idempotent
    registration.

    ``install_all`` iterates each Concordo's
    ``systems`` and ``projections`` and registers
    them on the dispatcher via
    ``dispatcher.add_system`` /
    ``dispatcher.add_projection`` — the same
    registration API the framework exposes for any
    system or projection. A duplicate ``name`` is
    a no-op (logged at INFO level). This keeps
    ``app_runner.py`` free of dedup logic and
    makes double-imports of the same vertical
    module safe.

    The catalog is a **composer**, not a side-effecting
    installer: it only iterates the bundles the
    application passed in. Side effects (DLQ
    ingestion, metrics, notifications) are wired by
    the application via ``dispatcher.subscribe``
    (§4.5.2) — Concordos and the catalog stay pure.

    The catalog is optional. An application that
    wants explicit ordering or that wants to mix
    Concordos with non-Concordo systems can iterate
    ``concordo.systems`` / ``concordo.projections``
    directly and call ``dispatcher.add_system`` /
    ``dispatcher.add_projection`` itself. The catalog
    is sugar, not a required composition root.
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
            for system in concordo.systems:
                dispatcher.add_system(system)
            for projection in concordo.projections:
                dispatcher.add_projection(projection)
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
+-- __init__.py              # Concordo Protocol; ConcordoCatalog;
│                            #   .from_yaml/from_dict classmethods;
│                            #   parses bundles and registers bundles
│                            #   with the SpecRegistry
+-- _private.py              # Module-private helpers (nothing exported)
+-- base.py                  # Specification, StepContext,
│                            #   ViewTrigger (NamedTuple),
│                            #   Composable mixin (≤ 200 lines)
+-- specs.py                 # Built-in Specifications (StepCompleted,
│                            #   StepFailed, StepTimedOut, StepResultEquals,
│                            #   DomainStateIs, ProfileTierIs, ContinuityToolUsed)
+-- _spec_registry.py        # SpecRegistry: register(name, spec) for
│                            #   app-defined Specifications referenced
│                            #   from YAML (PR 1.5)
+-- _mini_lang.py            # Predicate parser + evaluator
│                            #   (~150 lines; recursive descent, no eval;
│                            #   builtins bound to the spec registry)
+-- _loader.py               # Bundle loaders (YAML / JSON / dict);
│                            #   cross-validates events ⇄ transitions
│                            #   ⇄ steps; surfaces typed
│                            #   ConcordoValidationError with dotted path
+-- schemas.py               # Pydantic v2 models for the bundle
│                            #   surface (§14.4: BundleSchema, EventSchema,
│                            #   SpecificationSchema, FSMConfigSchema,
│                            #   SagaConfigSchema, FSMTransitionSchema,
│                            #   SagaStepSchema)
+-- fsm/
│   +-- __init__.py          # BusinessFSMConcordo (public; frozen bundle)
│   +-- _config.py           # FSMConfig, FSMTransition + from_dict/to_dict
│   +-- _components.py       # FSMAuditComponent (with delta-scan cursor)
│   +-- _system.py           # FSMSystem (WorldSystem; delta-scan reader)
│   +-- _state.py            # FSMProjection (fold projection;
│                            #   DomainComponent.state_field advance)
+-- saga/
│   +-- __init__.py          # WorkflowSagaConcordo (public; frozen bundle)
│   +-- _config.py           # SagaConfig, SagaStepConfig + from_dict/to_dict
│   +-- _components.py       # SagaProgressComponent
│   +-- _state.py            # SagaProjection (fold projection;
│                            #   compensation_started/compensated handlers)
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
# Validate a bundle YAML (no side effects; prints the
# resolved Concordos and exits non-zero on schema,
# cross-reference, or parse error).
uv run knt concordo validate --bundle app.yaml

# Round-trip: parse a bundle, dump back to stdout as YAML,
# so the user can normalise formatting.
uv run knt concordo format --bundle app.yaml > app.normalized.yaml

# Parse a bundle and emit a Python stub that builds
# the same Concordos programmatically (useful for
# migrating from declarative to imperative or vice
# versa).
uv run knt concordo codegen --bundle app.yaml > concordos_stub.py

# C-01: scaffold a starter bundle for an existing
# DomainComponent (interactive; emits a YAML with
# sensible defaults that the user can edit).
uv run knt concordo add fsm InvoiceFSM \
  --component InvoiceDomainComponent \
  --state-field status

# C-02: scaffold a starter bundle for a WorkflowSaga
# (interactive; the user supplies step names + tools).
uv run knt concordo new saga NfeEmission \
  --trigger document.ingested \
  --steps validate_fiscal:sefaz_validator,\
          emit_nfe:nfe_emitter,\
          register_receivable:erp_tool \
  --timeout-ms 300000

# List registered Specifications (built-in + app-registered).
uv run knt concordo specs list

# Lint a bundle for predicate hygiene (catches unused
# specifications, unreachable states, etc.).
uv run knt concordo lint --bundle app.yaml
```

Each ``add`` / ``new`` command produces:
1. A `concordos/<bundle_id>.yaml` bundle with
   typed configuration and starter states / steps.
2. A stub test file in `tests/unit/concordos/`
   (loads the bundle, asserts the systems emit
   the expected events on the canonical fixture).

The ``validate`` command is the CI gate for the
configuration layer. It runs four checks in
order:

1. **Schema** — Pydantic validation of the bundle
   (§14.4). Errors carry the dotted path.
2. **Cross-references** — `transitions.on_event`
   and `sagas.trigger_event` are in `events[].name`;
   `steps[].tool` is registered as a `@tool_worker`;
   `input_mapping` paths resolve to declared scopes
   (§14.5.4).
3. **FSM graph** — terminal states have no outgoing
   transitions; declared states are reachable from
   `initial_state`; `on_entry` events exist
   (no dead keys).
4. **Predicate syntax** — every `guard`, `fail_when`,
   `skip_when`, `compensate_when`, `pre_condition`,
   `proceed_when`, and `specifications[].expression`
   parses with the mini-language parser (§14.5.2).
   Semantic evaluation is **not** run at
   `validate` time — that requires a `StepContext`
   and happens lazily at guard execution.

The ``lint`` command runs deeper analysis
(unused specifications, shadowed predicates,
transition cycles that bypass terminal states).
It is optional in CI but recommended in pre-merge.

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
- **Resolution.** Every event build passes an
  explicit `correlation=` (typically propagated
  through the causal chain). PR 1 of the refactor
  plan migrates every emit site from
  ``Event.create(event_class="domain", ...)`` to
  ``Event.domain_from(...)``: the latter pins
  ``event_class`` to ``"domain"`` and rejects event
  types in the framework's operational namespace
  (``agent.*``) at construction time — a typo in
  the saga's event-type string fails loudly instead
  of silently producing an unrouteable event. The
  comment in §3.4 (post-PR 1) is shortened because
  the namespace alignment is no longer a
  ``"because we accept it"`` footnote — it is the
  builder's invariant.

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
  events, breaking idempotency. A later revision
  passed an explicit ``event_id`` but included
  ``elapsed_ms`` in the hash envelope — which grows
  on every tick, defeating idempotency as badly as
  the original bug.
- **Resolution.** The system calls
  `generate_deterministic_event_id(causation_id,
  event_type, data, agent_id=...)` (the canonical
  helper from `src/kntgraph/core/event/id_helpers.py`)
  and passes the resulting UUID to `Event.create`.
  The hash envelope contains only **stable fields**
  (``saga_id``, ``saga_name``, ``stuck_at_step``,
  ``timeout_ms``). ``elapsed_ms`` is included in the
  wire payload for observability but excluded from
  the hash. A tick that re-derives the same stuck
  saga produces the same ``event_id`` and is deduped
  by the EventLog.

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

  An earlier revision of this ADR also introduced
  a saga-specific ``SagaDLQAdapterSystem` and a
  separate ``saga.<name>.dlq`` event that the
  adapter would forward to ``DeadLetterQueue``.
  This was overengineering: the framework already
  exposes ``DeadLetterQueue.append`` /
  ``DeadLetterEvent`` / ``DLQReason``, and a
  saga-specific adapter duplicates the wiring any
  other consumer would need.

- **Resolution.** A compensation failure is detected
  via the `tool.<compensate_tool>.failed` event;
  the saga system then emits
  ``saga.<name>.compensation_failed``. That is the
  only saga-side artefact — a domain event that
  downstream business systems can react to. DLQ
  ingestion is wired by the application via
  ``dispatcher.subscribe`` (§4.5.2) and reuses the
  framework's existing ``DeadLetterQueue`` —
  exactly the pattern used for tool-call TTL
  failures (``ToolCallTTLSweeperSystem``). No new
  system, no new event type, no new
  ``DLQReason`` enum value.

  **Operator recovery — deferred to ADR-070.**
  The earlier draft also described two operator-
  triggered events — ``saga.<name>.manual_resolved``
  (declare the saga safe to abandon) and
  ``saga.<name>.retry_compensation`` (re-attempt
  compensation). These were declared in this ADR
  but never implemented in ``SagaSystem``. PR 4 of
  the refactor plan moves them to
  [ADR-070](./ADR-070-Worker-Level-Back-Pressure.md)
  as a formal proposal — they belong with the rest
  of the operator-recovery story, and the saga
  implementation should not consume events it does
  not yet handle. The privilege gate (ADR-017's
  ``saga.override`` permission, bypassing ADR-060
  gate 2 because these events do not trigger tool
  calls) is documented there too.

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

### 11.11 Concordo Protocol and bundle shape (§1.3.1, §6.3)

- **Issue.** The previous draft had three related
  gaps: (a) `Concordo Protocol` and
  `ConcordoCatalog` were referenced in §7 without
  being defined; (b) `Concordo` carried a
  `version: str` that no consumer read; (c) the
  `install(dispatcher)` method on `Concordo`
  broke the framework's convention — every other
  system in the codebase
  (``ToolCallTTLSweeperSystem``,
  ``MemoryHydrationProjection``,
  ``RuleBasedChatSystem``, the role systems) is
  constructed externally and passed to
  ``dispatcher.add_system`` /
  ``dispatcher.add_projection``. A custom
  ``install()`` method is unique to the Concordos
  and hides what is registered.
- **Resolution.** `Concordo` is now a structural
  `Protocol` with three attributes — `name: str`,
  `systems: tuple[WorldSystem, ...]`,
  `projections: tuple[WorldProjection, ...]` — and
  no methods. Concrete bundles (``BusinessFSMConcordo``,
  ``WorkflowSagaConcordo``) are frozen dataclasses
  that satisfy the Protocol structurally; their
  ``__post_init__`` builds the `systems` and
  `projections` tuples from the config. The
  `version` field is gone. `ConcordoCatalog` is a
  `dict[name, Concordo]` that iterates each
  bundle's `systems` and `projections` and calls
  the dispatcher's registration API — the same
  registration API the application uses for any
  custom system. The catalog is sugar, not a
  required composition root: the application can
  iterate ``concordo.systems`` directly when it
  needs explicit ordering.

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

- **Concern.** The draft emitted
  ``saga.<name>.dlq`` and added a saga-specific
  ``SagaDLQAdapterSystem``, but did not define
  the operator-side recovery path; an agent left
  in ``direction="compensation_failed"`` becomes
  a permanent ghost.
- **Resolution.** Two changes:

  1. **DLQ wiring.** The saga no longer emits
     ``saga.<name>.dlq``; that event was
     redundant. The saga emits
     ``saga.<name>.compensation_failed``, and the
     application wires DLQ ingestion via
     ``dispatcher.subscribe`` (§4.5.2) — reusing
     the framework's ``DeadLetterQueue`` and
     ``DeadLetterEvent`` without inventing a
     saga-specific adapter.
  2. **Operator override events.** Two
     privileged operator events are defined
     (deferred to ADR-070):

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

  The DLQ entry, when ingested, is an audit
  artefact consumed via the existing
  ``DeadLetterActions.reprocess(event_id)`` /
  ``discard(event_id)`` API. The saga system
  is the actor that consumes the override
  events; the DLQ store is the persistence
  layer.

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
  accordingly. The `make_last_event` / `make_world_with_components`
  test helpers are dropped in favour of the framework's
  SUT builders (`AgentViewBuilder` / `WorldBuilder` /
  `run_system` in `kntgraph.testing`, §13), which seed the
  view surface directly.

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

---

## 13. SUT builders (`src/kntgraph/testing/world_builder.py`)

The FSM/Saga unit tests (§3.7, §4.9) build a `World` and
call the system against it. To standardise that and
eliminate per-test `make_world` helpers, the framework
ships fluent SUT (System Under Test) builders in
`kntgraph.testing`:

```python
from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system

view = (
    AgentViewBuilder("inv-1")
    .with_component(InvoiceDomainComponent(status="validating"))
    .with_component(ContinuityComponent(tenant_id="t-1", user_id="u-1"))
    .with_trigger("invoice.approved", data={"nfe_required": True})
    .with_tool_completion(req_eid, ToolCallCompletion(...))
    .build()
)
world = WorldBuilder().with_agent(view).build()
events = run_system(FSMSystem(config, now=lambda: FIXED_NOW), world)
```

Design rules (they are what make the builders "SUT" and
not just a convenience):

- **No mocks, no monkey-patches.** The builders only
  assemble state; the system runs against the real
  `World`. This follows the `kntgraph-testing` skill §7.4
  rule — a test that needs a shim to make a system
  observable is hiding a production bug.
- **No fabricated `Event` envelopes.** `with_trigger`
  seeds `domain_phase` + `last_event_id` together (the
  trigger surface the FSM/Saga read, §11.16) and installs
  `data` under the component keyed by the event_type. The
  test exercises the same read path as production.
- **Storage kept in sync.** `WorldBuilder.build()`
  populates the `ArchetypeStorage` from the views, so
  `world.query_agents(...)` and `world.get_agent(...)`
  behave exactly as in production.
- **Correlation scope.** `run_system` invokes the system
  inside `correlation_middleware.scope()` (ADR-037), so a
  system that calls `correlation_middleware.current()` to
  build events does not raise.
- **Determinism.** The system's injected `now` is passed
  explicitly; the builder does not touch the clock.

The builders live in `kntgraph.testing` (the framework's
shared test surface, alongside `FakeEmbeddingProvider`),
not in `runner/`, so they are available to any vertical
without importing a runner-private module. They replace
the `make_world_with_components` / `make_last_event`
helpers that earlier drafts placed in
`src/kntgraph/runner/world_test_helpers.py` (that module
is not introduced).

---

## 14. Configuration schemas (YAML / JSON)

The FSM and Saga configs declared in §3 and §4 are
Python dataclasses. They can also be loaded from
external files via the **bundle format** described
here, so a vertical can declare its behavioral
patterns without touching code. This section
specifies the on-disk format, the validation
layer, the predicate mini-language, and the
interaction with the Python `SpecRegistry`.

### 14.1 Why externalise

- **Operational review.** A reviewer can read the
  YAML and reason about the saga's lifecycle
  without learning the Python API.
- **Multi-tenant overrides.** A tenant can ship a
  YAML override that adjusts `saga_timeout_ms` or
  swaps a guard, without the framework shipping a
  new release.
- **CI gate.** The `knt concordo validate` CLI
  (§8) parses and validates the YAML, so a broken
  config fails the pipeline before deploy.
- **Static cross-validation.** Declaring events
  up-front lets the loader catch typos at parse
  time (`transition.on_event = "docment.ingested"`
  → "event not declared in bundle").

The Python API remains the canonical source. The
YAML is a serialised view of the same objects;
loading and dumping round-trips through Pydantic
schemas (§14.4) without loss.

### 14.2 The bundle format

The top-level unit is a **bundle**: a named,
versioned, atomic package of behaviour. A bundle
declares its event vocabulary, its named
predicates, and its FSMs / sagas. A bundle is
loaded by `ConcordoCatalog.from_yaml(path)`
(§14.3) and produces one `Concordo` (§1.3.1) per
`business_fsm` block and one per entry in
`workflow_sagas`.

```yaml
# fmh_office/concordos/knowledge_pipeline.yaml
bundle_id: "com.acme.knowledge_pipeline"
version: "1.0.0"

# Event vocabulary. Schemas are JSON Schema lite
# (validated via `jsonschema`); unknown event
# types in transitions / steps are caught here.
events:
  - name: "document.ingested"
    schema:
      type: object
      required: [document_id, content]
      properties:
        document_id: {type: string}
        content:     {type: string}

  - name: "knowledge.entities_extracted"
    schema:
      type: object
      required: [entities, confidence_score]
      properties:
        entities:        {type: array}
        confidence_score: {type: number, minimum: 0, maximum: 1}

# Named predicates. Reused across transitions and
# steps; can reference other names and the
# mini-language builtins.
specifications:
  - id: "IsHighPriority"
    expression: "event.data.content_length > 50000"

  - id: "ExtractionConfidencePassed"
    expression: "event.data.confidence_score >= 0.80"

# FSM. `id` becomes the `Concordo.name` (prefix
# `fsm:`). `component` is a dotted path resolved at
# load time (Python's importlib). `states` is
# explicit so the loader can validate transition
# targets, terminal membership, and reachability.
business_fsm:
  id: "fsm:KnowledgeLifecycle"
  component: "fmh_office.knowledge.components.DocumentComponent"
  state_field: "lifecycle_state"
  initial_state: "INGESTED"
  states:
    - "INGESTED"
    - "EXTRACTING"
    - "WAITING_HUMAN_REVIEW"
    - "CONSOLIDATED"
    - "REJECTED"
  terminal: ["CONSOLIDATED", "REJECTED"]

  transitions:
    - {from: "INGESTED",  to: "EXTRACTING",          on_event: "document.ingested"}
    - {from: "EXTRACTING", to: "CONSOLIDATED",        on_event: "knowledge.entities_extracted",
                                                    guard: "ExtractionConfidencePassed"}
    - {from: "EXTRACTING", to: "WAITING_HUMAN_REVIEW", on_event: "knowledge.entities_extracted",
                                                    guard: "not(ExtractionConfidencePassed)"}

  # ``on_entry`` emits a domain event when the FSM
  # transitions into the named state. Mirrors
  # §3.2 ``FSMConfig.on_entry``.
  on_entry:
    CONSOLIDATED:         "knowledge.consolidated"
    WAITING_HUMAN_REVIEW:  "knowledge.awaiting_review"

# Sagas. ``id`` becomes the `Concordo.name`
# (prefix `saga:`). ``trigger_event`` is the
# external event that starts the saga; the
# loader emits ``saga.<name>.started`` internally
# and the projection materialises
# ``SagaProgressComponent`` (see §4).
workflow_sagas:
  - id: "saga:EntityExtractionSaga"
    trigger_event: "document.ingested"
    saga_timeout_ms: 300000

    # Failure policy. Default if omitted: fail on
    # the first failure of any step (matches the
    # Python ``fail_when=None`` semantics).
    fail_when: "step_failed('EntityExtraction') or step_timed_out('EntityExtraction')"

    steps:
      - name: "TextChunking"
        tool: "text_chunker_tool"
        timeout_ms: 10000
        # ``input_mapping`` injects values into the
        # tool's params; the LHS is the param name,
        # the RHS is a path expression evaluated at
        # dispatch time (§14.5).
        input_mapping:
          text: "event.data.content"

      - name: "EntityExtraction"
        tool: "gliner2_entity_extraction_tool"
        pre_condition: "step_completed('TextChunking')"
        timeout_ms: 60000
        compensate_tool: "gliner2_rollback_tool"
        # ``compensate_when`` mirrors §4.3; if
        # omitted, the compensation runs on every
        # failure of this step.
        compensate_when: "not(step_timed_out('EntityExtraction'))"
        input_mapping:
          chunks: "steps.TextChunking.output.chunks"
```

The same shape is accepted as JSON (the schema
parses both). The loader detects the file
extension and routes to `yaml.safe_load` (via
`pyyaml`, already used by
`agents/role_systems/_rule_based.py`) or
`json.loads`. JSON is a strict subset of YAML for
the shapes we use.

**Unknown keys are rejected** at every level —
both top-level (a typo in `bundleid` instead of
`bundle_id` is fatal) and nested (a typo in
`guardd` fails the load). The error path is
dotted (`workflow_sagas[0].steps[1].compensate_tool`)
so the operator can find the offender in a file
without reading line-by-line.

### 14.3 Loader

```python
from pathlib import Path
from kntgraph.concordos import ConcordoCatalog

catalog = ConcordoCatalog.from_yaml(Path("app.yaml"))
# The catalog now contains one Concordo per
# ``business_fsm`` and one per ``workflow_sagas``
# entry. Each Concordo is the frozen bundle
# described in §3.5 / §4.7.

dispatcher = ReactiveDispatcher(log=log, redis=redis)
catalog.install_all(dispatcher)
```

The loader does four things in order:

1. **Parse** the file via `pyyaml` / `json`.
2. **Validate** against the Pydantic schemas
   (§14.4). Schema errors raise
   `ConcordoValidationError` with the dotted path.
3. **Cross-validate**:
   - `business_fsm.transitions[].on_event` is in
     `events[].name`.
   - `business_fsm.transitions[].from` / `to` are
     in `states`.
   - `workflow_sagas[].trigger_event` is in
     `events[].name`.
   - `workflow_sagas[].steps[].tool` is a known
     `@tool_worker` (validated against the worker's
     registry; the dispatcher exposes the lookup).
   - `workflow_sagas[].steps[].name` is unique
     within the saga.
   - `input_mapping` paths reference declared scopes
     (§14.5).
4. **Resolve dotted paths** to Python objects
   (`component:` is loaded via `importlib`,
   builtin spec names are resolved against the
   built-in registry, named specs against
   `specifications:`).

`from_json(path)` is the same with `json.loads`.
`from_dict(d)` accepts a pre-parsed dict (useful
for tests).

### 14.4 Pydantic schemas

```python
# concordos/schemas.py
from __future__ import annotations
import re
from typing import Literal
from pydantic import BaseModel, Field, field_validator


# Pattern matches ``domain.subdomain.name`` /
# ``tool.<name>.requested`` / ``saga.<name>.started``
# / etc. Strict enough to catch typos, permissive
# enough for the framework's vocabulary.
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")


class EventSchema(BaseModel):
    """One event in the bundle's vocabulary."""
    name: str = Field(pattern=_EVENT_NAME.pattern)
    schema_: dict = Field(alias="schema")  # JSON Schema lite

    @field_validator("schema_")
    @classmethod
    def _json_schema_lite(cls, v: dict) -> dict:
        # ``jsonschema.Draft7Validator.check_schema``
        # ensures the inner schema is itself valid
        # JSON Schema. Importing jsonschema only at
        # load time keeps cold-start cost low.
        from jsonschema import Draft7Validator

        Draft7Validator.check_schema(v)
        return v


class SpecificationSchema(BaseModel):
    """A named predicate (§14.5).

    ``expression`` is a string in the mini-language.
    It is NOT validated for semantic correctness
    here — only syntactic parsing happens at load
    time. Semantic evaluation is lazy (at guard
    evaluation), so a typo is caught at runtime
    with the full ``StepContext`` available.
    """
    id: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    expression: str = Field(min_length=1)


class FSMTransitionSchema(BaseModel):
    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)
    on_event: str
    guard: str | None = None


class FSMConfigSchema(BaseModel):
    id: str = Field(pattern=r"^fsm:[A-Za-z_][A-Za-z0-9_]*$")
    component: str  # dotted path; resolved via importlib
    state_field: str = Field(min_length=1)
    initial_state: str = Field(min_length=1)
    states: list[str] = Field(min_length=1)
    terminal: list[str] = Field(default_factory=list)
    transitions: list[FSMTransitionSchema] = Field(min_length=1)
    on_entry: dict[str, str] = Field(default_factory=dict)


class SagaStepSchema(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    tool: str | None = None  # None declares a human step (§9.2)
    timeout_ms: int = Field(default=30_000, ge=1)
    pre_condition: str | None = None
    skip_when: str | None = None
    compensate_tool: str | None = None
    compensate_when: str | None = None
    approval_timeout_ms: int | None = None
    input_mapping: dict[str, str] = Field(default_factory=dict)


class SagaConfigSchema(BaseModel):
    id: str = Field(pattern=r"^saga:[A-Za-z_][A-Za-z0-9_]*$")
    trigger_event: str
    saga_timeout_ms: int = Field(default=300_000, ge=1)
    fail_when: str | None = None
    steps: tuple[SagaStepSchema, ...] = Field(min_length=1)


class BundleSchema(BaseModel):
    """The top-level shape.

    A bundle is the unit of versioning and loading.
    A vertical that needs two unrelated FSMs ships
    two bundles, not one bundle with two
    ``business_fsm`` keys (which would be
    syntactically ambiguous).
    """
    bundle_id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    events: list[EventSchema] = Field(default_factory=list)
    specifications: list[SpecificationSchema] = Field(default_factory=list)
    business_fsm: FSMConfigSchema | None = None
    workflow_sagas: list[SagaConfigSchema] = Field(default_factory=list)

    @field_validator("workflow_sagas")
    @classmethod
    def _unique_step_names(cls, v: list[SagaConfigSchema]) -> list[SagaConfigSchema]:
        for saga in v:
            names = [s.name for s in saga.steps]
            if len(names) != len(set(names)):
                dupes = {n for n in names if names.count(n) > 1}
                raise ValueError(
                    f"saga {saga.id!r} has duplicate step names: {sorted(dupes)}"
                )
        return v
```

Errors raise `ConcordoValidationError` (a
`pydantic.ValidationError` subclass) carrying the
dotted path and the source file/line. The
`knt concordo validate` CLI prints the path
inline; tests catch the exception and assert on
the path field.

### 14.5 The predicate mini-language

Predicates appear in seven places: `guard`,
`fail_when`, `skip_when`, `compensate_when`,
`pre_condition`, `proceed_when`, and the
`expression` field of a `specifications` entry.
The grammar is the same in every location.

#### 14.5.1 Two forms: name lookup or expression

```yaml
guard: "ExtractionConfidencePassed"      # name lookup
guard: "event.data.score >= 0.80"          # expression
guard: "not(ExtractionConfidencePassed)"  # expression with composition
guard: "step_completed('TextChunking')"   # builtin call
```

**Resolution rule.** The loader classifies the
string syntactically:

- **No parens, no operator, no path, no number** →
  it is a **name**; the loader looks it up in
  (a) the bundle's `specifications:` list, then
  (b) the global `SpecRegistry` (§14.6). A
  missing entry fails validation with the
  message "predicate 'foo' not declared; declared:
  [...]".
- **Anything else** → it is an **expression**;
  the loader parses it (§14.5.2) and produces
  an in-memory evaluator. Parse errors fail
  validation with the column/line annotation.

#### 14.5.2 Expression grammar

```
expr        := or_expr
or_expr     := and_expr ( "or"  and_expr )*
and_expr    := not_expr ( "and" not_expr )*
not_expr    := "not" not_expr | atom
atom        := comparison | call | path | "(" expr ")" | literal
comparison  := path comp_op value
comp_op     := "==" | "!=" | "<=" | ">=" | "<" | ">"
value       := number | string | "true" | "false" | "null"
call        := identifier "(" expr_list? ")"
path        := scope "." tail
scope       := "event.data" | "steps" | "agent" | "now"
tail        := ( "." identifier )*
```

The parser is a recursive-descent implementation
(~150 lines, lives in `concordos/_mini_lang.py`).
It rejects anything outside this grammar —
assignment, function definition, module import,
attribute access on arbitrary objects — at parse
time. There is no `eval` and no Python AST
exposure; the evaluator walks the parsed AST
against a `StepContext` and returns a `bool`.

#### 14.5.3 Built-in functions

These are callable in any expression:

| Function | Returns | Example |
|---|---|---|
| `step_completed(name)` | `true` when the named step is in `step_states[name] == "completed"` | `step_completed('TextChunking')` |
| `step_failed(name)` | `true` when the step is `failed` or `timed_out` | `step_failed('EntityExtraction')` |
| `step_timed_out(name)` | `true` when the step is `timed_out` | `step_timed_out('EntityExtraction')` |
| `domain_state_is(field, value)` | `true` when `ctx.domain.field == value` | `domain_state_is('tax_regime', 'simples')` |
| `profile_tier_is(tier)` | `true` when `ctx.profile.tier == tier` | `profile_tier_is('vip')` |
| `continuity_tool_used(name)` | `true` when the named tool appears in `ctx.continuity.last_tools` | `continuity_tool_used('nfe_emitter')` |

These map to the Python builtin Specifications
declared in §2.3. Adding a new builtin is a
two-step change: implement the `Specification`
subclass in `concordos/specs.py`, register the
parser token in `_mini_lang.py`. The two stay
in sync via a small test that walks both
registries.

#### 14.5.4 Path scopes

| Prefix | Resolves to | Example |
|---|---|---|
| `event.data.*` | The data payload of the trigger event | `event.data.content_length` |
| `steps.<name>.output.*` | The tool worker's result (ADR-034 `ToolCallCompletion.result`) | `steps.TextChunking.output.chunks` |
| `steps.<name>.result.*` | Same as `output.*` (alias kept for clarity; the framework's projection installs the result under both keys) | `steps.TextChunking.result.chunks` |
| `agent.<field>` | The agent's `DomainComponent` fields (e.g. `agent.lifecycle_state`) | `agent.lifecycle_state` |
| `now` | The dispatcher-injected `datetime` (§3.4 / §4.5) | `now` (used as `now.year`, `now.hour`, etc.) |

A path that references an undeclared scope
(`user.data.foo`) is a parse error. A path that
references an undeclared step (`steps.Missing.output.x`)
is a runtime evaluation error (the `StepContext`
does not contain it).

#### 14.5.5 `input_mapping` paths

The same scopes apply, with one addition:
`event.data.*` resolves to the **triggering
event** of the saga (the event declared in
`trigger_event`). This is the most common case —
the saga starts with `document.ingested` and the
first step reads from `event.data.content`.

### 14.6 SpecRegistry (Python-side complement)

The mini-language covers **declarative** predicates
— anything expressible as `event.data.*`,
`steps.<name>.*`, `agent.*`, and the builtins.
Predicates that need **runtime logic** (a
remote lookup, a cached computation, integration
with an external system) cannot live in the YAML
and are registered in Python via `SpecRegistry`.

```python
# fmh_office/concordos/specs.py
from dataclasses import dataclass
from kntgraph.concordos.base import Specification, StepContext


@dataclass(frozen=True, slots=True)
class NfeRequired(Specification):
    """``True`` when fiscal validation indicates NF-e is required."""
    default: bool = True

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        result = ctx.step_results.get("validate_fiscal")
        if not isinstance(result, Mapping):
            return self.default
        return bool(result.get("nfe_required", self.default))


# fmh_office/app_setup.py
from kntgraph.concordos._spec_registry import SpecRegistry
from fmh_office.concordos.specs import NfeRequired, TaxRegimeIs


def register_specs() -> None:
    SpecRegistry.register("nfe_required", NfeRequired())
    SpecRegistry.register("tax_regime_simples", TaxRegimeIs("simples"))
```

```yaml
# app.yaml
specifications:
  - id: "nfe_required_proxy"
    expression: "nfe_required"   # looked up in SpecRegistry
```

A YAML that references an unregistered name fails
validation. Tests register specs in a fixture
and tear them down — `SpecRegistry` is a
class-level dict, not a module global, so it can
be reset between tests.

Custom Specifications that take constructor
parameters are **registered as instances**, not as
factories. The YAML carries no factory syntax. A
vertical that needs parameterised custom specs
registers each parameterisation under a distinct
name (`tax_regime_simples` vs.
`tax_regime_lucro_real`). The YAML is the full
specification of the rule set; there is no
hidden runtime construction.

### 14.7 Round-trip

```python
catalog = ConcordoCatalog.from_yaml("app.yaml")
catalog.to_yaml("app.normalized.yaml")  # canonical formatting
catalog.to_dict()  # round-trip via Pydantic
```

`to_yaml` / `to_json` are the inverse of the
loaders. They emit the canonical form so
version-controlled configs do not drift in
formatting (whitespace, key ordering, comment
preservation is **not** a goal — diffs should
show semantic changes only).

A bundle loaded and dumped round-trips
identically through the Pydantic schemas. The
order of `transitions`, `states`, and `steps` is
preserved as declared (Pydantic's list fields
are order-stable); the order of
`specifications` is preserved too, which matters
because a name lookup can shadow an earlier
declaration only via explicit re-binding (the
loader rejects duplicate IDs).

### 14.8 What is NOT in the YAML

- **Code (lambdas, callables).** Forbidden by
  design (§14.5.2). Specs that need runtime logic
  are registered in Python (§14.6).
- **Concurrency / parallelism knobs.** The Saga
  runs synchronously through `SagaSystem`; there
  is no per-step concurrency to tune. Parallel
  step fan-out, if ever needed, lives in a
  separate ADR (it is not in scope here).
- **Auth / secrets.** YAML is not a place for
  API keys. The Dispatcher's external
  configuration (Redis URL, etc.) is handled by
  `kntgraph.infra.config.Settings` (pydantic-settings).
- **Cross-tenant overrides.** A bundle is the
  unit of loading; per-tenant overrides (e.g.
  `tax_regime_simples` in some tenants but not
  others) are loaded as additional bundles and
  selected by `bundle_id` at runtime. This keeps
  bundle loading pure and side-effect-free.
- **Operator override events.** The earlier draft
  declared `saga.<name>.manual_resolved` /
  `retry_compensation` (§11.9). They are deferred
  to ADR-070 (§11.9 update); they will arrive as
  a second bundle of privileged events, not as
  inline fields.

### 14.9 Migration story

The Python API is unchanged. Existing code that
constructs `FSMConfig(...)` or `SagaConfig(...)`
in Python continues to work; the YAML is an
optional layer for environments that prefer
declarative configuration. PR 1.5 of the refactor
plan ships:

- `concordos/_loader.py` — `ConcordoCatalog.from_yaml`,
  `.from_json`, `.from_dict`.
- `concordos/schemas.py` — Pydantic schemas for the
  bundle, FSM, saga, event, specification.
- `concordos/_mini_lang.py` — the predicate
  parser + evaluator.
- `concordos/_spec_registry.py` — Python-side
  complement for non-declarative specs.
- `tests/fixtures/concordos/*.yaml` — example
  bundles referenced by the unit tests.

A new vertical can mix modes (§6.1.3) —
programmatic + YAML — and a CI step that runs
`knt concordo validate` against the YAML catches
drift before deploy.



