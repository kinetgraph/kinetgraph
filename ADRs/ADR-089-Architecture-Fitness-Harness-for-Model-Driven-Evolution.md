<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-089: Architecture Fitness Harness for Model-Driven Solution Evolution

- **Status:** Proposed
- **Date:** 2026-11-04
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-001](./ADR-001-Arquitetura.md) — pure ECS, Event Sourcing, `World = fold(events)`
  - [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md) — `WorldSystem` purity
  - [ADR-019](./ADR-019-Redis-Adapter-Typing.md) — typed Redis adapters & external client encapsulation
  - [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — `ToolCallRequest` / `ToolCallCompletion`
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — `@tool_worker` + `WorkerManager`
  - [ADR-042](./ADR-042-Agents-Memory-Model-usage.md) — Memory tiers & ECS projections
  - [ADR-047](./ADR-047-Tool-Adapter-Pattern.md) — Tool Adapter Pattern (no direct third-party SDK imports)
  - [ADR-069](./ADR-069-Agent-Concordo-Foundation.md) — Concordo Foundation & Specification Pattern
  - [ADR-071](./ADR-071-BusinessFSM-Concordo.md) — BusinessFSM Concordo (C-01)
  - [ADR-072](./ADR-072-WorkflowSaga-Concordo.md) — WorkflowSaga Concordo (C-02)
  - [ADR-073](./ADR-073-Concordo-Bundle-Format.md) — Concordo Bundle Format
  - [ADR-077](./ADR-077-Storage-Adapter-Exception-Translation.md) — Exception translation at storage adapter boundary
  - [ADR-079](./ADR-079-Frozen-Dataclass-In-Place-Of-TypedDict.md) — Frozen Dataclass in place of `TypedDict`
  - [ADR-080](./ADR-080-Drop-Type-Any-In-ECS-Bag.md) — Drop Type `Any` in ECS Bag
  - [ADR-088](./ADR-088-Declarative-Agent-Definition-over-Concordos.md) — Declarative Agent Definition over Concordos

---

## 1. Context and Problem Statement

### 1.1 The Shift from Human to Agentic Engineering
Kinetgraph has codified its architectural standards into markdown-based **Skills** (`.agents/skills/`), covering type discipline, CI gates, file layouts, and ECS conventions. While these skills provide comprehensive guidance for human developers and on-demand context injection for LLMs, **skills are inherently passive and advisory**:
1. **Context Window Drift & Attention Degradation:** In multi-turn refactorings, autonomous coding agents suffer from attention drift, dropping subtle invariants (e.g., purity of the ECS fold, immutability of `StepContext`, or `JsonValue` wire typing).
2. **Heuristic Shortcuts:** Models prioritize making local unit tests pass at any cost. When faced with architectural constraints, they often take path-of-least-resistance shortcuts (e.g., importing storage adapters directly into framework code, bypassing guards with imperative `if/else`, or using `dict[str, Any]`).
3. **High-Level Drift in FSM and Sagas:**
   - **BusinessFSM (ADR-071):** Models frequently introduce I/O side effects inside reactive state transitions, duplicate state outside `DomainComponent`, or bypass the Specification pattern.
   - **WorkflowSagas (ADR-072):** Models often attempt imperative loops (`while` or `await tool()`) inside orchestrators, violate asynchronous event-driven tool dispatch, or break symmetric LIFO compensation sequences.

### 1.2 The Goal: From Skills to Harness
To enable AI models to evolve complex features autonomously without architectural erosion (*desvios de design*), we must promote advisory skills into an **active, deterministic Architecture Fitness Harness**. The harness acts as a closed-loop execution boundary that provides instant, machine-actionable feedback whenever architectural invariants are violated.

---

## 2. Decision

We establish the **Kinetgraph Architecture Fitness Harness**, composed of three automated enforcement pillars:

```
[ Model Iteration Loop ]
           │
           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    ARCHITECTURE FITNESS HARNESS                         │
│                                                                         │
│  [Pillar 1: Static Boundary & AST Linter]                               │
│  - Framework vs Vertical import enforcement (ADR-042, Skill §4)         │
│  - Type discipline: Ban on Any, TypedDict, dict[str, Any] (ADR-079/080) │
│                                                                         │
│  [Pillar 2: High-Level Concordo Contract Suites]                        │
│  - FSM Invariant Suite: Graph reachability, purity, zero-token guards   │
│  - Saga Chaos Suite: Step sequencing, crash recovery, LIFO compensation │
│                                                                         │
│  [Pillar 3: The Ratchet Gate & Diagnostic Error Surface]                │
│  - Machine-actionable error messages with rule, file:line, and ADR fix  │
│  - Strict non-regression on coverage, complexity, and typing            │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
                 ┌───────────────────┴───────────────────┐
                 │                                       │
            Passed Gate                             Failed Check
                 │                                       │
                 ▼                                       ▼
      Commit & Integration Run               Instant Model Self-Correction
```

---

## 3. Detailed Design

### 3.1 Pillar 1: Static Architectural & Boundary Linter (`design_gate.py`)
We codify the greppable rules defined in `kntgraph-type-discipline` §7 into an automated AST and module graph checker.

#### 3.1.1 Boundary Rules
- **Framework Import Boundary:** No module in `src/kntgraph/{core,tools,infra,stream,security,runner,resilience,concordos}` may import from vertical packages `src/kntgraph/{agents,api,cli,knowledge,events,memory}`.
  - Specifically protects against the `memory/` confusion (ADR-042 / Skill §4): Framework code must import `kntgraph.core.components.memory.SessionComponent`, never `kntgraph.memory.session.SessionState`.
- **Wire Type Discipline:** Wire payload fields (`Event.data`, Redis serialization boundaries) must strictly type as `Mapping[str, JsonValue]` or `dict[str, JsonValue]`. `dict[str, Any]` is banned.
- **Composition Shape Discipline:** Banning `TypedDict` in framework code (ADR-079); requiring `frozen=True, slots=True` dataclasses for shape-known return types.

#### 3.1.2 Adapter Encapsulation & Exception Translation Guardrail (ADR-047, ADR-077)
The harness enforces that **external implementation details never leak past the adapter**:
- **No Third-Party SDK Imports Outside Adapters (ADR-047):** Concrete third-party libraries (e.g., `redis`, `falkordb`, `httpx`, `boto3`, `stripe`, `litellm`) may **only** be imported inside `src/kntgraph/infra/<layer>/` or dedicated adapter modules. ToolWorkers and Domain systems must interact strictly through injected Protocols.
- **Strict Return Types (`Result[T, E]` / `ToolResult`):** All public mutating adapter methods must declare explicit return types wrapping errors into `Result[T, LayerError]` or `ToolResult` (ADR-079).
- **Total Exception Translation at the Boundary (ADR-077):**
  - Adapters must map, catch, and translate all third-party and OS exceptions (`RedisError`, `ConnectionError`, `TimeoutError`, `HTTPError`, `OSError`) into typed domain errors (`RedisUnavailableError`, `MemoryDecodeError`, `ToolError`, etc.).
  - Consumers must trust the Protocol contract: `try/except Exception` (BLE001) in call sites is an architectural violation flagged by the linter.
  - The only exceptions permitted to propagate past an adapter are system-level runtime control signals: `asyncio.CancelledError`, `KeyboardInterrupt`, and `SystemExit`.

### 3.2 Pillar 2: High-Level Concordo Verification Harness (FSM & Sagas)

The harness provides base verification suites that models must inherit or run when creating or modifying state machines and sagas.

#### 3.2.1 BusinessFSM Invariant Suite (`FSMContractTestSuite`)
Guarantees adherence to ADR-069 and ADR-071:
1. **Graph Soundness & Reachability:**
   - Evaluates the transition graph of `FSMConfig`.
   - Proves every state is reachable from the initial state.
   - Proves all non-terminal states have at least one outgoing transition.
   - Identifies accidental deadlock states.
2. **Strict System Purity:**
   - Evaluates `FSMSystem.process_events(world, events)`.
   - Invariant: Re-evaluating the system with identical `(World, Events)` produces bit-for-bit identical emitted events with zero I/O operations and zero external state changes.
3. **Zero-Token Governance:**
   - Injects invalid events or failing guards; verifies that `fsm.transition_rejected` is emitted cleanly without invoking any LLM workers or external transports.
4. **Specification Guard Contract:**
   - Validates via AST/reflection that transition guards implement the `Specification` protocol and evaluate exclusively against `StepContext`.

#### 3.2.2 WorkflowSaga Chaos Suite (`SagaContractTestSuite`)
Guarantees adherence to ADR-034, ADR-045, and ADR-072:
1. **Asynchronous Event-Driven Dispatch Invariant:**
   - Verifies that Saga steps dispatch tools solely by emitting `tool.<name>.requested` (never by invoking `WorkerManager` or tool functions synchronously).
2. **Automated Chaos & LIFO Compensation Injection:**
   - The harness automatically runs a parameterized permutation test for every declared Saga:
     - Happy path execution: Steps $1 \dots N$ complete in order.
     - Injected failure at step $K \in \{1 \dots N\}$ via `ToolCallCompletion(status="err")` or TTL timeout `tool.<name>.timed_out`.
     - Invariant: When step $K$ fails, the Saga must initiate compensation for completed steps $K-1 \dots 1$ in **exact reverse order (LIFO)**.
     - Verifies that intermediate compensation states (`compensation_started`, `compensated`) are emitted and recorded.
3. **Crash Recovery & Fold Consistency:**
   - Simulates a process termination at every step of the Saga.
   - Reconstructs state via `AgentView = fold(events)` and verifies that `SagaProgressComponent` enables the Saga to resume or compensate without state corruption or duplicate tool dispatches.

#### 3.2.3 Adapter Exception & Boundary Contract Suite (`AdapterContractTestSuite`)
Guarantees adherence to ADR-047 and ADR-077:
1. **Third-Party Exception Containment:**
   - Injects socket disconnects, DNS timeouts, malformed JSON responses, and OS-level network errors into the underlying mock/client.
   - Invariant: Adapter methods must intercept all third-party exceptions and return `Err(<LayerError>)`. They must **never** let raw library exceptions escape to the caller.
2. **System Control Signal Propagation:**
   - Injects `asyncio.CancelledError`, `KeyboardInterrupt`, and `SystemExit`.
   - Invariant: The adapter must **not** catch or swallow these signals; they must propagate cleanly to allow cancellation and process shutdown.
3. **Protocol Conformance & Return Type Narrowing:**
   - Verifies that return values strictly adhere to `Result[T, LayerError]` or `ToolResult`, enabling caller pattern-matching without runtime `try/except`.

### 3.3 Pillar 3: Machine-Actionable Diagnostic Error Surface
When an invariant fails, traditional test runners often output cryptic assertion traces. The harness emits structured, machine-actionable diagnostics formatted to guide the model's immediate self-repair:

```
[KNT-DESIGN-ERROR] VIOLATION: FrameworkVerticalSeam
File: src/kntgraph/concordos/agent.py:12
Rule: ADR-042 / kntgraph-type-discipline §4
Detail: Framework module cannot import from vertical package 'kntgraph.memory.session'.
Remedy: Import the ECS projection from 'kntgraph.core.components.memory.SessionComponent' instead.
```

```
[KNT-SAGA-ERROR] VIOLATION: BrokenCompensationLIFO
Saga: OrderProcessingSaga
Step Failed: 'reserve_inventory' (Step 2)
Expected: Emitting compensation for 'charge_payment' (Step 1).
Actual: No compensation tool declared or step skipped.
Rule: ADR-072 §2 (All stateful saga steps must provide rollback parity).
```

---

## 4. Implementation Plan

1. **Step 1: Architectural Gate Script (`scripts/design_gate.py`):**
   - Implement the AST visitor for import boundary checks and `JsonValue`/`dataclass` enforcement.
   - Integrate `design_gate` as step 0 in `scripts/ci.py` and pre-commit hooks.
2. **Step 2: Concordo Contract Test Fixtures (`src/kntgraph/testing/concordo_harness.py`):**
   - Provide `FSMContractTestSuite` and `SagaContractTestSuite` in the testing support package.
   - Validate existing foundation FSMs and Sagas against the harness.
3. **Step 3: Agent Archetype Scaffold:**
   - Update `kntgraph` CLI scaffolds to automatically generate compliant FSM/Saga declarations accompanied by pre-wired harness contract tests.

---

## 5. Consequences

### Positive
- **Guaranteed Conceptual Integrity:** Models can autonomously refactor, add agents, and build sagas without drifting from the pure ECS, event-sourcing, and type-discipline foundations.
- **Instant Local Feedback:** Architectural errors are flagged in milliseconds via AST checks and contract tests before running heavier integration suites.
- **Fail-Safe Sagas:** Guarantees that every multi-step tool workflow has verified LIFO rollback behavior and crash recovery prior to production deployment.
- **Synergy with ADR-088:** Ensures that declarative `AgentDefinition` instances strictly conform to the dual-cycle execution model (zero-token FSM governance vs deliberative Saga execution). The agent-level contract verifier is a follow-up, not part of this ADR (ADR-088 §5).

### Negative & Mitigations
- **Maintenance of AST Linters:** Custom AST linters can become brittle as Python syntax evolves.
  *Mitigation:* Keep AST rules minimal, focused on import statements, class decorators (`@dataclass(frozen=True)`), and type annotations.
- **Test Suite Run Duration:** Simulating failure permutations for large Sagas could increase test time.
  *Mitigation:* Keep the Chaos runner in-memory using mocked `AgentView` projections, executing in under 50ms per saga.
