<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Declarative Agent Modelling — a guide (ADR-088, DEBT §2.42)

This guide describes **how to model an agentic application in
Kinetgraph** with the style the framework settled on: the agent is
declared as data, the runtime is derived from that declaration, and
every business rule reads the same carrier in both execution cycles.

It is the companion to
[`examples/30_declarative_agent_modelling.py`](../examples/30_declarative_agent_modelling.py),
which runs the whole model end to end.

---

## 1. The four moves

### Move 1 — Declare the agent as data

One frozen dataclass carries everything the agent *is*: its persona,
its capability policy, its state machine, its workflows, and the
domain types its rules read.

```python
from kntgraph.agents import AgentDefinition, RoleSpec


@dataclass(frozen=True, slots=True)
class RoleSpec:
    persona: str
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    name: str
    persona: RoleSpec
    role_event_type: str
    fsm: BusinessFSMConcordo | None = None
    sagas: tuple[WorkflowSagaConcordo, ...] = ()
    domain_types: tuple[type[DomainComponent], ...] = ()
```

The definition holds **no runtime handles** — no dispatcher, no
credential, no derived state. That is what keeps it serialisable in
principle and reviewable in practice.

### Move 2 — Derive the runtime

The runtime is derived from the declaration by a method on it:

```python
bundle = definition.bundle()
```

The bundle carries the live objects the dispatcher registers:

```python
@dataclass(frozen=True, slots=True)
class AgentBundle:
    name: str
    systems: tuple[WorldSystem, ...]
    projections: tuple[WorldProjection, ...]
```

It **repeats no field** from the definition, so the two cannot
disagree. It is ephemeral: built at install time, discarded
afterwards. Its `name` is derived (`agent:<name>`), which is what
lets the bundle go through the catalog path:

```python
catalog = ConcordoCatalog(*(d.bundle() for d in definitions))
catalog.install_all(dispatcher)
```

The projection family is where the role becomes a component: the
bundle always carries a `RoleProjection`, which materialises
`RoleComponent` from the role event stream.

### Move 3 — Install

```python
definition.install(dispatcher, principal=principal)
```

Installation registers the derived systems and projections, and
tells the dispatcher to watch the agent the principal identifies.
The principal is a runtime credential the host supplies; it is
never stored in the declaration.

### Move 4 — Write the rules as Specifications

A business rule is a pure predicate over the carrier. It reads the
agent's domain component **by its concrete class** and the memory
tiers the carrier hydrates.

```python
@dataclass(frozen=True, slots=True)
class AmountWithinLimit(Specification):
    min_amount: float
    max_amount: float

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        order = ctx.get_component(OrderDomainComponent)
        if order is None:
            return False
        return self.min_amount <= order.amount <= self.max_amount
```

---

## 2. The two execution cycles

An agent runs two cycles, and they share the same rules.

### The reactive cycle — zero-token governance

The FSM (`BusinessFSMConcordo`) reacts to domain events. A
transition may carry a guard; when the guard is not satisfied, the
FSM emits `fsm.transition_rejected` with `reason="guard_failed"`
and **no LLM is invoked**.

```python
ORDER_FSM = FSMConfig(
    component_type=OrderDomainComponent,
    state_field="status",
    transitions={
        "draft": {"order.submitted": FSMTransition(to="validating")},
        "validating": {
            "order.approved": FSMTransition(
                to="approved",
                guard=AmountWithinLimit(1.0, 100_000.0),
            ),
        },
    },
    terminal=frozenset({"cancelled"}),
)
```

### The deliberative cycle — tool orchestration

The Saga (`WorkflowSagaConcordo`) walks its steps. Each step may
declare `skip_when`, `proceed_when` and `compensate_when` — the
same `Specification` protocol the FSM guard uses.

```python
ORDER_SAGA = SagaConfig(
    name="OrderFulfilment",
    steps=(
        SagaStepConfig(
            name="reserve",
            tool_name="reserve_inventory",
            skip_when=IsHighValueCustomer(),
            compensate_tool="release_inventory",
        ),
        SagaStepConfig(
            name="charge",
            tool_name="charge_payment",
            skip_when=OrderIsFree(),
            compensate_tool="refund_payment",
        ),
    ),
)
```

### Why the same rule works in both

Before DEBT §2.42, the two cycles built the carrier differently:
the FSM populated the memory tiers while two Saga paths passed
`None` for everything. A Specification written for a guard
therefore behaved differently as a skip condition.

Now the carrier is built in **one place**:

```python
from kntgraph.concordos import build_step_context

ctx = build_step_context(view, world, now)
```

Every FSM and Saga evaluation routes through it, so the same view
yields the same observations in either cycle. A guard and a skip
condition are interchangeable.

---

## 3. Reading the carrier

### The domain component, by class

```python
order = ctx.get_component(OrderDomainComponent)
if order is not None and order.status == "paid":
    ...
```

The lookup is **by exact class**, not by `isinstance`. Every domain
component shares the `DomainComponent` superclass, so a supertype
match returns an arbitrary component when the agent carries
several. `ctx.get_component` returns that class or nothing.

When a Specification cannot name the class — the built-in
`DomainStateIs` is the canonical case — it names a field instead and
the carrier resolves it:

```python
from kntgraph.concordos import find_domain_component

component = find_domain_component(ctx, "status")
```

The resolution is deterministic: zero or several matches yield
`None`, so the Specification never reads an arbitrary component.

### The memory tiers

| Attribute | Tier | Carries |
| :--- | :--- | :--- |
| `ctx.session` | Session | the active dialogue |
| `ctx.continuity` | Continuity | the sliding window (recent tools, entities) |
| `ctx.profile` | Profile | identity, preferences, billing tier |

A tier the agent does not carry is `None`. The Specification reads
`ctx.profile.tier` and treats `None` as "not a VIP customer".

### The step surface

`ctx.step_results` and `ctx.step_states` carry the saga's
progress. They are empty in the FSM cycle.

### The clock

`ctx.now` is the dispatcher's tick timestamp. A Specification MUST
read it rather than `datetime.now()` — the latter breaks
deterministic replay.

### Cross-agent access

A Specification that needs another agent's view accepts a
`cross_agent_resolver` callable and calls it inside
`is_satisfied_by`. The resolver is a closure over the post-fold
world, built by the system. The carrier itself never holds the
world: that would make every guard an O(N) scan over the fleet.

---

## 4. Authorisation — one policy, two gates

The agent's capability policy lives in one place: `RoleSpec.allowed_tools`.

```python
definition = AgentDefinition(
    name="order-fulfilment",
    persona=RoleSpec(
        persona="fulfilment",
        allowed_tools=("reserve_inventory", "charge_payment", "create_shipment"),
    ),
    role_event_type="role.assigned",
    ...
)
```

That list is projected into `RoleComponent.allowed_tools`, which is
what the framework reads before a tool request leaves the agent
(gate 2). The other gates are unchanged:

1. **Gate 1** — the `WorkerManager` ACL checks the principal.
2. **Gate 2** — `RoleComponent.allowed_tools` checks the agent's
   policy.
3. **Gate 3** — the tool's own implementation checks whatever else
   it needs.

### The role event

The bundle's `RoleProjection` materialises `RoleComponent` from the
role event stream. The definition names the event **type**; the
schema is the vertical's. The projection reads two payload keys:

| Key | Meaning | When absent |
|---|---|---|
| `persona` | the runtime-assigned persona | the declared `RoleSpec.persona` is used |
| `instructions` | the persona instructions | the empty string |

```python
await event_log.append(
    Event.create(
        event_type="role.assigned",
        agent_id=agent_id,
        event_class="domain",
        data={"persona": "fulfilment", "instructions": "You fulfil orders."},
        correlation=correlation,
    )
)
```

The capability policy is **never** read from the event. It comes
from `RoleSpec.allowed_tools`, because dropping it would silently
disable gate 2 — `has_tool_access` is permissive when no role is
installed. A vertical that needs a different payload schema
subclasses the projection rather than forking it.

---

## 5. The checklist

When modelling an agent in this style:

- [ ] The agent is **one** declaration: persona, policy, FSM, sagas,
      domain types.
- [ ] The declaration holds **no** runtime handle and **no** derived
      state.
- [ ] The runtime is **derived**, never hand-wired.
- [ ] Every business rule is a `Specification` — a pure predicate, no
      I/O, no mutation.
- [ ] Rules read the domain component **by class**, not by
      supertype.
- [ ] Rules read the clock from `ctx.now`.
- [ ] A rule used in both cycles goes through
      `build_step_context` — no ad-hoc `StepContext` construction
      anywhere in the codebase.
- [ ] The tool allow-list appears **once**, in `RoleSpec`.

---

## 6. What this style does not give you

- **Prompt assembly.** Formatting memory tiers into an LLM prompt is
  a separate concern with its own trust boundaries.
- **A typed domain scope in the mini-language.** The bundle
  mini-language cannot name a Python class, so it has no `agent.*`
  scope. Bundle expressions read the trigger event and the step
  results; domain facts reach Python Specifications through
  `ctx.get_component`.
- **Agent discovery.** Spawning, termination and multi-agent
  discovery stay outside the declaration.

---

## 7. Where to read next

| Topic | Document |
| :--- | :--- |
| The condition language | [`specification_pattern.md`](./specification_pattern.md) |
| The state machine | [`business_fsm.md`](./business_fsm.md) |
| The memory tiers | [`memory_model.md`](./memory_model.md) |
| The two cycles, in full | [ADR-069](../ADRs/ADR-069-Agent-Concordo-Foundation.md) |
| The declaration | [ADR-088](../ADRs/ADR-088-Declarative-Agent-Definition-over-Concordos.md) |
