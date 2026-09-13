<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-069: Agent Concordo Foundation

- **Status:** Accepted
- **Date:** 2026-09-07 (revised 2026-09-12)
- **Author:** kinetgraph architecture team
- **Supersedes:** initial draft (changes from review tracked in §11 of the archived revision)
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
  - [ADR-068](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md) — wakeup stream
  - [ADR-071](./ADR-071-BusinessFSM-Concordo.md) — BusinessFSM Concordo (C-01)
  - [ADR-072](./ADR-072-WorkflowSaga-Concordo.md) — WorkflowSaga Concordo (C-02)
  - [ADR-073](./ADR-073-Concordo-Bundle-Format.md) — Concordo Bundle Format
  - [ADR-074](./ADR-074-Per-System-Cursors-in-AgentView.md) — Per-system cursors in AgentView

---

## 1. Context

### 1.1 What the framework already provides

The framework delivers a correct mechanical substrate.
Before proposing new abstractions, the existing
capabilities are stated explicitly to avoid duplication:

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
level of **recognisable, reusable behavioural
compositions** that wire multiple framework modules
into a coherent whole.

Two patterns emerge in every business vertical:

1. **Business object lifecycle** (C-01, [ADR-071](./ADR-071-BusinessFSM-Concordo.md))
   — a document, an invoice, a task has a defined set of
   states and allowed transitions.
2. **Multi-step orchestration with compensation**
   (C-02, [ADR-072](./ADR-072-WorkflowSaga-Concordo.md)) —
   a business operation requires a sequence of tool calls
   where (a) each step may fail, (b) context from previous
   steps feeds into subsequent ones, and (c) failures
   must trigger compensating actions.

Both patterns share a foundation: a condition language
to express business rules, and a composition mechanism
to install them on the dispatcher. This ADR captures
the foundation; the FSM and Saga are separate ADRs.

### 1.3 What this ADR does NOT cover

- The FSM implementation (`FSMConfig`, `FSMSystem`,
  `FSMProjection`, `FSMAuditComponent`) lives in
  [ADR-071](./ADR-071-BusinessFSM-Concordo.md).
- The Saga implementation (`SagaConfig`, `SagaSystem`,
  `SagaTimeoutSystem`, `SagaProjection`,
  `SagaProgressComponent`) lives in
  [ADR-072](./ADR-072-WorkflowSaga-Concordo.md).
- The YAML / JSON serialization of Concordos (the
  "bundle format", the mini-language, the Pydantic
  schemas) lives in
  [ADR-073](./ADR-073-Concordo-Bundle-Format.md) and
  its implementation reference
  [docs/concordos-bundle-spec.md](../docs/concordos-bundle-spec.md).
- Worker-level back-pressure (a follow-up concern) lives
  in [ADR-070](./ADR-070-Worker-Level-Back-Pressure.md).
- Operator recovery events (`manual_resolved`,
  `retry_compensation`) are deferred to ADR-070.

---

## 2. The Specification Pattern

The **Specification Pattern** (Evans, *Domain-Driven
Design* §9) defines a pure, composable predicate
language over a `StepContext`. Both the FSM (as guard
on a transition) and the Saga (as `skip_when`,
`compensate_when`, `pre_condition`, etc.) use
Specifications.

The implementation reference is
[docs/specification_pattern.md](../docs/specification_pattern.md).
This section captures the design rationale and the
public surface.

### 2.1 Protocol

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

    ``now`` is the dispatcher's current tick timestamp
    (injected by the system that builds the context; see
    ADR-071 §3.4 and ADR-072 §4.5 for the injection
    pattern). Specifications MUST treat ``now`` as the
    only clock source; reading ``datetime.now()`` inside
    ``is_satisfied_by`` would break replay determinism.

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
    cheap" contract.

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
    cross_agent_resolver: "Callable[[str], AgentView | None] | None" = None


class Composable:
    """
    Mixin providing ``and_``, ``or_``, ``not_``
    combinators as default implementations.

    Concrete Specifications inherit this mixin and
    only implement ``is_satisfied_by``. The combinators
    return typed ``AndSpec`` / ``OrSpec`` / ``NotSpec``
    instances, which themselves compose further via the
    same mixin.
    """

    def and_(self: "Specification", other: "Specification") -> "AndSpec":
        return AndSpec(self, other)

    def or_(self: "Specification", other: "Specification") -> "OrSpec":
        return OrSpec(self, other)

    def not_(self: "Specification") -> "NotSpec":
        return NotSpec(self)


class Specification(ABC, Composable):
    """Composable predicate over ``StepContext``."""

    @abstractmethod
    def is_satisfied_by(self, ctx: StepContext) -> bool: ...


@dataclass(frozen=True, slots=True)
class AndSpec(Specification):
    left: Specification
    right: Specification
    def is_satisfied_by(self, ctx): ...


@dataclass(frozen=True, slots=True)
class OrSpec(Specification):
    left: Specification
    right: Specification
    def is_satisfied_by(self, ctx): ...


@dataclass(frozen=True, slots=True)
class NotSpec(Specification):
    inner: Specification
    def is_satisfied_by(self, ctx): ...
```

### 2.2 Built-in Specifications

The framework ships a standard library of
Specifications:

```python
StepCompleted(step_name)        # step_states[name] == "completed"
StepFailed(step_name)           # step_states[name] in ("failed", "timed_out")
StepTimedOut(step_name)         # step_states[name] == "timed_out"
StepResultEquals(step, field, value)  # step_results[step][field] == value
DomainStateIs(field, value)     # ctx.domain.field == value
ProfileTierIs(tier)             # ctx.profile.tier == tier
ContinuityToolUsed(tool_name)   # tool_name in ctx.continuity.last_tools
```

These map to the mini-language builtins in
[docs/concordos-bundle-spec.md §4](../docs/concordos-bundle-spec.md#4-built-in-specs-catalog).
A CI test asserts the two stay in sync.

### 2.3 Vertical Specifications

A vertical defines its own Specifications by inheriting
the ABC. Three rules:

1. **No globals, no I/O.** `is_satisfied_by` is a pure
   function of constructor params and `StepContext`.
2. **No clock injection.** Time-dependent rules read
   `ctx.now`, not `datetime.now()`.
3. **Parameters via constructor, runtime context via
   `StepContext`.** A Specification that varies per
   deployment (e.g. `TaxRegimeIs("simples")`) takes the
   parameter in its constructor; per-evaluation context
   comes from `ctx`.

### 2.4 SpecRegistry

App-defined Specifications are registered for cross-bundle
reference:

```python
# fmh_office/concordos/specs.py
@dataclass(frozen=True, slots=True)
class NfeRequired(Specification):
    default: bool = True
    def is_satisfied_by(self, ctx: StepContext) -> bool: ...

# fmh_office/app_setup.py
SpecRegistry.register("nfe_required", NfeRequired())
```

A YAML that references a name not in `SpecRegistry`
fails validation. Tests register in fixtures and tear
down — `SpecRegistry` is a class-level dict, reset
between tests.

The mini-language (in the bundle format, ADR-073) is
the **declarative complement** to `SpecRegistry`.
Declarative predicates (`event.data.x > 5`) live in
the YAML; non-declarative predicates (needing runtime
logic) live in Python via `SpecRegistry`. Both resolve
the same way at evaluation time.

---

## 3. The Concordo Protocol

A *Concordo* is a **named, versioned, composable
behavioural pattern** that wires existing framework
modules into a coherent end-to-end behaviour.

### 3.1 Definition

> **Definition.** A Concordo is expressed as a **frozen
> bundle** with three attributes:
>
> 1. ``name`` — stable identifier
>    (``fsm:KnowledgeLifecycle``,
>    ``saga:EntityExtractionSaga``).
> 2. ``systems`` — tuple of ``WorldSystem`` instances
>    the dispatcher should register.
> 3. ``projections`` — tuple of ``WorldProjection``
>    instances the dispatcher should register.

A Concordo does **not** mutate the dispatcher — the
caller (catalog or application code) iterates the
bundle and calls `dispatcher.add_system(...)` /
`dispatcher.add_projection(...)`. This matches the
framework's existing convention: every other system
(`ToolCallTTLSweeperSystem`, `MemoryHydrationProjection`,
`RuleBasedChatSystem`, the role systems) is
constructed externally and passed to the dispatcher.

A Concordo does **not** introduce new runtime
infrastructure. It does **not** duplicate
`ToolCallRequest`, `ToolCallCompletion`, TTL, or any
existing component. It composes them.

### 3.2 The Protocol

```python
from typing import Protocol


class Concordo(Protocol):
    """
    Public surface every Concordo bundle exposes.

    A Concordo is a frozen bundle of ``(name, systems,
    projections)``. The Protocol is structural —
    concrete bundles (FSM, Saga) are frozen dataclasses
    that satisfy it without explicit inheritance.

    The catalog (§4) iterates each bundle and calls the
    dispatcher's registration API. The application can
    also iterate ``concordo.systems`` directly when it
    needs explicit ordering.

    Side effects (DLQ ingestion, metrics, notifications)
    are wired by the application via
    ``dispatcher.subscribe`` (§5), not by the Concordo
    — Concordos stay pure and side-effect-free.
    """

    name: str
    systems: tuple["WorldSystem", ...]
    projections: tuple["WorldProjection", ...]
```

### 3.3 Concrete bundles

Both Concordos in the foundation are frozen dataclasses
with `__post_init__` that derives `systems` and
`projections` from the config:

```python
@dataclass(frozen=True, slots=True)
class BusinessFSMConcordo:
    """C-01 — see ADR-071 for the full design."""
    config: FSMConfig
    name: str = field(init=False)
    systems: tuple["WorldSystem", ...] = field(init=False)
    projections: tuple["WorldProjection", ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "name", f"fsm:{self.config.component_type.__name__}"
        )
        object.__setattr__(self, "systems", (FSMSystem(self.config),))
        object.__setattr__(
            self, "projections", (FSMProjection(self.config),)
        )
```

The full definition of `BusinessFSMConcordo` lives in
[ADR-071 §3.5](./ADR-071-BusinessFSM-Concordo.md#35-concordo-class).
The full definition of `WorkflowSagaConcordo` lives in
[ADR-072 §4.7](./ADR-072-WorkflowSaga-Concordo.md#47-concordo-class).

---

## 4. The Catalog

`ConcordoCatalog` is a thin bag that dedupes by name
and iterates bundles at install time. It is
**optional** — an application can iterate
`concordo.systems` directly when it needs explicit
ordering.

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
    a no-op (logged at INFO level).
    """

    def __init__(self, *concordos: Concordo) -> None:
        self._concordos: dict[str, Concordo] = {}
        for c in concordos:
            if c.name in self._concordos:
                continue
            self._concordos[c.name] = c

    def install_all(self, dispatcher: "ReactiveDispatcher") -> None:
        for concordo in self._concordos.values():
            for system in concordo.systems:
                dispatcher.add_system(system)
            for projection in concordo.projections:
                dispatcher.add_projection(projection)
```

### 4.1 Bundle loading

The catalog also accepts Concordos loaded from a YAML
or JSON bundle. The bundle format is defined in
[ADR-073](./ADR-073-Concordo-Bundle-Format.md) and
implemented in `concordos/_loader.py`. Loading
produces one `Concordo` per `business_fsm` block and
one per entry in `workflow_sagas`:

```python
catalog = ConcordoCatalog.from_yaml("app.yaml")
catalog.install_all(dispatcher)
```

### 4.2 Composition

Three equivalent entry points (full code in
[ADR-073 §6.1](./ADR-073-Concordo-Bundle-Format.md#61-composition-and-installation)):

- **Programmatic** — instantiate Concordos in Python,
  pass to the catalog.
- **YAML-loaded** — load a bundle, the catalog is
  populated automatically.
- **Hybrid** — mix programmatic and YAML; later
  `.add(...)` calls with the same name replace earlier
  entries.

The order of registration does not affect correctness
(Concordos communicate only through events). When the
order matters — e.g. a saga that must observe FSM
transitions before timeout — the application iterates
`concordo.systems` directly.

---

## 5. Cross-cutting concerns

### 5.1 Clock injection

Systems that need wall-clock time accept an optional
`now: Clock | None` in the constructor:

```python
class FSMSystem:
    def __init__(self, config: FSMConfig, *, now: "Clock | None" = None):
        self._now = injectable_clock(now)
```

The framework centralises this in `core/clock.py`:

```python
Clock = Callable[[], datetime]

def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)

def injectable_clock(now: Clock | None) -> Clock:
    return now or utcnow
```

`FSMSystem`, `SagaSystem`, `SagaTimeoutSystem`, and
the role systems follow this pattern. The dispatcher
does **not** carry a shared clock — each system
injects its own. A vertical that needs two systems
aligned (e.g. FSM and Saga in the same tick) passes
the same `now` callable to both constructors.

### 5.2 DLQ integration

The framework already has `DeadLetterQueue` /
`DeadLetterEvent` / `DLQReason` (`events/dlq/`).
The Concordos **do not** ship a saga-specific DLQ
adapter. Instead, the application wires DLQ ingestion
once via `dispatcher.subscribe`:

```python
# fmh_office/app_runner.py
async def ingest_compensation_failures(event):
    if not event.event_type.endswith(".compensation_failed"):
        return
    dl_event = DeadLetterEvent(
        event=event,
        reason=DLQReason.PROCESSING_FAILED,
        error_message="compensation_failed",
        original_timestamp=event.timestamp,
        dlq_timestamp=datetime.now(tz=timezone.utc),
        metadata=event.data,
    )
    await dlq.append(dl_event)

dispatcher.subscribe(["*"], ingest_compensation_failures)
```

The pattern matches what the framework already does
for tool-call TTL failures: `ToolCallTTLSweeperSystem`
emits `tool.<name>.failed` events; the application
decides whether to forward them to the DLQ, metrics,
or ignore. The same convention applies to the Saga's
`saga.<name>.compensation_failed` events.

### 5.3 Event emission convention

All events emitted by Concordo systems use
`Event.domain_from(...)` (not `Event.create(...)`).
`domain_from` pins `event_class="domain"` and rejects
events in the framework's operational namespace
(`agent.*`) at construction time — a typo in the
saga's event-type string fails loudly instead of
silently producing an unrouteable event.

### 5.4 Correlation propagation

Every event emitted by a Concordo system carries an
explicit `correlation=` (typically propagated from
the trigger). This is enforced by `Event.create`
itself, which raises `TypeError` if `correlation` is
missing (ADR-037).

### 5.5 EventLog subscribe

The saga's `compensation_failed` events are forwarded
to the DLQ via `dispatcher.subscribe` (§5.2). This
uses the existing `EventLog.subscribe_many` mechanism
from [ADR-068](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md).

### 5.6 Per-system cursors in AgentView

`AgentView` carries a per-system cursor
(`view.cursors: Mapping[str, str]`) that lets systems
detect which events they have already processed. The
dispatcher advances the cursor after each system
emits events; persistence piggy-backs on the
WorldCheckpoint.

This primitive addresses the **multi-event tick**
problem (`view.domain_phase` is a single slot;
earlier events in the same tick are silently
dropped) and provides replay-safety without per-system
state. Defined in
[ADR-074](./ADR-074-Per-System-Cursors-in-AgentView.md).

Systems that opt in read `view.cursors.get("X")`
in their `__call__`. Systems that don't opt in
ignore the field — `view.cursors.get("X")` returns
`None`. The dispatcher still writes the cursor for
all systems (conservative bookkeeping).

The first consumer is the FSM (ADR-071 §3.4.2). The
Saga does not opt in today; it tracks the latest
`tool.<name>.completed` per step and does not face
the multi-event problem.

---

## 6. Source code layout

The foundation lives in `concordos/` (shared between
the FSM and Saga ADRs):

```
src/kntgraph/concordos/
+-- __init__.py              # Concordo Protocol; ConcordoCatalog;
│                            #   from_yaml/from_dict classmethods
+-- base.py                  # Specification, StepContext,
│                            #   ViewTrigger (NamedTuple),
│                            #   Composable mixin (≤ 200 lines)
+-- specs.py                 # Built-in Specifications
│                            #   (StepCompleted, StepFailed, etc.)
+-- _spec_registry.py        # SpecRegistry.register(name, spec)
```

The FSM-specific code lives under `concordos/fsm/`
(see [ADR-071](./ADR-071-BusinessFSM-Concordo.md) for
the file layout). The Saga-specific code lives under
`concordos/saga/` (see
[ADR-072](./ADR-072-WorkflowSaga-Concordo.md)). The
bundle loader lives at `concordos/_loader.py`,
`concordos/schemas.py`, and `concordos/_mini_lang.py`
(see [ADR-073](./ADR-073-Concordo-Bundle-Format.md)).

---

## 7. Open questions

1. **Concurrency / re-entrancy.** If two
   `document.ingested` events arrive for the same
   agent in consecutive ticks, does the saga spawn
   two instances or queue? Currently undefined. To
   be addressed before the FSM/Saga ADRs ship their
   first PRs.
2. **State lifecycle.** `SagaProgressComponent` and
   `FSMAuditComponent` are append-only. After a saga
   reaches `done` or `compensated`, the component
   stays on the agent forever (memory growth).
   GC strategy is undefined.
3. **Mini-language method calls on `now`.** Calls
   like `now.weekday()` work via the same `call`
   production as builtin specs. If `weekday` is ever
   added as a builtin, the parser cannot
   distinguish. Tracked; out of scope for v1.
4. **Step ordering.** Saga steps are linear. Branching
   (a step dispatching multiple successors based on
   result) is out of scope. Vertical that needs
   branching composes multiple sagas or orchestrates
   externally.

---

## 8. References

- Evans, E. *Domain-Driven Design*, 2003 — Chapter 9, Specification Pattern
- Richardson, C. *Microservices Patterns*, 2018 — Chapter 4, Saga Pattern
- [Akka FSM](https://doc.akka.io/docs/akka/current/fsm.html)
- [ADR-001 — Pure ECS + Event Sourcing](./ADR-001-Arquitetura.md)
- [ADR-018 — WorldSystem + ReactiveDispatcher](./ADR-018-WorldIncremental-WorldSystem.md)
- [ADR-034 — ToolCall ECS Components](./ADR-034-ToolCall-ECS-Components.md)
- [ADR-037 — Mandatory Correlation Propagation](./ADR-037-Mandatory-Correlation-Propagation.md)
- [ADR-042 — Memory Model](./ADR-042-Agents-Memory-Model-usage.md)
- [ADR-045 — Tool Call TTL](./ADR-045-Tool-Call-Request-TTL.md)
- [ADR-059 — Domain Memory ECS Components](./ADR-059-Domain-Memory-ECS-Components.md)
- [ADR-068 — Idle Redis traffic and EventLog subscribe](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md)
- [ADR-071 — BusinessFSM Concordo](./ADR-071-BusinessFSM-Concordo.md)
- [ADR-072 — WorkflowSaga Concordo](./ADR-072-WorkflowSaga-Concordo.md)
- [ADR-073 — Concordo Bundle Format](./ADR-073-Concordo-Bundle-Format.md)

---

## 9. Implementation reference

- [docs/specification_pattern.md](../docs/specification_pattern.md) —
  the Specification Pattern in code.
- [docs/concordos-bundle-spec.md](../docs/concordos-bundle-spec.md) —
  the bundle format spec (mini-language, schemas,
  validator pipeline).
- [docs/business_fsm.md](../docs/business_fsm.md) —
  the BusinessFSM Concordo in code (companion to
  ADR-071).
- [docs/workflow_saga.md](../docs/workflow_saga.md) —
  the WorkflowSaga Concordo in code (companion to
  ADR-072).
