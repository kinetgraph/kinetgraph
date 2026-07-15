<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-042: Memory Model Exposure in ECS Pattern (Systems vs. Tools) and CLI Support

**Status:** Proposed
**Date:** July 13, 2026
**Version:** 1.1.0
**Authors:** FMH Architecture Team
**Related to:** [ADR-004](./ADR-004-Memory-Tools-Knowledge.md), [ADR-014](./ADR-014-Continuity-Tier.md), [ADR-034](./ADR-034-ToolCall-ECS-Components.md), [ADR-036](./ADR-036-Non-Blocking-Systems.md), [ADR-039](./ADR-039-Role-rethinking-and-intentions-routing.md)

> **Event type form (canonical).** All tool events in this
> ADR follow the canonical ADR-036 form
> `tool.<name>.requested` / `tool.<name>.completed` /
> `tool.<name>.failed`, where `<name>` is the registered
> tool name (e.g. `tool.weather_api.requested`). The
> legacy bare form `tool.requested` / `tool.completed` /
> `tool.failed` (ADR-034) is recognised only for
> back-compat with old EventLogs — see the callout in
> [ADR-034 §event-type-form](./ADR-034-ToolCall-ECS-Components.md#event-type-form).
> When this ADR says "the tool emits `tool.<name>.requested`",
> it means the canonical form, not the legacy one.

---

## 1. Context

With the approval of **ADR-039**, we established that `Role` and `Intent` are purely data components (ECS Components) and that Tools exclusively manage I/O side-effects. Pure systems (WorldSystems) process state in RAM without causing network blocking (**ADR-036**).

However, the framework has multiple memory models with distinct latency and complexity characteristics (defined in ADR-004):

1. **Transient (Fast):** `Session`, `Profile`, and `Continuity` — kept in the Redis cache. **In scope of this ADR.**

The Knowledge and Business tiers (FalkorDB sub-graphs) are **explicitly out of scope** for this ADR. A separate ADR will address the GraphRAG / FalkorDB exposure pattern when the corresponding tool layer is ready. Mentioning them here would commit the framework to a contract for a tier that has not yet been designed.

If we allow *Tools* to access Redis-backed memories directly, we violate the purity of the flow. On the other hand, if *Systems* access Redis during the `__call__` cycle, we will block the Event Loop. This ADR defines the architectural pattern for the Redis tiers only, and how `knt-cli` will automate the creation of structures that comply with these rules.

---

## 2. Decision

Memory exposure will be strictly divided by the network I/O barrier. Low-latency memories become **Pure Components**, while complex-query memories become **Exclusive Tools**. The `knt-cli` will be the guardian of this convention.

### 2.1 Transient Memories as ECS Components

`Session`, `Profile`, and `Continuity` are projected in the `AgentView` as pure `dataclass` instances. Each of these three components carries its own identifier tuple:

| Component | Key parts (ADR-004 / ADR-014) | Read in (sync) | Updated in (async) |
| --- | --- | --- | --- |
| `SessionComponent` | `(session_id,)` | `System.__call__` (read-only) | `SessionManager.write_cache()` (after every `session.x` event) |
| `ProfileComponent` | `(tenant_id, user_id)` | `System.__call__` | `ProfileManager.write_cache()` (after `profile.x` event) |
| `ContinuityComponent` | `(tenant_id, user_id)` | `System.__call__` | `ContinuityManager.write_cache()` (sliding TTL, after every `continuity.x` event) |

> **State vs. history.** All three components are **read-only projections of the *current* state**, not slices of the event log. The truth of *what happened* lives in the `EventLog` (Redis Stream); the components are merely the snapshot of *what is true right now*. A System that needs history must read the stream via `EventReader`; reading the component is always a point-in-time lookup.

```python
from dataclasses import dataclass, field
from kntgraph.core.component import Component

@dataclass(frozen=True, slots=True)
class SessionComponent(Component):
    """Tier 'Per-conversation state' (ADR-004 §2.1).

    Projection of the *current* session — last ``messages`` tuple
    and the ``context`` KV. Historical messages live in the EventLog.
    """
    session_id: str
    user_id: str
    tenant_id: str
    messages: tuple[dict, ...] = ()
    context: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProfileComponent(Component):
    """Tier 'What the SME is' (ADR-004 §2.2).

    Projection of the *current* profile — the last written value
    for each field. The component is a **flat point-in-time snapshot**;
    audit of *how* ``tier`` or a ``preferences[key]`` was reached
    lives in the EventLog (``profile.tier_changed``,
    ``profile.preference_updated``, ...).
    """
    tenant_id: str
    user_id: str
    preferences: dict[str, str] = field(default_factory=dict)
    tier: str  # "vip" | "standard" | "basic"
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class ContinuityComponent(Component):
    """Tier 'What the SME was doing' (ADR-014).

    Projection of the *current* continuity — sliding-window
    aggregation over recent ``continuity.*`` events.
    """
    last_tools_used: dict[str, float] = field(default_factory=dict)
    recent_categories: dict[str, str] = field(default_factory=dict)
```

The **canonical source of truth** for the field set is the existing `SessionState` / `ProfileState` / `ContinuityState` dataclasses in `src/kntgraph/memory/`. The components are projections of those — same field names, same types. Field drift is forbidden (see §9 acceptance checklist).

**Why the name "Profile" stays.** "Profile" is ambiguous: it can mean (a) *user preferences*, (b) *event-history snapshot*, or (c) *domain-object profile* (CNPJ, tax regime, etc.). In this framework, `ProfileComponent` denotes **(a) only** — a flat KV of preferences plus a billing-driven tier. If/when a "BusinessProfile" (c) is needed, it will be a separate component with its own `ProfileManager` subclass, **not** a field on `ProfileComponent`. The `tier` field stays here because it is keyed on `(tenant_id, user_id)` (the same identity as preferences); the lifecycle divergence is documented in §2.4.

### 2.2 Context Binding (Hydration by Systems)

The responsibility to fill missing parameters based on recent history is **exclusive to Pure Systems**. The canonical pattern:

```python
# Only Systems read memory components (read-only, sync).
profile = view.components.get(ProfileComponent)
continuity = view.components.get(ContinuityComponent)

# Typed access — no string-compare on the tool name.
# The Intent carries a typed target; the System dispatches
# based on intent.target.tool (object identity), not on
# a string match.
if intent.target.tool == "invoice.issue":
    enriched_params.setdefault(
        "cfop",
        continuity.recent_categories.get("cfop") if continuity else None,
    )
```

Two rules that follow from this pattern:

1. **String-compare on the tool name is forbidden.** Use the `Intent.target.tool` object (or a typed enum in a future revision) so a typo or rename is caught at type-check time.
2. **Defaulting is preferred over overriding.** `setdefault` (not direct assignment) means the explicit caller-provided value always wins, and the System only fills in *missing* fields. This makes hydration observable: a `param not in enriched_params` check at audit time reveals which parameters the System filled from memory.

### 2.3 Support and Standardization via `knt-cli`

To prevent developers from creating impure Tools that attempt to read Redis directly, the `knt-cli` will receive new templates to abstract memory scaffolding:
1. **System Scaffolding with Memory Injection:**
The `knt new system` command will gain the `--with-memory` flag to generate a *WorldSystem* that already includes the boilerplate for reading `SessionComponent`, `ProfileComponent`, and `ContinuityComponent`.
```bash
uv run knt new system billing.InvoiceRouter --with-memory
```

2. **Extended Domain Memory Component Generation:**
If a specific domain needs extensions to the base profile, the CLI will facilitate creation with the correct annotations for folding:
```bash
uv run knt new component billing.ExtendedBillingProfile --extends kntgraph.ProfileComponent
```


### 2.4 Field Semantics and Lifecycle Boundaries

The three components share the *shape* of "current-state KV" but their fields have **different lifecycles**, and conflating them leads to bugs (e.g., a billing event writing to the same Redis Hash as a UX preference and racing the wrong writer). This section pins the lifecycle for each field on `ProfileComponent` (the only component that mixes lifecycles in a single flat dataclass).

#### ProfileComponent field lifecycle

| Field | Owner / driver | Mutation event | Cadence | Notes |
| --- | --- | --- | --- | --- |
| `tenant_id` | Identity (immutable) | — | Set on creation, never changes | Component key. |
| `user_id` | Identity (immutable) | — | Set on creation, never changes | Component key. |
| `preferences: dict[str, str]` | **UX / the user** (e.g. settings UI, `profile.preference_updated`) | `profile.preference_updated` | Rare (user-driven) | Flat KV, last-write-wins per key. Each key has its own `updated_at` (lives in the event, not the field). |
| `tier: str` | **Billing / admin** (e.g. plan change, account upgrade) | `profile.tier_changed` | Very rare (plan lifecycle) | Categorical: `vip` \| `standard` \| `basic`. The change is always stamped with the actor (`actor=admin:<id>`) on the event. |
| `created_at: float` | Identity (immutable) | — | Set on creation | First-write timestamp. |
| `updated_at: float` | **Any of the above** | Whichever event landed most recently | Bumped on every `profile.*` event | Bumper for ordering; not a per-field timestamp. |

The `tier` field is intentionally co-located with `preferences` in the **same** `ProfileComponent` because both are keyed on `(tenant_id, user_id)` and read together by the same `IntentResolutionSystem` Context-Binding step. The separation by **driver / cadence** (UX vs. billing) is enforced *by convention* on the event side: only billing-driven systems should emit `profile.tier_changed`, and only UX-driven systems should emit `profile.preference_updated`. A `ruff` lint rule (separate from §6.1) will flag a System that imports the *wrong* emitter.

#### When to split ProfileComponent

The choice to keep `tier` and `preferences` in the same component is deliberate but **reversible**. Split signals:

- A `ProfileComponent` write from a billing system and a UX system races for the same Redis Hash (current code is last-write-wins per key, but a future race on the same key between two systems is a smell).
- The two fields start being read by **different** Systems at different times (currently both are read in Context Binding).
- A new field appears that has a *third* lifecycle (e.g., `usage_quota` driven by telemetry, not the user).

If any of the above happens, the right move is to introduce a `BillingProfileComponent` (or similar) with its own `ProfileManager` subclass, and keep the existing `ProfileComponent` for the UX-driven fields. **This ADR does not preempt that split**; it only documents the current single-component design and the conditions that would justify splitting it.

### 2.5 Prohibition of Tool-to-Tool Calling

It is strictly forbidden for a Tool's code to directly invoke another Tool. If Tool `A` needs data from Tool `B`, the orchestration moves up to the Systems layer (Tool A emits `completed`, System evaluates and emits `requested` for Tool B).

---

## 3. Consequences

### Positive

* **Ergonomics and Compliance (CLI):** With the `knt-cli` update, developers won't need to memorize `Continuity` or `Profile` injection. The generated code will inherently force them to handle memory within *Systems*, keeping *Tools* pure by default.
* **Absolute Testability:** *Context Binding* is testable synchronously just by instantiating the `ContinuityComponent` in the `AgentView` mock, without instantiating any database.
* **Refined Auditability:** Parameter hydration becomes clear in the EventLog (the origin of the `CFOP` parameter is traceable to the `tool.<name>.requested` event).

### Negative

* **CLI Maintenance Overhead:** The core team will need to keep the `knt-cli` templates updated whenever there are changes to the internal structure of the `AgentView`.

### Mitigations

| Problem | Mitigation |
| --- | --- |
| CLI Maintenance Overhead | Integrate End-to-End (E2E) integration tests into the main repository pipeline to ensure that projects generated by the CLI initialize with the correct memory imports. |
| Complexity in Tool Chaining | Creation of a `WorkflowSystem` utility in the future. |

---

## 4. Migration and Implementation Strategy

1. **Core:** Create the `SessionComponent`, `ProfileComponent`, and `ContinuityComponent` datatypes in `kntgraph/core/components/`.
2. **Hydration:** Modify the agent's hydration pipeline to read the Redis cache and populate these components in the `AgentView` before `World.fold`. The three components must mirror the field set of the existing `SessionState`/`ProfileState`/`ContinuityState` dataclasses in `src/kntgraph/memory/` — no field drift.
3. **Intent Resolution:** Update the `IntentResolutionSystem` to support native `Context Binding` (read from `SessionComponent`/`ProfileComponent`/`ContinuityComponent` and `setdefault` onto the intent params) before dispatching events.
4. **Runtime guard:** Add `ToolChainingForbiddenError` and the `_in_dispatch` flag to `ToolInvoker` (see §6.2).
5. **Audit script:** Add `scripts/audit_tool_chains.py` for the EventLog-level enforcement (see §6.3).
6. **knt-cli Update (New Step):**
   * Add the `--with-memory` flag to the systems scaffolding scripts in the CLI.
   * Create Jinja2 templates for generating System scaffolding with the three memory components imported by default.
   * Release the new version of the `kntgraph[cli]` package.

**Out of scope for this migration:** the Knowledge / FalkorDB tier (deferred to ADR-043). The current `knowledge/graphrag/` and `knowledge/falkordb/` packages stay as-is until the dedicated ADR ships.

---

## 5. Migration Inventory (companion to §4)

ADR-042 §4 covers the **Redis tiers only**. The migration is **local** to the memory subpackage: no changes are required to the knowledge, falkordb, or graphrag packages. The files that change are:

| File | LOC | What changes | Owner |
| --- | ---: | --- | --- |
| `src/kntgraph/memory/session.py` | ~452 | Add `SessionComponent` projection to `SessionState`; no logic change | memory/ |
| `src/kntgraph/memory/profile.py` | ~402 | Add `ProfileComponent` projection to `ProfileState`; no logic change | memory/ |
| `src/kntgraph/memory/continuity/manager.py` | ~127 | Add `ContinuityComponent` projection to `ContinuityState`; no logic change | memory/ |
| `src/kntgraph/core/world/view.py` | ~110 | Extend `AgentView` to expose the three components | core/ |
| `src/kntgraph/agents/roles/resolution.py` | ~135 | Add Context Binding pattern (`setdefault` on intent params) | agents/ |

Total: 5 files, ~1226 LOC (touched, not rewritten), ~2 days of work. Each file change is **additive**: no public signature is removed, no field is renamed.

**Out of scope for this inventory:** the `knowledge/`, `falkordb/`, `graphrag/`, and `embedding/` packages are **not touched** by this ADR. They stay as-is and will be addressed in ADR-043 (the future graph-tier ADR).

---

## 6. Three-Layer Enforcement (companion to §2.5)

ADR-042 §2.5 prohibits Tool → Tool calling. Convention alone is insufficient; here is the layered defence.

### 6.1 Layer 1 — CLI scaffolding (design-time)

The `knt new tool <name>` template (ADR-042 §2.3) emits code that **cannot** import a sibling Tool. The generated `invoke` method has the shape:

```python
def invoke(self, **kwargs) -> Result[ToolOutput, ToolError]:
    # Generated by Jinja2 from kntgraph[cli] template.
    # Notice: no imports of other Tool classes.
    ...
```

The template's import block is a fixed list (`redis`, `pydantic`, `structlog`). Tools that need other Tools' output must emit a `tool.<name>.requested` event and read the result from the `tool.<name>.completed` event in the next System call.

A `ruff` rule (introduced in `pyproject.toml::tool.ruff.lint`) flags any new import of `kntgraph.agents.tools.*` or `kntgraph.tools.*` inside a Tool's `invoke` method. This catches the dev-time "I just want to call this other Tool" slip.

### 6.2 Layer 2 — Runtime guard (ToolInvoker)

`ToolInvoker` (in `agents/tools/invoker/_invoker.py`) already wraps every Tool invocation. Add a check before dispatch:

```python
# pseudocode (in kntgraph/agents/tools/invoker/_invoker.py)
class ToolChainingForbiddenError(RuntimeError):
    """A Tool attempted to invoke another Tool directly.

    See ADR-042 §2.5. Tools must emit a `tool.<name>.requested`
    event instead, and the next System tick reads the
    matching `tool.<name>.completed` event from the EventLog.
    """


async def _safe_dispatch(self, tool: Tool, **kwargs):
    # The Tool is currently inside its own async frame;
    # the EventLog does NOT yet contain a "completed"
    # for THIS tool (call it ``tool.<a>``). So if a Tool
    # emits a new "tool.<b>.requested" while running,
    # the "tool.<b>.completed" cannot land before this
    # "tool.<a>.completed" (the EventLog is append-only
    # and the system hasn't ticked yet). A synchronous
    # await on the b-tool would be the only way to
    # chain — and that is the forbidden pattern.
    if self._in_dispatch.is_set():
        raise ToolChainingForbiddenError(
            f"Tool {tool.name!r} attempted synchronous "
            f"Tool-to-Tool chaining. Emit a "
            f"tool.<name>.requested event instead."
        )
    self._in_dispatch.set()
    try:
        return await tool.invoke(**kwargs)
    finally:
        self._in_dispatch.clear()
```

The `_in_dispatch` flag is a per-call `ContextVar` (or `asyncio.Event`) that the ToolInvoker toggles around the Tool's `invoke`. A Tool that synchronously awaits another Tool will hit the guard and raise immediately, with a clear ADR-042 reference.

### 6.3 Layer 3 — Audit (EventLog query)

The third layer is **observability**: a script in `scripts/audit_tool_chains.py` (new, to be added) scans the EventLog for the forbidden pattern:

```
intent.requested
  → tool.<a>.requested → tool.<a>.completed
                                  → tool.<b>.requested (within 1 tick)
                                          → tool.<b>.completed
```

where the `tool.<b>.requested` lands **between** `tool.<a>.requested` and `tool.<a>.completed`. That is the **forbidden pattern**: a Tool emitted a new request before its own completion. The script flags any occurrence and links to the offending EventLog entries.

In the legitimate pattern, the second `tool.<b>.requested` comes from a System, not from inside the `tool.<a>` completion. The script distinguishes by the `actor=system:<role>` field on the event (which `IntentResolutionSystem` already stamps).

---

## 7. Sequence Diagram and Timing (companion to §2)

ADR-042 says "Systems must emit an event requesting the use of this Tool" (§2.2 of the original draft, now in §6) but does not pin the **timing** of the hydration → World.fold → System → WorkerManager → Tool cycle. The "1 dispatcher tick" latency between a System request and the corresponding Tool result is part of the contract (ADR-036 prohibits synchronous I/O in Systems). The diagram:

```mermaid
sequenceDiagram
    autonumber
    participant Caller as External Caller
    participant API as IntentRouter
    participant EL as EventLog (Redis)
    participant Log as World.fold (sync)
    participant Sys as IntentResolutionSystem
    participant View as AgentView (in RAM)
    participant WM as WorkerManager
    participant Tool as Tool (HTTP / Redis / etc.)

    Note over Caller,Tool: T0 — Request arrives

    Caller->>API: POST /agents/{id}/intents
    API->>EL: append(intent.requested)
    EL-->>API: event_id

    Note over API: T0+ε — API returns 202; the agent<br/>does NOT block on Tool execution.

    Note over Log: T1 — Next dispatcher tick<br/>(ADR-036: < 1s typical)

    Log->>EL: read all events for agent_id
    EL-->>Log: events[]
    Log->>Log: pure fold (sync, no I/O)

    Note over Log,View: T1.5 — Hydration<br/>(Redis tier → ECS Components)

    Log->>View: assemble(AgentView(<br/>  RoleComponent,<br/>  ProfileComponent,<br/>  ContinuityComponent,<br/>  SessionComponent,<br/>  IntentComponent<br/>))
    Note right of View: All memory reads here are<br/>Redis (low-latency). In scope of<br/>this ADR.

    Log->>Sys: world.call(sys_1, ...)

    Note over Sys,View: T1.6 — System runs pure<br/>(no I/O — ADR-036, ADR-039)

    Sys->>View: read ProfileComponent
    View-->>Sys: profile
    Sys->>View: read ContinuityComponent
    View-->>Sys: continuity
    Sys->>Sys: hydrate(intent, profile, continuity)
    Note right of Sys: Context Binding happens HERE.<br/>Cf. ADR-042 §2.3.

    Sys->>EL: append(tool.<name>.requested<br/>+ hydrated params)
    Note over Sys,EL: System emits without waiting<br/>for Tools. Continues processing.

    Note over EL,Tool: T2 — Next tick (one or more later)

    EL->>WM: dispatch(tool.<name>.requested)
    WM->>Tool: invoke(**params)
    Tool-->>WM: Result[ToolOutput, Error]
    WM->>EL: append(tool.<name>.completed)
    WM-->>EL: event_id

    Note over API,EL: T2+ε — Tool result lands in<br/>the EventLog. The next System<br/>tick (T3) can read it.
```

### Why this matters

The "1 dispatcher tick" between T1 and T2 is the cost of the architectural purity. It is **not** a regression to optimise away. ADR-036 makes the same point: any attempt to make `System.__call__` synchronously call a Tool breaks the non-blocking guarantee.

For callers that need the result inside the same logical operation, the recommended pattern is to emit `tool.<name>.requested` from the System, then a downstream System (in the next tick, T2 or later) consumes the `tool.<name>.completed` event. The pattern is identical to the one in `examples/04_reactive_system_with_llm.py`.

---

## 8. Open Questions (v2 backlog)

These were not in the original ADR; this section surfaces them so v2 (or the future graph-tier ADR) can address them:

1. **Hydration failure semantics** — what happens if Redis is unavailable at T1.5? The fold should fail fast (don't dispatch a half-hydrated AgentView). The current code logs a WARNING and continues with an empty `ContinuityComponent`; v2 should make this an explicit `Err(HydrationFailed(...))` that the dispatcher can route.
2. **Test data for hydration** — a fixture for `AgentView` with all three memory components populated (Session/Profile/Continuity) would unblock parallel test writing. Suggest adding `tests/unit/fixtures/agent_view_factory.py` as part of the migration.
3. **SLA on the dispatch loop** — ADR-035 sets the sharding strategy but the tick interval is a hot tunable. Document the current default (1s) and the back-pressure behaviour when the queue grows.
4. **Defaulting vs. fail-closed hydration** — when a System reads a `ProfileComponent` that is missing (e.g., a brand-new tenant with no profile yet), should Context Binding fail the dispatch (ADR-014 says profile is required for some flows) or proceed with `None` defaults? v2 should pick one and document the rule.
5. **Cache stampede** — when a popular `ProfileComponent` expires, multiple agents can hit the same Redis key simultaneously. Today the read path is single-flight within `ProfileManager.read`, but a future v2 should document the back-pressure behaviour under high concurrency.

---

## 9. Acceptance Checklist (Proposed → Accepted)

When moving the ADR from `Proposed` to `Accepted`, the following must be true:

- [ ] `SessionComponent`, `ProfileComponent`, `ContinuityComponent` exist in `src/kntgraph/core/components/` with the exact field set used by the existing `SessionState`/`ProfileState`/`ContinuityState` (no field drift).
- [ ] ADR-042 §2.4 (Field Semantics and Lifecycle Boundaries) is reflected in the docstring of each `*Component` class — i.e. the `tier` field is documented as billing-driven and `preferences` as UX-driven, and the "current-state vs. history" warning is present.
- [ ] `AgentView` exposes the three memory components by class (not by name string).
- [ ] `IntentResolutionSystem` implements Context Binding with `setdefault` (no string-compare on tool name).
- [ ] `ToolChainingForbiddenError` is raised at runtime when a Tool synchronously awaits another Tool.
- [ ] `ruff` rule blocks the import of sibling Tools from inside a Tool's `invoke` method.
- [ ] `scripts/audit_tool_chains.py` exists and reports zero forbidden patterns on a fresh EventLog replay.
- [ ] The 5 files in §5 are migrated (or have a tracked backlog with owner + sprint).
- [ ] Tests in `tests/unit/memory/` cover the hydration + Context Binding round-trip. **Note:** `tests/unit/knowledge/` is **out of scope** for this ADR; see ADR-043.
- [ ] At least one E2E test (per §3, mitigation table) validates that a `knt new system --with-memory` project initialises with the correct imports.
- [ ] The `knt new tool` CLI template does **not** include a `--type falkordb-rag` flag (deferred to ADR-043).

When all boxes are checked, ADR-042 is `Accepted` and the Redis memory tier of the framework has a single, enforced boundary.