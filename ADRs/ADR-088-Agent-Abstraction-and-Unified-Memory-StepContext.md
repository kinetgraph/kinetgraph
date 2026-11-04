<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-088: Agent Abstraction over Concordos and Unified StepContext Memory

- **Status:** Proposed
- **Date:** 2026-11-04
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-001](./ADR-001-Arquitetura.md) — pure ECS, Event Sourcing, `World = fold(events)`
  - [ADR-004](./ADR-004-Memory-Tools-Knowledge.md) — multi-tier memory model
  - [ADR-014](./ADR-014-Continuity-Tier.md) — `ContinuityComponent` sliding window
  - [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — `ToolCallRequest` / `ToolCallCompletion`
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — `@tool_worker` + `WorkerManager`
  - [ADR-039](./ADR-039-Role-rethinking-and-intentions-routing.md) — `RoleComponent` + `IntentResolutionSystem`
  - [ADR-042](./ADR-042-Agents-Memory-Model-usage.md) — `SessionComponent`, `ProfileComponent`, `ContinuityComponent`
  - [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) — `DomainComponent` via ECS fold
  - [ADR-069](./ADR-069-Agent-Concordo-Foundation.md) — Specification Pattern & Concordo Protocol
  - [ADR-071](./ADR-071-BusinessFSM-Concordo.md) — BusinessFSM Concordo (C-01)
  - [ADR-072](./ADR-072-WorkflowSaga-Concordo.md) — WorkflowSaga Concordo (C-02)
  - [ADR-073](./ADR-073-Concordo-Bundle-Format.md) — Concordo Bundle Format

---

## 1. Context and Problem Statement

### 1.1 Existing Primitives
Kinetgraph has established robust foundational primitives across multiple tiers:
1. **Event Sourcing & Pure ECS (ADR-001, ADR-018):** `AgentView = fold(events)`. Side effects are isolated into workers.
2. **Decoupled Tool Workers (ADR-036):** Tools execute in isolated worker processes off the dispatcher loop with at-most-once guarantees and idempotency keys.
3. **Layered Memory Model (ADR-042, ADR-059):**
   - *Volatile Dialogue:* `SessionComponent` (active conversation turns).
   - *Sliding-Window Recency:* `ContinuityComponent` (recent tools, categories, and hashed entities).
   - *Identity & Preferences:* `ProfileComponent` (tenant configurations, billing tiers).
   - *Structural Facts:* `DomainComponent` (permanent business state materialized via fold).
4. **Declarative Concordos (ADR-069, ADR-071, ADR-072):**
   - `BusinessFSMConcordo`: Deterministic lifecycle transitions and guard validation.
   - `WorkflowSagaConcordo`: Multi-step tool orchestration with automatic compensation and timeouts.
   - `Specification`: Composable, pure predicates evaluated against a unified runtime carrier: `StepContext`.

### 1.2 The Architectural Gap
Despite these primitives, building an autonomous agent currently requires manual wiring across disparate packages:
- Developers must manually instantiate systems, wire memory projections, connect intent routers, and attach FSMs and Sagas to the dispatcher.
- The `StepContext` carrier in `src/kntgraph/concordos/base.py` aggregates `domain`, `continuity`, and `profile`, but **omits `SessionComponent`**. This creates an artificial divide where conversation memory lives outside the unified context.
- There is no first-class **`AgentDefinition`** abstraction representing the agent as a declarative bundle of its identity, memory requirements, state machines, workflows, and tools.
- Industry-standard agent abstractions (e.g. LangChain, CrewAI) rely on stateful, imperative `while`-loops that block event loops, break on crashes, and lack transactional compensation. Kinetgraph requires a purely declarative, event-driven agent model.

---

## 2. Decision

We introduce a declarative **Agent Abstraction** layered cleanly over Concordos, Tools, and a promoted **Unified `StepContext`**.

### 2.1 Promote `StepContext` to Canonical Memory Carrier
We extend `StepContext` to include `SessionComponent`. `StepContext` becomes the single, transparent in-memory lens for an agent's complete cognitive state:

| Semantic Tier | Memory Component | Nature | Retention / Scope |
|---|---|---|---|
| **Short-Term (Working Memory)** | `session: SessionComponent \| None` | Interactive dialogue turns & context | Ephemeral per conversation |
| **Mid-Term (Episodic Context)** | `continuity: ContinuityComponent \| None` | Recency window, past tools, hashed PII | Sliding window (TTL / event count) |
| **Long-Term (Domain Facts)** | `domain: DomainComponent \| None` | Concrete business state (e.g. invoice, order) | Durable, permanent fold of events |
| **Identity & Policy** | `profile: ProfileComponent \| None` | Tenant tier (VIP/Standard), user preferences | Stable entity configuration |
| **In-Flight Operations** | `step_results`, `step_states`, `trigger_data` | Current step outputs and triggering event | Transient per-tick / per-saga |

`StepContext` remains strictly:
- **Pure:** No I/O, no network calls, no Redis access during evaluation.
- **Immutable:** Frozen dataclass with slot optimization.
- **O(1) Access:** Pre-hydrated from `AgentView` components during the tick.

### 2.2 First-Class Declarative Agent (`AgentDefinition`)
An Agent is modeled not as an imperative worker loop, but as a **declarative specification of capabilities and governance**:

```python
@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Declarative specification of an autonomous agent."""
    name: str
    identity: Principal
    tools: tuple[str, ...]
    memory_tiers: tuple[type, ...] = (
        DomainComponent,
        ProfileComponent,
        ContinuityComponent,
        SessionComponent,
    )
    fsm: BusinessFSMConcordo | None = None
    sagas: tuple[WorkflowSagaConcordo, ...] = ()
    intent_router: IntentResolutionSystem | None = None
```

### 2.3 The Dual-Cycle Execution Model
The Agent runtime strictly separates deterministic governance from cognitive inference:

1. **Reactive Cycle (Zero-Token Governance via FSM):**
   - Domain events trigger transition evaluation.
   - The FSM evaluates `transition.guard.is_satisfied_by(ctx)`.
   - If guards fail or transitions are illegal, `fsm.transition_rejected` is emitted. **Zero tokens spent; invalid actions are structurally impossible.**
2. **Deliberative Cycle (Sagas & Workers):**
   - Valid intents trigger a `WorkflowSagaConcordo`.
   - Steps evaluate `skip_if` and `proceed_when` Specifications using `StepContext`.
   - In-flight steps emit `tool.<name>.requested` to the worker pool.
   - LLM workers receive prompt sections structured directly from `StepContext`.
   - Failures automatically initiate compensation sequences defined by the Saga.

---

## 3. Detailed Design

### 3.1 Extended `StepContext` Definition
In `src/kntgraph/concordos/base.py`:

```python
@dataclass(frozen=True, slots=True)
class StepContext:
    """Unified runtime carrier for Specifications, FSMs, Sagas, and Agents."""

    step_results: Mapping[str, JsonValue]
    step_states: Mapping[str, str]
    domain: DomainComponent | None
    continuity: ContinuityComponent | None
    profile: ProfileComponent | None
    agent_id: str
    now: datetime
    session: SessionComponent | None = None
    trigger_data: Mapping[str, JsonValue] | None = None
    cross_agent_resolver: Callable[[str], AgentView | None] | None = None

    def get_preference(self, key: str, default: str = "") -> str:
        """Convenience accessor for Profile preferences."""
        if self.profile and key in self.profile.preferences:
            return self.profile.preferences[key]
        return default

    def get_fact(self, key: str, default: JsonValue = None) -> JsonValue:
        """Convenience accessor for DomainComponent facts."""
        if self.domain and hasattr(self.domain, "data"):
            return self.domain.data.get(key, default)
        return default
```

### 3.2 Selective Hydration
To prevent unnecessary memory allocation, the dispatcher inspects `agent_definition.memory_tiers`:
- If an agent does not declare `ContinuityComponent`, the continuity hydration projection is bypassed for that agent.
- Systems constructing `StepContext` pass only the active components declared in the agent archetype.

### 3.3 Prompt Assembly from `StepContext`
When an LLM worker is invoked within a Saga step or Intent system, a pure helper formats `StepContext` into standardized markdown blocks:

```python
def format_prompt_memory(ctx: StepContext) -> str:
    """Pure serialization of StepContext into structured LLM context sections."""
    sections = []
    if ctx.profile:
        sections.append(f"### Identity & Policy\nTier: {ctx.profile.tier}")
    if ctx.domain and hasattr(ctx.domain, "data"):
        sections.append(f"### Current Domain State\n{ctx.domain.data}")
    if ctx.continuity and ctx.continuity.last_categories:
        sections.append(f"### Recent Activity\nCategories: {list(ctx.continuity.last_categories.keys())}")
    if ctx.session and ctx.session.messages:
        recent_turns = ctx.session.messages[-5:]
        sections.append(f"### Active Dialogue\n{recent_turns}")
    return "\n\n".join(sections)
```

---

## 4. Consequences

### Positive
- **Unified Mental Model:** Developers define an agent through a single coherent bundle (`AgentDefinition`) rather than orchestrating 5+ low-level concepts manually.
- **Zero-Token Guardrails:** Unifies DDD Specifications with FSM and Sagas, preventing prompt injection or LLM hallucinations from causing illegal state transitions.
- **Transparent Memory Access:** Specifications and Saga steps inspect `session`, `continuity`, `domain`, and `profile` through one immutable `StepContext`.
- **Fault-Tolerant & Crash-Proof:** Built 100% on pure folds and event sourcing; agent state reconstructs cleanly after any process crash or restart.
- **Selective Overhead:** Agents declare required memory tiers, avoiding unwanted Redis hydration or memory bloat.

### Negative & Mitigations
- **`SessionComponent` Payload Size:** Long chat histories could bloat `StepContext`.
  *Mitigation:* `SessionComponent.messages` remains a bounded tuple of recent turns; older turns are archived to the immutable `EventLog` or summarized.
- **Backwards Compatibility:** Existing `StepContext` instantiations in FSM and Saga systems default `session=None`, preserving full compatibility with prior tests and implementations.
