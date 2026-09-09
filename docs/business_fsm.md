<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# BusinessFSM — the pure state machine Concordo (ADR-069 §3)

A **BusinessFSM** (Business Finite State Machine) declares the
lifecycle of a business object as an explicit state machine over
a `DomainComponent`. It is a **pure reactive system**: no I/O, no
tool calls. It reacts to domain events, validates transitions, and
emits `fsm.transitioned` or `fsm.transition_rejected`. State is
carried by the `DomainComponent` — the FSM does not duplicate it.

This is the C-01 Concordo of ADR-069. It composes with the
WorkflowSaga (C-02): the Saga drives execution; upon completion it
emits an event that the FSM uses to advance state.

> **Status**: implemented. The `concordos.fsm` package ships the
> `BusinessFSMConcordo`, `FSMConfig`, `FSMTransition`, `FSMSystem`,
> and `FSMAuditComponent`.

---

## 1. What you get

- A typed, declarative state machine over a `DomainComponent`.
- **Guards** — each transition can carry a `Specification` that is
  evaluated before the transition is allowed.
- **On-entry events** — entering a state can emit a follow-up domain
  event.
- **Terminal states** — states from which no transition is allowed.
- **Audit** — `FSMAuditComponent` records the last transition for
  an agent.
- **Determinism** — the system is pure: the same `World` produces
  the same `list[Event]`. `now` is injected so a replayed log
  re-evaluates guards with the same timestamp.

---

## 2. Concepts

### 2.1 The FSM is pure

The FSM reads the `DomainComponent` state from the post-fold
`World`, scans every agent whose archetype carries the configured
component, and validates each incoming event against the declared
transition table. It emits events; it never performs I/O.

### 2.2 The trigger is derived from the view

The FSM does **not** subscribe to event types. Every tick it reads
the post-fold view and derives the trigger from the existing fields
(ADR-069 §11.16):

- `view.domain_phase` — the **type** of the most recent domain event.
- `view.last_event_id` — the **id** of that event (used as the
  `causation_id` of emitted events).
- `view.components[domain_phase]` — the **data** of that event.
- `correlation_middleware.current()` — the correlation for freshly
  emitted events.

### 2.3 State lives in the `DomainComponent`

The FSM does not introduce a new component for state. The
`state_field` attribute on the configured `DomainComponent` holds
the current state. The FSM reads it; the projection (ADR-059)
materialises the component from domain events.

> **Note (DEBT §2.34 item 1).** The FSM emits `fsm.transitioned`
> but the `DomainComponent` projection does not yet update
> `state_field` from that event. The state-advance wiring is an
> open item. Today the FSM is exercised as a pure guard/audit layer
> over a component whose state is set by the vertical's own domain
> events.

---

## 3. Configuration

### 3.1 `FSMConfig`

```python
from kntgraph.concordos.fsm import FSMConfig, FSMTransition

config = FSMConfig(
    component_type=InvoiceDomainComponent,   # the DomainComponent subclass
    state_field="status",                    # attribute holding the state
    transitions={
        "draft": {
            "invoice.submitted": FSMTransition(to="validating"),
        },
        "validating": {
            "invoice.approved": FSMTransition(
                to="issued",
                guard=ContinuityToolUsed("nfe_emitter").not_(),
            ),
            "invoice.rejected": FSMTransition(to="draft"),
        },
        "issued": {
            "payment.received": FSMTransition(to="paid"),
            "invoice.cancelled": FSMTransition(to="cancelled"),
        },
    },
    on_entry={
        "issued": "invoice.issuance_confirmed",
        "paid": "invoice.payment_confirmed",
    },
    terminal=frozenset({"paid", "cancelled"}),
)
```

- `transitions` — `dict[from_state, dict[event_type, FSMTransition]]`.
- `on_entry` — `dict[to_state, event_type]`; the event emitted when
  entering a state.
- `terminal` — states from which no transition is allowed.

### 3.2 `FSMTransition`

```python
@dataclass(frozen=True, slots=True)
class FSMTransition:
    to: str
    guard: Specification | None = None
```

`guard` is an optional `Specification` evaluated before allowing the
transition. If the guard is not satisfied, `fsm.transition_rejected`
is emitted with `reason="guard_failed"`.

---

## 4. Guards as Specifications

A guard is a `Specification` — a pure, immutable, composable
predicate over a `StepContext` (ADR-069 §2). The framework ships a
standard library in `kntgraph.concordos.specs`:

| Specification | True when |
|---|---|
| `StepCompleted(step_name)` | the named step completed |
| `StepFailed(step_name)` | the named step failed |
| `StepTimedOut(step_name)` | the named step timed out |
| `StepResultEquals(step_name, field, value)` | a step result field equals a value |
| `DomainStateIs(field, value)` | the DomainComponent has a state value |
| `ProfileTierIs(tier)` | `ProfileComponent.tier` matches |
| `ContinuityToolUsed(tool_name)` | the tool was the last used in the continuity window |

Specifications compose via `and_`, `or_`, `not_`:

```python
guard = NfeRequired().and_(ContinuityToolUsed("nfe_emitter").not_())
```

This reads: "approve the issuance iff NF-e is required AND the
`nfe_emitter` tool was NOT the last tool used in the recent
continuity window."

Vertical-specific Specifications are defined the same way — a frozen
dataclass inheriting `Specification` and implementing
`is_satisfied_by(ctx)`.

---

## 5. Emitted events

| Event | When |
|---|---|
| `fsm.transitioned` | a declared transition with a satisfied guard |
| `fsm.transition_rejected` | invalid transition, failed guard, or terminal-state violation |

`fsm.transitioned` carries `from`, `to`, `trigger`, and
`trigger_event_id`. `fsm.transition_rejected` carries
`current_state`, `trigger`, and `reason` (`terminal_state`,
`transition_not_declared`, or `guard_failed`).

---

## 6. Wiring it up

### 6.1 As a Concordo

```python
from kntgraph.concordos.fsm import BusinessFSMConcordo, FSMConfig

invoice_fsm = BusinessFSMConcordo(config)
```

The Concordo exposes a stable `name` (`fsm:InvoiceDomainComponent`)
and `version`. `install(dispatcher)` registers the `FSMSystem` on a
`ReactiveDispatcher`. Use `ConcordoCatalog` to install several
Concordos idempotently:

```python
from kntgraph.concordos import ConcordoCatalog

catalog = ConcordoCatalog(invoice_fsm, nfe_emission_saga)
catalog.install_all(dispatcher)
```

### 6.2 Directly as a system

```python
from kntgraph.concordos.fsm import FSMSystem

dispatcher.add_system(FSMSystem(config))
```

---

## 7. Testing

The FSM is pure, so it is tested with the SUT builders in
`kntgraph.testing` — no Redis, no dispatcher, no mocks:

```python
from datetime import datetime, timezone
from kntgraph.concordos.fsm import FSMSystem
from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system

FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)

view = (
    AgentViewBuilder("inv-1")
    .with_component(InvoiceDomainComponent(status="validating"))
    .with_trigger("invoice.approved")
    .build()
)
world = WorldBuilder().with_agent(view).build()
out = run_system(FSMSystem(config, now=lambda: FIXED_NOW), world)
assert "fsm.transitioned" in [e.event_type for e in out]
```

`AgentViewBuilder.with_trigger(event_type)` sets `domain_phase` and
`last_event_id` together, mirroring the post-fold view the FSM reads.

---

## 8. Worked example

See [`examples/23_business_fsm.py`](../../examples/23_business_fsm.py)
for a runnable six-scenario walkthrough (valid transition, guarded
allow, guarded block, undeclared transition, terminal state, no
trigger):

```bash
KNT_REDIS_FAKE=1 uv run python examples/23_business_fsm.py
```

---

## 9. See also

- [ADR-069 §3](../../ADRs/ADR-069-Agent-Concordo-Macro-Behaviors.md) —
  the BusinessFSM design record.
- [ADR-069 §2](../../ADRs/ADR-069-Agent-Concordo-Macro-Behaviors.md) —
  the Specification Pattern.
- [ECS](ecs.md) — `World`, `AgentView`, `WorldSystem`.
- [Event Sourcing](event_sourcing.md) — `EventLog`, `World.fold`.
- [DEBT §2.34](../../DEBT.md) — open items (FSM state-advance
  projection).
