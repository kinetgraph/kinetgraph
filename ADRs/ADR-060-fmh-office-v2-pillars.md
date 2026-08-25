<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-060: fmh_office v2 — pillars redefined over current framework primitives

- **Status:** Proposed
- **Date:** 2026-08-24
- **Author:** kinetgraph architecture team
- **Supersedes (in part):** [ADR-015](./ADR-015-fmh_office-vertical.md) §4.1–§4.6 (the v1 vertical's `Team` / `ProcessModel` / `Step` / `Role` / `RuleSet` shape)
- **Related to:**
  - [ADR-039](./ADR-039-Role-rethinking-and-intentions-routing.md) — `RoleComponent` and `IntentResolutionSystem`
  - [ADR-041](./ADR-041-agents-roles-deprecation.md) — `agents.roles` package deprecation (v0.8 → v1.0)
  - [ADR-043](./ADR-043-LiteLLM-Worker-Migration.md) — `LiteLLMToolWorker` migration
  - [ADR-049](./ADR-049-Zero-Token-Architecture.md) — Zero Token Architecture (Rule-based, Solution lookup)
  - [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) — `DomainComponent` and the memory decision tree
  - [ADR-046](./ADR-046-CLI-Intent-Routing-Scaffold.md) — routing modes (`external` / `autonomous` / `collaborate`)

> **Scope.** ADR-015 §4 introduced four vertical-specific
> pieces — `Team`, `ProcessModel`, `Step`, `RuleSet`,
> plus a `Role` class — to support a multi-actor
> backoffice workflow ("pedido → faturamento →
> cobrança"). Those pieces were written **before**
> ADR-039, ADR-041, ADR-043, ADR-049 and ADR-059 were
> accepted. The framework now has the primitives the
> v1 vertical was reinventing:
>
> - `RoleComponent` (ADR-039) replaces the v1 `Role` class.
> - `IntentResolutionSystem` (ADR-039) replaces the v1
>   `Role.execute(data, step)` orchestration.
> - `RoleSystem`s (ADR-039/043/044) + `RuleBasedChatSystem`
>   (ADR-049) cover the LLM-backed and deterministic
>   execution paths the v1 `Role` mixin provided.
> - `DomainComponent` (ADR-059) replaces the v1
>   `BusinessProcessState`.
> - `WorkerManager` (ADR-043) replaces the v1
>   `LLMDispatcher` fallback.
>
> This ADR redefines the vertical's **four pillars**
> (`System` / `Component` / `Event` / `Tool`) over those
> primitives and **supersedes the v1 vertical's
> `Team` / `ProcessModel` / `Step` / `Rule` vocabulary**
> in favour of the framework-native shape. The
> deprecation schedule for v1's vertical-specific code
> is in §4.

## 1. Context

ADR-015 was accepted when the framework offered:

- A `Role` as a **Python class** wrapping an LLM call
  (ADR-006 era).
- No `RoleComponent`; persona lived on the `Role`
  instance.
- `LiteLLMClient` as a sync orchestrator (the
  `ToolWorker` migration came later).
- Memory as a single mutable `AgentState`; the
  Profile / Continuity / Session split and the
  Domain Memory tier did not exist.

Under those constraints, the v1 vertical introduced
its own four pillars to model a multi-actor business
process:

| v1 pillar | Implementation | Reference |
|---|---|---|
| `Team` | dataclass; `role_name → Role instance` + `ProcessModel` | ADR-015 §4.3 |
| `ProcessModel` | YAML loader; `Step[]` + `RuleSet` | ADR-015 §4.2 |
| `Step` | trigger + role + action + emits | ADR-015 §4.2 / §4.4 |
| `RuleSet` | mini-parser JSONLogic-like; 200 LOC; whitelist DSL | ADR-015 §4.5 |

Each pillar carried its own runtime: `Team.__init__`
wired `StepAdvancerSystem` and `RuleEvaluator` into
the dispatcher; `Role.execute(data, step)` returned
`Result[dict, ToolError]`; `BusinessProcessState` was
a custom ECS component.

After ADR-039 + ADR-041 + ADR-043 + ADR-049 + ADR-059
landed, the framework primitives map almost
one-to-one onto v1's pillars:

| v1 vertical piece | Framework primitive that replaces it |
|---|---|
| `Role` class | `RoleComponent` (data) + `ChatRoleSystem` / `PlannerRoleSystem` / `SummarizerRoleSystem` / `PersonalizedRoleSystem` / `RuleBasedChatSystem` (pure `WorldSystem`s) |
| `Role.execute(data, step)` | `IntentResolutionSystem` (ADR-039) for tool selection + `RoleSystem` (ADR-039/043) for LLM execution |
| `LLMDispatcher` fallback | `WorkerManager` + `LiteLLMToolWorker` (ADR-043) — out-of-process, no event-loop blocking |
| `BusinessProcessState` | `DomainComponent` (ADR-059) + `@dataclass(frozen=True, slots=True)` + auto-registered via `@domain_component(event_type)` |
| `RuleSet.evaluate(rules, data)` | A pure `WorldSystem` reading the relevant `DomainComponent` and emitting `*.handoff_accepted` / `*.rule_matched` events |
| `StepAdvancerSystem` | A pure `WorldSystem` reading `DomainComponent.process_state` and the trigger event |
| `Team` (the wrapping dataclass) | A *composition* — no longer a primitive. A vertical composes Systems and Tools in its `main.py` / `consumer.py` (the same place the HTTP scaffold already wires them) |

The v1 vocabulary is now an **indirection layer**
between the framework and its own primitives. Keeping
it alive costs maintenance (each framework evolution
needs to be mirrored in the vertical), blocks users
from the canonical API, and re-introduces the
"framework primitives vs vertical primitives" seam
that ADR-019 §4 and ADR-025 already closed for the
core.

## 2. Decision

### 2.0 Terminology — `Role`, `Agent`, `PrincipalLevel`

The framework currently uses the word **"Role"** in
two unrelated senses, a debt ADR-039 §1.1 already
flagged but did not resolve:

| Sense | Today | Reference |
|---|---|---|
| **RBAC level on a `Principal`** | `Role.agent` / `Role.admin` / `Role.service` (the `Role` enum) | ADR-017 §2.1 |
| **Semantic persona on a `RoleComponent`** | `role_name="financeiro"`, `persona="..."`, `allowed_tools=[...]` | ADR-039 §2.1 |

Both are conceptually different: the RBAC enum
governs **who may call what** (a permission level
on a `Principal`); the `RoleComponent` governs
**how a persona behaves** (a semantic tag on a
`WorldView`). The shared name forces readers to
disambiguate from context on every read.

This ADR fixes the conflict by **renaming the RBAC
enum** and pinning the vocabulary used everywhere
in v2:

| Term | Definition | Lifetime |
|---|---|---|
| **`PrincipalLevel`** | RBAC permission on a `Principal`. Values: `service` / `agent` / `admin`. Replaces `Role.agent` / `Role.admin` / `Role.service` from ADR-017. | per request (`ContextVar`); not persisted on events. |
| **`Role`** (the only one left) | Semantic persona + allowed tools + handoff ACL on a `RoleComponent`. Always flexible — same shape covers operation (`oncall`, `auditor`) **and** business (`financeiro`, `gerente`). | per `agent_id`; persisted via the fold (ADR-002). |
| **`Agent`** | A bundle identified by an `agent_id` (string), carrying one `RoleComponent` (its persona), a set of `WorldSystem`s registered for that `agent_id`, a set of `Tool` ACLs (per `RoleComponent.allowed_tools`), and a set of `DomainEvent` types it emits and consumes. **Not** a new framework type — it is the composition the user already writes in `main.py` / `consumer.py`. | per deployment; persisted via the EventLog (the `agent_id` is the durable identity). |
| **`agent_id`** | The string that anchors an `Agent`. Opaque to the framework; conventionally scoped under `tenant_id` (`tenant-A.nf-001` per ADR-017 §2.1). | per event; the source of truth for replay. |

**Why `Role` is the one we keep.** The semantic
`RoleComponent` is the one a user names (`financeiro`,
`estoquista`, `gerente`); the RBAC enum is internal
plumbing. Renaming the enum to `PrincipalLevel` makes
the public vocabulary smaller: "what is the persona?"
= `Role`. "what permission does the request carry?"
= `PrincipalLevel`.

**Why no new `Agent` type.** Today the user composes:

```python
dispatcher = ReactiveDispatcher(
    log=log,
    systems=[
        ChatRoleSystem(persona="financeiro"),
        StepAdvancerSystem(process_yaml="..."),
        SolutionPipeline(...),
    ],
    tool_registry=registry,
)
```

The dispatcher already binds `agent_id`s to systems
implicitly (each `WorldSystem` walks
`world.views.items()`). Adding an `Agent` class would
duplicate that wiring without giving the framework
new capability. The **vocabulary** is added — the
**type** is not.

**Mapping from today to v2:**

| v1 / current | v2 |
|---|---|
| `Principal(role=Role.agent)` | `Principal(level=PrincipalLevel.agent)` |
| `require_role(Role.admin)` | `require_level(PrincipalLevel.admin)` |
| `RoleComponent(role_name="financeiro", ...)` | unchanged — already uses the name "Role" |
| `class FinanceiroRole(Role)` (legacy) | **deleted** per ADR-041 |
| The implicit "agent in the dispatcher" | called `Agent`; the user's `main.py` is the place where one Agent is wired |

The rename of the RBAC enum is a **breaking change**.
Schedule: introduce `PrincipalLevel` in v0.10 alongside
the v2 agents/ package; deprecate `Role` (the enum) in
v0.11; remove in v1.0. The migration is mechanical
(`Role.admin` → `PrincipalLevel.admin`), tracked in
DEBT.md.

### 2.1 The four pillars, redefined

The fmh_office v2 vertical is built on the same
four primitives the framework already provides. No
new framework types are introduced.

| Pillar | What it is in v2 | Reference |
|---|---|---|
| **`System`** | A pure `WorldSystem` (ADR-018 §2.1) that observes the `World` and emits events. Three role systems (`ChatRoleSystem`, `PlannerRoleSystem`, `RuleBasedChatSystem`) cover LLM-backed and deterministic execution. One `StepAdvancerSystem` covers process progression. | ADR-018, ADR-039, ADR-049 |
| **`Component`** | A `@dataclass(frozen=True, slots=True)` `DomainComponent` (ADR-059) holding the process state. Auto-registered via `@domain_component(event_type)`. No custom ECS projection — the framework's fold already materialises it. | ADR-059 |
| **`Event`** | A `DomainEvent` (ADR-003 §2.2 `event_class="domain"`). Hand-offs between agents are **`*.handoff_requested`** / **`*.handoff_accepted`** events — not direct calls. Process progress is **`*.step_completed`** / **`*.process.completed`**. Rules emit **`*.rule_matched`** / **`*.rule_rejected`**. | ADR-003, ADR-059 |
| **`Tool`** | A `Tool` implementing the Protocol (ADR-025). LLM goes through `LiteLLMToolWorker` (ADR-043); external I/O goes through user-defined `@tool_worker`s. ACL is enforced by `ToolACL` (ADR-017) and `RoleComponent.allowed_tools` (ADR-039). | ADR-025, ADR-043, ADR-017 |

The four pillars compose without ceremony: a vertical
ships a `ProcessYAML` declaring the steps and rules;
the framework's CLI scaffold (`knt init project
--vertical=fmh-office`) generates the
`StepAdvancerSystem` wiring; the user fills in the
domain-specific `Tool` calls.

### 2.2 What the vertical v2 **does not** ship

- **`Team` dataclass.** No replacement. The vertical's
  `main.py` instantiates each system directly. The
  four role systems are independent and stack in any
  order; composition is `dispatcher = ReactiveDispatcher(systems=[...])`,
  which the scaffolding already emits.
- **`ProcessModel` class.** Replaced by `ProcessYAML`,
  the YAML/JSON file the user writes. The
  `StepAdvancerSystem` reads it via `pyyaml` at boot;
  the YAML schema is **unchanged** from v1 (ADR-015
  §4.2), so existing v1 examples parse.
- **`Step` class.** Replaced by the YAML node
  (`step.id`, `step.role`, `step.trigger`, etc.).
  `StepAdvancerSystem` reads the nodes directly — no
  intermediate class.
- **`RuleSet` class.** Replaced by a pure `WorldSystem`
  that reads the rule conditions and emits
  `*.rule_matched` / `*.rule_rejected`. The DSL stays
  the v1 JSONLogic-like dialect (ADR-015 §4.5); no
  LLM for rule evaluation.
- **`Role` class.** Already deprecated (ADR-041). v2
  emits `RoleComponent` instead; the YAML's
  `role: financeiro` field is mapped to a
  `RoleComponent(role_name="financeiro", ...)` in the
  generated wiring.

### 2.3 Handoff between agents

The v1 vertical did not specify how one role hands
off to another; the assumption was that the engine
called `role.execute(data, step)` on whichever role
the YAML named. v2 makes the handoff explicit and
event-driven:

```
financeiro                          gerente
   │                                  ▲
   ▼                                  │
step.completed ──► handoff_requested ─┘
                       │
                       ▼
              gerente.<RoleComponent> evaluates
              ──► handoff_accepted
                       │
                       ▼
                  step.approved
```

All transitions are `DomainEvent`s
(`event_class="domain"`). The framework's
`IntentResolutionSystem` (ADR-039) gates each
emission: the source role's `RoleComponent` must
have the destination's `role_name` in its
`handoff_targets` (semantic ACL, per §2.0); otherwise
the request fails ACL and emits
`intent.validation_failed`.

This is the same ACL pattern the `external` /
`autonomous` / `collaborate` modes use
(ADR-046 §6) — v2 inherits it instead of inventing
a parallel ACL.

### 2.4 Process state as a `DomainComponent`

v1's `BusinessProcessState` was a hand-rolled
component with `completed_steps: set[str]`,
`current_step: str`, `pending_approval: bool`. v2
uses the ADR-059 pattern: a pure
`@dataclass(frozen=True, slots=True)` decorated with
`@domain_component(event_type)`. The framework's
fold materialises it from the EventLog; the
`StepAdvancerSystem` reads it; idempotent replay is
inherited from the fold (ADR-002).

```python
@domain_component(event_type="process.pedido.started")
@dataclass(frozen=True, slots=True)
class PedidoProcessState:
    process_id: str
    current_step: str
    completed_steps: frozenset[str]
    pending_approval_from: str | None = None
```

The user writes the state shape; the framework
provides the hydration, the durability, and the
replay safety.

## 3. The four pillars in detail

### 3.0 The three-gate model — who controls what

Before detailing the four pillars, this section fixes
the **authorisation model** that v2 inherits from
ADR-017 + ADR-039 and extends with a third gate
(unique to v2). The model answers the question
"can an external user, by virtue of being an admin
or service principal, force an agent to do something
the agent's persona forbids?" — the answer is **no**,
and the three gates explain why.

A tool call (or a handoff) must clear **three
sequential, independent gates**. Failing any one
blocks the action; passing all three emits
`tool.<name>.requested` (or `*.handoff_accepted`).

| Gate | Owner | Lives in | Persists? | What it answers |
|---|---|---|---|---|
| **1. RBAC of the request** | `PrincipalLevel` (ADR-017, renamed per §2.0) on the inbound `Principal` | `ToolACL.check(principal)` | No (`ContextVar`) | "Does **this request** have the infrastructure-level permission to invoke this tool?" |
| **2. Persona of the agent** | `RoleComponent` on the destination `AgentView` | `target_tool in role.allowed_tools` (ADR-039) | Yes (fold) | "Is **the agent that will execute** dressed with a persona that admits the tool?" |
| **3. Handoff ACL (v2 only)** | `RoleComponent.handoff_targets: frozenset[str]` on the **source** agent | `RoleComponent.SwitcherSystem` (§3.1) | Yes (fold) | "If this is a cross-agent handoff, does **the source role** admit the destination role?" |

The three gates operate at three levels:

- **Gate 1** answers about the **request** —
  a "trust" check on the inbound principal.
  Short-lived, no audit beyond the request log.
- **Gate 2** answers about the **agent** — a
  "capability" check on the persona the agent is
  currently dressed with. Long-lived, auditable via
  the EventLog (every `RoleComponent` change is a
  Domain Event).
- **Gate 3** answers about the **transition** — a
  "delegation" check between personas. Long-lived,
  auditable via the EventLog (the handoff itself is
  `*.handoff_requested` / `*.handoff_accepted`).

#### 3.0.1 The four scenarios the three gates unlock

The interplay of the three gates is the point of v2.
Consider an external user invoking
`tools.billing.refund` against an agent whose
persona is `atendente`:

| Scenario | PrincipalLevel (req) | Persona (agent) | Outcome |
|---|---|---|---|
| External user is `agent`, agent is `atendente` | `agent` | `atendente` (no `refund` in `allowed_tools`) | **block @ gate 2** — the persona does not admit the tool |
| External user is `admin`, agent is `atendente` | `admin` | `atendente` (no `refund` in `allowed_tools`) | **block @ gate 2** — admin RBAC does **not** bypass persona |
| External user is `admin`, agent is `financeiro` (with `refund` in `allowed_tools`) | `admin` | `financeiro` | **allow** — both gates pass |
| External user is `agent`, agent is `financeiro` (with `refund` in `allowed_tools`) | `agent` | `financeiro` | **allow** — both gates pass (the request itself is allowed; the persona is what governs audit) |

The second row is the load-bearing one: **a higher
RBAC level on the request does not bypass a persona
that forbids the tool.** The persona is the agent's
**capability**, not a permission that an admin can
override. To bypass it, the admin must **change the
persona first** (emit `RoleComponent.swapped` carrying
a new `RoleComponent` with `refund` in
`allowed_tools`), wait for the fold to project the new
state, and re-issue the intent. Every step is
auditable via the EventLog and `correlation_id`.

The fourth row is the symmetric case: a low-RBAC
request can succeed if the agent's persona already
admits the tool. This is the **multi-level
authorisation pattern**: the request needs *some*
RBAC level to enter the system, but what the agent
actually does is governed by its persona. A read-only
`agent` principal against a `financeiro` agent can
trigger a refund; the audit log records the principal
that initiated the request, the agent that executed,
and the persona that admitted the tool.

#### 3.0.2 Why three gates and not one

A single-gate model conflates two concerns that
mature systems keep separate:

- **Who is asking** — a question of trust, scoped to
  the request, often provided by an upstream
  authn layer (HTTP gateway, message broker).
- **What the agent can do** — a question of
  capability, scoped to the agent's lifecycle,
  emitted as part of the agent's domain state.

Putting both in one field collapses them: either
the persona is the RBAC level (and a role-swap is a
permission change — wrong), or the RBAC level is the
persona (and the principal has a persona — also
wrong). The double-lock of ADR-039 (gates 1 and 2)
already separates these for tool calls. v2 adds
gate 3 to do the same for cross-agent handoffs.

#### 3.0.3 Where each gate lives in code

| Gate | Where it is enforced |
|---|---|
| 1 | `ToolACL.check(principal)` in `IntentResolutionSystem` (ADR-039 §2.2 step 3) |
| 2 | `target_tool in role.allowed_tools` in `IntentResolutionSystem` (ADR-039 §2.2 step 4) |
| 3 | `destination.role_name in source.handoff_targets` in `RoleComponent.SwitcherSystem` (this ADR §3.1) |

A future revision may add a fourth gate (e.g.,
**rate-limit per principal** or **rate-limit per
persona**); the model is open to extension because
each gate is a single function call on a single
component. Tracked as an open question (§7.5).

### 3.1 `System`

The vertical ships **five** `WorldSystem`s (one per
canonical role + one process engine). All are pure
`__call__(world) -> list[Event]`. All read components
and emit events; none do I/O.

| System | Trigger event | Emits | Stack position |
|---|---|---|---|
| `RuleBasedChatSystem` (ADR-049) | `user.intent` | `chat.reply.generated` (short-circuit) | first |
| `ChatRoleSystem` (ADR-039) | `user.intent` | `tool.chat_llm.requested` | after `RuleBasedChatSystem` |
| `PlannerRoleSystem` (ADR-039) | `plan.request` | `tool.chat_llm.requested` | per-process |
| `RoleComponent.SwitcherSystem` (new in this ADR) | `*.handoff_requested` | `*.handoff_accepted` or `intent.validation_failed` | per-process |
| `StepAdvancerSystem` (new in this ADR) | `*.step_completed`, `*.step.approved`, `*.step.rejected` | `*.step_completed`, `*.process.completed`, `*.process.failed` | per-process |

`RoleComponent.SwitcherSystem` is the v2 replacement
for v1's "the engine calls role.execute on whichever
role the YAML names". It is **pure**: reads the
target role's `RoleComponent` from the destination
agent's view; validates that the source role's
`RoleComponent.handoff_targets` (a `frozenset[str]`
listing allowed destination `role_name`s, on the
**semantic** `RoleComponent`, per §2.0) admits the
target; emits `*.handoff_accepted` carrying the
payload. The destination agent's own
`IntentResolutionSystem` then resolves the payload
to a tool call.

`StepAdvancerSystem` is the v2 replacement for v1's
`StepAdvancerSystem` — same name, different
implementation. v2's version reads
`PedidoProcessState` (a `DomainComponent`) and the
YAML-declared `ProcessModel`, decides the next step,
and emits the corresponding domain event. No direct
calls to roles; handoff is via events.

### 3.2 `Component`

Vertical-specific state lives in `DomainComponent`s.
The vertical ships **one** component per business
process declared in the YAML (v2 ships with
`PedidoProcessState`; user-defined processes add
their own). The framework's
`@domain_component(event_type)` decorator registers
the fold; the user writes the field set.

```python
# Generated by the CLI scaffold (knt init project
# --vertical=fmh-office --process=pedido_faturamento)
@domain_component(event_type="process.pedido.started")
@dataclass(frozen=True, slots=True)
class PedidoProcessState:
    process_id: str
    current_step: str
    completed_steps: frozenset[str]
    pending_approval_from: str | None = None
    cancelled_reason: str | None = None
```

No `BusinessProcessState` class. No custom hydration
code. The framework's ECS projection does the work
(ADR-059 §2.2).

### 3.3 `Event`

The vertical emits **only** `DomainEvent`s
(`event_class="domain"`). The naming convention is:

| Pattern | Emitted by | Consumed by |
|---|---|---|
| `process.<id>.started` | HTTP intake / adapter | `StepAdvancerSystem` |
| `*.step_completed` | `StepAdvancerSystem` | `StepAdvancerSystem` (next step), `RoleComponent.SwitcherSystem` |
| `*.handoff_requested` | `RoleComponent.SwitcherSystem` | `IntentResolutionSystem` (target agent) |
| `*.handoff_accepted` | `IntentResolutionSystem` | `StepAdvancerSystem` |
| `*.step.approved` / `*.step.rejected` | approval flow | `StepAdvancerSystem` |
| `process.<id>.completed` / `process.<id>.failed` | `StepAdvancerSystem` | HTTP intake (200 to client) |
| `*.rule_matched` / `*.rule_rejected` | `RuleEvaluatorSystem` (new) | audit log + `StepAdvancerSystem` |

The `*.rule_matched` / `*.rule_rejected` events are
the **only** way a rule produces a decision. v1's
`RuleSet.evaluate(rules, data) -> Decision` (which
returned a `goto` / `require_approval` / `block` enum
value) is replaced by a `RuleEvaluatorSystem` that
emits the event. The `StepAdvancerSystem` listens
for the next event in its own step's grammar.

### 3.4 `Tool`

The vertical uses **only** `Tool`s implementing the
Protocol (ADR-025). v2 ships three exemplar tools
in the scaffold:

- `pedido.estoque.check` (HTTP-backed; `@tool_worker`).
- `pedido.financeiro.issue_invoice` (DB-backed; `@tool_worker`).
- `pedido.financeiro.send_boleto` (HTTP-backed; `@tool_worker`).

LLM execution goes through `LiteLLMToolWorker`
(ADR-043) — no vertical-specific `LiteLLMClient` or
`LLMDispatcher`. PII redaction (when the payload
contains CNPJ / CPF) goes through `PiiRedactionTool`
(ADR-010).

ACL is enforced by `ToolACL` (ADR-017) +
`RoleComponent.allowed_tools` (ADR-039). The v1
vertical's hand-rolled role ACL is replaced by the
framework's shared ACL.

## 4. Deprecation schedule for v1

ADR-015 §4.1–§4.6 (`Team`, `ProcessModel`, `Step`,
`RuleSet`, `BusinessProcessState`, the
`fmh_office.catalog.roles` package) are deprecated.
The migration follows the AGENTS.md §7 deprecation
policy (one minor cycle, then removal):

| v1 vertical piece | v2 replacement | Removal target |
|---|---|---|
| `fmh_office.catalog.roles` (the legacy `Role` class hierarchy) | `RoleComponent` + `RoleSystem`s | v0.11.0 (mirrors ADR-041 v1.0 deletion) |
| `fmh_office.engine.process.ProcessModel` (Python class) | `ProcessYAML` file + `StepAdvancerSystem` reading it | v0.11.0 |
| `fmh_office.engine.systems.StepAdvancerSystem` (v1 implementation) | v2 `StepAdvancerSystem` (event-driven) | v0.11.0 |
| `fmh_office.engine.rules.RuleSet` | `RuleEvaluatorSystem` emitting `*.rule_matched` | v0.11.0 |
| `fmh_office.engine.state.BusinessProcessState` | `DomainComponent` (ADR-059) | v0.11.0 |
| `fmh_office.engine.Team` (the wrapping dataclass) | composition in `main.py` (no replacement) | v0.11.0 |
| `examples/pedido.yml` (v1 schema) | unchanged — `ProcessYAML` parses v1 schema verbatim | n/a |

The v0.10 cycle ships the v2 implementation under
`fmh_office.v2`. v1 lives under `fmh_office` with a
`DeprecationWarning` at module import (same pattern
as ADR-041). The CLI scaffold emits a v2 project by
default; `--legacy` keeps the v1 scaffold for the
duration of the deprecation window.

## 5. What the CLI scaffold generates (v2)

```bash
knt init my-project --vertical=fmh-office --process=pedido_faturamento
```

Generates:

```
src/my_project/
  main.py                          # dispatcher + 5 systems wired
  consumer.py                      # Redis Streams consumer
  core/config.py                   # Settings
  routing/                         # ADR-046 scaffold (external)
  fmh_office/
    components.py                  # PedidoProcessState (DomainComponent)
    systems/
      step_advancer.py             # the v2 StepAdvancerSystem
      rule_evaluator.py            # emits *.rule_matched
      role_switcher.py             # emits *.handoff_accepted
    tools/
      estoque_check.py             # @tool_worker
      financeiro_invoice.py        # @tool_worker
      financeiro_boleto.py         # @tool_worker
    process/
      pedido_faturamento.yaml      # the v1 YAML, parsed verbatim
  tests/
    integration/test_pedido_flow.py
```

The scaffold **does not** ship:

- A `Team` class.
- A `ProcessModel` Python class.
- A `BusinessProcessState` class.
- A `RuleSet` class.
- A `Role` class.

Each `process/<name>.yaml` parses into the data the
five systems read at boot. Adding a new process is
**a YAML edit**, not a Python edit (same as v1;
ADR-015 §3.3).

## 6. Consequences

### Positive

- **No indirection.** The vertical no longer
  re-implements primitives the framework already
  ships. ADR-019 §4 / ADR-025 ("primitives live in
  the framework") is honoured for fmh_office too.
- **Same authoring experience.** The YAML schema is
  unchanged from v1 (ADR-015 §4.2); v2 is a refactor
  *under* the YAML, not a new DSL.
- **Same ACL story.** The hand-rolled role ACL from
  v1 is gone; `IntentResolutionSystem` + `ToolACL` is
  the framework-wide policy, and fmh_office inherits
  it (same as ADR-046).
- **Same observability.** v2 emits
  `*.step_completed` / `*.handoff_accepted` /
  `*.rule_matched` — all `DomainEvent`s, all
  traceable via `correlation_id` (ADR-037).
- **Same idempotence.** v2 inherits the fold's
  replay-safety (ADR-002) and the per-class
  Redis retention (ADR-057 §4.11.6) for the
  process events.

### Negative

- **Migration cost for projects already on v1.** A
  v1 project that imports `fmh_office.catalog.roles`
  must convert each `Role` to a `RoleComponent` and
  each `Role.execute` to an event-driven handoff.
  The deprecation window is one minor cycle (per
  AGENTS.md §7).
- **One more system to register.** v2 introduces
  `RoleComponent.SwitcherSystem` (3.1) which v1
  did not have; users wire it explicitly in
  `main.py`. The CLI scaffold emits the wiring
  for them, but the count of systems per project
  grows by one.
- **Rule grammar split.** v1 had `goto` /
  `require_approval` / `block` as return values of
  `RuleSet.evaluate`. v2 has them as event payloads
  (`*.rule_matched` carrying the decision in
  `data`). Consumers that previously polled
  `Decision.goto` must now subscribe to the event.
  This is consistent with ADR-036 (full payload
  fan-out), but it is a behavioural change.

### Watch-outs

- **Stackability of role systems.** v2 stacks
  `RuleBasedChatSystem` before `ChatRoleSystem`
  (ADR-049); the order is significant. The CLI
  scaffold emits the order; users adding more role
  systems must respect the convention.
- **`handoff_targets` ACL on `RoleComponent`.** The
  `RoleComponent.SwitcherSystem` rejects a handoff
  if the destination's `role_name` is not in the
  source's `RoleComponent.handoff_targets` (semantic
  ACL, per §2.0). v1 did not have this check; v2
  emits `intent.validation_failed` and the process is
  blocked. Projects migrating from v1 must audit
  their YAML for handoffs that previously "just
  worked".
- **`BusinessProcessState` field drift.** v1 had
  mutable fields; v2 has `frozen=True, slots=True`
  dataclasses with `frozenset[str]` for
  `completed_steps`. Any project code that mutated
  the state in place must be rewritten to emit a
  Domain Event and let the fold re-derive the new
  state.

## 6.5 `kntgraph.agents` package — re-write over current primitives

The same "primitives live in the framework; verticals
consume them" argument that motivates §2–§5 applies
to the framework's own `agents/` package. After
ADR-039 / ADR-041 / ADR-043 / ADR-044 / ADR-047 /
ADR-049 / ADR-059, the `agents/` submodules carry a
mix of legacy and current surfaces:

| Submodule | Current state | Issue |
|---|---|---|
| `agents/__init__.py` | docstring says *"vertical agents sobre o framework FMH"* (legacy package name); references ADR-006 separation | Naming drift; ADR-006 is superseded (ADR-039) |
| `agents/tools/protocol.py` | re-exports from `kntgraph.tools.protocol` | Pure re-export; ADR-025 §3 says "vertical re-export is legitimate", but this submodule has no other content |
| `agents/tools/llm.py` | `LiteLLMToolWorker` + `LiteLLMTransportAdapter` (540 LOC) | Single-file kitchen sink; ADR-047 already identified 3 sub-Workers that could split out |
| `agents/tools/pii/` | `PiiRedactionTool` + level 1/2 helpers | Tool is current; level-3 is a placeholder per ADR-010 §2.5 |
| `agents/tools/capability.py` | `Capability` Protocol + helpers | Couples Tool + Role (legacy separation); ADR-039 made Role a pure component, so `Capability` is half-current |
| `agents/tools/arg_validation.py` | `SchemaValidationError` + `validate_args` | Pure helpers; ADR-025 §1 says these belong to the framework, not the vertical |
| `agents/config/llm.py` | `_LLMSettings`, `LLMConfig`, `CostBudget` | Re-implements Pydantic Settings; framework already has `BaseSettings` patterns |
| `agents/role_systems/` | `_BaseRoleSystem` + 5 concrete systems + `_rule_based.py` (ADR-049) | **Current**; docstring still says "legacy Roles" |
| `agents/role_systems/_prompts.py` | prompts + output models + `parse_role_output` | Mixes prompts (product config) with output schemas (domain contract) |
| `agents/memory/solutions/` | 7 modules; `SolutionExtractor`, `SolutionPromoter`, `SolutionPromotionBus`, etc. | Pipeline is current but **inverted**: extractor publishes, promoter promotes; should be a single pipeline class |
| `agents/memory/solution_lookup.py` | `SolutionLookupSystem` (read) | Current; ~290 LOC is on the edge of "too big for a system" |
| `agents/memory/solution_extractor.py`, `solution_review_publisher.py`, `solution_promoter.py` | 3 small systems wrapping the same candidate | 3 systems + `solution_projector.py` (adapter) = 4 modules where 1 would do |
| `agents/knowledge/solution_projector.py` | adapter to FalkorDB; not a `WorldSystem` | Class with `upsert` + `ensure_tool_nodes`; never participates in the dispatcher's tick |

The `agents/` package is half-migrated. The
`role_systems/` half follows the current pattern;
the `memory/solutions/` + `knowledge/` half still
ships the legacy "vertical owns the
orchestrator" shape. This ADR re-writes the
package over the current primitives, parallel to
§2–§5.

### 6.5.1 Target structure

```text
src/kntgraph/agents/
├── __init__.py            # canonical re-exports; no legacy doc
├── tools/
│   ├── __init__.py        # re-exports kntgraph.tools.protocol (kept; ADR-025)
│   ├── llm.py             # LiteLLMToolWorker (kept; ADR-043)
│   └── pii/
│       ├── __init__.py    # exports PiiRedactionTool
│       ├── _tool.py       # PiiRedactionTool (kept; ADR-010)
│       ├── _level1.py     # regex redaction (kept)
│       ├── _level2.py     # NER redaction (kept)
│       └── _level3.py     # NEW: GLiNER2 v1.5 task="pii" (was a placeholder)
├── memory/
│   ├── __init__.py        # re-exports kntgraph.core.components.memory (new home)
│   ├── role_systems/      # promoted from agents/role_systems/ (kept)
│   │   ├── __init__.py    # docstring updated; no "legacy Roles"
│   │   ├── _base.py       # _BaseRoleSystem (kept)
│   │   ├── _prompts.py    # split: prompts → _prompts.py; output schemas → _schemas.py
│   │   ├── _schemas.py    # NEW: ChatReply, Plan, Summary (output dataclasses)
│   │   ├── chat.py        # NEW: ChatRoleSystem (was __init__.py)
│   │   ├── planner.py     # NEW: PlannerRoleSystem (was __init__.py)
│   │   ├── summarizer.py  # NEW: SummarizerRoleSystem (was __init__.py)
│   │   ├── personalized.py # NEW: PersonalizedRoleSystem (was __init__.py)
│   │   └── rule_based.py  # RuleBasedChatSystem (kept; ADR-049)
│   └── solutions/
│       ├── __init__.py    # canonical re-exports
│       ├── pipeline.py    # NEW: SolutionPipeline (single class replacing 3 systems + adapter)
│       ├── _values.py     # Problem / Action / Outcome / SolutionCandidate (kept)
│       ├── _store.py      # NEW: SolutionStoreLike impl (InMemorySolutionStore; was in solution_lookup.py)
│       ├── _fingerprints.py  # kept
│       ├── _bus.py        # kept (renamed: internal)
│       └── _review.py     # NEW: human-review queue (was ReviewQueueLike in solution_review_publisher.py)
└── domain/                # NEW: vertical domain components
    ├── __init__.py
    └── components.py      # generic helpers (DecoratorStudio, PiiGateStudio)
```

The deprecation map:

| v1 path | v2 path | Reason |
|---|---|---|
| `agents.roles.*` (legacy `Role` class) | **removed in v1.0** per ADR-041 | ADR-041 already schedules this |
| `agents.knowledge.SolutionProjector` | `agents.memory.solutions.pipeline.SolutionPipeline.write_to_graph()` | Projector is no longer a top-level class; it's a method on the pipeline |
| `agents.memory.solution_lookup.SolutionLookupSystem` | `agents.memory.solutions.pipeline.SolutionPipeline.read_for_overlay()` | Read-side becomes a method too |
| `agents.memory.solution_extractor.SolutionExtractorSystem` | `agents.memory.solutions.pipeline.SolutionPipeline.extract()` | |
| `agents.memory.solution_review_publisher.SolutionReviewPublisherSystem` | `agents.memory.solutions._review.ReviewQueue.register()` (event-driven) | Review is a side-effect of `*.solution.extracted`, not its own system |
| `agents.memory.solution_promoter.SolutionPromoterSystem` | `agents.memory.solutions.pipeline.SolutionPipeline.promote()` | |
| `agents.tools.arg_validation` | `kntgraph.tools.arg_validation` (move to framework) | Pure helper; ADR-025 §1 |
| `agents.tools.capability.Capability` | **removed** (replaced by `RoleComponent.allowed_tools`) | ADR-039 makes the coupling redundant |

### 6.5.2 `SolutionPipeline` — the consolidation

The four "Solution" pieces (read, extract, review,
promote) collapse into one pipeline class that
implements both `WorldSystem` (for the read /
extract / promote cycle) **and** exposes a
side-effect method (for the review queue).

```python
class SolutionPipeline(ToolAwareSystem):
    """Single home for the Solution tier (ADR-010 / ADR-049).

    The pipeline composes the four operations:

      - ``extract`` — runs in ``__call__`` when a
        ``*.tool_call.completed`` event lands.
      - ``review`` — runs in ``__call__`` when the
        pipeline is in ``review`` mode and a
        ``*.solution.extracted`` event lands.
      - ``promote`` — runs in ``__call__`` when a
        ``*.solution.approved`` event lands.
      - ``read_for_overlay`` — runs in ``__call__`` for
        every new ``ToolCallRequest`` on the view;
        emits a synthetic ``tool.<name>.completed``
        with the cached payload.

    The pipeline owns one ``ReviewQueue`` (a Redis-backed
    pub/sub) and one ``SolutionStoreLike`` (FalkorDB or
    in-memory). It is the **only** class the user wires
    into ``main.py``; the four sub-systems disappear.
    """
```

Why this is better:

1. **One registration site.** The user wires one
   system. The four sub-systems required
   `dispatcher = ReactiveDispatcher(systems=[Extractor, Review, Promoter, Lookup])`
   and the order mattered.
2. **One source of state.** Today the
   `seen_request_event_ids` set in `SolutionLookupSystem`
   and the candidate counter in `SolutionExtractorSystem`
   are independent; v2 has one pipeline object with
   one `seen_request_event_ids` set.
3. **Same pattern as the rest of the framework.**
   The `ice_maintenance` agent (ADR-057 §4.2) is
   already a "one cyclic system owns the maintenance
   lifecycle" shape. v2 makes the Solutions follow
   the same convention.
4. **Same ACL story.** The pipeline is gated by
   `RoleComponent.allowed_tools` for the synthetic
   `tool.<name>.completed` emission, the same way
   `IntentResolutionSystem` is gated.

### 6.5.3 `_BaseRoleSystem` — current, but split

The `_base.py` file is current (ADR-039 / ADR-043 /
ADR-044 / ADR-049). What changes:

1. **Five concrete systems move out of `__init__.py`**
   into one-file-per-system (`chat.py`,
   `planner.py`, etc.). The `__init__.py` re-exports.
   Rationale: today `agents/role_systems/__init__.py`
   is 224 LOC, mixing five classes, six constants,
   three event-type constants, and prompts. Splitting
   one-file-per-system matches the convention in
   `agents/memory/solutions/` (one class per file).
2. **`_prompts.py` splits into `_prompts.py` (the
   prompts) and `_schemas.py` (the output
   dataclasses).** Today `CHAT_SYSTEM_PROMPT` lives
   next to `ChatReply`. The prompt is a product
   config (deployment-overridable in v2); the
   schema is a domain contract (always current).
3. **The `_persona_for_view` stub in
   `RuleBasedChatSystem` is fixed.** Today it returns
   `""`; v2 reads the persona from the
   `RoleComponent` (or from the
   `_RolePersonaComponent` if the user attached one).
4. **`PersonalizedRoleSystem` is aligned.** Today its
   `OUTPUT_MODEL = BaseModel` is inconsistent with the
   other three; v2 ships a typed `_PersonalizedReply`
   schema.
5. **Dead `_emit_chat_completion`** (in `_base.py` line
   236) is either wired into the LLM path's
   `_consume_pending_completions` (it currently is
   not — the comment says "shared between Chat and
   RuleBased" but only RuleBased calls it) or
   removed.

### 6.5.4 `agents.tools.capability` — removed

`Capability` was the v1 coupling between Tool and
Role (ADR-006 era). After ADR-039, the Role is a
pure `RoleComponent` and the Tool's ACL is enforced
by `ToolACL` (ADR-017) + `IntentResolutionSystem`.
`Capability` has no callers in the framework today;
the file is dead weight. v2 removes it.

### 6.5.5 `agents.tools.arg_validation` — moved to framework

`SchemaValidationError` and `validate_args` are pure
helpers — they have no vertical-specific knowledge.
Per ADR-025 §1 ("primitives live in the framework;
verticals consume them"), they move to
`kntgraph.tools.arg_validation`. The vertical keeps
a re-export for one minor cycle.

### 6.5.6 Deprecation schedule for `agents/`

| v1 path | v2 path | Removal target |
|---|---|---|
| `agents.role_systems` (the whole package) | `agents.memory.role_systems` | v0.11.0 |
| `agents.knowledge.SolutionProjector` | `agents.memory.solutions.pipeline.SolutionPipeline.write_to_graph()` | v0.11.0 |
| `agents.memory.solution_lookup.SolutionLookupSystem` | `agents.memory.solutions.pipeline.SolutionPipeline` | v0.11.0 |
| `agents.memory.solution_extractor.SolutionExtractorSystem` | `agents.memory.solutions.pipeline.SolutionPipeline.extract()` | v0.11.0 |
| `agents.memory.solution_review_publisher.SolutionReviewPublisherSystem` | `agents.memory.solutions._review.ReviewQueue` | v0.11.0 |
| `agents.memory.solution_promoter.SolutionPromoterSystem` | `agents.memory.solutions.pipeline.SolutionPipeline.promote()` | v0.11.0 |
| `agents.tools.arg_validation` | `kntgraph.tools.arg_validation` (framework) | v0.11.0 |
| `agents.tools.capability.Capability` | **removed** (replaced by `RoleComponent.allowed_tools`) | v0.11.0 |
| `agents.config.llm._LLMSettings.LLMConfig` | `kntgraph.config.llm.LLMConfig` (framework; same BaseSettings pattern) | v0.11.0 |
| `security.principal.Role` enum (`Role.agent`, `Role.admin`, `Role.service`) | `PrincipalLevel` enum (`PrincipalLevel.agent`, `PrincipalLevel.admin`, `PrincipalLevel.service`) | introduced v0.10; deprecated v0.11; removed v1.0 |
| `agents.__init__.py` docstring | rewritten; mentions `kntgraph` not `fmh_*` | this PR |

The v0.10 cycle ships the v2 packages under their
new paths; v1 paths emit `DeprecationWarning` at
import time (same pattern as ADR-041). The CLI
scaffold emits v2 wiring by default.

### 6.5.7 Why this is **not** a refactor

The reframing question is: "isn't this just moving
files around?" Two reasons it is not:

1. **`SolutionProjector` becomes a method, not a
   class.** Today the projector is a top-level
   object the user instantiates; v2 the write to
   graph is a method on the pipeline. The class
   boundary disappears because the four pieces
   share state (the candidate counter, the
   `seen_request_event_ids` set, the review queue
   handle) that has no reason to be split across
   three `__init__.py`s.
2. **`RoleComponent.SwitcherSystem` (3.1) is new
   surface.** The handoff gate did not exist
   before; v2 introduces it as the framework's
   cross-role handoff primitive. fmh_office and
   any other multi-role vertical consume it.

The agents/ rewrite and the fmh_office v2 pillars
land together in v0.10. They are the same change
under two names.

## 7. Open questions

1. **Approval flow.** v1 had `require_approval role=gerente`
   in the YAML. v2's `RoleComponent.SwitcherSystem`
   is the gate, but the **human approval** path
   (HTTP, dashboard, or external service) is not in
   scope for this ADR. ADR-048 (`Visibility Dashboard`)
   covers the operator-facing surface; the integration
   point (how the dashboard signals
   `*.step.approved`) is a follow-up.
2. **`*.rule_matched` ordering.** When multiple rules
   match a single event, the v1 grammar took the
   first match (by YAML order). v2 emits one
   `*.rule_matched` per match; the consumer must
   decide. The convention is "first match wins",
   but the framework does not enforce it. Tracked
   as a follow-up if/when ordering matters in
   practice.
3. **Process versioning.** v1's `ProcessModel`
   carried a `version` field. v2 reads it from the
   YAML header but does not act on it. A future
   revision may require explicit version handling
   when a project's `process/<name>.yaml` changes
   under it; tracked alongside ADR-051 (release
   versioning).
4. **`fmh_office.v2` namespace vs. in-place upgrade.**
   This ADR proposes shipping v2 under
   `fmh_office.v2` for v0.10 and replacing the
   import path in v0.11. The alternative — deleting
   v1 immediately on v2 land — violates AGENTS.md
   §7 (one-minor-cycle deprecation). The choice is
   documented for the reader; not revisited here.
