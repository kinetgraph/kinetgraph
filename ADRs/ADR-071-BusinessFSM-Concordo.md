<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-071: BusinessFSM Concordo (C-01)

- **Status:** Accepted
- **Date:** 2026-09-07 (revised 2026-09-12)
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-069](./ADR-069-Agent-Concordo-Foundation.md) — Concordo Foundation (Specification, Concordo Protocol, Catalog)
  - [ADR-001](./ADR-001-Arquitetura.md) — pure ECS, Event Sourcing
  - [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md) — `WorldSystem`
  - [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) — `DomainComponent`
  - [ADR-073](./ADR-073-Concordo-Bundle-Format.md) — Bundle Format (loader)
  - [ADR-074](./ADR-074-Per-System-Cursors-in-AgentView.md) — Per-system cursors (consumer of `view.cursors`)

---

## 1. Context

A **BusinessFSM** (Business Finite State Machine)
declares the lifecycle of a business object as an
explicit state machine over a `DomainComponent`.

The FSM is a **pure reactive system**: no I/O, no tool
calls. It reacts to domain events, validates
transitions, and emits `fsm.transitioned` or
`fsm.transition_rejected`. State is carried by the
`DomainComponent` — the FSM does not duplicate it.

### 1.1 Why a separate ADR

The FSM is one of two Concordos in the foundation
(the other is the Saga, ADR-072). It uses the
Specification Pattern (ADR-069 §2) for guards and the
Concordo Protocol (ADR-069 §3) for composition. It
does **not** introduce new infrastructure — it
consumes `DomainComponent` (ADR-059) and the
projection layer.

This ADR captures the FSM's specific design decisions:
the trigger derivation, the cursor-based delta-scan
that handles multi-event ticks, the audit component,
and the projection that advances state.

---

## 2. What you get

A `BusinessFSMConcordo` is a frozen bundle (§3.5) that
exposes:

- A typed, declarative state machine over a
  `DomainComponent`.
- **Guards** — each transition can carry a
  `Specification` that is evaluated before the
  transition is allowed. Guards reuse the foundation's
  Specification Pattern (ADR-069 §2).
- **`on_entry`** — the FSM can emit a domain event when
  entering a state (e.g. `invoice.issuance_confirmed`
  when transitioning to `issued`).
- **Idempotent dispatch** — the FSM is pure: same
  `World` ⇒ same `list[Event]`. Re-running the
  dispatcher on the same batch produces the same
  events; the EventLog's `event_id` dedup catches
  duplicates.
- **Audit trail** — `FSMAuditComponent` records the
  last transition for the agent.

---

## 3. Design

### 3.1 Configuration

```python
@dataclass(frozen=True, slots=True)
class FSMTransition:
    to: str
    guard: "Specification | None" = None


@dataclass(frozen=True, slots=True)
class FSMConfig:
    component_type: type["DomainComponent"]
    state_field: str
    transitions: Mapping[str, Mapping[str, FSMTransition]]
    on_entry: Mapping[str, str] = field(default_factory=dict)
    terminal: frozenset[str] = field(default_factory=frozenset)
```

The `transitions` is a two-level dict:
`transitions[from_state][event_type] = FSMTransition`.
This shape matches the framework's pattern of nested
event-keyed lookups and is the source for the bundle
format's `transitions: list[{from, to, on_event, guard}]`
(ADR-073 §3.2).

### 3.2 ECS Component (audit + cursor)

The FSM does not introduce a component for state —
state lives in the existing `DomainComponent`. It
introduces one component for audit AND the FSM's own
delta-scan cursor:

```python
@dataclass(frozen=True, slots=True)
class FSMAuditComponent:
    """
    Last transition record for this agent PLUS the
    delta-scan cursor the FSM uses to detect
    transitions that ``view.domain_phase`` (a
    single slot) would have hidden when an agent
    produces more than one domain event in a tick.

    **Audit fields** — ``from_state``, ``to_state``,
    ``trigger_event_type``, ``trigger_event_id``,
    ``transitioned_at``, ``guard_evaluated`` —
    materialised from the latest ``fsm.transitioned``
    event. Read-only for external systems.

    **Cursor field** — ``last_processed_event_id`` is
    the FSM's per-agent cursor (§3.4). It is the
    ``event_id`` of the most recent domain event the
    FSM has processed for this agent. On the next
    tick, the FSM compares it against
    ``view.last_event_id``: when they diverge, the
    FSM re-derives the missed triggers from the
    EventLog between the cursor and the new
    ``last_event_id`` (§3.4).
    """
    from_state: str
    to_state: str
    trigger_event_type: str
    trigger_event_id: str
    transitioned_at: datetime
    guard_evaluated: bool
    last_processed_event_id: str | None = None
```

The cursor field is the only mutable piece; PR 2
(of the refactor plan in §6) implements the writer
and the delta-scan reader.

### 3.3 Projection

The FSM needs a projection that advances the
configured `DomainComponent`'s `state_field` from
`fsm.transitioned` events. This projection
**implements the existing `WorldProjection` Protocol**
(`runner/reactive_extensions.py:62`), it does not
introduce a new abstraction. The registration API is
`dispatcher.add_projection(...)`.

#### 3.3.1 Why a dedicated projection is necessary

Two existing mechanisms were considered and rejected:

1. **Existing overlay projections.** The framework
   ships `MemoryHydrationProjection` (which reads
   `session.*` / `profile.*` / `continuity.*` events)
   and the tool-call overlay (which reads `tool.*`
   events). Neither knows about `fsm.transitioned` —
   extending them would couple unrelated concerns.

2. **`@domain_component` decorator.** The decorator
   in `core/world/component.py` auto-hydrates a
   component from the payload of its own event type
   (`core/world/projection.py:332-336` builds it via
   `cls(**event.data)`). This works for components
   whose full state fits in one event's payload; it
   does **not** work for FSM state advance because:
   - `fsm.transitioned` carries only `from` / `to` /
     `trigger` / `trigger_event_id` — not the rest of
     the `InvoiceDomainComponent` fields, which must
     be preserved across the transition.
   - The FSM needs to **merge** the new `state_field`
     value into the existing component, not replace
     it.

Therefore the FSM ships a dedicated projection that
follows the existing `WorldProjection` Protocol. The
projection is registered via `dispatcher.add_projection`
— the same API the application uses to register its
own custom projections.

```python
class FSMProjection:
    """A ``WorldProjection`` that advances the
    configured ``DomainComponent``'s ``state_field``
    from ``fsm.transitioned`` events.

    Composed into the dispatcher fold after the base
    fold and before the tool overlay. Pure: same
    ``(world, events)`` ⇒ same ``World``.
    """

    __slots__ = ("_config",)

    def __init__(self, config: FSMConfig) -> None:
        self._config = config

    def __call__(self, world: "World", events: list[Event]) -> "World":
        new_views = reconcile_fsm_state(self._config, events, world.views)
        ...
```

### 3.4 WorldSystem (the FSM)

The FSM is a pure reactive system. It reads the
`DomainComponent` state from the post-fold `World`
and validates each incoming event against the declared
transition table. It does not run on every tick —
it runs only when an event lands for the agent.

#### 3.4.1 Trigger derivation

The trigger (the event that MAY justify a transition)
is derived from existing view fields — the framework
does NOT add a `view.last_event` envelope:

- `view.domain_phase` — the **type** of the most recent
  domain event (populated by the default projection at
  `core/world/projection.py:198`).
- `view.last_event_id` — the **id** of that event,
  used as the `causation_id` of the events the FSM emits.
- `view.components[domain_phase]` — the **data**
  payload of that event. The FSM reads this for guards
  that reference `event.data.*`.

The FSM's `ViewTrigger` carrier:

```python
class ViewTrigger(NamedTuple):
    agent_id: str
    event_type: str
    event_id: UUID | None
    data: Mapping[str, "JsonValue"]
    correlation: "CorrelationContext | None"
    causation_id: UUID | None = None
```

The `data` field is read from
`view.components[trigger_type]` (not hardcoded to
`MappingProxyType({})` as an earlier draft had — that
was a bug, fixed in the refactor plan).

#### 3.4.2 Single-slot caveat (delta-scan)

`view.domain_phase` is a single slot. When an agent
produces more than one domain event in a single tick
(e.g. an external adapter emits both
`invoice.created` and `invoice.validated` for the same
agent in the same fold), only the last event survives
the fold; any FSM trigger that would have matched the
earlier event is silently dropped.

**Solution.** The FSM reads the **per-system cursor**
from `view.cursors["FSMSystem"]` — a field on
`AgentView` defined in
[ADR-074](./ADR-074-Per-System-Cursors-in-AgentView.md).
The dispatcher advances the cursor automatically after
the FSM emits events; persistence piggy-backs on the
WorldCheckpoint (no new storage layer).

```python
def __call__(self, world: "World") -> list[Event]:
    out: list[Event] = []
    for agent_id, view in self._agents(world):
        cursor_id = view.cursors.get("FSMSystem")
        last_id = view.last_event_id
        if cursor_id == last_id:
            continue  # happy path: nothing new since last tick

        # Delta-scan: re-derive triggers between
        # cursor_id+1 and last_id from the EventLog.
        triggers = self._resolve_triggers(
            view, cursor_id, last_id, world.event_log
        )
        for trigger in triggers:
            out.extend(self._emit_transition(view, trigger))
    return out
```

The cursor advances to the latest event the view has
seen (`view.last_event_id`) after the FSM emits. The
delta-scan re-derives missed triggers from the
EventLog between the cursor and the latest event. On
the happy path (one event per tick, the common case)
the cursor matches and no scan runs.

**Why a framework-level primitive.** An earlier draft
of this ADR proposed keeping the cursor as
per-instance state (`dict[agent_id, last_processed_event_id]`).
That lost the cursor on process restart and required
the FSM to manage its own persistence. ADR-074
moved the cursor into `AgentView` itself, managed by
the dispatcher, persisted via the existing
WorldCheckpoint — uniform with the framework's other
state, zero new storage.

See [ADR-074 §5-6](./ADR-074-Per-System-Cursors-in-AgentView.md#5-multi-event-tick-end-to-end-example)
for end-to-end examples (multi-event tick, restart
recovery).

#### 3.4.3 Clock injection

```python
class FSMSystem:
    def __init__(self, config: FSMConfig, *, now: "Clock | None" = None):
        self.config = config
        self._now = injectable_clock(now)
```

The injected clock is used for `FSMAuditComponent.transitioned_at`.
A test injects a fixed clock; production uses
`utcnow` (ADR-069 §5.1).

#### 3.4.4 Event emission

The FSM emits events via `Event.domain_from(...)`,
which pins `event_class="domain"` and rejects
operational namespace events. The four event types:

```python
Event.domain_from(
    agent_id=trigger.agent_id,
    type="fsm.transitioned",
    data={"from": from_state, "to": to_state,
          "trigger": trigger.event_type, "trigger_event_id": str(trigger.event_id)},
    causation_id=trigger.event_id,
    correlation=trigger.correlation,
)
```

The `on_entry` event is emitted after the transition:

```python
entry_type = self.config.on_entry.get(transition.to)
if entry_type is not None:
    out.append(self._entry_event(trigger, transition.to, entry_type))
```

#### 3.4.5 Idempotency

The FSM is naturally idempotent: re-running on the same
`World` produces the same events. The EventLog dedups
via `event_id` (ADR-001). The cursor-based delta-scan
ensures that re-runs after a process restart do not
double-emit transitions for the same trigger.

### 3.5 Concordo class

```python
@dataclass(frozen=True, slots=True)
class BusinessFSMConcordo:
    """C-01: BusinessFSM Concordo bundle (ADR-069 §3)."""

    config: FSMConfig
    name: str = field(init=False)
    systems: tuple["WorldSystem", ...] = field(init=False)
    projections: tuple["WorldProjection", ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "name",
            f"fsm:{self.config.component_type.__name__}"
        )
        object.__setattr__(self, "systems", (FSMSystem(self.config),))
        object.__setattr__(
            self, "projections", (FSMProjection(self.config),)
        )
```

### 3.6 Example — invoice lifecycle

```python
from kntgraph.concordos.fsm import (
    BusinessFSMConcordo, FSMConfig, FSMTransition,
)
from kntgraph.concordos.specs import ContinuityToolUsed
from fmh_office.concordos.specs import NfeRequired
from fmh_office.components import InvoiceDomainComponent


invoice_fsm = BusinessFSMConcordo(FSMConfig(
    component_type=InvoiceDomainComponent,
    state_field="status",
    transitions={
        "draft": {
            "invoice.submitted": FSMTransition(to="validating"),
        },
        "validating": {
            "invoice.approved": FSMTransition(
                to="issued",
                guard=NfeRequired().and_(
                    ContinuityToolUsed("nfe_emitter").not_()
                ),
            ),
            "invoice.approved_bypass": FSMTransition(
                to="issued",
                guard=NfeRequired().not_(),
            ),
            "invoice.rejected": FSMTransition(to="draft"),
        },
        "issued": {
            "payment.received":   FSMTransition(to="paid"),
            "invoice.cancelled":  FSMTransition(to="cancelled"),
            "invoice.overdue":    FSMTransition(to="overdue"),
        },
        "overdue": {
            "payment.received":   FSMTransition(to="paid"),
            "invoice.cancelled":  FSMTransition(to="cancelled"),
        },
    },
    on_entry={
        "issued":    "invoice.issuance_confirmed",
        "paid":      "invoice.payment_confirmed",
        "cancelled": "invoice.cancellation_confirmed",
    },
    terminal=frozenset({"paid", "cancelled"}),
))
```

The guard reads: "approve the issuance iff NF-e is
required AND the nfe_emitter tool was NOT the last
tool used in the recent continuity window." The
latter rule prevents re-emitting NF-e immediately
after a previous emission that has not yet aged out
of the window.

### 3.7 Unit tests

```python
from datetime import datetime, timezone
from kntgraph.core.world.component import DomainComponent
from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system


@dataclass(frozen=True, slots=True)
class InvoiceDomainComponent(DomainComponent):
    status: str = "draft"
    tax_regime: str = "lucro_real"


FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def test_fsm_allows_valid_transition() -> None:
    """
    Given:  InvoiceDomainComponent.status = "validating".
    When:   the last domain event is invoice.approved;
            NfeRequired satisfied and the nfe_emitter tool
            is NOT in the recent-continuity window.
    Then:   fsm.transitioned + invoice.issuance_confirmed.
    """
    view = (
        AgentViewBuilder("inv-1")
        .with_component(
            InvoiceDomainComponent(status="validating", tax_regime="lucro_real")
        )
        .with_trigger("invoice.approved")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(
        FSMSystem(invoice_fsm.config, now=lambda: FIXED_NOW), world
    )
    types = [e.event_type for e in out]
    assert "fsm.transitioned" in types
    assert "invoice.issuance_confirmed" in types


def test_fsm_rejects_terminal_state() -> None:
    """status="paid" (terminal) + invoice.submitted → rejected."""
    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="paid"))
        .with_trigger("invoice.submitted")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(
        FSMSystem(invoice_fsm.config, now=lambda: FIXED_NOW), world
    )
    assert len(out) == 1
    assert out[0].event_type == "fsm.transition_rejected"
    assert out[0].data["reason"] == "terminal_state"


def test_fsm_guard_blocks_when_nfe_emitter_was_last() -> None:
    """ContinuityComponent.last_tools has 'nfe_emitter' → guard fails."""
    from kntgraph.core.components.memory import ContinuityComponent

    view = (
        AgentViewBuilder("inv-1")
        .with_component(InvoiceDomainComponent(status="validating"))
        .with_component(
            ContinuityComponent(
                tenant_id="t-1",
                user_id="u-1",
                last_tools={"nfe_emitter": "2026-09-07T11:59:00Z"},
            )
        )
        .with_trigger("invoice.approved")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(
        FSMSystem(invoice_fsm.config, now=lambda: FIXED_NOW), world
    )
    assert out[0].event_type == "fsm.transition_rejected"
    assert out[0].data["reason"] == "guard_failed"


def test_fsm_delta_scan_handles_multi_event_tick() -> None:
    """
    Given:  An agent with status="draft" receives two
            domain events in the same tick:
            invoice.submitted and invoice.validated.
    When:   The FSM runs (with cursor zeroed).
    Then:   Both transitions fire (draft → validating → ...).
    """
    # Set up the World with both events in the
    # post-fold view (multi-event tick).
    # The FSM's delta-scan re-derives the trigger for
    # the earlier event from the EventLog.
    # ... (full test deferred to PR 2 implementation)
    pass
```

The tests use the framework's SUT builders
(`AgentViewBuilder`, `WorldBuilder`, `run_system` in
`kntgraph.testing`) — no mocks on the dispatcher, no
fabricated `Event` envelopes. `with_trigger` seeds
`domain_phase` + `last_event_id` together, mirroring
the FSM's read path.

### 3.8 Composition with Saga

The Saga (ADR-072) drives execution; the FSM uses the
saga's emitted events to advance state. They share no
internal state — they communicate only through the
EventLog. A typical sequence:

```
FSM: invoice.approved (validating → issued)
  → on_entry: invoice.issuance_confirmed
  → Saga: triggers on invoice.issuance_confirmed
    → saga.nfe_emission.started
    → tool.sefaz_validator.requested
    → ...
    → saga.nfe_emission.completed
  → FSM: no transition declared for this event (ignored)
```

If the business needs the saga completion to drive an
FSM transition, it is declared explicitly:

```python
FSMConfig(
    transitions={
        "issued": {
            "saga.nfe_emission.completed": FSMTransition(to="transmitted"),
            "saga.nfe_emission.compensated": FSMTransition(
                to="emission_failed"
            ),
        },
        ...
    },
)
```

---

## 4. Source code layout

```
src/kntgraph/concordos/fsm/
+-- __init__.py          # BusinessFSMConcordo (public; frozen bundle)
+-- _config.py           # FSMConfig, FSMTransition + from_dict/to_dict
+-- _components.py       # FSMAuditComponent (with delta-scan cursor)
+-- _system.py           # FSMSystem (WorldSystem; delta-scan reader)
+-- _state.py            # FSMProjection (fold projection;
│                        #   DomainComponent.state_field advance)
```

The split keeps each file under the 500-line ceiling
(AGENTS.md §3.1). `_state.py` carries the projection
logic (~140 lines); `_system.py` carries the FSM
(~200 lines); `_config.py` is the dataclasses
(~60 lines). The bundle reference is
`docs/business_fsm.md`.

---

## 5. Open questions

1. **Concurrency / re-entrancy** — see ADR-069 §7.
2. **State lifecycle** — see ADR-069 §7.
3. **Step ordering** — N/A for the FSM (FSM has no
   steps; only transitions).

---

## 6. Implementation refactor plan

The FSM ships via PR 2 of the broader refactor plan
tracked in ADR-069's open-questions history. The plan:

1. **PR 1 (alignment).** `FSMSystem` migrates from
   `Event.create(event_class="domain")` to
   `Event.domain_from(...)`. `FSMProjection` is
   registered explicitly via
   `dispatcher.add_projection(...)`. `FSMAuditComponent`
   loses `last_processed_event_id` (now obsolete — the
   cursor lives in `view.cursors`, see ADR-074).

2. **PR 2 (delta-scan via framework cursor).**
   `FSMSystem` reads `view.cursors.get("FSMSystem")`
   (provided by ADR-074) and runs the EventLog scan
   on divergence. No per-instance state on the FSM.
   Tests cover multi-event ticks, replay, and process
   restart.

3. **PR 5 (spec hygiene).** The `Specification`
   base ABC and `Composable` mixin are tightened
   (cross-agent access becomes opt-in, see
   ADR-069 §2.1).

The cursor mechanism is implemented in a separate
ADR-074, which adds `view.cursors` and the dispatcher
logic to advance it. PR 1 of this plan lands ADR-074
inertly (no consumer); PR 2 wires the FSM to it.

---

## 7. References

- Evans, E. *Domain-Driven Design*, 2003 — Chapter 9, Specification Pattern
- [Akka FSM](https://doc.akka.io/docs/akka/current/fsm.html)
- [ADR-001 — Pure ECS + Event Sourcing](./ADR-001-Arquitetura.md)
- [ADR-018 — WorldSystem + ReactiveDispatcher](./ADR-018-WorldIncremental-WorldSystem.md)
- [ADR-059 — Domain Memory ECS Components](./ADR-059-Domain-Memory-ECS-Components.md)
- [ADR-069 — Concordo Foundation](./ADR-069-Agent-Concordo-Foundation.md)
- [ADR-072 — WorkflowSaga Concordo](./ADR-072-WorkflowSaga-Concordo.md)
- [ADR-073 — Concordo Bundle Format](./ADR-073-Concordo-Bundle-Format.md)
- [docs/business_fsm.md](../docs/business_fsm.md) — implementation reference
