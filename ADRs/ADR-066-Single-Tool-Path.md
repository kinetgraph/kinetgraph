<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-066: Single Tool Path — WorkerManager canonical, `ToolRegistry`/`ToolInvoker` removed

- **Status:** Proposed
- **Date:** 2026-08-26
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-017](./ADR-017-Identity-Authorization.md) — `ToolACL` + `default_acl` (currently dead code)
  - [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md) — `Tool` Protocol split; `ToolInvoker` deprecation target
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — `@tool_worker` + `WorkerManager` canonical path
  - [ADR-043](./ADR-043-LiteLLM-Worker-Migration.md) — `LiteLLMToolWorker` (the canonical LLM bridge)
  - [ADR-047](./ADR-047-Tool-Adapter-Pattern.md) — Tool-Adapter pattern + three sub-Workers
  - [ADR-061 §5](./ADR-061-litellm-integration-review.md) — three-gate ACL gap flagged for `chat_llm`
  - [DEBT §2.27](../DEBT.md) — WorkerManager ACL hook (predecessor of this ADR)

> **Scope.** This ADR proposes **one** tool execution
> path: the `@tool_worker` / `WorkerManager` model
> (ADR-036). The legacy `Tool` Protocol /
> `ToolInvoker` / `ToolRegistry` model is **removed**.
> The `ToolACL` / `default_acl` / `acl_for` surface
> migrates to `WorkerManager`. This is a structural
> change that touches every tool the framework knows
> about; it cannot land in v0.14 (which is fix-first)
> and is planned for a dedicated minor (v0.16+).

## 1. Context

The framework has two parallel tool-execution paths
that coexist today:

| Path | ADR | Where it runs | Dispatcher |
|---|---|---|---|
| `Tool` Protocol + `ToolInvoker` + `ToolRegistry` | ADR-005, ADR-025 | **In-process** (same event loop as the dispatcher) | `ToolInvoker` reads `tool.<name>.requested` events from the EventLog and calls `Tool.invoke(...)` synchronously |
| `@tool_worker` + `WorkerManager` | ADR-036, ADR-043 | **Cross-process** (`ProcessPoolExecutor`, spawn start method) | `ToolRouter.route_batch` `XADD`s the event into `knt:tools:<name>:queue`; `WorkerManager` consumes via Redis consumer group |

ADR-043 (v0.9.0) moved the LLM tool (`LiteLLMTool`
→ `LiteLLMToolWorker`) onto the Worker path. ADR-047
(v0.10.0) split the LLM adapter into three sub-Workers.
Both ADRs treated the Worker path as the **canonical**
future, but neither removed the legacy path; ADR-025's
plan to deprecate `ToolInvoker` was accelerated
(removed in v0.9.0) but `ToolRegistry` and the `Tool`
Protocol remained.

**Today, the dual-path state is inconsistent.**

  - **The line between "in-process" and "Worker" is
    arbitrary.** Tools that "look" pure (a `lookup`,
    a `validate`, a `transform`) sit in `ToolRegistry`;
    tools that "look" I/O (`chat_llm`, embeddings,
    HTTP fetch, search) sit in `WorkerManager`. But
    the boundary is fuzzy: a `lookup` that touches
    Redis is I/O; an `embedding` against a local
    model is "pure". A future refactor that moves an
    existing tool from one bucket to the other (e.g.
    swapping a local lookup for a remote API call)
    silently crosses the boundary without any code
    signal.

  - **ACL is half-implemented.** `ToolACL`,
    `default_acl()`, and `ToolRegistry.acl_for` are
    defined and have unit tests
    (`tests/unit/security/test_rbac.py:295–360`,
    `tests/unit/tools/test_registry.py:50`), but **have
    zero callers in production code**
    (`src/`). The Worker path never imported
    `ToolACL`; the `Tool` Protocol path was supposed
    to enforce ACL but the `ToolInvoker` removal in
    v0.9.0 left the enforcement orphaned. ADR-061 §5
    flag 1 (`chat_llm` is "an unauthenticated tool")
    is the visible symptom; the underlying disease is
    that the ACL hook lives on a dead path.

  - **Two registries, two test surfaces.** `ToolRegistry`
    and `WorkerManager._tools` are independent data
    structures. A user registering a tool in the
    Worker path has no way to attach ACL metadata;
    a user attaching ACL metadata has no way to
    enforce it at Worker dispatch. ADR-025 §5
    acknowledges the split as intentional (the
    Worker path was "newer, lighter"), but the
    intentional split outlived its purpose.

  - **The "two paths" decision cannot be enforced by
    tooling.** A new framework user does not get a
    clear signal: "use `@tool_worker` for everything;
    `Tool` Protocol is gone". The CLI scaffold
    (`cli/templates/dispatcher.py.jinja`,
    `cli/templates/main.py.jinja`) still imports
    `ToolRegistry` from `kntgraph.agents.tools.protocol`,
    which is itself a re-export shim of
    `kntgraph.tools.registry.ToolRegistry`. The
    scaffolds teach the dual-path as if it were
    first-class.

  - **3 examples + 3 scaffolds + 14 tests still
    reference `ToolRegistry`.** Removing the legacy
    path touches: `examples/10_http_intent_router.py`,
    `examples/20_security_authorization.py`,
    `examples/knt-cli/weather_platform/src/weather_platform/main.py`,
    the 3 Jinja templates above, and a non-trivial
    set of tests under `tests/unit/api/`,
    `tests/unit/knowledge/`,
    `tests/unit/security/`, and
    `tests/unit/tools/`. The migration is **not
    one-line**; it is a coordinated removal with
    a deprecation cycle.

## 2. Decision

The framework adopts **one** tool execution path:

  - **Canonical path:** `@tool_worker` decorator +
    `WorkerManager`. Every tool that wants to run
    inside the dispatcher must be a class decorated
    with `@tool_worker(name=...)` and registered via
    `WorkerManager.register(tool_cls)`.

  - **Removed:**
    - `kntgraph.tools.registry.ToolRegistry` (the
      legacy registry)
    - `kntgraph.tools.protocol.Tool` (the legacy
      Protocol)
    - `kntgraph.agents.tools.protocol` (the
      backward-compat shim that re-exported both)
    - `kntgraph.tools.descriptors.ToolDescriptor`
      and `list_descriptors` (used only by the
      Solution promoter; the canonical equivalent
      becomes `WorkerManager.list_workers()`)

  - **Migrated:** `ToolACL` + `default_acl()` +
    `ToolRegistry.acl_for` move to `WorkerManager`:

      - `WorkerManager.register(tool_cls, *, acl:
        ToolACL | None = None)` — the `acl` kwarg
        is new. Default is `default_acl()`.
      - `WorkerManager.acl_for(name) -> ToolACL |
        None` — replaces `ToolRegistry.acl_for`.
      - `WorkerManager._process_message` (or a
        pre-dispatch hook in `ToolRouter.route_batch`)
        calls `acl.check(principal)` before
        `run_in_executor` (per DEBT §2.27).

  - **Unchanged:** the `WorkerManager` runtime
    semantics (ProcessPoolExecutor with `spawn`,
    Redis consumer group, idempotency via
    `xpending`/`xautoclaim`, retry policy) are the
    same as ADR-036 / ADR-043.

The "Tool" concept survives as **a single, narrow
abstraction**: a `@tool_worker`-decorated class with
a `name`, `description`, `input_schema`, and
`invoke(...)` coroutine. The Protocol is replaced
by `@runtime_checkable` on the decorator's
metadata; no separate `Tool` Protocol is needed.

## 3. Why now (vs. leaving the dual-path state)

The dual-path state survives today because no single
symptom has been acute enough to force a decision.
The accumulating evidence:

  - **ADR-061 §5** (2026-08-25 audit) flagged that
    `chat_llm` is unauthenticated because the
    Worker path has no ACL hook. The recommended
    fix was narrower (add the hook to
    `WorkerManager`), but the deeper question is
    *why does the Worker path have no hook while
    the dead path has one*? The answer is historical
    accident (the ACL hook predates the Worker
    migration), and the cure is to retire the dead
    path.

  - **DEBT §2.27** (2026-08-26) recorded the
    WorkerManager ACL hook as **Open** with a
    sketch that touches 4 modules + a schema change
    on `Event` (producer_principal_id). That work
    is **most of this ADR**; doing them together
    is cheaper than doing them separately.

  - **The "arbitrary line" problem** does not have
    a code-level signal. New tools can land on
    either side, and the framework does not help
    the author choose. As more I/O-bearing tools
    land (embeddings, search, HTTP fetch), the
    number of "is this Worker or in-process?"
    questions compounds. Removing the in-process
    option collapses the question.

  - **CLI scaffolds teach the dual path.**
    `cli/templates/dispatcher.py.jinja` imports
    `ToolRegistry`; `cli/templates/main.py.jinja`
    registers tools in both `WorkerManager` and
    `ToolRegistry`. New users copy this. Removing
    the dual path collapses the scaffold surface.

  - **Tests reinforce the dual path.**
    `tests/unit/tools/test_registry.py` and
    `tests/unit/security/test_rbac.py` exercise
    `ToolRegistry.register` + `acl_for` as if the
    path were alive. They would not exist if the
    path were clearly deprecated.

## 4. Scope of the change

### 4.1 Production code

  - **Remove:**
    - `src/kntgraph/tools/registry.py` (file)
    - `src/kntgraph/tools/protocol.py` (file)
    - `src/kntgraph/tools/descriptors.py` (file)
    - `src/kntgraph/agents/tools/protocol.py` (file,
      the backward-compat shim)
    - The `ToolACL`, `default_acl`, `ToolRegistry`,
      `Tool`, `ToolDescriptor` symbols from every
      `__init__.py`

  - **Modify:**
    - `src/kntgraph/tools/manager.py`: add
      `acl_for(name)` + an `acl` kwarg on
      `register`. Implement the pre-dispatch ACL
      check (the work in DEBT §2.27 becomes part
      of this ADR).
    - `src/kntgraph/tools/router.py`: stamp
      `producer_principal_id` on `XADD` from the
      caller's `principal_ctx` (per DEBT §2.27
      step 1).
    - `src/kntgraph/core/event/event.py`: add the
      optional `producer_principal_id: str | None`
      field with `__post_init__` validation
      (signature verifies, tenant ownership
      re-derived from the agent_id's tenant).

  - **Re-export `ToolACL` / `default_acl`** from
    `kntgraph.tools.acl` (kept) into
    `kntgraph.tools.manager` (or directly into
    `kntgraph.tools.__init__`) so the surface is
    discoverable from the new home.

### 4.2 CLI scaffolds

  - `cli/templates/dispatcher.py.jinja`: drop the
    `ToolRegistry` import and the `registry.register(...)`
    line. Tools register exclusively via
    `worker_manager.register(tool_cls)`.
  - `cli/templates/main.py.jinja`: same.
  - `cli/templates/agent.py.jinja`: same. The
    `_make_acl` helper (if any) becomes a
    `_make_worker_acl` returning a `ToolACL`
    passed to `WorkerManager.register(acl=...)`.

### 4.3 Examples

  - `examples/10_http_intent_router.py`:
    `EchoTool(Tool)` → `EchoWorker(@tool_worker)`.
  - `examples/20_security_authorization.py`:
    `ToolRegistry` / `acl_for` calls → `WorkerManager`
    / `acl_for`.
  - `examples/knt-cli/weather_platform/src/weather_platform/main.py`:
    same migration.

### 4.4 Tests

  - **Delete:**
    - `tests/unit/tools/test_registry.py` (file)
    - The `TestToolRegistry` class in
      `tests/unit/tools/test_manager.py`
    - The `_EchoTool` fixture and ACL tests in
      `tests/unit/security/test_rbac.py:295–360`
      (replaced by `WorkerManager`-based ACL tests
      in the same file)

  - **Add:**
    - `tests/agents/unit/tools/test_worker_manager_acl.py`:
      gate-1 (WorkerManager denies per ACL) +
      gate-2 (RoleComponent.allowed_tools denial
      at emission) coverage (per DEBT §2.27 step 5,
      5+ tests).
    - Migration tests in
      `tests/unit/test_import_graph_no_cycle.py`
      to ensure the legacy modules are no longer
      importable.

### 4.5 Documentation

  - Update `docs/security/authorization.md`,
    `docs/quickstart.md`, `docs/architecture.md` to
    describe the single-path model.
  - Add `docs/migration_<from>_to_<to>.md` covering
    `Tool` Protocol → `@tool_worker`,
    `ToolRegistry.register` → `WorkerManager.register`,
    `acl_for(name)` (same surface, new home).

## 5. Migration path

The ADR ships in three steps (one minor each):

  1. **Minor v0.16: ACL hook on WorkerManager.**
     DEBT §2.27 work. `WorkerManager.register` gains
     `acl=`; `WorkerManager._process_message`
     consults it. The `ToolACL` class moves from
     `kntgraph.tools.acl` to
     `kntgraph.tools.manager` (or stays in `acl.py`
     with a re-export). **The legacy `ToolRegistry`
     is NOT removed yet** — it stays with a
     `DeprecationWarning` on `import` and a runtime
     warning when `acl_for(...)` is called from
     production code (a logger.warning, not an
     error, to give users one minor to migrate).

  2. **Minor v0.17: DeprecationWarning + CLI scaffolds
     flipped.** The scaffolds
     (`cli/templates/dispatcher.py.jinja` etc.)
     stop importing `ToolRegistry`. The examples
     migrate. `import kntgraph.tools.registry`
     emits `DeprecationWarning` with the migration
     message ("use `WorkerManager.register(acl=...)`
     instead"). The `ToolRegistry` class still
     works (no error) but its `__init__` logs the
     same warning.

  3. **Minor v0.18: Removal.** `git rm` the legacy
     files. `ToolACL` / `default_acl` /
     `WorkerManager.acl_for` are the only ACL
     surface. `tests/unit/tools/test_registry.py`
     is removed. The `acl.py` module shrinks to
     just the dataclass + `default_acl` (no
     registry dependencies).

The three-minor cycle matches the AGENTS.md §7
deprecation window (`DeprecationWarning` for one
minor, then removal). Each step ships with the
migration guide update.

## 6. Consequences

### 6.1 Positive

  - **One path, one ACL hook.** `WorkerManager._process_message`
    is the single point of authorization; ACL cannot
    be forgotten, the gap that ADR-061 §5 flagged
    closes by construction.
  - **No "is this Worker or in-process?" decision.**
    New tools land on the Worker path by default;
    the arbitrary line disappears.
  - **Scaffolds teach one model.** New users see
    `WorkerManager.register(tool_cls)` and stop
    learning `ToolRegistry`.
  - **Dead code disappears.** `acl_for` /
    `ToolACL.check` become the single enforcement
    surface. `tools.registry.py`,
    `tools/protocol.py`, `tools/descriptors.py`,
    `agents/tools/protocol.py` are `git rm`'d.
  - **Test surface shrinks.** The dual-path tests
    collapse; the new ACL tests focus on
    `WorkerManager`.

### 6.2 Negative

  - **Migration cost.** 3 examples, 3 scaffolds,
    14 tests must change in v0.17; in v0.18 the
    legacy module is removed (no rollback). Users
    with custom `Tool` Protocol subclasses must
    migrate in one minor (the deprecation
    warning window).
  - **In-process "cheap" tools pay the Worker
    cost.** A pure in-memory validation tool that
    would run in <1ms today pays the
    `ProcessPoolExecutor` cold-start cost
    (~50–200ms per worker process; mitigated by
    the pool's reuse, but the first call in a
    cold worker is not free). For high-volume
    tools, this is a real regression. The trade-off
    is justified by ACL uniformity and operational
    consistency (one pool to monitor, one place
    to put circuit breakers, one place to put
    rate-limit hooks).
  - **The `Tool` Protocol concept disappears.**
    Some users may have built decorators or
    type-check logic against
    `Tool: Protocol`. The migration guide covers
    this; the decorator is replaced by
    `@runtime_checkable` on `WorkerManager`'s
    public metadata.
  - **The `ToolDescriptor` for Solution promotion
    changes.** Today, `ToolRegistry.list_descriptors`
    feeds `(:Tool)` nodes into the Solution
    sub-graph (FalkorDB). The replacement is
    `WorkerManager.list_workers()`, with the same
    shape (`name` / `description` /
    `input_schema`). The FalkorDB schema is
    unchanged.

### 6.3 Risks

  - **Cold-start regression on hot paths.** Mitigation:
    the framework keeps a `WorkerManager._pool`
    reuse contract; the per-cold-worker cost is
    paid once per process lifetime. For workloads
    with high tool-call rate, configure
    `max_workers` higher. (Already documented in
    `manager.py:106–110`.)
  - **Process startup for stateful tools.** A
    Worker process does not share state with the
    dispatcher process. Tools that today rely on
    in-process state (e.g. an in-memory cache
    that the dispatcher process holds) must
    migrate the state to Redis (the framework's
    shared state layer) or to the tool's own
    backend. The migration guide covers this.
  - **The deprecation cycle is two minors, not
    one.** A user reading "this is going away in
    v0.18" has one minor (v0.17) to migrate. If
    they miss the window, the import breaks.
    The migration guide is the safety net; the
    `DeprecationWarning` in v0.17 is the visible
    signal.

## 7. Alternatives considered

  - **Status quo (do nothing).** The dual-path
    state persists. ADR-061 §5 gaps remain; the
    WorkerManager ACL hook (DEBT §2.27) lands as
    a partial fix on top of a dead path. Cost
    compounds as more tools land. **Rejected.**

  - **Only add ACL to WorkerManager (no removal).**
    This is DEBT §2.27 as a standalone. Closes the
    visible gap but leaves `ToolRegistry` as
    dead-but-present code, leaves scaffolds
    teaching the dual path, leaves the arbitrary
    line. **Rejected:** the dead path is the
    disease; the ACL gap is a symptom.

  - **Remove the legacy path without an ACL hook.**
    Ships in one minor. Closes the dual-path
    question but **leaves `chat_llm` without ACL
    enforcement** (because the Worker path has no
    hook). This is the worst outcome: the legacy
    ACL surface is gone and the new surface has
    none. **Rejected.**

  - **Replace `WorkerManager` with a different
    runtime.** Out of scope. The Worker pattern
    (ADR-036) is the framework's documented
    canonical path; replacing it would be a much
    larger ADR.

  - **Make the legacy path the canonical one.**
    Reverse the migration. Reintroduces the
    in-process I/O problem ADR-043 was written to
    solve. **Rejected.**

## 8. Acceptance checklist

The ADR is **Accepted** when:

  - [ ] `WorkerManager.acl_for` and
    `WorkerManager.register(acl=...)` exist and
    are tested (≥5 ACL tests).
  - [ ] `WorkerManager._process_message` enforces
    `acl.check(principal)` before
    `run_in_executor` (gate 1).
  - [ ] `_BaseRoleSystem._emit_request` enforces
    `RoleComponent.allowed_tools` for `chat_llm`
    (gate 2).
  - [ ] `Event.producer_principal_id` exists and
    is signed by the API layer; the Worker path
    uses it for ACL.
  - [ ] The legacy `ToolRegistry` / `Tool`
    Protocol / `ToolDescriptor` files are still
    present (v0.16 step), still functional, but
    flagged with `DeprecationWarning` on import.
  - [ ] The CLI scaffolds
    (`cli/templates/{dispatcher,main,agent}.py.jinja`)
    are unchanged in v0.16 (the v0.17 step).

The ADR is **Implemented (v0.18 step)** when:

  - [ ] `git rm src/kntgraph/tools/registry.py`
  - [ ] `git rm src/kntgraph/tools/protocol.py`
  - [ ] `git rm src/kntgraph/tools/descriptors.py`
  - [ ] `git rm src/kntgraph/agents/tools/protocol.py`
  - [ ] `git rm tests/unit/tools/test_registry.py`
  - [ ] All examples + scaffolds migrated.
  - [ ] Migration guide published in
    `docs/migration_<from>_to_<to>.md`.
  - [ ] `scripts/ci.py` gates pass (lint, format,
    complexity, pyright, tests, reuse).

## 9. See also

  - [ADR-005](./ADR-005-Checkpoints-Idempotency.md)
    — original `ToolInvoker` design (in-process).
  - [ADR-017](./ADR-017-Identity-Authorization.md)
    — `ToolACL` / `default_acl` rationale (the
    surface that survives, in a new home).
  - [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md)
    — `Tool` Protocol split; `ToolInvoker`
    deprecation (accelerated; `ToolRegistry`
    remained).
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md)
    — `@tool_worker` + `WorkerManager`
    canonical.
  - [ADR-043](./ADR-043-LiteLLM-Worker-Migration.md)
    — `LiteLLMToolWorker` (the first migration
    onto the Worker path; precedent for the
    full migration in this ADR).
  - [ADR-047](./ADR-047-Tool-Adapter-Pattern.md)
    — Tool-Adapter pattern, three sub-Workers.
  - [ADR-061 §5](./ADR-061-litellm-integration-review.md)
    — the audit that surfaced the visible gap.
  - [DEBT §2.27](../DEBT.md) — WorkerManager ACL
    hook (the v0.16 step in this ADR).
  - [DEBT §2.28](../DEBT.md) — Single Tool Path
    (this ADR; the v0.17 + v0.18 steps).