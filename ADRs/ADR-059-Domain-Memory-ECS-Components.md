<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-059: Domain Memory via ECS Components

- **Status:** Accepted
- **Date:** 2026-08-21
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-010](./ADR-010-Memory-Business-Tier.md) — Memory Business Tier
  - [ADR-014](./ADR-014-Continuity-Tier.md) — Continuity Tier
  - [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md) — World and incremental fold

## 1. Context and Problem

Kinetgraph natively provides three memory tiers backed by Redis to persist state that escapes a single tick's lifecycle:
- **Profile:** Static tenant preferences and settings.
- **Continuity:** Short-term, sliding-window memory focusing on recent usage and hashed PII.
- **Session:** Ephemeral state for interactive interactions (e.g., chat sessions).

As applications built on Kinetgraph evolve (such as complex Backoffice validation pipelines), we observed a recurring anti-pattern: developers attempting to use the **Continuity** tier to store immutable, long-lasting domain facts (e.g., "Company Size is MEI", or the final result of an official classification). 

**Why is this problematic?**
1. **Sliding Window Amnesia:** `Continuity` is strictly designed for behavioral, volatile data subject to a sliding window (TTL / token limits). Essential domain facts pushed here can be evicted ("erased") if enough subsequent interactions occur, causing the agent to "forget" critical business state.
2. **Loss of Purity:** Using `ContinuityManager` (or forcing data into `Profile`) requires impure I/O during business logic execution, violating the functional core principle (`f(State, Event) -> Event`).
3. **Event Sourcing Violation:** It breaks the principle that the `EventLog` is the single source of truth for structural business facts.

## 2. Decision

To represent **durable, bounded-context domain facts** (like validation results or aggregate state), we introduce **Domain Memory** implemented purely through **ECS Components**.

**Established Rules:**

1. **State as a Log Projection:** When a System discovers a structural domain fact, it must emit a **Domain Event** (e.g., `company_size.loaded`). It must **not** save this fact to `Continuity`.
2. **Materialization (Fold & Auto-Registry):** The system's *reducer* (fold) materializes this event into a pure **ECS Component** (`@dataclass(frozen=True, slots=True)`). To eliminate boilerplate, Kinetgraph uses an inverted-control registry. Developers inherit from `DomainComponent` and apply the `@domain_component(event_type)` decorator. The framework automatically intercepts matching events during the default `World.fold` and auto-hydrates the component into the AgentView.
3. **Type-Safe Component Access:** Unlike previous text-based keys, components in the `World` will be indexed and accessed via their Type/Class to enforce Type Discipline. 
   - `world.get_agent(id).get_component(CompanySizeProjection)`
4. **Zero Extra Redis I/O:** The framework natively reconstructs this state by running the `fold` against the `EventLog` (or reading a World Snapshot). The component travels inside the `World` object, providing immediate, synchronous, and pure access to all downstream Systems across ticks.
5. **Componentized Aggregates:** We do not replicate monolithic Backend aggregates. We slice them into Satellite components. If the backend changes, it publishes an event (via Transactional Outbox), which Kinetgraph consumes to update the specific ECS Component.

### The Memory Decision Tree

When designing feature state, developers must use the following flow:

```text
Is the data a direct structural fact or outcome of a business process?
 ├── YES: It is a Domain Fact. Use **Pure ECS Components** (Domain Event -> Component).
 └── NO: Is it interaction/behavioral metadata?
      ├── Changes globally without user interaction (Billing/SLA)? → Profile
      ├── Is it the "latest thing" the user did/said (Recency)?    → Continuity
      └── Is it a draft of an unfinished conversation?             → Session
```

## 3. Consequences

### Positive Impacts
- **True ECS Permanence:** Solves the "Continuity Sliding Window" data erasure problem. Once a domain component is attached to the entity via the event log, it remains permanently available to all future ticks.
- **Zero Boilerplate:** The `@domain_component` decorator provides automatic hydration inside `World.fold`, eliminating the need for application developers to write manual event loops or call private internal APIs (`_apply_event`).
- **Pure Systems:** Systems remain 100% pure. They read from the `World` and yield events.
- **Testing:** Simplifies TDD. Developers only need to mock the past event list to set up any complex state, rather than mocking Redis caches.
- **Type Safety:** Type-based access for components aligns with Kinetgraph's Type Discipline.

### Trade-offs / Watch-outs
- **Fold Performance (Rehydration):** For entities with thousands of events, the pure `fold` operation could introduce latency. Kinetgraph's `IncrementalWorldStore` (Snapshots) must be properly tuned to mitigate this without leaking the complexity to the developer's abstraction.
- **Event Schema Evolution:** Since the log is immutable, changes to the payload of domain events will require developers to implement *Upcasters* or tolerate optional fields in the component `@dataclass`.
- **Eventual Consistency:** When syncing state from external monolithic backends via Transactional Outbox, Kinetgraph will operate on eventually consistent Domain Components. Systems must be designed to tolerate this brief latency.
