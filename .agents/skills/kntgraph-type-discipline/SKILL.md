---
name: kntgraph-type-discipline
description: Use when writing or reviewing Python types in kntgraph framework code (src/kntgraph/core/, tools/, infra/, stream/, security/, runner/, resilience/, concordos/). Covers the no-Any / no-bare-object rule, the four primitives (JsonValue, Mapping[K,V], frozen dataclass, ComponentT TypeVar), the framework-never-imports-from-vertical boundary, the AgentView.components exception, the Event/Result/JsonValue/ToolResult public types, the `memory/` naming collision, and the canonical dict-construction rules. Trigger keywords: Any, object, JsonValue, AgentView, Event.data, TYPE_CHECKING, framework import, vertical dependency, ToolResult, frozen dataclass, MappingProxyType, Mapping, TypedDict, ToolError, Result, ComponentT, slot, projection, fold.
---

<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->


# Type discipline

## 0. TL;DR

The framework has **four primitives** for shape-carrying
data. Pick the right one for the lifetime and visibility
of the data:

| Primitive | Use it for | Lifetime | Example |
|---|---|---|---|
| `JsonValue` | Wire payloads (`Event.data`, Redis values) | crosses the framework/vertical seam | `data: dict[str, JsonValue]` |
| `Mapping[K, V]` | Read-only view over an in-memory bag | process-lifetime, never on the wire | `view.components: Mapping[str \| type, Any]` |
| `frozen=True, slots=True` dataclass | Shape-known composition (return types, ECS components) | process-lifetime, never on the wire | `class ToolResult: ...` (see `tools/_result.py`) |
| `ComponentT = TypeVar("ComponentT")` (from `core/_typing.py`) | ECS component value type bound on a generic class | bound at the call site | `class ArchetypeStorage[ComponentT]: ...` |

`Any` and bare `object` are **banned** in framework code
(`src/kntgraph/core/`, `tools/`, `infra/`, `stream/`, `security/`, `runner/`, `resilience/`, `concordos/`). There is **one** legitimate `Any` exception in framework code today: the **value** of `AgentView.components` (the heterogeneous ECS bag). The **key** was tightened by ADR-080 from `Mapping[str | type[Any], Any]` to `Mapping[str | type, Any]`.

The framework **never** imports from the verticals
(`agents/`, `api/`, `cli/`, `knowledge/`, `events/`, `memory/`).
The `memory/` naming collision is the most common
import mistake in the codebase (§4.2 below).

For the *why* of each rule, see the ADR cross-references
in §6.

---

## 1. Scope

### 1.1 Framework paths (skill applies in full)

```
src/kntgraph/core/        — primitives: Event, Result, JsonValue, World, AgentView, ComponentT
src/kntgraph/tools/       — @tool_worker + WorkerManager
src/kntgraph/infra/       — Redis / HTTP / FalkorDB / WorldCheckpoint adapters
src/kntgraph/stream/      — ReactiveDispatcher + EventLog
src/kntgraph/security/    — auth, signing, key registry
src/kntgraph/runner/      — runner loop, observation, metrics
src/kntgraph/resilience/  — bulkhead, circuit breaker, rate limit, timeout
src/kntgraph/concordos/   — FSM + Saga + bundle loader (ADR-069)
```

`concordos/` is a new entry; it is framework-tier
(it ships only primitives — no domain semantics).

### 1.2 Vertical paths (skill applies as guidance only)

```
src/kntgraph/agents/      — domain agents (FMH, etc.)
src/kntgraph/api/         — FastAPI installers
src/kntgraph/cli/         — typer-based CLI
src/kntgraph/knowledge/   — extraction, graph, graphrag
src/kntgraph/events/      — DLQ, sweepers
src/kntgraph/memory/      — domain memory tier
```

Vertical code may use `Any` and `object` (today) but the
canonical patterns from §3 still apply — vertical
**importing** framework types is encouraged; vertical
**defining** parallel `Any`-typed shapes is a debt
item (see `DEBT.md` §2.40 follow-up).

---

## 2. The decision tree

Use this when you are about to write or change a type
annotation. Walk the tree top-to-bottom; the first match
is your answer.

```
Q1. Is the data a *value on the wire* (Event payload,
    Redis value, HTTP request body)?
   YES → use `JsonValue` (§3.1).
   NO  → continue.

Q2. Is the data a *shape-known composition* (a return
    type, an ECS component, a config record)?
   YES → use a `frozen=True, slots=True` dataclass (§3.3).
   NO  → continue.

Q3. Is the data a *read-only view* over an in-memory
    collection (a system reading AgentView, a saga
    reading step_results, an adapter exposing a config)?
   YES → use `Mapping[K, V]` (§3.2).
   NO  → continue.

Q4. Is the data an *ECS component value type* bound on
    a generic class (ArchetypeStorage holding a
    typed component value)?
   YES → use the `ComponentT` TypeVar from
        `core/_typing.py` (§3.4).
   NO  → continue.

Q5. None of the above? You probably have a real
    exception. Stop and either:
    - read ADR-079 §5 and ADR-080 §6 (the "no `Any`"
      rationale);
    - or open a new ADR proposing a fifth primitive.
```

`Any` and bare `object` are the *default reject* — if
you reach this skill looking for permission to use one
of them, the answer is "no, except in the cases
listed in §3.5".

---

## 3. The four primitives

### 3.1 `JsonValue` — wire payloads

Defined in `src/kntgraph/core/_typing.py:64`:

```python
JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]
```

Use for:

- `Event.data` (the public event payload; `core/event/event.py:120`).
- Redis values written by framework infra (see ADR-067).
- HTTP request / response bodies that cross the framework/vertical seam.

Canonical pattern:

```python
from kntgraph.core._typing import JsonValue

def tool_emit(
    request_event: Event,
) -> Event:
    return Event.create(
        event_type="tool.requested",
        agent_id=request_event.agent_id,
        event_class="domain",
        data={"tool": "weather", "params": {"city": "São Paulo"}},  # dict[str, JsonValue]
        correlation=request_event.correlation,
    )
```

Anti-pattern: `data: dict[str, Any] = ...` (loses the wire
discipline). See §5.1 for the fix.

### 3.2 `Mapping[K, V]` — read-only views

Use for:

- `AgentView.components: Mapping[str | type, Any]` — the ECS bag
  (`core/world/view.py:97`).
- `SagaProgressComponent.step_states: Mapping[str, str]`,
  `step_results: Mapping[str, JsonValue]`,
  `awaiting_approval_at: Mapping[str, datetime]`
  (`concordos/saga/_components.py`).
- `StepContext.step_results`, `step_states`, `trigger_data`
  (`concordos/base.py`).
- `ToolCallRequest.params: Mapping[str, JsonValue]`,
  `ToolCallCompletion.result: Mapping[str, JsonValue] | None`
  (`core/world/components.py`).

`MappingProxyType` was **removed** in production by
ADR-079 §6.2 (commit `90fa734`). The single remaining
production usage is the **vertical**
`knowledge/extraction/argument/_finder.py` (a
class-level frozen regex table) — out of scope.

Canonical pattern:

```python
from collections.abc import Mapping

@dataclass(frozen=True, slots=True)
class SagaProgressComponent(DomainComponent):
    step_states: Mapping[str, str]   # ADR-079 §3.2
    step_results: Mapping[str, JsonValue]
```

The static `Mapping[K, V]` type is enough to prevent
caller-side mutation in well-typed code. `frozen=True`
on the surrounding dataclass prevents the field from
being reassigned. No runtime wrapper is needed.

### 3.3 `frozen=True, slots=True` dataclass — shape-known composition

The canonical primitive for "return type with a known
discriminated shape". `ToolResult` is the precedent
(`tools/_result.py:30`):

```python
@dataclass(frozen=True, slots=True)
class ToolResult:
    status: Literal["ok", "err"]
    value: JsonValue | None = None
    error: str | None = None

    def to_wire(self) -> Mapping[str, str | JsonValue]:
        """Project the dataclass to the wire shape used by
        ``build_completion_event`` and ``build_failure_event``.
        The ``{}`` literal lives here and nowhere else
        (ADR-079 §5).
        """
        if self.status == "ok":
            return {"status": "ok", "value": self.value}
        return {"status": "err", "error": self.error}

    @classmethod
    def ok(cls, value: JsonValue) -> ToolResult:
        return cls(status="ok", value=value)

    @classmethod
    def err(cls, error: str) -> ToolResult:
        return cls(status="err", error=error)
```

The discriminated union enables type narrowing at the
caller:

```python
result = await dispatch.invoke()

if result.status == "ok":
    # pyright narrows: result.value is JsonValue | None
    handle_success(result.value)
else:
    # pyright narrows: result.error is str | None
    handle_failure(result.error)
```

Use this primitive whenever:

- A function has a *shape-known* return type with 2+
  fields, especially when the fields are *conditional*
  on a discriminator.
- A class represents an ECS component (per skill §1.4
  and ADR-034, ADR-042, ADR-059).

### 3.4 `ComponentT = TypeVar("ComponentT")` — generic component value

Defined in `src/kntgraph/core/_typing.py:85`. The
`ArchetypeStorage[ComponentT]` class is the precedent
(`core/storage.py:46`):

```python
from kntgraph.core._typing import ComponentT

class ArchetypeStorage[ComponentT]:
    def __init__(self) -> None:
        self._archetypes: dict[
            ArchetypeId, dict[str, dict[str | type, ComponentT]]
        ] = {}
```

Use this for *internal* generic classes that need to
hold a typed component value across methods. The
`Mapping[str | type, ComponentT]` is the standard
shape.

`AgentView` is **not** generic (`AgentView[ComponentT]`)
because every public surface that returns an
`AgentView` would have to thread the TypeVar, and the
`value: Any` exception (the heterogeneous bag) would
still apply — see ADR-080 §3.3.

### 3.5 The `Any` exception — `AgentView.components`

The one legitimate `Any` in framework code today. The
value side of `AgentView.components` is heterogeneous
(JSON payloads + frozen ECS components), and the
framework encodes it as `Any` (per skill §1.1 original
allowlist + the file docstring at
`core/world/view.py:73-93`).

**The key was narrowed** by ADR-080:

```python
# Before (pre-ADR-080)
components: Mapping[str | type[Any], Any]

# After (current state)
components: Mapping[str | type, Any]
```

The `Any` in `type[Any]` was gratuitous — `type` is the
precise constraint. The value `Any` stays because
encoding it as `JsonValue | FrozenComponent` (a discriminated
Union) is a separate ADR with backward-compat
implications.

If a future change narrows the value, the type
becomes `Mapping[str | type, JsonValue | FrozenComponent]`
(or a runtime-tagged Protocol). Until then, this is
the only legitimate `Any` in framework production
code.

---

## 4. The `memory/` confusion

The `memory` name appears in **two** places with
opposite roles, and this is the single most common
import mistake in the codebase. Read this section
before touching anything memory-related.

| Path | Role | What lives there | Framework can import? |
|------|------|------------------|-----------------------|
| `src/kntgraph/core/components/memory.py` | **Framework** | The frozen-dataclass ECS projections of the three memory tiers: `SessionComponent`, `ProfileComponent`, `ContinuityComponent` (ADR-042). These are the ECS slot shapes systems read by class — framework primitives, on the same plane as `ToolCallRequest` / `ToolCallCompletion` (ADR-034). | **Yes** |
| `src/kntgraph/memory/` | **Vertical** | The domain memory tier: `SessionState` / `ProfileState` / `ContinuityState` dataclasses (the mutable source-of-truth pre-projection), plus the Redis adapters (`_store.py`), the cache warmer, the consolidator, the continuity sub-package. The vertical owns the storage layer; the framework owns only the projection. | **No** |

The projection (in `core/`) is a **view** of the
state (in `memory/`). The state knows how to
write to Redis; the projection knows how to be
read by a `WorldSystem`. The framework imports the
projection and never the state.

A grep for the canonical patterns:

```bash
# Framework-correct (uses the projection):
from kntgraph.core.components.memory import SessionComponent

# Vertical-correct (uses the state / adapter):
from kntgraph.memory.session import SessionState

# Framework FORBIDDEN (would couple framework to
# the vertical's storage layer):
from kntgraph.memory.session import SessionState
# ↑ same import name, different layer — the path
# is what matters.
```

The naming collision is intentional (the framework
projection mirrors the vertical state shape), but the
path is the source of truth. When in doubt,
open both files and read the docstring: framework
files say "ECS component (ADR-042)"; vertical
files say "session state / Redis adapter".

---

## 5. Cookbook: violation → fix

The most common regressions the audit (2026-10) and the
reliability / pyright gates surface. Each row has the
**violation pattern** (greppable), the **fix** (canonical
pattern), and the **anchor** (the file:line that already
follows the fix).

### 5.1 `dict[str, Any]` for a wire payload

```python
# VIOLATION
def emit() -> Event:
    return Event.create(
        event_type="tool.requested",
        data={"foo": 1},  # dict[str, Any]
    )

# FIX
from kntgraph.core._typing import JsonValue

def emit() -> Event:
    return Event.create(
        event_type="tool.requested",
        data={"foo": 1},  # dict[str, JsonValue]
    )
```

Anchor: `core/event/event.py:120` (`Event.data: Mapping[str, JsonValue]`).

### 5.2 `Any` in a `MappingProxyType`-typed slot

```python
# VIOLATION (pre-ADR-079 §6.2)
class SagaProgressComponent(DomainComponent):
    step_results: MappingProxyType[str, JsonValue]

# FIX (current state)
class SagaProgressComponent(DomainComponent):
    step_results: Mapping[str, JsonValue]
```

Anchor: `concordos/saga/_components.py` (post-ADR-079).

### 5.3 `type[Any]` in an ECS bag key

```python
# VIOLATION (pre-ADR-080)
def get_components(self, entity_id: str) -> dict[str | type[Any], Any] | None:
    ...

# FIX (current state)
from kntgraph.core._typing import ComponentT  # the framework's TypeVar

def get_components(self, entity_id: str) -> dict[str | type, ComponentT] | None:
    ...
```

Anchor: `core/storage.py:87` (post-ADR-080).

### 5.4 Shape-known factory returning a `dict`

```python
# VIOLATION
def make_response(value: int) -> dict[str, Any]:
    return {"status": "ok", "value": value}

# FIX
@dataclass(frozen=True, slots=True)
class ToolResult:
    status: Literal["ok", "err"]
    value: JsonValue | None = None
    error: str | None = None

    def to_wire(self) -> Mapping[str, str | JsonValue]:
        if self.status == "ok":
            return {"status": "ok", "value": self.value}
        return {"status": "err", "error": self.error}

def make_response(value: int) -> ToolResult:
    return ToolResult.ok(value)
```

Anchor: `tools/_result.py:30` (`ToolResult`).

### 5.5 `return dict()` for an empty sentinel

```python
# VIOLATION (was needed under the strict ADR-079 §5,
# but ADR-079 §5 was refined in 2026-10-10 to allow
# this)
return dict()  # ruff C408 fires

# FIX
return {}  # empty sentinel; allowed by ADR-079 §5
```

Anchor: `tools/schema.py:123, 126` and
`concordos/saga/_compensation.py:202` (post-refinement).

### 5.6 `TypedDict` for shape-known data

```python
# VIOLATION
class ToolResult(TypedDict, total=False):
    status: Literal["ok", "err"]
    value: JsonValue
    error: str

# FIX
@dataclass(frozen=True, slots=True)
class ToolResult:
    status: Literal["ok", "err"]
    value: JsonValue | None = None
    error: str | None = None
```

`TypedDict` is **not adopted** in the framework. The
frozen dataclass gives the same shape with strict
attribute access, `frozen=True` immutability, and
discriminated-union narrowing. Anchor: ADR-079 §3.1.

### 5.7 Framework importing from a vertical

```python
# VIOLATION (in src/kntgraph/core/, src/kntgraph/tools/, ...)
from kntgraph.memory.session import SessionState  # vertical!

# FIX
from kntgraph.core.components.memory import SessionComponent  # framework projection
```

The full canonical / forbidden import table is in §4.
If a framework primitive genuinely needs data the
vertical owns (e.g. an agent_id namespace), the fix is
to add the primitive to the framework and have the
vertical call it — never the reverse.

---

## 6. ADR cross-references

The skill is the **on-demand reference** for type
discipline. The ADRs are the **rationale and decision
record**. When in doubt, read the ADR.

| ADR | What it decided | Where it shows up in the skill |
|---|---|---|
| [ADR-001](../ADRs/ADR-001-Arquitetura.md) | Pure ECS, Event Sourcing, `World = fold(events)` | §3.4 (ComponentT on `ArchetypeStorage`) |
| [ADR-034](../ADRs/ADR-034-ToolCall-ECS-Components.md) | `ToolCallRequest` / `ToolCallCompletion` as canonical typed components | §3.2 (`Mapping[str, JsonValue]`) |
| [ADR-042](../ADRs/ADR-042-Agents-Memory-Model-usage.md) | `SessionComponent` / `ProfileComponent` / `ContinuityComponent` as frozen memory components | §4 (the `memory/` confusion) |
| [ADR-059](../ADRs/ADR-059-Domain-Memory-ECS-Components.md) | `DomainComponent` superclass; the fold projection honours it | §3.2, §3.3 |
| [ADR-066](../ADRs/ADR-066-Single-Tool-Path.md) | Three-gate ACL; orthogonal to types | (cross-cutting) |
| [ADR-067](../ADRs/ADR-067-derived-component-ownership-and-implicit-materialisation.md) | `JsonValue` discipline; this ADR is the wire primitive anchor | §3.1 |
| [ADR-069](../ADRs/ADR-069-Agent-Concordo-Foundation.md) | Concordos foundation (FSM + Saga) | §1.1 (`concordos/` is framework-tier) |
| [ADR-078](../ADRs/ADR-078-Decommision-ProcessPoolExecutor.md) | Decommission the internal `ProcessPoolExecutor`; `executor_factory` opt-in | (cross-cutting — type discipline independent) |
| [ADR-079](../ADRs/ADR-079-Frozen-Dataclass-In-Place-Of-TypedDict.md) | Frozen dataclass in place of `TypedDict`; drop `MappingProxyType`; `ToolResult` primitive; refined dict-construction rules | §3.2, §3.3, §5.4, §5.5, §5.6 |
| [ADR-080](../ADRs/ADR-080-Drop-Type-Any-In-ECS-Bag.md) | Drop `type[Any]` in the ECS bag key; `value: Any` stays as the documented exception | §3.5, §5.3 |

---

## 7. Greppable rules (for review and CI)

These greps surface violations. They are **not** in
`scripts/ci.py` today — the gate is review-driven.
Adding them to a future CI gate is the natural
follow-up; the allowlist is in DEBT §2.40.

```bash
# 5.1 — `dict[str, Any]` for a wire payload
rg -nP '\bdata\s*[:=]\s*dict\[str,\s*Any\]' src/kntgraph/{core,tools,infra,stream,security,runner,resilience,concordos}

# 5.2 — `MappingProxyType` in production
rg -nP '\bMappingProxyType\b' src/kntgraph/{core,tools,infra,stream,security,runner,resilience}  # noqa: vertical
# Expected: zero matches in framework; one match in vertical (`knowledge/extraction/argument/_finder.py`).

# 5.3 — `type[Any]` in an ECS bag key
rg -nP 'type\[Any\]' src/kntgraph/{core,tools,infra,stream,security,runner,resilience,concordos}
# Expected: zero matches.

# 5.4 — shape-known factory returning a dict
rg -nP 'return\s+\{["\x27][a-zA-Z_]+["\x27]\s*:' src/kntgraph/{core,tools,infra,stream,security,runner,resilience}  # noqa: tests, examples
# Expected: matches only inside `to_wire()` factories (see §3.3).

# 5.5 — `return dict()` for an empty sentinel
rg -nP 'return\s+dict\(\)' src/kntgraph
# Expected: zero matches in framework production.

# 5.6 — `TypedDict` in framework
rg -nP 'TypedDict|from typing import .* TypedDict' src/kntgraph/{core,tools,infra,stream,security,runner,resilience,concordos}
# Expected: zero matches.

# 5.7 — framework importing from vertical
rg -nP 'from kntgraph\.(agents|api|cli|knowledge|events|memory)\.' src/kntgraph/{core,tools,infra,stream,security,runner,resilience}
# Expected: zero matches.
```

When the gate is added, the allowlist per rule lives
in `DEBT.md` §2.40 (open until CI is in place).
