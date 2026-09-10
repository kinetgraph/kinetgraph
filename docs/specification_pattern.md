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
| `domain` | `DomainComponent \| None` | The agent's active domain component. |
| `profile` | `ProfileComponent \| None` | Memory Profile component (`tier`, etc., ADR-042). |
| `continuity` | `ContinuityComponent \| None` | Memory Continuity component (`last_tools`, ADR-042). |
| `world` | `World` | The full post-fold `World` (for cross-agent queries). |
| `agent_id` | `str` | The ID of the agent being evaluated. |
| `now` | `datetime` | The dispatcher's current tick timestamp. |
| `step_results` | `MappingProxyType[str, JsonValue]` | Results of executed saga steps (Saga only). |
| `step_states` | `MappingProxyType[str, str]` | Execution states of saga steps (Saga only). |

> [!IMPORTANT]
> **Clock Discipline**: Specifications MUST treat `ctx.now` as the only clock source. Reading `datetime.now()` inside `is_satisfied_by` breaks deterministic replay in time-travel log evaluation.

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
| `DomainStateIs(field, value)` | `getattr(ctx.domain, field) == value` |
| `ProfileTierIs(tier)` | `ctx.profile.tier == tier` |
| `ContinuityToolUsed(tool_name)` | `tool_name` is present in `ctx.continuity.last_tools` |
| `StepCompleted(step_name)` | `ctx.step_states[step_name] == "completed"` |
| `StepFailed(step_name)` | `ctx.step_states[step_name] in ("failed", "timed_out")` |
| `StepTimedOut(step_name)` | `ctx.step_states[step_name] == "timed_out"` |
| `StepResultEquals(step_name, field, value)` | `ctx.step_results[step_name][field] == value` |

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
        if ctx.domain is None:
            return False
        return getattr(ctx.domain, "amount", 0.0) < self.max_amount
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
from kntgraph.concordos.saga import SagaConfig, SagaStep

saga_config = SagaConfig(
    name="order_saga",
    steps=[
        SagaStep(
            name="fraud_check",
            tool="fraud_scanner",
            skip_when=ProfileTierIs("vip"),
            proceed_when=StepResultEquals("fraud_check", "risk_level", "low"),
        ),
    ],
)
```

---

## 6. Runnable Example

See [`examples/25_specification_pattern.py`](../examples/25_specification_pattern.py) for a complete runnable demonstration of specifications and composition.
