<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-042: Sequence Diagram and 3-Layer Enforcement

This document supplements
[ADR-042](../ADRs/ADR-042-Agents-Memory-Model-usage.md) with
two artefacts the ADR is missing:

1. **A sequence diagram** that pins the **timing** of the
   hydration → World.fold → System → WorkerManager → Tool
   cycle. The "1 dispatcher tick" latency between a System
   request and the corresponding Tool result is part of
   the contract (ADR-036 prohibits synchronous I/O in
   Systems).
2. **A 3-layer enforcement** strategy that prevents Tool →
   Tool calling, with a clear escalation path when each
   layer is breached. ADR-042 §2.5 currently relies on
   convention only.

The diagram is drawn in Mermaid (renders in GitHub,
GitLab, and most Markdown viewers). The text below the
diagram explains each step and links the timing to the
testability and auditability claims in §3 of the ADR.

---

## 1. The happy path: hydration, fold, resolve, execute

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
    participant Tool as Tool (FalkorDB / HTTP / etc.)

    Note over Caller,Tool: T0 — Request arrives

    Caller->>API: POST /agents/{id}/intents
    API->>EL: append(intent.requested)
    EL-->>API: event_id

    Note over API: T0+ε — API returns 202; the agent<br/>does NOT block on Tool execution.

    Note over Log: T1 — Next dispatcher tick<br/>(ADR-036: < 1s typical)

    Log->>EL: read all events for agent_id
    EL-->>Log: events[]
    Log->>Log: pure fold (sync, no I/O)

    Note over Log,View: T1.5 — Hydration<br/>(memory tier → ECS Components)

    Log->>View: assemble(AgentView(<br/>  RoleComponent,<br/>  ProfileComponent,<br/>  ContinuityComponent,<br/>  SessionComponent,<br/>  IntentComponent<br/>))
    Note right of View: All memory reads here are<br/>Redis (low-latency). No FalkorDB<br/>access during hydration.

    Log->>Sys: world.call(sys_1, ...)

    Note over Sys,View: T1.6 — System runs pure<br/>(no I/O — ADR-036, ADR-039)

    Sys->>View: read ProfileComponent
    View-->>Sys: profile
    Sys->>View: read ContinuityComponent
    View-->>Sys: continuity
    Sys->>Sys: hydrate(intent, profile, continuity)
    Note right of Sys: Context Binding happens HERE.<br/>Cf. ADR-042 §2.3.

    alt Context bound (synchronous, same tick)
        Sys->>EL: append(tool.x.requested<br/>+ hydrated params)
        Note over Sys,EL: System emits without waiting<br/>for Tools. Continues processing.
    else Tool knowledge needed (FalkorDB)
        Sys->>EL: append(tool.knowledge_retriever.requested)
        Note over Sys,EL: FalkorDB read is async.<br/>Result lands in T2, not T1.
    end

    Note over EL,Tool: T2 — Next tick (one or more later)

    EL->>WM: dispatch(tool.x.requested)
    WM->>Tool: invoke(**params)
    Tool-->>WM: Result[ToolOutput, Error]
    WM->>EL: append(tool.x.completed)
    WM-->>EL: event_id

    Note over API,EL: T2+ε — Same response path for<br/>both paths above; only the tool's<br/>execution timing differs.
```

### Reading the diagram

- **T0** is the inbound HTTP request. The `IntentRouter`
  appends `intent.requested` to the EventLog and returns
  `202`. The caller does not block on the tool.
- **T1** is the next dispatcher tick. The interval is
  bounded by the dispatcher loop (ADR-035); in the
  current implementation it is 1 second.
- **T1.5** is hydration. The `World.fold` reads the Redis
  cache for Session/Profile/Continuity and projects them
  into the `AgentView` as `Component` instances. **No
  FalkorDB access happens here** — that is the rule the
  ADR enforces.
- **T1.6** is the `System.__call__`. It runs synchronously
  in RAM. The Context Binding (§2.3) happens here. Tools
  that need a FalkorDB read are emitted as
  `tool.knowledge_retriever.requested` and deferred.
- **T2** is the next tick (or later). The WorkerManager
  picks up the pending `requested` event, invokes the
  Tool, and appends `tool.x.completed` to the EventLog.

### Why this matters

The "1 dispatcher tick" between T1 and T2 is the cost
of the architectural purity. It is **not** a regression
to optimise away. ADR-036 makes the same point: any
attempt to make `System.__call__` synchronously call a
Tool breaks the non-blocking guarantee.

For callers that need the result inside the same logical
operation, the recommended pattern is to emit
`tool.knowledge_retriever.requested` from the System and
have a downstream System (in the next tick) consume the
`tool.knowledge_retriever.completed` event. The pattern
is identical to the one in `examples/04_reactive_system_with_llm.py`.

---

## 2. The 3-layer enforcement strategy

ADR-042 §2.5 prohibits Tool → Tool calling. Convention
alone is not enough; here is the layered defence.

### Layer 1 — CLI scaffolding (design-time)

The `knt new tool <name>` template (ADR-042 §2.4) emits
code that **cannot** import a sibling Tool. The generated
`invoke` method has the shape:

```python
def invoke(self, **kwargs) -> Result[ToolOutput, ToolError]:
    # Generated by Jinja2 from kntgraph[cli] template.
    # Notice: no imports of other Tool classes.
    ...
```

The template's import block is a fixed list (`falkordb`,
`redis`, `pydantic`, `structlog`). Tools that need other
Tools' output must emit a `tool.b.requested` event and
read the result from the `tool.b.completed` event in the
next System call.

A `ruff` rule (introduced in `pyproject.toml::tool.ruff.lint`)
flags any new import of `kntgraph.agents.tools.*` or
`kntgraph.tools.*` inside a Tool's `invoke` method. This
catches the dev-time "I just want to call this other Tool"
slip.

### Layer 2 — Runtime guard (ToolInvoker)

`ToolInvoker` (in `agents/tools/invoker/_invoker.py`)
already wraps every Tool invocation. Add a check before
dispatch:

```python
# pseudocode (in kntgraph/agents/tools/invoker/_invoker.py)
class ToolChainingForbiddenError(RuntimeError):
    """A Tool attempted to invoke another Tool directly.

    See ADR-042 §2.5. Tools must emit a `tool.b.requested`
    event instead, and the next System tick reads the
    matching `tool.b.completed` event from the EventLog.
    """

async def _safe_dispatch(self, tool: Tool, **kwargs):
    # The Tool is currently inside its own async frame;
    # the EventLog does NOT yet contain a "completed"
    # for THIS tool. So if a Tool emits a new
    # "tool.b.requested" while running, the
    # "tool.b.completed" cannot land before this
    # "tool.a.completed". A synchronous call would be
    # the only way to chain — and that is the
    # forbidden pattern.
    if self._in_dispatch.is_set():
        raise ToolChainingForbiddenError(
            f"Tool {tool.name!r} attempted synchronous "
            f"Tool-to-Tool chaining. Emit a "
            f"tool.b.requested event instead."
        )
    self._in_dispatch.set()
    try:
        return await tool.invoke(**kwargs)
    finally:
        self._in_dispatch.clear()
```

The `_in_dispatch` flag is a per-call `ContextVar` (or
`asyncio.Event`) that the ToolInvoker toggles around the
Tool's `invoke`. A Tool that synchronously awaits another
Tool will hit the guard and raise immediately, with a
clear ADR-042 reference.

### Layer 3 — Audit (EventLog query)

The third layer is **observability**: a script in
`scripts/audit_tool_chains.py` (new, to be added) scans
the EventLog for the pattern:

```
intent.requested
  → tool.a.requested → tool.a.completed
                            → tool.b.requested (within 1 tick)
                                    → tool.b.completed
```

where `tool.b.requested` lands **between** `tool.a.requested`
and `tool.a.completed`. That is the **forbidden pattern**:
a Tool emitted a new request before its own completion.
The script flags any occurrence and links to the offending
EventLog entries.

In the legitimate pattern, the second `tool.b.requested`
comes from a System, not from inside the `tool.a`
completion. The script distinguishes by the
`actor=system:<role>` field on the event (which
`IntentResolutionSystem` already stamps).

---

## 3. Migration inventory (companion to ADR-042 §4)

ADR-042 §4 says "Refactor legacy GraphRAG scripts to the
new `FalkorDBKnowledgeTool` class" without listing which
files. This is the inventory (LOC count, owner, current
state):

| File | LOC | Has direct Tool? | Owner | Migration cost |
| --- | ---: | --- | --- | --- |
| `src/kntgraph/knowledge/graphrag/retriever.py` | 87 | No (called by Knowledge Tool) | knowledge/ | Wrap as FalkorDBKnowledgeTool |
| `src/kntgraph/knowledge/falkordb/adapter.py` | 130+ | No (adapter) | knowledge/ | Reuse as `FalkorDBAdapter` in the new Tool |
| `src/kntgraph/knowledge/embedding/_ollama.py` | 60+ | No (embedding helper) | knowledge/ | Move to a dedicated `EmbeddingTool` |
| `src/kntgraph/agents/knowledge/solution_projector.py` | 300+ | Yes (calls Knowledge) | agents/ | Refactor — break Tool-to-Tool call into System emission |
| `src/kntgraph/tools/llm_transport.py` | 200+ | No (transport) | tools/ | No change |
| `src/kntgraph/agents/knowledge/solution_projector.py` | (above) | (above) | agents/ | (above) |
| `src/kntgraph/agents/memory/solution_extractor.py` | 200+ | Yes (calls Knowledge indirectly) | agents/ | Same pattern as projector |

Total: ~5 files, ~1000 LOC to refactor, ~3 days of work
(estimate, no formal sprint booked). Each refactor must:

1. Move the Knowledge access into a `FalkorDBKnowledgeTool`.
2. Replace the direct call with `tool.knowledge_retriever.requested` event emission from the System.
3. Add a unit test that asserts `ToolChainingForbiddenError` is raised if the legacy pattern is re-introduced.

---

## 4. Open questions for ADR-042 v2

These were not in the original ADR; this document surfaces
them so v2 can address them:

1. **Schema canonical source** — what is the canonical
   schema for the "Tools that need FalkorDB knowledge"?
   Should it be a new component (`KnowledgeRequest`)
   carrying the query, or just enriched intent params?
   Current `IntentComponent` is silent on this.
2. **ACL for Knowledge tier** — the current `default_acl`
   has 3 roles (admin/agent/service). Knowledge tier
   needs per-tenant ACL. Owner?
3. **Hydration failure semantics** — what happens if
   Redis is unavailable at T1.5? The fold should fail
   fast (don't dispatch a half-hydrated AgentView). The
   current code logs a WARNING and continues with an
   empty `ContinuityComponent`; v2 should make this an
   explicit `Err(HydrationFailed(...))` that the dispatcher
   can route.
4. **Test data for hydration** — a fixture for
   `AgentView` with all four memory components populated
   (Session/Profile/Continuity/Role) would unblock
   parallel test writing. Suggest adding
   `tests/unit/fixtures/agent_view_factory.py` as part
   of the migration.
5. **SLA on the dispatch loop** — ADR-035 sets the
   sharding strategy but the tick interval is a hot
   tunable. Document the current default (1s) and the
   back-pressure behaviour when the queue grows.

---

## 5. Acceptance checklist for ADR-042

When moving the ADR from `Proposed` to `Accepted`, the
following must be true:

- [ ] `ProfileComponent`, `ContinuityComponent`,
      `SessionComponent` exist in `src/kntgraph/core/components/`
      with the exact field set used by the existing
      `SessionState`/`ProfileState`/`ContinuityState` (no
      field drift).
- [ ] `FalkorDBKnowledgeTool` exists and is registered
      in the example `ToolRegistry`.
- [ ] `ToolChainingForbiddenError` is raised at runtime
      when a Tool synchronously awaits another Tool.
- [ ] `ruff` rule blocks the import of sibling Tools from
      inside a Tool's `invoke` method.
- [ ] `scripts/audit_tool_chains.py` exists and reports
      zero forbidden patterns on a fresh EventLog replay.
- [ ] The 5 files in §3 are migrated (or have a tracked
      backlog with owner + sprint).
- [ ] Tests in `tests/unit/memory/` and `tests/unit/knowledge/`
      cover the hydration + Context Binding + Knowledge
      Tool round-trip.
- [ ] At least one E2E test (per §3, mitigation table)
      validates that a `knt new system --with-memory`
      project initialises with the correct imports.

When all boxes are checked, ADR-042 is `Accepted` and the
memory tier of the framework has a single, enforced
boundary.
