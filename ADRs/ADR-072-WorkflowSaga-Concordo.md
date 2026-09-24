<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-072: WorkflowSaga Concordo (C-02)

- **Status:** Accepted
- **Date:** 2026-09-07 (revised 2026-09-12)
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-069](./ADR-069-Agent-Concordo-Foundation.md) — Concordo Foundation
  - [ADR-001](./ADR-001-Arquitetura.md) — pure ECS, Event Sourcing
  - [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md) — `WorldSystem`
  - [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — `ToolCallRequest` / `ToolCallCompletion`
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — `@tool_worker` + `WorkerManager`
  - [ADR-045](./ADR-045-Tool-Call-Request-TTL.md) — Tool Call TTL
  - [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) — `DomainComponent`
  - [ADR-068](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md) — wakeup stream
  - [ADR-073](./ADR-073-Concordo-Bundle-Format.md) — Bundle Format
  - [ADR-074](./ADR-074-Per-System-Cursors-in-AgentView.md) — Per-system cursors (potential consumer if multi-event tick becomes a concern)

---

## 1. Context

A **WorkflowSaga** orchestrates a **sequence of tool
calls** with context enrichment, skip conditions,
failure policies, and compensation. It is built
entirely on top of existing framework primitives —
`ToolCallRequest` / `ToolCallCompletion` (ADR-034),
the per-call TTL (ADR-045), and the dispatcher (ADR-018).

The Saga is **not** a reinvented workflow engine. It is
a thin orchestration layer that:

- Reads `ToolCallCompletion` from the agent's view
  (the dispatcher already materialises this via the
  tool overlay).
- Reads `SagaProgressComponent` (its own component)
  for the current step.
- Emits `tool.<name>.requested` to dispatch steps.
- Emits `saga.<name>.{started, step_started,
  step_completed, step_failed, compensating,
  compensation_started, compensated, compensation_failed,
  timed_out, completed, dlq}` for downstream
  observability and business systems.

### 1.1 Critical design principle

The Saga does **not** reinvent the tool call lifecycle.
It is built entirely on top of existing framework
primitives:

| Lifecycle aspect | Existing primitive | Saga's role |
|---|---|---|
| In-flight tracking | `ToolCallRequest` (ADR-034) | None — reads from AgentView |
| Resolution tracking | `ToolCallCompletion` (ADR-034) | Reacts to `.completed` / `.failed` |
| Step timeout | `tool.<name>.timed_out` (ADR-045) | Treats `timed_out` as failure |
| Saga-level timeout | None | **New: `SagaTimeoutSystem` WorldSystem** |
| Context enrichment | `ContinuityComponent` (ADR-042) | Reads before dispatching each step |
| Sequencing + compensation | None | **New: `SagaProgressComponent` + `SagaSystem`** |

---

## 2. What you get

A `WorkflowSagaConcordo` is a frozen bundle (§4.7) that
exposes:

- **Ordered steps** — a saga is a sequence of
  `SagaStepConfig`s, each dispatching a `@tool_worker`
  (ADR-036).
- **Context enrichment** — a step's tool params are
  enriched from previous step results
  (`steps.<name>.output.*`) and from the trigger
  event (`event.data.*`).
- **Skip conditions** — a step declares `skip_when`;
  when satisfied, the step is skipped (not dispatched).
- **Failure policies** — the saga declares `fail_when`;
  when satisfied after a step failure, the saga
  compensates instead of moving forward.
- **Compensation** — each step can declare a
  `compensate_tool` invoked in LIFO order on rollback.
- **Crash-safety** — granular events
  (`compensation_started`, `compensated`) record
  per-step compensation progress so a crash mid-
  compensation is recoverable.
- **DLQ integration** — on permanent compensation
  failure, the saga emits
  `saga.<name>.compensation_failed`; the application
  wires DLQ ingestion via `dispatcher.subscribe`
  (ADR-069 §5.2).

---

## 3. Design

### 3.1 Supervision model

The Saga requires three supervision layers:

```
Layer 1 — Step timeout (ADR-045 already covers this):
  tool.<name>.requested emitted
  → TTL clock starts (ADR-045)
  → if no completion within step TTL:
      tool.<name>.timed_out emitted
  → SagaSystem reacts: treats as step failure

Layer 2 — Saga-level timeout (Saga adds this):
  saga.<name>.started emitted
  → SagaTimeoutSystem (WorldSystem) scans every tick
  → if elapsed > saga_timeout_ms:
      saga.<name>.timed_out emitted
  → SagaSystem reacts: initiates compensation

Layer 3 — Agent liveness (Runner already covers this):
  existing CyclicSystem in the Runner
  → detects agents stuck without events
  → outside Saga scope
```

### 3.2 Configuration

```python
@dataclass(frozen=True, slots=True)
class SagaStepConfig:
    """Configuration for a single saga step."""
    name: str
    tool_name: str | None
    compensate_tool: str | None = None
    skip_when: "Specification | None" = None
    proceed_when: "Specification | None" = None
    compensate_when: "Specification | None" = None
    enrich_from: tuple[str, ...] = ()
    timeout_ms: int = 30_000
    approval_timeout_ms: int | None = None  # human steps only

    def __post_init__(self) -> None:
        if self.tool_name is not None and not self.tool_name:
            raise ValueError(
                f"SagaStepConfig.tool_name must be a non-empty "
                f"string or None (human step); got empty string "
                f"for step {self.name!r}."
            )


@dataclass(frozen=True, slots=True)
class SagaConfig:
    """Configuration for a WorkflowSaga."""
    name: str
    steps: tuple[SagaStepConfig, ...]
    fail_when: "Specification | None" = None
    saga_timeout_ms: int = 300_000
```

The default for `fail_when` (when omitted) is **fail
on the first failure of any step**. A "best-effort"
saga (continue past failures) is expressed by setting
`fail_when` to a Specification that is never
satisfied, or to one that gates on a specific step.

A "REQUIRED step" concept was proposed in an earlier
draft but never defined as a step attribute. The
correct default is "fail on the first failure of any
step" — what `fail_when=None` means in the
implementation.

### 3.3 ECS Component

```python
@dataclass(frozen=True, slots=True)
class SagaProgressComponent(DomainComponent):
    """
    Saga execution state.

    *Execution* fields (``saga_id``, ``saga_name``,
    ``current_step``, ``direction``, ``started_at``)
    are written by saga events; the saga-system
    projection materialises them.

    *History* fields (``step_states``, ``step_results``,
    ``compensate_stack``) are derived from the
    EventLog via a fold projection. They are cached
    here for hot-path reads; on re-fold the
    projection reconstructs them deterministically.

    ``awaiting_approval_at`` records when a human step
    entered ``awaiting_approval``, used by the
    per-step approval timeout (§3.5.3).

    Archetype evolution:
      Running:     {SagaProgressComponent}
      Completed:   {SagaProgressComponent}  (direction="done")
      Compensated: {SagaProgressComponent}  (direction="compensated")
      Failed:      {SagaProgressComponent}  (direction="compensation_failed")
    """
    saga_id: str
    saga_name: str
    current_step: str
    direction: str  # forward | compensating | done | compensated |
                   # compensation_failed
    step_order: tuple[str, ...]
    step_states: MappingProxyType[str, str]
    step_results: MappingProxyType[str, "JsonValue"]
    compensate_stack: tuple[str, ...]
    started_at: datetime
    awaiting_approval_at: MappingProxyType[str, datetime] = (
        MappingProxyType({})
    )
```

### 3.4 WorldSystem — `SagaSystem`

`SagaSystem` orchestrates the steps. It is a pure
WorldSystem (ADR-018): same `World` ⇒ same
`list[Event]`.

For every agent whose archetype carries a
`SagaProgressComponent`, `SagaSystem` reads the
trigger and reacts to:

- `saga.<name>.started` → dispatch first step
- `tool.<name>.completed` → advance or compensate
- `tool.<name>.failed` → evaluate `fail_when`;
  compensate or continue
- `tool.<name>.timed_out` → treat as failure (ADR-045)
- `saga.<name>.timed_out` → force compensation
  (SagaTimeoutSystem, §3.5)
- `saga.<name>.compensation_failed` → escalate to DLQ

`SagaSystem` reads `ToolCallCompletion` from the
`tool_completions` slot (already materialised by
`project_tool_calls`, ADR-034). It does NOT
re-implement in-flight or resolution tracking.

#### 3.4.1 Step matching

The join key between a saga step and an incoming
tool completion is the step currently in flight
(`saga.current_step`). When `tool.<step.tool_name>.completed`
arrives, the saga finds the matching step via
`current_step`. The completion's `request_event_id`
joins against the `tool_requests` slot to look up the
result.

#### 3.4.2 Skip and proceed conditions

- `skip_when` is evaluated BEFORE dispatch. A step
  with satisfied `skip_when` is recorded as
  `step_states[name] = "skipped"` and the saga moves
  to the next step.
- `proceed_when` is evaluated AFTER a successful
  completion. If unsatisfied, the step is treated as
  failed (the saga does not advance).

#### 3.4.3 Enrichment

`enrich_from` is a tuple of field names. The saga
resolves each field via the `event.data` of the trigger
and the `step_results` of previous steps. The current
implementation searches `step_results[*]` for the
named field and uses `params.setdefault(...)` so
later wins (or stays unset if no source provides it).

The bundle format (ADR-073) extends this with explicit
`input_mapping: {param_name: path}` for clarity.

#### 3.4.4 Compensation

When `fail_when` is satisfied (or `fail_when=None`
and any step fails), the saga enters
`direction="compensating"`. For each step on the
`compensate_stack` (in LIFO order), if
`compensate_when` is unsatisfied the saga skips it
and emits `tool.<compensate_tool>.requested`
otherwise.

#### 3.4.5 Crash-safety (granular events)

The `compensate_stack` is a cache, not the source of
truth. The source of truth is the EventLog via two
**granular events**:

- `saga.<name>.<step>.compensation_started` — emitted
  when the saga system dispatches the compensation
  tool. Carries `{"step_name": "emit_nfe"}`.
- `saga.<name>.<step>.compensated` — emitted when
  the matching `tool.<compensate_tool>.completed`
  arrives. Carries `{"step_name": "emit_nfe"}` and is
  causally linked to `compensation_started`.

The fold projection reads the sequence of these
events and reconstructs the compensated-step list
from the EventLog alone. A crash between
`compensation_started` and `compensated` is
recovered by re-dispatching the compensation on the
next tick (the worker's compensation is idempotent
because the saga emits the same `compensate_when`
context on replay).

Without these granular events the `compensate_stack`
field is necessary but not sufficient: it is a cache
that helps the system avoid re-scanning the log, but
the log is the only authoritative record.

#### 3.4.6 Event emission

All events use `Event.domain_from(...)`
(ADR-069 §5.3):

```python
Event.domain_from(
    agent_id=trigger.agent_id,
    type=f"saga.{self._cfg.name}.step_completed",
    data={"step_name": ..., "step_states": ..., ...},
    causation_id=trigger.event_id,
    correlation=trigger.correlation,
)
```

### 3.5 WorldSystem — `SagaTimeoutSystem`

`SagaTimeoutSystem` runs on every dispatcher tick and
detects sagas that have exceeded `saga_timeout_ms`. It
emits `saga.<name>.timed_out`. The emitted event's
`event_id` is **deterministic** — derived from
**stable fields only** so a tick that re-derives the
same timeout produces the same `event_id` and is
deduped by the EventLog.

```python
@dataclass(frozen=True, slots=True)
class SagaTimeoutSystem:
    """
    Runs on every dispatcher tick. Detects sagas that
    have exceeded ``saga_timeout_ms`` and emits
    ``saga.<name>.timed_out``.

    **Determinism.** The emitted event's ``event_id``
    is computed by ``generate_deterministic_event_id``
    from stable fields (``saga_id``, ``saga_name``,
    ``stuck_at_step``, ``timeout_ms``). ``elapsed_ms``
    is reported for observability but is NOT part of
    the hash envelope: it grows on every tick and
    would defeat idempotency. (Earlier drafts placed
    ``elapsed_ms`` inside the hash; that was wrong.)
    """

    def __init__(
        self,
        configs: "Mapping[str, SagaConfig]",
        *,
        now: "Clock | None" = None,
    ) -> None:
        self._configs = configs
        self._now = injectable_clock(now)

    def __call__(self, world: "World") -> list["Event"]:
        now = self._now()
        out: list[Event] = []
        for view in world.query_agents(SagaProgressComponent):
            saga = view.get_component(SagaProgressComponent)
            if saga is None or saga.direction != "forward":
                continue
            config = self._configs.get(saga.saga_name)
            if config is None:
                continue
            elapsed_ms = (now - saga.started_at).total_seconds() * 1000
            if elapsed_ms <= config.saga_timeout_ms:
                continue
            # Hash envelope (stable, idempotent)
            hash_data = {
                "saga_id": saga.saga_id,
                "saga_name": saga.saga_name,
                "stuck_at_step": saga.current_step,
                "timeout_ms": config.saga_timeout_ms,
            }
            # Wire envelope (per-tick observability)
            data = {**hash_data, "elapsed_ms": elapsed_ms}
            event_type = f"saga.{saga.saga_name}.timed_out"
            eid = generate_deterministic_event_id(
                causation_id="root",
                event_type=event_type,
                data=hash_data,
                agent_id=view.agent_id,
            )
            correlation = correlation_middleware.current()
            out.append(Event.domain_from(
                event_id=eid,
                agent_id=view.agent_id,
                type=event_type,
                data=data,
                correlation=correlation,
            ))
        return out
```

#### 3.5.1 Per-step approval timeout (human steps)

A human step (`tool_name is None`) declares
`approval_timeout_ms`. While the step is in
`awaiting_approval`, the timeout is measured against
`awaiting_approval_at[step_name]` (set by the
projection when the saga emits
`saga.<name>.<step>.awaiting_approval`). When the
deadline expires, `SagaTimeoutSystem` emits
`saga.<name>.<step>.approval_timed_out`. The
projection materialises these events and the
SagaSystem reacts on the next tick (treat as failure).

#### 3.5.2 Cross-bundle and multi-saga dispatchers

The `SagaTimeoutSystem` accepts a `Mapping[saga_name,
SagaConfig]` so multiple sagas share one sweeper. The
dispatcher iterates all tracked agents and looks up
the saga's config by `saga.saga_name`.

### 3.6 DLQ integration

The Saga does **not** ship a saga-specific DLQ
adapter. When a compensation fails permanently, the
saga emits `saga.<name>.compensation_failed`. The
application wires DLQ ingestion via
`dispatcher.subscribe` (ADR-069 §5.2):

```python
async def ingest_compensation_failures(event):
    if not event.event_type.endswith(".compensation_failed"):
        return
    dl_event = DeadLetterEvent(
        event=event,
        reason=DLQReason.PROCESSING_FAILED,
        error_message="compensation_failed",
        original_timestamp=event.timestamp,
        dlq_timestamp=datetime.now(tz=timezone.utc),
        metadata=event.data,
    )
    await dlq.append(dl_event)
```

The pattern matches the framework's convention for
tool-call TTL failures (`ToolCallTTLSweeperSystem`).

### 3.7 Concordo class

```python
@dataclass(frozen=True, slots=True)
class WorkflowSagaConcordo:
    """C-02: WorkflowSaga Concordo bundle (ADR-069 §3)."""

    config: SagaConfig
    name: str = field(init=False)
    systems: tuple["WorldSystem", ...] = field(init=False)
    projections: tuple["WorldProjection", ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", f"saga:{self.config.name}")
        object.__setattr__(
            self, "systems",
            (
                SagaSystem(self.config),
                SagaTimeoutSystem({self.config.name: self.config}),
            ),
        )
        object.__setattr__(
            self, "projections", (SagaProjection(self.config),)
        )
```

DLQ ingestion is NOT wired here. The application
decides whether to forward
`saga.<name>.compensation_failed` events to the
framework's `DeadLetterQueue` by registering a
`dispatcher.subscribe` callback.

### 3.8 Example — NF-e emission saga

```python
nfe_emission_saga = WorkflowSagaConcordo(SagaConfig(
    name="nfe_emission",
    saga_timeout_ms=300_000,  # 5 minutes total

    # Fail the saga when ANY emission path failed.
    # Earlier drafts composed
    # ``StepFailed("emit_nfe").and_(StepFailed("emit_nfce"))`` —
    # but the two steps are mutually exclusive at
    # runtime (one is skipped via
    # ``skip_when=NfeRequired().not_()`` exactly when
    # the other runs), so the AND was logically
    # unreachable. A failing saga must be triggered
    # by EITHER branch.
    fail_when=StepFailed("emit_nfe").or_(StepFailed("emit_nfce")),

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
            skip_when=NfeRequired().not_(),
            compensate_when=StepTimedOut("emit_nfe").not_(),
            enrich_from=("cfop", "tax_amount", "series"),
            timeout_ms=30_000,
        ),
        SagaStepConfig(
            name="emit_nfce",
            tool_name="nfce_emitter",
            compensate_tool="nfce_canceller",
            skip_when=TaxRegimeIs("simples").not_(),
            compensate_when=StepTimedOut("emit_nfce").not_(),
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
))
```

### 3.9 Unit tests

```python
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system


FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def test_saga_dispatches_first_step_on_start() -> None:
    """
    Given:  SagaProgressComponent set, trigger is
            saga.nfe_emission.started.
    Then:   tool.sefaz_validator.requested is emitted.
    """
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            SagaProgressComponent(
                saga_id="saga-001",
                saga_name="nfe_emission",
                current_step="validate_fiscal",
                direction="forward",
                step_order=("validate_fiscal", "emit_nfe"),
                step_states=MappingProxyType({"validate_fiscal": "pending"}),
                step_results=MappingProxyType({}),
                compensate_stack=(),
                started_at=FIXED_NOW,
            )
        )
        .with_trigger(
            "saga.nfe_emission.started", data={"saga_id": "saga-001"}
        )
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(
        SagaSystem(nfe_emission_saga.config, now=lambda: FIXED_NOW), world
    )
    assert any(
        e.event_type == "tool.sefaz_validator.requested" for e in out
    )


def test_saga_skips_nfe_when_not_required() -> None:
    """validate_fiscal completed with nfe_required=False → no nfe_emitter."""
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            SagaProgressComponent(
                saga_id="saga-001",
                saga_name="nfe_emission",
                current_step="emit_nfe",
                direction="forward",
                step_order=("validate_fiscal", "emit_nfe"),
                step_states=MappingProxyType(
                    {"validate_fiscal": "completed",
                     "emit_nfe": "in_flight"}
                ),
                step_results=MappingProxyType({
                    "validate_fiscal": {"nfe_required": False},
                }),
                compensate_stack=(),
                started_at=FIXED_NOW,
            )
        )
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                tool_name="sefaz_validator",
                status="completed",
                result={"nfe_required": False},
            ),
        )
        .with_trigger("tool.sefaz_validator.completed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(
        SagaSystem(nfe_emission_saga.config, now=lambda: FIXED_NOW), world
    )
    assert not any(
        e.event_type == "tool.nfe_emitter.requested" for e in out
    )


def test_saga_compensates_on_timeout_except_timed_out_steps() -> None:
    """emit_nfe timed out → no nfe_canceller dispatched."""
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            SagaProgressComponent(
                saga_id="saga-001",
                saga_name="nfe_emission",
                current_step="emit_nfe",
                direction="forward",
                step_order=("validate_fiscal", "emit_nfe"),
                step_states=MappingProxyType(
                    {"validate_fiscal": "completed",
                     "emit_nfe": "timed_out"}
                ),
                step_results=MappingProxyType({
                    "validate_fiscal": {"nfe_required": True},
                    "emit_nfe": {},
                }),
                compensate_stack=("validate_fiscal", "emit_nfe"),
                started_at=FIXED_NOW,
            )
        )
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                tool_name="nfe_emitter",
                status="timed_out",
                error="ttl_expired",
            ),
        )
        .with_trigger("tool.nfe_emitter.timed_out")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(
        SagaSystem(nfe_emission_saga.config, now=lambda: FIXED_NOW), world
    )
    assert not any(
        e.event_type == "tool.nfe_canceller.requested" for e in out
    )


def test_saga_timeout_system_emits_timed_out() -> None:
    """Started 6 minutes ago; timeout=5min → saga.<name>.timed_out."""
    past = FIXED_NOW - timedelta(minutes=6)
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            SagaProgressComponent(
                saga_id="saga-001",
                saga_name="nfe_emission",
                current_step="emit_nfe",
                direction="forward",
                step_order=("validate_fiscal", "emit_nfe"),
                step_states=MappingProxyType({"emit_nfe": "in_flight"}),
                step_results=MappingProxyType({}),
                compensate_stack=("validate_fiscal",),
                started_at=past,
            )
        )
        .with_trigger("saga.nfe_emission.started")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    system = SagaTimeoutSystem(
        {"nfe_emission": nfe_emission_saga.config},
        now=lambda: FIXED_NOW,
    )
    out = run_system(system, world)
    assert any(
        e.event_type == "saga.nfe_emission.timed_out" for e in out
    )


def test_saga_compensation_failure_emits_dlq() -> None:
    """direction=compensating + tool.nfe_canceller.failed → dlq."""
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            SagaProgressComponent(
                saga_id="saga-001",
                saga_name="nfe_emission",
                current_step="emit_nfe",
                direction="compensating",
                step_order=("validate_fiscal", "emit_nfe"),
                step_states=MappingProxyType(
                    {"emit_nfe": "compensation_failed"}
                ),
                step_results=MappingProxyType({}),
                compensate_stack=("emit_nfe",),
                started_at=FIXED_NOW,
            )
        )
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                tool_name="nfe_canceller",
                status="failed",
                error="se_faz_offline",
            ),
        )
        .with_trigger("tool.nfe_canceller.failed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(
        SagaSystem(nfe_emission_saga.config, now=lambda: FIXED_NOW), world
    )
    assert any(
        e.event_type == "saga.nfe_emission.dlq" for e in out
    )
```

---

## 4. Saga event vocabulary

| Event | When | Class |
|---|---|---|
| `saga.<name>.started` | Saga begins (initial trigger) | domain |
| `saga.<name>.step_started` | A step begins (dispatched) | domain |
| `saga.<name>.step_completed` | A step succeeded; carries `step_results` snapshot | domain |
| `saga.<name>.step_failed` | A step failed; carries `step_results` snapshot | domain |
| `saga.<name>.<step>.awaiting_approval` | Human step awaiting approval | domain |
| `saga.<name>.<step>.approved` | Human approval received | domain |
| `saga.<name>.<step>.rejected` | Human rejection received | domain |
| `saga.<name>.<step>.approval_timed_out` | Human approval timeout | domain |
| `saga.<name>.compensating` | Saga enters compensation; carries `reason` | domain |
| `saga.<name>.<step>.compensation_started` | Compensation tool dispatched | domain |
| `saga.<name>.<step>.compensated` | Compensation tool succeeded | domain |
| `saga.<name>.compensation_failed` | A compensation tool failed permanently | domain |
| `saga.<name>.timed_out` | Saga exceeded `saga_timeout_ms` | domain |
| `saga.<name>.completed` | Saga finished forward successfully | domain |
| `saga.<name>.dlq` | Compensation failed AND escalation requested | domain |

The `saga.<name>.dlq` event is **only** emitted when
the application has wired DLQ ingestion via
`dispatcher.subscribe`. Without the listener, the
event lands in the EventLog and is inspectable via
the standard log tools. The saga system does not
insert into the DLQ directly.

### 4.1 Crash-safe compensation vocabulary

The three compensation events form a sub-protocol that
makes the `compensate_stack` reconstructible from the
EventLog (ADR-072 §11.18.2; resolution to §11.10 / §11.18.2):

- `saga.<name>.compensating` -- the saga enters compensation
  (seed event). Marks `direction = "compensating"` and
  initialises `compensate_stack` from the config
  (steps with `compensate_tool` that completed, in
  LIFO order).
- `saga.<name>.<step>.compensation_started` -- a
  compensation tool was dispatched for ``<step>``.
  **Durable marker**: "I dispatched a compensation."
  Adds the step to `compensate_stack` (if not already
  there).
- `saga.<name>.<step>.compensated` -- the compensation
  tool succeeded. **Durable marker**: "this step is fully
  compensated." Removes the step from `compensate_stack`.

The projection reads these granular events to reconstruct
the **exact compensated-step list** from the EventLog
alone. A process crash between `compensation_started` and
`compensated` is recovered by re-dispatching on the next
tick (the worker receives the same `compensate_when`
context on replay). Without these events, the
`compensate_stack` field is a cache that might
disagree with the EventLog after a restart.

---

---

## 5. Source code layout

```
src/kntgraph/concordos/saga/
+-- __init__.py          # WorkflowSagaConcordo (public; frozen bundle)
+-- _config.py           # SagaConfig, SagaStepConfig
+-- _components.py       # SagaProgressComponent
+-- _state.py            # SagaProjection (fold projection;
│                        #   compensation_started/compensated handlers)
+-- _system.py           # SagaSystem (WorldSystem)
+-- _timeout_system.py   # SagaTimeoutSystem (WorldSystem)
```

The split keeps each file under the 500-line ceiling
(AGENTS.md §3.1). `_state.py` carries the
reconciliation helpers (~320 lines);
`_system.py` carries the WorldSystem (~735 lines —
**over the 500-line ceiling; PR 1 of the refactor
plan splits this further**).

---

## 6. Open questions

1. **Concurrency / re-entrancy.** If two
   `document.ingested` events arrive for the same
   agent in consecutive ticks, does the saga spawn
   two instances or queue? Currently undefined. To be
   addressed in a follow-up ADR (likely ADR-070 or
   a dedicated ADR-NNN).
2. **State lifecycle.** `SagaProgressComponent`
   stays on the agent forever after the saga
   finishes. Memory growth. GC strategy undefined.
3. **Operator recovery.** `saga.<name>.manual_resolved`
   / `retry_compensation` are deferred to ADR-070.
4. **Step ordering.** Saga steps are linear. A step
   dispatching multiple successors based on result
   is out of scope.

---

## 7. References

- Richardson, C. *Microservices Patterns*, 2018 — Chapter 4, Saga Pattern
- [ADR-001 — Pure ECS + Event Sourcing](./ADR-001-Arquitetura.md)
- [ADR-018 — WorldSystem + ReactiveDispatcher](./ADR-018-WorldIncremental-WorldSystem.md)
- [ADR-034 — ToolCall ECS Components](./ADR-034-ToolCall-ECS-Components.md)
- [ADR-036 — Tool Worker Pattern](./ADR-036-Tool-Worker-Pattern.md)
- [ADR-045 — Tool Call TTL](./ADR-045-Tool-Call-Request-TTL.md)
- [ADR-059 — Domain Memory ECS Components](./ADR-059-Domain-Memory-ECS-Components.md)
- [ADR-068 — Idle Redis traffic and EventLog subscribe](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md)
- [ADR-069 — Concordo Foundation](./ADR-069-Agent-Concordo-Foundation.md)
- [ADR-071 — BusinessFSM Concordo](./ADR-071-BusinessFSM-Concordo.md)
- [ADR-073 — Concordo Bundle Format](./ADR-073-Concordo-Bundle-Format.md)
- [docs/workflow_saga.md](../docs/workflow_saga.md) — implementation reference
