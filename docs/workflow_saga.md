<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# WorkflowSaga — orchestrate a sequence of tool calls (ADR-069 §4)

A **WorkflowSaga** orchestrates a sequence of tool calls with
context enrichment, skip conditions, failure policies, and
compensation. It is built entirely on top of the framework's
tool-call primitives (ADR-034 tool calls, ADR-045 TTL, ADR-042
memory) and does **not** reinvent the tool lifecycle.

This is the C-02 Concordo of ADR-069. It composes with the
BusinessFSM (C-01): the Saga drives execution; upon completion it
emits an event that the FSM uses to advance state.

> **Status**: implemented. The `concordos.saga` package ships the
> `WorkflowSagaConcordo`, `SagaConfig`, `SagaStepConfig`,
> `SagaSystem`, `SagaTimeoutSystem`, `SagaProjection`, and
> `SagaProgressComponent`.

---

## 1. What you get

- **Ordered steps** — a saga is a sequence of `SagaStepConfig`s,
  each dispatching a `@tool_worker` (ADR-036).
- **Context enrichment** — a step's tool params are enriched from
  the previous step's result and the `ContinuityComponent`.
- **Skip conditions** — a step whose `skip_when` is satisfied is
  skipped (not dispatched).
- **Failure policies** — `fail_when` decides whether a step failure
  fails the saga or continues.
- **Compensation** — a failed saga rolls back the completed steps
  via their `compensate_tool` (LIFO).
- **Timeouts** — a saga-level deadline (`saga_timeout_ms`) and
  per-step approval timeouts for human steps.
- **Human steps** — a step with `tool_name=None` blocks until an
  external `saga.<step>.approved` / `rejected` event arrives.
- **Determinism** — the system is pure: the same `World` produces
  the same `list[Event]`. `now` is injected so a replayed log
  re-evaluates guards / timeouts with the same timestamp.

---

## 2. Concepts

### 2.1 The saga is a pure reactive system

The `SagaSystem` reads the post-fold `World`. For every agent whose
archetype carries a `SagaProgressComponent`, it walks the agent's
recent events and reacts to:

- `saga.<name>.started` → dispatch the first step.
- `tool.<name>.completed` → advance or compensate.
- `tool.<name>.failed` → evaluate `fail_when`; compensate.
- `tool.<name>.timed_out` → treat as failed (ADR-045).
- `saga.<name>.timed_out` → force compensation (`SagaTimeoutSystem`).
- `saga.<name>.compensation_failed` → DLQ + alert.

### 2.2 The trigger is derived from the view

Like the FSM, the saga derives its trigger from the existing view
fields (ADR-069 §11.16): `view.domain_phase` (the last domain
event's type), `view.last_event_id`, `view.components[domain_phase]`,
and `correlation_middleware.current()`.

### 2.3 Tool completions come from the tool-call overlay

The saga reads `ToolCallCompletion` from the `tool_completions` slot
(already materialised by the tool-call overlay, ADR-034). It does
**not** re-implement in-flight or resolution tracking.

### 2.4 Progress is materialised by the `SagaProjection`

The `SagaProgressComponent` (execution state) is materialised from
the saga events by the `SagaProjection` (ADR-069 §9.2 item 6). It
reconstructs `step_states` / `step_results` from the event
snapshots, `compensate_stack` from the config (LIFO), `direction`,
and `current_step`. A re-fold of the EventLog reconstructs the same
progress without an in-memory cache.

---

## 3. Configuration

### 3.1 `SagaConfig`

```python
from kntgraph.concordos.saga import SagaConfig, SagaStepConfig

config = SagaConfig(
    name="nfe_emission",
    saga_timeout_ms=300_000,          # 5 minutes total
    fail_when=None,                    # None = fail on first step failure
    steps=(
        SagaStepConfig(
            name="validate_fiscal",
            tool_name="sefaz_validator",
            timeout_ms=10_000,
        ),
        SagaStepConfig(
            name="emit_nfe",
            tool_name="nfe_emitter",
            compensate_tool="nfe_canceller",
            compensate_when=StepTimedOut("emit_nfe").not_(),
            enrich_from=("cfop", "tax_amount"),
            timeout_ms=30_000,
        ),
        SagaStepConfig(
            name="register_receivable",
            tool_name="erp_receivable_tool",
            compensate_tool="erp_reversal_tool",
            timeout_ms=15_000,
        ),
    ),
)
```

- `name` — unique saga identifier.
- `steps` — ordered tuple of step configs.
- `fail_when` — `Specification` evaluated after every step failure;
  the saga fails when satisfied. `None` = fail on the first step
  failure.
- `saga_timeout_ms` — wall-clock timeout for the entire saga,
  enforced by `SagaTimeoutSystem`.

### 3.2 `SagaStepConfig`

| Field | Meaning |
|---|---|
| `name` | unique step identifier within the saga |
| `tool_name` | registered `@tool_worker` name; `None` = human step |
| `compensate_tool` | `@tool_worker` called on rollback; `None` = no compensable effect |
| `skip_when` | `Specification`; step is skipped when satisfied |
| `proceed_when` | `Specification` evaluated after completion; if not satisfied, treated as failure |
| `compensate_when` | `Specification` evaluated when deciding to compensate; `None` = always compensate |
| `enrich_from` | field names to inject into tool params before dispatch |
| `timeout_ms` | step-level timeout (ADR-045 TTL); ignored for human steps |
| `approval_timeout_ms` | per-step timeout for a human step waiting for approval; `None` disables |

---

## 4. Emitted events

| Event | When |
|---|---|
| `saga.<name>.started` | the saga begins (external trigger) |
| `saga.<name>.step_started` | a step is dispatched |
| `saga.<name>.step_completed` | a step completed (carries `step_states` / `step_results`) |
| `saga.<name>.step_failed` | a step failed (carries `step_states` / `step_results`) |
| `saga.<name>.compensating` | the saga is rolling back |
| `saga.<name>.completed` | the saga finished forward |
| `saga.<name>.timed_out` | the saga exceeded its deadline |
| `saga.<name>.<step>.awaiting_approval` | a human step is waiting for approval |
| `saga.<name>.<step>.approval_timed_out` | a human step exceeded its approval timeout |
| `saga.<name>.compensation_failed` | a compensation tool failed |
| `saga.<name>.dlq` | the saga routes to the DLQ |

---

## 5. Wiring it up

### 5.1 As a Concordo

```python
from kntgraph.concordos.saga import WorkflowSagaConcordo

nfe_emission_saga = WorkflowSagaConcordo(config)
```

`install(dispatcher)` registers the `SagaSystem`, the
`SagaTimeoutSystem`, and the `SagaProjection` on a
`ReactiveDispatcher`. Use `ConcordoCatalog` to install several
Concordos idempotently:

```python
from kntgraph.concordos import ConcordoCatalog

catalog = ConcordoCatalog(nfe_emission_saga, invoice_fsm)
catalog.install_all(dispatcher)
```

### 5.2 Directly as systems + projection

```python
from kntgraph.concordos.saga import SagaSystem, SagaTimeoutSystem, SagaProjection

dispatcher.add_system(SagaSystem(config))
dispatcher.add_system(SagaTimeoutSystem({config.name: config}))
dispatcher.add_projection(SagaProjection(config))
```

---

## 6. Testing

The saga is pure, so it is tested with the SUT builders in
`kntgraph.testing` — no Redis, no dispatcher, no mocks:

```python
from kntgraph.concordos.saga import SagaSystem
from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system

view = (
    AgentViewBuilder("agent-1")
    .with_component(_progress(current_step="validate_fiscal"))
    .with_trigger("saga.nfe_emission.started", data={"saga_id": "saga-001"})
    .build()
)
world = WorldBuilder().with_agent(view).build()
out = run_system(SagaSystem(config, now=lambda: FIXED_NOW), world)
assert any(e.event_type == "tool.sefaz_validator.requested" for e in out)
```

`AgentViewBuilder.with_tool_request` / `with_tool_completion` attach
the tool-call slots the saga reads to match a completion to a step.

---

## 7. Worked example

See [`examples/24_workflow_saga.py`](../../examples/24_workflow_saga.py)
for a runnable five-scenario walkthrough (start → dispatch, advance,
completion, compensation, projection):

```bash
KNT_REDIS_FAKE=1 uv run python examples/24_workflow_saga.py
```

---

## 8. See also

- [ADR-069 §4](../../ADRs/ADR-069-Agent-Concordo-Macro-Behaviors.md) —
  the WorkflowSaga design record.
- [ADR-069 §2](../../ADRs/ADR-069-Agent-Concordo-Macro-Behaviors.md) —
  the Specification Pattern.
- [BusinessFSM](business_fsm.md) — the state machine Concordo (C-01).
- [Tools](tools.md) — the `@tool_worker` pattern (ADR-036).
- [ECS](ecs.md) — `World`, `AgentView`, `WorldSystem`.
- [DEBT §2.34](../../DEBT.md) — the ADR-069 follow-up tracker
  (items 1, 2, 3, 6 closed).
