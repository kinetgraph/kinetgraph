<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Kinetgraph Memory Model

The Kinetgraph framework implements a multi-tiered memory model to handle agent state, conversational context, and long-term knowledge. The short-term memory architecture is heavily inspired by the **Redis Agent Builder (RAB)** cookbook, adapted to fit a purely event-sourced paradigm.

## Core Architectural Principles (Event-Sourced RAB)

The Redis Agent Builder defines "short-term memory" as a per-conversation or per-user store featuring standard `read`, `write`, and `clear` operations. Kinetgraph adopts this pattern with a robust event-sourced twist:

1. **EventLog as Source of Truth**: The definitive state of any memory tier is always the EventLog (stored in Redis Streams).
2. **Rebuildable TTL Caches**: The actual short-term memory representations (stored in Redis Hashes or JSON) are treated strictly as caches. A cold, missing, or evicted cache is never a critical failure because the state can always be rebuilt from the EventLog.
3. **Read-Through Pattern**: When an agent requests state via `read()`, the framework first attempts a cache hit. On a miss, it gracefully falls back to folding the EventLog from scratch and repopulates the cache.
4. **Write-Through & Refresh**: 
   - `write_cache()` directly updates the cache and is primarily used by the `Projector`.
   - `refresh_cache()` rebuilds the cache from the EventLog and is heavily utilized by the `CacheWarmer` adapter.

---

## Memory Tiers

The framework categorizes memory into three distinct, Redis-backed short-term tiers (defined in ADR-014), managed by specialized classes inheriting from `BaseShortTermMemory`, alongside a long-term knowledge graph tier.

### 1. Session Tier (`SessionManager`)
- **Purpose**: Short-term conversational memory. Captures the ephemeral state of the current interaction.
- **Cache Strategy**: Redis JSON string payload.
- **Lifecycle**: Highly ephemeral with a TTL (Time-To-Live) of ≤ 24 hours.
- **Identity**: Bound to a single `(session_id,)`.
- **Selection Criteria**: Used for state that is only relevant during the active conversation window.

### 2. Profile Tier (`ProfileManager`)
- **Purpose**: Long-term static preferences of the user/tenant (e.g., tax regime, SLA tier, default notification emails).
- **Cache Strategy**: Redis Hash mapping.
- **Lifecycle**: Persistent (no TTL). Changes occur mostly via backend administrative events or explicit preference updates, not through frequent user interaction.
- **Identity**: Bound to a two-part identity `(tenant_id, user_id)`.
- **Materialisation (implicit, ADR-067)**: The state materialises from ANY `profile.*` event — an explicit `profile.created` is optional. A profile without `created` carries `created_at == 0.0` ("never formally created"); the identity is recovered from the agent_id convention when the events do not carry it.
- **Selection Criteria**: Used for state that changes *without* active user interaction within a session.

### 3. Continuity Tier (`ContinuityManager`)
- **Purpose**: Recent "state-of-use" context. Remembers the last context the user interacted with (e.g., last used tool, last mentioned client CNPJ, last selected CFOP).
- **Cache Strategy**: Redis Hash mapping.
- **Lifecycle**: Ephemeral but prolonged via a **sliding TTL** (the TTL resets on every write).
- **Identity**: Bound to `(tenant_id, user_id)`.
- **Privacy (LGPD)**: Because this tier often captures Third-Party Personally Identifiable Information (PII) from active tool usage, all PII is stored strictly as hashes and is marked as `cleared` in accordance with LGPD data policies.
- **Materialisation (implicit, ADR-067)**: The state materialises from ANY `continuity.*` event — an explicit `continuity.created` is optional (same contract as the profile tier).
- **Selection Criteria**: Used for state that changes *in response to a tool call*.

### 4. Knowledge Tier (Long-Term Semantic/Episodic Memory)
*Note: While the first three tiers are short-term and user-specific, Kinetgraph also features a vertical knowledge tier.*
- **Purpose**: Cross-agent, long-term memory for solutions, tool call outcomes, and semantic knowledge.
- **Storage**: Projected into **FalkorDB** (a graph database).
- **Mechanism**: Extracts patterns and entities from the EventLog (via `SolutionExtractorSystem`) and promotes them into a queryable graph (via `SolutionPromoterSystem`) for GraphRAG retrieval.

### 5. Domain Tier (ECS Components)
*Introduced in ADR-059*
- **Purpose**: Permanent, structural domain facts derived from the event stream (e.g., a company's legal size, a validation outcome, an aggregate's snapshot).
- **Storage Strategy**: Pure in-memory ECS (Entity-Component-System). The framework natively reconstructs these components directly from the EventLog during the `World.fold()` operation.
- **Usage (Auto-Hydration)**: Kinetgraph utilizes an inverted-control registry. By simply inheriting from `DomainComponent` and applying the `@domain_component("your.event.type")` decorator to a `@dataclass`, the component is automatically intercepted and hydrated by the framework. No manual loops or extractions are required.
- **Update Semantics (Last-Event-Wins)**: Because the component is keyed by its class identity in the `AgentView`, any subsequent event with the same `event_type` will automatically overwrite the previous state of that component. This guarantees that the component always reflects the most recent fact without requiring manual merging logic. (Enforced by the ownership rule of ADR-067: an event writes only the component keys it produced; overlay-owned slots are never clobbered.)
- **Lifecycle**: Permanent (bound to the immutable `EventLog`). These components do not suffer from "sliding window amnesia" and never expire.
- **Identity**: Bound to `agent_id` (the aggregate root instance).
- **Selection Criteria**: Used for data that is the direct, structural result of a business process, which systems must rely on indefinitely without arbitrary TTLs.

---

## Shared Orchestration (`BaseShortTermMemory`)

To adhere to DRY (Don't Repeat Yourself) principles, all three short-term managers inherit from `BaseShortTermMemory`. 

**The Base Class owns the orchestration:**
- Constructor wiring connecting the `EventLog`, `ShortMemoryStorage` protocol, and TTL config.
- The `read()` logic (cache → fold → refresh).
- The `refresh_cache()` rebuild logic.

**The Subclasses own the shape:**
- `cache_key()`: How the logical identity is transformed into a Redis key.
- `_read_cache()` & `_write_cache()`: Handling the specifics of JSON vs. Hash storage.
- `_fold_from_log()`: The pure logic that reduces a stream of raw events into a typed state object (e.g., `SessionState`, `ContinuityState`).
