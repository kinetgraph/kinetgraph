<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-041: `agents.roles` deprecation and removal schedule

**Status:** Accepted

**Date:** July 13, 2026

**Authors:** Architecture Team

**Related:** [ADR-006](./ADR-006-Tool-Role-Separation.md) (Superseded), [ADR-039](./ADR-039-Role-rethinking-and-intentions-routing.md)

---

## 1. Context

`ADR-039` re-defined the semantic *Role* as a pure ECS component (`RoleComponent`) and routed intent execution through a pure `IntentResolutionSystem`. The legacy `kntgraph.agents.roles` package (`ChatRole`, `PlannerRole`, `SummarizerRole`, `PersonalizedRole`, `SemanticRoutingRole`) implements the pre-ADR-039 model: a `Role` is a Python class that wraps a `LiteLLMTool` to provide a domain prompt and parse the LLM output.

The legacy classes are still imported by:

- the test suite (`tests/agents/unit/roles/`)
- the CLI scaffolding template (`src/kntgraph/cli/templates/dispatcher.py.jinja`)
- 6 example scripts under `examples/`
- 2 documentation pages (`docs/architecture.md`, `docs/routing.md`)

The `IntentResolutionSystem`, `RoleComponent`, and `IntentComponent` already ship in `agents/roles/resolution.py` (re-exported from the package), so the *new* API is available behind the same `kntgraph.agents.roles` import path. The legacy classes coexist with the new components.

## 2. Decision

`kntgraph.agents.roles` is **deprecated as a package**:

- Importing the package emits a `DeprecationWarning` (one per process).
- The package remains importable through **v0.9** (target: end of 2026 Q3) to give downstream code time to migrate.
- The package and its submodules are **removed in v1.0** (target: end of 2026 Q4). The new `RoleComponent`/`IntentResolutionSystem` API remains available at its current path.

## 3. Migration guide

| Legacy symbol | Replacement |
|---|---|
| `ChatRole` | `RoleComponent` with persona + `IntentResolutionSystem` |
| `PlannerRole` | `RoleComponent` + `IntentComponent` describing the plan request |
| `SummarizerRole` | `RoleComponent` summarising an existing `World` view |
| `PersonalizedRole` | merge persona into `RoleComponent`; resolve via `IntentResolutionSystem` |
| `SemanticRoutingRole` | replace with a `GLiNER2`-backed intent router (see `ADR-013`) |
| `route_on_user_message` / `async_route_on_user_message` | same: a custom `WorldSystem` reading the GLiNER2 routing decision |

The migration is **architectural**, not mechanical: a legacy `Role` is an LLM call wrapped in a domain prompt; the new flow is a *pure* resolution step that picks a tool to call and an LLM (or other inference) that runs out-of-band. See `ADR-039 §3` for the full pipeline.

## 4. Consequences

### Positive

- The warning surfaces the deprecation to every consumer at import time without breaking their code in v0.8/v0.9.
- The CLI template (`dispatcher.py.jinja`) and example scripts keep working until the deprecation is enforced at v1.0.
- The new API (`RoleComponent`, `IntentComponent`, `IntentResolutionSystem`) is reachable from the same package today, lowering the migration cost.

### Negative

- A `DeprecationWarning` is noisy in test runs. Tests already filter warnings (pytest's default is *not* to elevate them to errors), but downstream consumers that run with `-W error::DeprecationWarning` will break on first import. This is the intended behaviour for a deprecation.
- The deprecation is silent for users that import the new symbols only (`RoleComponent`, `IntentResolutionSystem`) — the warning fires on *any* import of the `agents.roles` package, including the new symbols, because they share a namespace.

## 5. Rollout plan

1. **v0.8.0** (this PR): add `DeprecationWarning` at package import, update `CHANGELOG.md`, ship `ADR-041`.
2. **v0.9.x**: keep the package alive. New features only land on `RoleComponent` / `IntentResolutionSystem`. The CLI template continues to use the new API; example scripts that use legacy roles are updated as time permits.
3. **v1.0.0**: delete `src/kntgraph/agents/roles/`, delete `tests/agents/unit/roles/`, delete the 6 example scripts that import legacy roles, update `cli/templates/dispatcher.py.jinja`, and update `docs/architecture.md`/`docs/routing.md`.

A deprecation removal must be coordinated with the **first major release (v1.0)**; per `AGENTS.md §7` ("Deprecations must survive at least one minor cycle"), the package lives for two minor cycles (v0.8, v0.9) before removal.
