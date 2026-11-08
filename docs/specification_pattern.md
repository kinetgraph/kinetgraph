<!--
SPDX-FileCopyrightText: 2026 kinetgraph
SPDX-License-Identifier: Apache-2.0
-->

# Specification Pattern — Composable Business Rules (ADR-069 §2)

The **Specification Pattern** (Evans, *Domain-Driven Design* §9) defines a pure, composable predicate language over a `StepContext`. It is the shared condition language used by Concordos (`BusinessFSM` and `WorkflowSaga`) to express business rules, guards, and step execution predicates.

Public surface:

```python
from kntgraph.concordos.base import (
    Specification,
    StepContext,
    AndSpec,
    OrSpec,
    NotSpec,
)
from kntgraph.concordos.specs import (
    DomainStateIs,
    ProfileTierIs,
    ContinuityToolUsed,
    StepCompleted,
    StepFailed,
    StepTimedOut,
    StepResultEquals,
)
```

---

## 1. Core Concepts

### `StepContext`

`StepContext` is a read-only view passed to specifications for evaluation:

| Field | Type | Description |
| :--- | :--- | :--- |
| `domain_components` | `Mapping[type, DomainComponent]` | The agent's domain components, keyed by their concrete class. |
| `session` | `SessionComponent \| None` | Memory Session component (the active dialogue, ADR-042). |
| `profile` | `ProfileComponent \| None` | Memory Profile component (`tier`, etc., ADR-042). |
| `continuity` | `ContinuityComponent \| None` | Memory Continuity component (`last_tools`, ADR-042). |
| `agent_id` | `str` | The ID of the agent being evaluated. |
| `now` | `datetime` | The dispatcher's current tick timestamp. |
| `step_results` | `Mapping[str, JsonValue]` | Results of executed saga steps (Saga only). |
| `step_states` | `Mapping[str, str]` | Execution states of saga steps (Saga only). |
| `trigger_data` | `Mapping[str, JsonValue] \| None` | The trigger event's payload. |
| `cross_agent_resolver` | `Callable[[str], AgentView \| None] \| None` | Opt-in access to another agent's view. |

The carrier is built by `build_step_context`, the single
construction path the FSM and Saga systems share. Build it through
that function; do not construct a `StepContext` by hand.

> [!IMPORTANT]
> **Clock Discipline**: Specifications MUST treat `ctx.now` as the only clock source. Reading `datetime.now()` inside `is_satisfied_by` breaks deterministic replay in time-travel log evaluation.

### Reading a domain component

Domain components are addressed **by their concrete class**:

```python
order = ctx.get_component(OrderDomainComponent)
if order is not None and order.amount < self.max_amount:
    ...
```

The lookup is exact, not `isinstance`-based: every domain component
shares the `DomainComponent` superclass, so a supertype match
returns an arbitrary component when the agent carries several.

When the Specification cannot name the class — the built-in
`DomainStateIs` is the canonical case — it names a field and the
carrier resolves it through `find_domain_component`. The resolution
is deterministic: zero or several matches yield `None`.

---

## 2. Composition Operators

All specifications inherit from `Composable`, which provides boolean combinator methods and classes.

### Fluent Method Chaining

- `.and_(other: Specification) -> AndSpec`
- `.or_(other: Specification) -> OrSpec`
- `.not_() -> NotSpec`

Example:

```python
policy = (
    ProfileTierIs("vip")
    .and_(DomainStateIs("status", "validating"))
    .and_(ContinuityToolUsed("fraud_alert").not_())
)
```

### Concrete Combinator Classes

- `AndSpec(left: Specification, right: Specification)`
- `OrSpec(left: Specification, right: Specification)`
- `NotSpec(inner: Specification)`

Example:

```python
policy = AndSpec(
    left=ProfileTierIs("vip"),
    right=NotSpec(inner=ContinuityToolUsed("fraud_alert")),
)
```

---

## 3. Built-in Specifications (`kntgraph.concordos.specs`)

The framework provides a standard library of built-in specifications for common conditions:

| Specification | Evaluates True When |
| :--- | :--- |
| `DomainStateIs(field, value)` | the one domain component carrying `field` has that value |
| `ProfileTierIs(tier)` | `ctx.profile.tier == tier` |
| `ContinuityToolUsed(tool_name)` | `tool_name` is present in `ctx.continuity.last_tools` |
| `StepCompleted(step_name)` | `ctx.step_states[step_name] == "completed"` |
| `StepFailed(step_name)` | `ctx.step_states[step_name] in ("failed", "timed_out")` |
| `StepTimedOut(step_name)` | `ctx.step_states[step_name] == "timed_out"` |
| `StepResultEquals(step_name, field, value)` | `ctx.step_results[step_name][field] == value` |

`DomainStateIs` names a field rather than a component class. The
carrier resolves the field to the one component that carries it;
zero or several matches leave the condition unmet. A Specification
that knows its component's class should read it through
`ctx.get_component(MyComponent)`, which is statically typed.

---

## 4. Writing Custom Specifications

To create custom domain rules, inherit from `Specification` and implement `is_satisfied_by(ctx: StepContext) -> bool`:

```python
from dataclasses import dataclass
from kntgraph.concordos.base import Specification, StepContext


@dataclass(frozen=True, slots=True)
class OrderAmountBelow(Specification):
    max_amount: float

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        order = ctx.get_component(OrderDomainComponent)
        if order is None:
            return False
        return order.amount < self.max_amount
```

### Agent Permission & Profile Validation

Permissions, roles, and agent tiers live on `ctx.profile` (`ProfileComponent`). Custom specifications can inspect `ctx.profile` to enforce role-based access control (RBAC) or tenant-level authorization:

```python
@dataclass(frozen=True, slots=True)
class AgentHasPermission(Specification):
    required_permission: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        if ctx.profile is None:
            return False
        permissions = ctx.profile.preferences.get("permissions", "").split(",")
        return self.required_permission in permissions
```


---

## 5. Usage in Concordos

### Transition Guards in `BusinessFSM`

Guards are evaluated before authorizing a transition. If satisfied, `fsm.transitioned` is emitted; if not, `fsm.transition_rejected` (`reason="guard_failed"`) is emitted.

```python
from kntgraph.concordos.fsm import FSMConfig, FSMTransition

fsm_config = FSMConfig(
    component_type=MyDomainComponent,
    state_field="status",
    transitions={
        "validating": {
            "invoice.approved": FSMTransition(
                to="issued",
                guard=ContinuityToolUsed("nfe_emitter").not_(),
            ),
        },
    },
)
```

### Step Conditions in `WorkflowSaga`

Specifications drive saga control flow:

- `skip_when`: Step is skipped if satisfied.
- `proceed_when`: Evaluated post-execution; if unsatisfied, step is marked as failed.
- `compensate_when`: Evaluated when deciding whether to run a compensation step.
- `fail_when`: Evaluated after every step to determine if the saga should fail.

```python
from kntgraph.concordos.saga import SagaConfig, SagaStepConfig

saga_config = SagaConfig(
    name="order_saga",
    steps=(
        SagaStepConfig(
            name="fraud_check",
            tool_name="fraud_scanner",
            skip_when=ProfileTierIs("vip"),
            proceed_when=StepResultEquals("fraud_check", "risk_level", "low"),
        ),
    ),
)
```

### The same rule in both cycles

The carrier is built in one place, so a Specification written for
an FSM guard works unchanged as a Saga skip condition. See
[`declarative_agent_modelling.md`](./declarative_agent_modelling.md)
for the full pattern.

---

## 6. Runnable Example

See [`examples/25_specification_pattern.py`](../examples/25_specification_pattern.py) for a complete runnable demonstration of specifications and composition, and
[`examples/30_declarative_agent_modelling.py`](../examples/30_declarative_agent_modelling.py)
for the same rules used across the two execution cycles.
