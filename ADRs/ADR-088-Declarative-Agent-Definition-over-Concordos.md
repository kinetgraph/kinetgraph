<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-088: Declarative Agent Definition over Concordos

- **Status:** Proposed
- **Date:** 2026-11-04
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-001](./ADR-001-Arquitetura.md) — pure ECS, Event Sourcing, `World = fold(events)`
  - [ADR-004](./ADR-004-Memory-Tools-Knowledge.md) — multi-tier memory model
  - [ADR-014](./ADR-014-Continuity-Tier.md) — `ContinuityComponent` sliding window
  - [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — `ToolCallRequest` / `ToolCallCompletion`
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — `@tool_worker` + `WorkerManager`
  - [ADR-039](./ADR-039-Role-rethinking-and-intentions-routing.md) — `RoleComponent` and role-based routing
  - [ADR-041](./ADR-041-agents-roles-deprecation.md) — removal of the legacy `kntgraph.agents.roles` package
  - [ADR-042](./ADR-042-Agents-Memory-Model-usage.md) — `SessionComponent`, `ProfileComponent`, `ContinuityComponent`
  - [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) — `DomainComponent` via ECS fold
  - [ADR-060](./ADR-060-fmh-office-v2-pillars.md) — the three-gate authorisation model, `PrincipalLevel`
  - [ADR-066](./ADR-066-Single-Tool-Path.md) — `WorkerManager` as the canonical tool path, gate 2
  - [ADR-069](./ADR-069-Agent-Concordo-Foundation.md) — Specification Pattern & Concordo Protocol
  - [ADR-071](./ADR-071-BusinessFSM-Concordo.md) — BusinessFSM Concordo (C-01)
  - [ADR-072](./ADR-072-WorkflowSaga-Concordo.md) — WorkflowSaga Concordo (C-02)
  - [ADR-089](./ADR-089-Architecture-Fitness-Harness-for-Model-Driven-Evolution.md) — architecture fitness harness

---

## 1. Context and Problem Statement

### 1.1 Existing Primitives

Kinetgraph has established the foundational primitives that an agentic application needs:

1. **Event Sourcing & Pure ECS (ADR-001, ADR-018):** `AgentView = fold(events)`. Side effects are isolated into workers.
2. **Decoupled Tool Workers (ADR-036):** Tools execute in isolated worker processes off the dispatcher loop, with at-most-once guarantees and idempotency keys.
3. **Layered Memory Model (ADR-042, ADR-059):**
   - *Volatile Dialogue:* `SessionComponent` (active conversation turns).
   - *Sliding-Window Recency:* `ContinuityComponent` (recent tools, categories, and hashed entities).
   - *Identity & Preferences:* `ProfileComponent` (tenant configurations, billing tiers).
   - *Structural Facts:* `DomainComponent` subclasses, materialised via the fold (ADR-059).
4. **Declarative Concordos (ADR-069, ADR-071, ADR-072):**
   - `BusinessFSMConcordo`: deterministic lifecycle transitions with `Specification` guards.
   - `WorkflowSagaConcordo`: multi-step tool orchestration with compensation and timeouts.
   - `Specification`: a composable, pure predicate evaluated against a runtime carrier.
   - `Concordo` Protocol and `ConcordoCatalog`: the composition mechanism that installs a bundle's systems and projections on the dispatcher.
5. **Authorisation (ADR-060, ADR-066):** the three-gate model — `WorkerManager` ACL (gate 1), `RoleComponent.allowed_tools` (gate 2), and the tool's own implementation (gate 3).

### 1.2 The Architectural Gap

The primitives exist, but they are assembled by hand at every call site. A developer who wants to model an agent must currently perform six independent wirings:

1. Construct `FSMConfig` / `FSMTransition` objects and wrap them in a `BusinessFSMConcordo`.
2. Construct `SagaConfig` / `SagaStepConfig` objects and wrap them in a `WorkflowSagaConcordo`.
3. Construct a `RoleComponent` and arrange for it to be materialised on the `AgentView`.
4. Register the agent's tools with the `WorkerManager` and the correct `ToolACL`.
5. Assemble a `ConcordoCatalog` and call `install_all(dispatcher)`.
6. Ensure that every `Specification` — FSM guard, Saga `skip_when`, Saga `proceed_when` — observes the same memory tiers.

Step 6 is the sharpest edge. `StepContext` is the carrier every `Specification` evaluates against, but the framework builds it in five places with inconsistent content: the FSM path populates `domain`, `continuity` and `profile`, while two Saga paths (`first_non_skipped_step` and the compensation path) pass `domain=None`, `continuity=None` and `profile=None`. A Specification written for an FSM guard therefore behaves differently when reused as a Saga skip condition. That inconsistency is technical debt, not a design decision; it is tracked separately in `DEBT.md` §2.42.

The result is that "modeling an agent" and "wiring an agent into the runtime" are the same activity. There is no first-class declaration of what an agent *is*.

### 1.3 What Industry Abstractions Do Not Solve Here

Stateful, imperative agent loops (as in LangChain or CrewAI) conflict with the framework's invariants: they block the event loop, they lose in-flight work on a crash, and they cannot express transactional compensation. The agent model here must remain declarative and event-driven.

---

## 2. Decision

We introduce a **declarative agent definition** in the `agents` vertical, layered over the Concordos, the tools path, and the ECS projections. The developer models an agent once; the framework derives the installable runtime from that model.

### 2.1 Scope

This ADR decides one thing: the shape of the declarative agent model and the derivation of its runtime.

**In scope:**

- The `AgentDefinition` and `RoleSpec` data model.
- The `AgentBundle` derivation, and the boundary between model data and runtime objects.
- The `RoleProjection` that materialises the `RoleComponent` from the event stream.
- The installation contract against the dispatcher.

**Explicitly out of scope:**

- **Serialisation.** `AgentDefinition` is a Python API, not an interchange format. A YAML/JSON representation is deferred; the model/data boundary established here (§3.2) is the seam where such a format would attach.
- **Unified `StepContext` construction.** That is a technical-debt correction, tracked in `DEBT.md` §2.42, not a decision of this ADR.
- **Prompt assembly from memory.** Formatting memory tiers into LLM prompt sections, and the trust boundaries that formatting requires, are a separate concern with their own security requirements.
- **Agent-aware dispatch.** The dispatcher continues to know only `WorldSystem` and `WorldProjection`. It never learns the concept of an agent (§3.6).
- **Agent discovery and lifecycle.** Spawning, termination, and multi-agent discovery remain outside this ADR.

### 2.2 The Declarative Model

An agent is declared as data. The developer creates exactly **one** object:

```python
@dataclass(frozen=True, slots=True)
class RoleSpec:
    """Declarative persona and capability policy of an agent.

    The model side of the role. ``RoleComponent`` is its ECS
    projection; the two are distinct because a spec is static
    configuration while a component is runtime state that the
    event stream can change.
    """

    persona: str
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Declarative model of one agent.

    The developer fills this object once; the framework derives the
    installable systems and projections from it. The object carries
    no runtime credentials and no dispatcher handles.
    """

    name: str
    persona: RoleSpec
    role_event_type: str
    fsm: BusinessFSMConcordo | None = None
    sagas: tuple[WorkflowSagaConcordo, ...] = ()
    domain_types: tuple[type[DomainComponent], ...] = ()
```

### 2.3 The Runtime Derivation

The framework derives an `AgentBundle` from the definition. The bundle carries **only** the live objects the dispatcher registers; it repeats no field from the definition.

```python
@dataclass(frozen=True, slots=True)
class AgentBundle:
    """Installable artifact derived from an ``AgentDefinition``.

    Ephemeral: built at install time and discarded. It exists so the
    derivation has a name and so the bundle satisfies the ``Concordo``
    Protocol without the definition storing derived state.
    """

    systems: tuple[WorldSystem, ...]
    projections: tuple[WorldProjection, ...]
```

The derivation is the only place where the definition's parts become runtime objects:

```text
AgentDefinition
  │
  ├─ fsm.systems + fsm.projections
  ├─ sagas[*].systems + sagas[*].projections
  └─ RoleProjection(spec=persona, event_type=role_event_type)
        │
        ▼
AgentBundle(systems=..., projections=...)
```

There is **no duplication**: `name`, `persona`, `fsm`, `sagas` and `domain_types` live in the definition only; `systems` and `projections` live in the bundle only. The bundle cannot disagree with the definition because it never stores a copy of it.

### 2.4 The Installation Contract

The definition owns the installation. The `Principal` is a runtime credential and is passed in, never stored in the model.

```python
def install(self, dispatcher: ReactiveDispatcher, *, principal: Principal) -> None:
    """Register this agent's systems and projections on the dispatcher.

    The principal authorises the agent's outbound requests; it is
    supplied by the host because a ``Principal`` carries a revocable
    key identifier and does not belong in a declarative model.
    """
    bundle = self.bundle()
    for system in bundle.systems:
        dispatcher.add_system(system)
    for projection in bundle.projections:
        dispatcher.add_projection(projection)
```

Because `AgentBundle` satisfies the `Concordo` Protocol (ADR-069 §3.2), a caller that prefers the catalog path can write:

```python
catalog = ConcordoCatalog(*(d.bundle() for d in definitions))
catalog.install_all(dispatcher)
```

Both paths are supported; the definition remains the single source of truth.

### 2.5 The Dual-Cycle Execution Model

The definition composes the two execution cycles the framework already provides:

1. **Reactive cycle (zero-token governance via FSM).** Domain events trigger transition evaluation. The FSM evaluates `transition.guard.is_satisfied_by(ctx)`. An illegal transition or a failing guard emits `fsm.transition_rejected`. No LLM is invoked and no token is spent.
2. **Deliberative cycle (sagas and workers).** A valid intent advances a `WorkflowSagaConcordo`. Steps evaluate `skip_when` and `proceed_when` Specifications, dispatch tools by emitting `tool.<name>.requested`, and compensate in LIFO order when a step fails.

### 2.6 Role Materialisation

The framework currently has no projection that materialises `RoleComponent`; each vertical invents its own (`examples/21_two_level_authorization.py` defines a `DemoRoleComponent` subclass with a local `@domain_component` registration). The definition closes that gap with a projection that combines the static spec with the dynamic event payload:

```python
class RoleProjection:
    """Materialise ``RoleComponent`` from the role event stream.

    The spec supplies the static capability policy; the event
    supplies the persona and instructions that the runtime assigned.
    """

    def __init__(self, spec: RoleSpec, event_type: str) -> None: ...

    def __call__(self, world: World, events: list[Event]) -> World: ...
```

This follows the pattern established in ADR-069: `FSMProjection` and `SagaProjection` are dedicated projections registered on the dispatcher, and `RoleProjection` is the third member of that family.

`RoleComponent` itself is **not** deprecated. It remains the ECS projection and the gate-2 input read by `has_tool_access` (`src/kntgraph/core/components/role.py`), and it remains a framework type because `ToolAwareSystem` and `PrincipalLevel` both reference it. What this ADR adds is the model-side `RoleSpec` that precedes it — the same relationship that ADR-060 established between the deprecated `Role` class and `RoleComponent`.

---

## 3. Detailed Design

### 3.1 Module Placement

The abstraction lives in the `agents` vertical, because it is a high-level abstraction over ECS, not a framework primitive:

```text
src/kntgraph/agents/
  _definition.py    RoleSpec, AgentDefinition
  _bundle.py        AgentBundle, AgentDefinition.bundle(), AgentDefinition.install()
  _projection.py    RoleProjection
```

The `agents` package is a vertical in the type-discipline taxonomy, so it may use `Any` and `object` where the framework may not. The types introduced here are nevertheless written to the framework's stricter standard, because a vertical that models agents is the most likely place for the abstraction to grow.

### 3.2 The Model / Runtime Boundary

The boundary between data and runtime is the reason the derivation is a separate object:

| Layer | Object | Holds | Serialisable (future) |
|---|---|---|---|
| Model | `RoleSpec` | persona, allowed tools | Yes |
| Model | `AgentDefinition` | name, persona, role event type, FSM/Saga references, domain types | Partially — the Concordos are the obstacle |
| Derived | `AgentBundle` | live `WorldSystem` / `WorldProjection` instances | No |
| Runtime | `Principal` | identity, level, tenant, key id | No — revocable credential |

A future serialisation format attaches at the model layer. It would need a declarative schema for `FSMConfig` and `SagaConfig` — which ADR-073 already provides in bundle form — but not for the derived layer. Keeping the derived layer out of the model is what makes that future possible without redesigning the abstraction.

### 3.3 Domain Components Are Read by Type

`domain_types` declares which `DomainComponent` subclasses the agent's Specifications read. `DomainComponent` is the superclass of every domain component (including `RoleComponent`), so it is not itself a tier: `view.get_component(DomainComponent)` resolves by `isinstance` and is ambiguous when an agent carries several domain components.

The agent's own FSM already resolves its state component by concrete type — `FSMConfig.component_type` is `type[DomainComponent]` and `FSMSystem` calls `view.get_component(self.config.component_type)` (`src/kntgraph/concordos/fsm/_system.py:159`). The declaration extends that pattern to the whole agent: a Specification reads a domain component by its concrete class, and the declaration records which classes the agent uses.

The corresponding `StepContext` change — replacing the single ambiguous `domain` field with type-indexed access — belongs to `DEBT.md` §2.42, since it is a correction of the existing carrier rather than a decision introduced here.

### 3.4 The Definition Is the Only Developer-Facing Object

The developer writes one object per agent and installs it:

```python
definition = AgentDefinition(
    name="checkout",
    persona=RoleSpec(
        persona="checkout",
        allowed_tools=("reserve_inventory", "charge_payment"),
    ),
    role_event_type="role.assigned",
    fsm=BusinessFSMConcordo(config=fsm_config),
    sagas=(WorkflowSagaConcordo(config=saga_config),),
)

definition.install(dispatcher, principal=principal)
```

`AgentBundle` is derived and discarded; the developer never constructs it directly. `Principal` is supplied by the host because it carries a revocable key identifier and belongs to the runtime, not to the model.

### 3.5 Tool Authorisation

The agent's tool capability policy is `RoleSpec.allowed_tools`. It is projected into `RoleComponent.allowed_tools`, which is what gate 2 reads. `AgentDefinition` therefore has no separate `tools` field: a second list would duplicate the policy and could contradict it.

Gate 1 (`WorkerManager` ACL) and gate 3 (the tool's own implementation) remain where they are. This ADR does not change the three-gate model.

### 3.6 The Dispatcher Does Not Know About Agents

The dispatcher registers `WorldSystem` and `WorldProjection` instances. It never reads a definition, never consults a memory-tier declaration, and never branches on an agent archetype. The agent notion lives entirely in the composition layer — the host that builds definitions, derives bundles, and installs them.

Consequently, the earlier draft's "selective hydration" proposal (the dispatcher inspecting `memory_tiers`) is withdrawn. Memory hydration remains a global post-fold projection (`project_memory`, ADR-042 §6.1), independent of any agent declaration.

### 3.7 What Is Not Modelled

- **Runtime prompts.** `RoleComponent.instructions` is assigned at runtime by the role event, not declared in `RoleSpec`. A static spec does not carry the prompt.
- **Role events.** The definition names the event type; it does not define the event schema.
- **Tool implementations.** The definition names capabilities; the tools themselves are registered on the `WorkerManager` by the host.
- **Tenant scope.** `Principal.tenant_id` is the authority for scope. The definition does not duplicate it.

---

## 4. Consequences

### Positive

- **One place to model an agent.** The developer fills a single frozen dataclass instead of wiring six independent concepts. The FSM, the Saga, the persona and the domain surface are declared together and installed together.
- **Specifications become portable.** Because the model declares one memory and domain surface, a Specification authored for an FSM guard is reusable as a Saga skip condition. The prerequisite — identical `StepContext` content on both paths — is `DEBT.md` §2.42.
- **No duplicated state.** The model, the derived bundle and the runtime credential each hold distinct information. The bundle cannot drift from the definition because it stores no copy of it.
- **Role policy has one source.** `RoleSpec.allowed_tools` projects into `RoleComponent.allowed_tools`, so gate 2 reads the same policy the developer declared.
- **The framework stops leaking agent mechanics into verticals.** The per-vertical `RoleComponent` projection pattern is replaced by one framework projection.
- **The dispatcher stays ignorant of agents.** Agent knowledge lives in the composition layer, which is where it belongs.

### Negative & Mitigations

- **A second role vocabulary.** `RoleSpec` and `RoleComponent` both name a persona and a tool allow-list.
  *Mitigation:* the split is the model/runtime distinction ADR-060 already established for `Role` → `RoleComponent`. `RoleSpec` is static configuration; `RoleComponent` is runtime state the event stream can change. A `to_component()` conversion on `RoleSpec` keeps the boundary explicit and the conversion single-sourced.
- **The definition holds live Concordos.** `BusinessFSMConcordo` and `WorkflowSagaConcordo` are objects, not data, which blocks a future serialisation of `AgentDefinition`.
  *Mitigation:* the model/runtime boundary (§3.2) confines the obstacle to the model layer, where ADR-073's bundle schemas already describe the declarative shape of `FSMConfig` and `SagaConfig`. Serialisation remains out of scope until that work is scheduled.
- **`domain_types` is declared but not yet consumed.** Until `DEBT.md` §2.42 lands, `StepContext.domain` remains the ambiguous single field.
  *Mitigation:* the declaration is inert — it changes no runtime behaviour — so it can land before the carrier change without ordering constraints.
- **The `RoleProjection` event schema is the vertical's.** The projection reads `persona` and `instructions` from the event payload, so each vertical must use those key names.
  *Mitigation:* the projection documents the expected payload keys, and a vertical that needs a different schema subclasses it rather than forking it.

---

## 5. Validation and Acceptance

The ADR is **Accepted** when the design above is agreed and the implementation plan is scheduled.

It is **Implemented** when:

- [ ] `RoleSpec` and `AgentDefinition` exist in `src/kntgraph/agents/_definition.py`, with frozen dataclasses and slots.
- [ ] `AgentBundle` exists in `src/kntgraph/agents/_bundle.py` and satisfies the `Concordo` Protocol.
- [ ] `AgentDefinition.bundle()` derives the systems and projections, including the `RoleProjection`, and stores no duplicated field.
- [ ] `AgentDefinition.install(dispatcher, principal=principal)` registers the systems and projections on the dispatcher.
- [ ] `RoleProjection` materialises `RoleComponent` from the declared event type, combining `RoleSpec.allowed_tools` with the event's persona and instructions.
- [ ] `RoleComponent` and `has_tool_access` remain in the framework unchanged; gate 2 reads the projected component.
- [ ] The dispatcher gains no agent-awareness: no definition, persona or memory-tier field is read by `ReactiveDispatcher` or its modules.
- [ ] `DEBT.md` §2.42 is open, with the `StepContext` unification and the type-indexed domain access tracked.
- [ ] Tests cover the happy path and at least one failure mode for every public function: `bundle()` (with and without an FSM), `install()` (registration order and idempotence), and `RoleProjection` (event present, event absent, payload missing a key).
- [ ] `kntgraph-type-discipline` rules hold for the new modules: no `Any`, no `TypedDict`, frozen dataclasses for shape-known data.

### Follow-up, not blocking

- An `AgentContractVerifier` in `kntgraph.testing` (ADR-089 §3.2) that asserts a definition's bundle registers exactly the expected systems and projections, and that a `RoleProjection` round-trips a `RoleSpec` through a role event.
- The `DEBT.md` §2.42 work: unified `StepContext` construction, `session` in the carrier, type-indexed domain access, and the FSM/Saga parity test.

---

## 6. Alternatives Considered

- **Keep the current hand-wiring.** Rejected. The model and the wiring are the same activity, and Specifications are not portable between the two execution cycles because the carrier differs.
- **Make `AgentDefinition` a framework primitive in `concordos/`.** Rejected. The abstraction sits above ECS and above the Concordos; placing it in the framework tier would make a high-level application concept a framework commitment.
- **Let `AgentDefinition` implement the `Concordo` Protocol directly.** Rejected in favour of a derived `AgentBundle`. Implementing the Protocol on the definition would store derived state (`systems`, `projections`) on the model, which is exactly the duplication this design avoids.
- **Keep `AgentBundle` and `AgentBinding` as separate developer-facing objects.** Rejected. The developer should create one object; the bundle is derived and ephemeral, and the `Principal` is a parameter, not stored state.
- **Make the dispatcher agent-aware** so it could skip hydration for agents that declare no memory tier. Rejected. It couples the dispatcher to a concept it does not own, and the global post-fold hydration is already the framework's established shape.
- **Decommission `RoleComponent` in favour of `RoleSpec`.** Rejected. `RoleComponent` is the ECS projection read by gate 2 and referenced by `ToolAwareSystem` and `PrincipalLevel`; the model/runtime split is the established pattern, not a replacement.
- **Serialise the agent definition.** Deferred, not rejected. The model/runtime boundary in §3.2 is designed so that a future format attaches without redesigning the abstraction.

---

## 7. See also

- [ADR-069 — Agent Concordo Foundation](./ADR-069-Agent-Concordo-Foundation.md)
- [ADR-071 — BusinessFSM Concordo](./ADR-071-BusinessFSM-Concordo.md)
- [ADR-072 — WorkflowSaga Concordo](./ADR-072-WorkflowSaga-Concordo.md)
- [ADR-089 — Architecture Fitness Harness](./ADR-089-Architecture-Fitness-Harness-for-Model-Driven-Evolution.md)
- `DEBT.md` §2.42 — unified `StepContext` construction and type-indexed domain access
