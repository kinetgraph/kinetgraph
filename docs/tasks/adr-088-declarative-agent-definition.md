<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# TASK-adr-088-declarative-agent-definition — Implement the declarative agent model

- **BC:** `platform`
- **Feature doc:** [`docs/declarative_agent_modelling.md`](../declarative_agent_modelling.md)
- **Status:** `DONE`
- **Criado:** 2026-11-07
- **Concluído:** 2026-11-07

## Context

ADR-088 decides the shape of a declarative agent model: the
developer fills one frozen dataclass, and the framework derives the
installable runtime from it. The primitives it builds on all exist —
the Concordos, the three-gate authorisation model, the memory
projections — but they are assembled by hand at every call site, so
"modelling an agent" and "wiring an agent into the runtime" are the
same activity.

The groundwork landed in commit `4cda340`: the `StepContext` carrier
is now built in one place (DEBT §2.42 closed), with a parity test
proving that an FSM guard and a Saga skip condition observe the same
data. The example `examples/30_declarative_agent_modelling.py` and
the guide `docs/declarative_agent_modelling.md` already specify the
API — the example defines `RoleSpec`, `AgentDefinition` and
`AgentBundle` **locally**, because the library does not have them yet.

What is missing is the three modules ADR-088 §3.1 places:

```
src/kntgraph/agents/_definition.py    RoleSpec, AgentDefinition
src/kntgraph/agents/_bundle.py        AgentBundle, the derivation
src/kntgraph/agents/_projection.py    RoleProjection
```

## Scope

**Included:**

- The three modules, with the public API of ADR-088 §2.2–§2.6.
- The `RoleProjection` that materialises `RoleComponent` — the
  framework's replacement for the per-vertical pattern in
  `examples/21_two_level_authorization.py`.
- Tests covering the happy path and at least one failure mode for
  every public function (ADR-088 §5).
- The example rewritten to import from the library instead of
  defining the types locally.

**Out of scope:**

- Serialisation of `AgentDefinition` (ADR-088 §2.1, deferred).
- Prompt assembly from memory tiers (ADR-088 §2.1, separate concern).
- The `AgentContractVerifier` (ADR-088 §5, "follow-up, not blocking").
- The type-indexed domain access that `domain_types` anticipates
  (DEBT §2.42 open item 1 — the declaration is inert until it lands).

## Decisions

| # | Decision | Rationale | ADR / Skill |
|---|---|---|---|
| D1 | `AgentBundle` carries a derived `name` (`agent:<name>`) | The `Concordo` Protocol (ADR-069 §3.2) requires `name`, and §2.4 supports the catalog path. The name is derived, never stored on the definition. | [ADR-069](../../ADRs/ADR-069-Agent-Concordo-Foundation.md) §3.2 |
| D2 | `install()` uses `principal.agent_id` to call `dispatcher.track_agent(...)` | The principal is the runtime credential the host supplies. Tracking the agent it identifies is the one dispatcher-native action the credential enables. | [ADR-060](../../ADRs/ADR-060-fmh-office-v2-pillars.md) §2.0 |
| D3 | A role event with a missing payload key still materialises the component, with the spec's policy | `allowed_tools` is the capability policy and must not depend on the event's well-formedness; dropping it would silently disable gate 2. | [ADR-088](../../ADRs/ADR-088-Declarative-Agent-Definition-over-Concordos.md) §2.6 |
| D4 | The projection is a pure function of `(world, events)` | The dispatcher replays ticks during recovery; a projection that is not pure breaks replay. | [ADR-069](../../ADRs/ADR-069-Agent-Concordo-Foundation.md) §9.2 |

## Phases

| # | Phase | Deliverable | Verified by |
|---|---|---|---|
| F1 | The model | `RoleSpec`, `AgentDefinition` | `tests/agents/unit/test_declarative_agent.py` |
| F2 | The derivation | `AgentBundle`, `bundle()` | `tests/agents/unit/test_declarative_agent.py` |
| F3 | The projection | `RoleProjection` | `tests/agents/unit/test_declarative_agent.py` |
| F4 | The installation | `install()` | `tests/agents/unit/test_declarative_agent.py` |
| F5 | The example | `examples/30_declarative_agent_modelling.py` uses the library | `tests/unit/examples/` |

## Definition of Done

- [x] **D1** — `RoleSpec` and `AgentDefinition` exist in `src/kntgraph/agents/_definition.py` as frozen dataclasses with slots, and hold no runtime handle
  - check: `uv run pytest tests/agents/unit/test_declarative_agent.py::test_role_spec_is_frozen_and_carries_no_runtime_handle`
- [x] **D2** — `AgentBundle` exists in `src/kntgraph/agents/_bundle.py` and satisfies the `Concordo` Protocol
  - check: `uv run pytest tests/agents/unit/test_declarative_agent.py::test_agent_bundle_satisfies_the_concordo_protocol`
- [x] **D3** — `AgentDefinition.bundle()` derives the systems and projections, including the `RoleProjection`, and stores no duplicated field
  - check: `uv run pytest tests/agents/unit/test_declarative_agent.py::test_bundle_derives_systems_and_projections`
- [x] **D4** — `AgentDefinition.install(dispatcher, principal=principal)` registers the systems and projections on the dispatcher
  - check: `uv run pytest tests/agents/unit/test_declarative_agent.py::test_install_registers_systems_and_projections`
- [x] **D5** — `RoleProjection` materialises `RoleComponent` from the declared event type, combining `RoleSpec.allowed_tools` with the event's persona and instructions
  - check: `uv run pytest tests/agents/unit/test_declarative_agent.py::test_role_projection_materialises_role_component`
- [x] **D6** — `RoleComponent` and `has_tool_access` remain in the framework unchanged; gate 2 reads the projected component
  - check: `uv run pytest tests/agents/unit/test_declarative_agent.py::test_projected_component_is_what_gate_two_reads`
- [x] **D7** — The dispatcher gains no agent-awareness: no definition, persona or memory-tier field is read by `ReactiveDispatcher` or its modules
  - check: `uv run pytest tests/architecture/test_agent_awareness.py`
- [x] **D8** — Tests cover the happy path and at least one failure mode for every public function
  - check: `uv run pytest tests/agents/unit/test_declarative_agent.py`
- [x] **D9** — The `kntgraph-type-discipline` rules hold for the new modules: no `Any`, no `TypedDict`, frozen dataclasses for shape-known data
  - check: `uv run python scripts/design_gate.py`
- [x] **D10** — The example uses the library instead of defining the types locally
  - check: `uv run pytest tests/unit/examples/`
- [x] **D11** — The whole gate passes
  - check: `uv run scripts/ci.py`

## Implementation notes

The derivation lives in `_bundle.py`; `_definition.py` imports it.
The dependency direction is one-way — `_bundle.py` refers to
`AgentDefinition` only under `TYPE_CHECKING` — so there is no
import cycle and no deferred import inside a method.

The example currently defines `RoleSpec`, `AgentDefinition` and
`AgentBundle` locally. After this task it must import them from
`kntgraph.agents`; a local copy would be a second vocabulary for the
same concept, which is exactly the drift the harness prevents.
