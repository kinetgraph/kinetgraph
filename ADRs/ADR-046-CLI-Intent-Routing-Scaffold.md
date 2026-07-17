<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-046: CLI Scaffold for Intent Routing Modes

**Status:** Proposed

**Date:** July 17, 2026

**Version:** 0.1.0

**Authors:** Architecture Team

**Related:** ADR-012, ADR-034, ADR-036, ADR-039, ADR-043

---

## 1. Context

The current CLI supports scaffold generation for the HTTP gateway described in ADR-012, but it does not expose a first-class path for the intent-routing architecture introduced in ADR-039.

That gap creates two problems:

1. **Architectural mismatch:** the CLI can scaffold projects for the HTTP adapter, but not for the ECS-based intent resolution model.
2. **Inconsistent onboarding:** teams that want to adopt ADR-039 must manually reconstruct the same runtime structure for each project.

The CLI should therefore provide a structured scaffold option that makes the intent-routing architecture explicit and reproducible.

---

## 2. Decision

We will extend the CLI scaffold so that a project can be generated in one of three explicit routing modes:

- `external`: the intention originates from outside the agent, such as a user request, HTTP gateway, or external event.
- `autonomous`: the intention is produced by the agent itself after internal planning, classification, or tool selection.
- `collaborate`: the intention is initially received from outside the agent but is then handed to a collaborative coordination layer that involves one or more autonomous agents before execution.

All modes share the same execution core:

- a shared intent model,
- a shared resolution system,
- a shared policy/ACL evaluation layer,
- and a shared emission path to the runtime tool execution pipeline.

The difference between the modes is the origin and coordination pattern of the intent, not the execution semantics.

---

## 3. Proposed CLI UX

The CLI will accept a routing mode option during project initialization:

```bash
knt init my-project --routing-mode=external
```

or

```bash
knt init my-project --routing-mode=autonomous
```

or

```bash
knt init my-project --routing-mode=collaborate
```

A fallback default will be provided:

```bash
knt init my-project
```

The resolved value may also be overridden through environment configuration:

```bash
export KNT_ROUTING_MODE=external
```

The effective precedence should be:

1. CLI flag
2. environment variable
3. default value

---

## 4. Architecture of the Generated Project

The scaffold should generate a project structure that makes the runtime shape explicit.

```text
src/my_project/
  main.py
  settings.py
  routing/
    __init__.py
    components.py
    adapters/
      __init__.py
      external.py
      autonomous.py
      collaborate.py
    resolution.py
    policy.py
    coordinator.py
```

### 4.1 Shared Core

All generated projects will include a shared core layer:

- `RoleComponent`: semantic role and permitted tool inventory
- `IntentComponent`: pending intent state and correlation context
- `IntentResolutionSystem`: shared runtime that resolves pending intents
- `IntentPolicy`: shared authorization and ACL evaluation layer

This shared core is the architectural contract for both modes.

### 4.2 Mode-Specific Adapters

The difference between the modes is the adapter and coordination strategy that materializes the initial intent.

- `external` adapter:
  - accepts user-originated intent payloads
  - creates or updates `IntentComponent`
  - passes the intent into the shared resolution pipeline

- `autonomous` adapter:
  - accepts agent-produced intent payloads
  - creates or updates `IntentComponent`
  - passes the intent into the same shared resolution pipeline

- `collaborate` adapter:
  - accepts an externally originated intent payload
  - creates or updates `IntentComponent`
  - forwards the intent to a collaboration coordinator that can recruit or negotiate with one or more autonomous agents
  - preserves the same shared resolution and policy pipeline once a concrete execution target is selected

This keeps runtime behavior consistent and avoids branching the policy engine by mode.

---

## 5. Execution Semantics

The generated runtime should follow a single execution flow for all modes:

1. **Intent ingestion**
   - the selected adapter receives an intent candidate

2. **Intent normalization**
   - the payload is converted into the shared intent model

3. **Coordination, when applicable**
   - in `collaborate`, the coordinator may select a collaborating agent or a delegation target
   - in `external` and `autonomous`, this step is effectively a no-op

4. **Resolution**
   - the intent target tool is determined
   - the tool is validated against the registry

5. **Policy evaluation**
   - the principal is checked against ACLs
   - the role permissions are checked against the target tool

6. **Emission**
   - on success, the runtime emits the canonical event shape
   - on failure, it emits a deterministic validation failure event

This ensures that the CLI scaffold is consistent with ADR-039 and preserves the downstream runtime contract expected by ADR-036 and ADR-034.

---

## 6. ACL Placement

ACL evaluation must not be embedded in the `autonomous` or `collaborate` modes themselves.

Instead, ACLs must be evaluated by a shared policy component that runs after the intent has been resolved and before the execution event is emitted.

This is important for three reasons:

1. **Consistency:** both modes obey the same authorization rules.
2. **Security:** authorization is independent of the source of the intent.
3. **Extensibility:** future policy layers can be added without changing the mode adapters.

In other words, the mode answers “who created the intent?”, while the shared policy answers “is this intent allowed?”

---

## 7. Behavioral Rules

The generated scaffold should enforce the following rules:

- all modes must produce the same event contract,
- all modes must share the same validation and rejection semantics,
- all modes must route successful intents into the same execution path,
- all modes must emit the same failure shape for invalid or unauthorized intents,
- and all modes must be observable through the same logging and tracing hooks.

The architectural distinction is therefore about intent origin and coordination pattern, not execution policy.

---

## 8. Implementation Plan

The implementation should proceed in four steps:

1. **CLI surface**
   - add `--routing-mode` to the scaffold command
   - support `external`, `autonomous`, and `collaborate`

2. **Template generation**
   - generate shared routing modules for both modes
   - generate mode-specific adapter modules

3. **Runtime wiring**
   - create a minimal bootstrap that registers the shared resolution system and the selected adapter

4. **Documentation and tests**
   - document both modes
   - add scaffold tests that verify the generated project structure and importability

---

## 9. Consequences

### Pros

- The CLI becomes aligned with ADR-039 instead of only scaffolding the HTTP gateway.
- New projects can start from a clearly defined intent-routing architecture.
- The separation between intent origin, collaboration, and authorization policy becomes explicit and maintainable.
- The onboarding experience is consistent for external, autonomous, and collaborative flows.

### Cons

- The CLI becomes slightly more complex because it must generate and explain two runtime paths that share a common core.
- The generated scaffold is opinionated and may not fit all teams without adaptation.

---

## 10. Recommendation

We should implement the CLI scaffold around a shared intent-resolution core and mode-specific adapters.

The mode name should describe the origin and coordination pattern of the intent:

- `external` for user-originated or externally injected intents
- `autonomous` for agent-generated intents
- `collaborate` for externally originated intents that are coordinated across multiple autonomous agents

The ACL and policy decisions should remain outside the mode-specific adapter layer and be executed by the shared resolution pipeline.

This design preserves consistency, keeps the architecture aligned with ADR-039, and makes the CLI a credible entry point for intent-driven project generation.
