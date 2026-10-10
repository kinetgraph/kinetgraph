<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-079: Frozen dataclass as the default for shape-known dicts; `Mapping` instead of `MappingProxyType`

- **Status:** Proposed
- **Date:** 2026-10-09
- **Author:** kinetgraph architecture team
- **Related to:**
  - [`kntgraph-type-discipline`](../.agents/skills/kntgraph-type-discipline/SKILL.md) — the no-`Any` / no-bare-`object` rule in framework code (§1.1); the framework-never-imports-from-vertical boundary (§1.2); `Event` / `Result` / `JsonValue` as public framework types (§1.3); frozen dataclasses for components (§1.4).
  - [ADR-001](./ADR-001-Arquitetura.md) — pure ECS, Event Sourcing, `World = fold(events)`; the principle that shapes cross the framework/vertical seam as `Event` / `Result` / `JsonValue` only.
  - [ADR-019](./ADR-019-Redis-Adapter-Typing.md) — typed adapters; `RedisLike` Protocol.
  - [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — `ToolCallRequest` / `ToolCallCompletion` as the canonical "shape-known dict replaced by frozen dataclass" precedent.
  - [ADR-042](./ADR-042-Agents-Memory-Model-usage.md) — `SessionComponent` / `ProfileComponent` / `ContinuityComponent` as frozen dataclasses on `AgentView`.
  - [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) — `DomainComponent`; the same pattern applied to domain memory.
  - [ADR-067](./ADR-067-derived-component-ownership-and-implicit-materialisation.md) — `JsonValue` discipline; this ADR extends it from "wire data" to "in-process shape-known compositions".

---

## 1. Context

The `kntgraph-type-discipline` skill (`§1.1`) forbids `Any` in framework code; `§1.3` declares `Event`, `Result`, `JsonValue` as the **only** shapes that cross the framework/vertical seam; `§1.4` mandates frozen dataclasses for ECS components. The audit of `Any` in `src/kntgraph/{core,tools,infra,runner,concordos,...}` (commit `a...`, see DEBT §2.40 follow-up) catalogued ~13 surviving occurrences in **dict-shaped** positions:

- `mapping[str, str | JsonValue]` for `result_dict` returned by every tool worker (`tools/manager.py:138-141`, `tools/_worker_invocation.py:46-71`, `concordos/...` step result states wrapped in `MappingProxyType`).
- `frozenset[Any]` for `_DERIVED_COMPONENT_KEYS` (`core/world/projection.py:67`).
- ECS bag `Mapping[str | type[Any], Any]` (`core/world/view.py:97`, `core/storage.py` 14×).

Each surviving `Any` is a **failure of the existing discipline** to express a shape that the project already understands. Two wrong fixes are available and were considered and rejected:

1. **`TypedDict` + `dict(K=V, ...)` factories.** Runtime still is `dict`; access stays string-keyed (`r["status"]`); the type checker treats the factory return structurally. Source-code-wise, no `{}` literal appears outside the factory, but inside the factory `dict(K=V, ...)` is still a dict construction in spirit.
2. **`MappingProxyType` to enforce read-only at runtime.** Adds wrapping + overhead without a type-level effect; the static type `Mapping[K, V]` is already enough to prevent caller mutation in well-typed code.

This ADR picks a third form:

## 2. Decision

> **Shape-known dicts in framework code are frozen dataclasses.** The `TypedDict` primitive is **not adopted**; `MappingProxyType` is **removed** in favour of `Mapping[K, V]` types alone.

Specifically:

- **`frozen=True, slots=True` dataclass** is the canonical primitive for shape-known in-process compositions (3-10 fields with stable names). Used for ECS components today (skill §1.4); extended to internal "result" and "report" shapes that today are dict-literal returns.
- **`Mapping[K, V]`** is the canonical read-only view in framework signatures. `MappingProxyType` is removed from production code; `Mapping` is sufficient.
- **`JsonValue` / `Mapping[str, JsonValue]`** is the canonical wire shape (unchanged from ADR-067). Crossing the framework/vertical seam requires conversion via an explicit `to_wire()` factory on the dataclass.
- **`{}` and `dict(K=V, ...)` literals** are banned in production source. The only legitimate site for `{}` is inside a dataclass factory, where the result is built before being handed to the frozen dataclass constructor — and even there the constructor form is preferred (`make(result).to_wire()` instead of inline literal).
- **No CI allowlist yet.** A follow-up ADR (when the team feels the pain) introduces the gate.

## 3. The primitives

### 3.1 Frozen dataclass for shape-known compositions

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
from kntgraph.core._typing import JsonValue


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The result of one ``@tool_worker`` invocation.

    Discriminated union over ``status``. ``value`` is set on
    success; ``error`` is set on failure. The on-wire shape
    (Redis / EventLog) is produced by ``to_wire()``.
    """
    status: Literal["ok", "err"]
    value: JsonValue | None = None
    error: str | None = None

    def to_wire(self) -> Mapping[str, str | JsonValue]:
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

Properties the dataclass form buys over the dict literal:

1. **Type-narrowed discriminators.** `if result.status == "ok":` makes `result.value` accessible; `else:` makes `result.error` accessible. With `dict[str, ...]` the branching is cego.
2. **Attribute access.** `result.status` instead of `result_dict["status"]`. Refactors that rename a field are caught by mypy/pyright.
3. **Immutability.** `frozen=True` prevents accidental mutation at runtime without needing `MappingProxyType`.
4. **Wire shape is one method away.** `to_wire()` is the only place a dict literal is permitted.

### 3.2 `Mapping[K, V]` for read-only views

```python
from collections.abc import Mapping


def run_step(ctx: StepContext) -> Mapping[str, str | JsonValue]:
    """Read step results; the return is a view — callers
    must NOT mutate (the static ``Mapping`` type enforces
    this; no runtime wrapper is added).
    """
    return ctx.step_results
```

`MappingProxyType` is not adopted. The type-checker guarantees caller-side non-mutation in well-typed code; if a caller casts to `dict` explicitly to mutate, that is a deliberate act of unsafety and should be flagged by code review, not by an in-process trap.

### 3.3 `JsonValue` / `Mapping[str, JsonValue]` for the wire

Unchanged. `Event.data`, Redis values, and tool params (from `Event.data`) all carry this discipline. The `ToolResult.to_wire()` boundary is the place where the dataclass casts the populated wire dict (`{"status": "ok", "value": value}`); empty `{}` sentinels in framework code are also allowed (see §5).

## 4. Top 3 migration candidates

### 4.1 `ToolResult` (replace `Mapping[str, str | JsonValue]`)

Today 4 callsites in `tools/manager.py`, `tools/_worker_invocation.py`, `tools/_message_handlers.py` (`build_completion_event`, `build_failure_event`). Migration:

- Introduce `kntgraph/tools/_result.py` with `ToolResult` + `to_wire()` + `ok()` / `err()` factories.
- `_dispatch_to_tool` returns `ToolResult` (not `Mapping`).
- `_invoke_tool_sync` returns `ToolResult`.
- `build_completion_event` / `build_failure_event` take `ToolResult` and call `.to_wire()` only at the EventLog boundary.

### 4.2 Saga step results (`step_results` + `step_states`)

`concordos/saga/_state.py:417-421` wraps `state.step_results` and `state.step_states` in `MappingProxyType(dict(...))`. Migration: drop the `MappingProxyType` wrap (the field already lives on a frozen dataclass state — runtime immutability comes from `frozen=True`); the public type becomes `Mapping[str, str]` / `Mapping[str, JsonValue]` only.

### 4.3 `_DERIVED_COMPONENT_KEYS` (`frozenset[Any]` → `frozenset[type[ComponentMeta]]`)

Not a dataclass (it's a set), but the same "use precise static types" theme. Replace `frozenset[Any]` with `frozenset[type[ComponentMeta]]`. Independent PR.

## 5. Anti-patterns

This ADR explicitly bans:

| Anti-pattern | Why |
|---|---|
| `return dict(K=V, ...)` from production code | Duck-types the return; the static type is `dict[str, Any]` or wider, hiding the real shape. The factory should return a `frozen=True` dataclass (this ADR's `ToolResult` form) — the call site then gets attribute access, `frozen=True` immutability, and discriminated `Literal` narrowing. |
| A **shape-known factory** returning a populated `{"key": value, ...}` literal | Build a frozen dataclass instead (see `ToolResult.ok`/`err` for the canonical pattern). The literal hides the shape from the static checker. |
| `TypedDict` (anywhere in framework) | Not adopted; frozen dataclass is the project's primitive (skill §1.4). |
| `MappingProxyType` in production | Runtime overhead without static benefit; `Mapping[K, V]` is enough. |
| `dict[str, Any]` / `dict[Any, Any]` | The skill §1.1 already forbids `Any`; this ADR adds the dataclass replacement for shape-known dicts. |

The allowed sites for dict literals in framework code:

| Site | Why |
|---|---|
| `return {}` | Empty sentinel — Pythonic, ruff C408-compatible (the rule fires on `dict()` with no args, not on `{}`), and the static `Mapping`/`dict` return type carries the type discipline. |
| `return {"status": "ok", "value": value}` in `to_wire()` | The wire boundary (§3.3); the `Mapping[str, str \| JsonValue]` return type makes the shape explicit. |
| `return {**other, "key": value}` | Merging; the static type carries the union. |
| `dict(other_dict)` | Explicit copy of an existing `Mapping`/`dict`; never a factory return. |

The previous version of this section banned `r = {"key": value, ...}` wholesale. That was too strict: the populated `{"status": "ok", "value": value}` in `to_wire()` is a wire shape, not a shape-known factory return. The real rule is "use a dataclass for shape-known composition" (which `ToolResult` satisfies) — the dict-literal ban follows from that, not the other way around. The empty-sentinel `{}` was always idiomatic and ruff-aligned.

## 6. Migration plan

### 6.1 Commit 1 — `ToolResult` dataclass (the canary)

- New file `src/kntgraph/tools/_result.py` with the dataclass + `to_wire()` + `ok()` / `err()` factories.
- `_dispatch_to_tool` returns `ToolResult` (drop the `Awaitable[Mapping[str, str | JsonValue]]`).
- `_invoke_tool_sync` returns `ToolResult` and constructs via `ToolResult.ok(...)` / `ToolResult.err(...)`.
- `build_completion_event` / `build_failure_event` take `ToolResult`.
- `ExecutorFactory` type alias becomes `Callable[[ToolSyncInvoke, str, Mapping[str, JsonValue]], Awaitable[ToolResult]]`.
- All `tests/` and `concordos/` references to `result_dict["status"]` migrate to `result.status`.
- `scripts/ci.py --only tests`: green; `--only lint`: green; `--only complexity`: green (delta ≤ 0).

### 6.2 Commit 2 — Saga state immutability via `frozen=True` only

- Drop `MappingProxyType(...)` wraps in `concordos/saga/_state.py:417-421`; field types stay `Mapping[str, str | JsonValue]`.
- Internal saga call-sites that read step results via `state.step_results["x"]` become `state.step_results.x` if `step_results` is **also** a frozen dataclass (out of scope for this ADR; tracked as a follow-up if/when the saga result shape stabilises).
- Tests pass; complexity gate green.

### 6.3 Commit 3 — `_DERIVED_COMPONENT_KEYS` typed set

- `frozenset[Any]` → `frozenset[type[ComponentMeta]]`.
- 1 line; small PR.

### 6.4 Out of scope

- A CI gate that bans `Any` / `TypedDict` / `MappingProxyType` / `{}` literal at the source level. To be proposed separately when the team signs off on this ADR.
- Migration of `AgentView.components` and `core/storage.py`'s `dict[str | type[Any], ComponentT]`. These are heterogeneous ECS bags (key can be class OR string); the dataclass form does not apply. The migration target there is `Mapping[str | type[T], T]` with a `TypeVar` bound, not a frozen dataclass — separate ADR when the time comes.

## 7. Consequences

### 7.1 Positive

- **`Any` count drops.** `tools/` should go from 23 (post ADR-078 + yesterday's PR) to ~5-7 (just the remaining ECS bag keys and the `_is_cpu_bound` heuristic helper).
- **Type-narrowed discriminated unions.** `if r.status == "ok": ... r.value ...` works at last.
- **Refactor safety.** Renaming `value` → `payload` is a single rename + test run; today it's a `result_dict["value"]` across 4 callsites.
- **`MappingProxyType` removed.** One less runtime wrapper, one less `from types import MappingProxyType` across files; saga state code shrinks.
- **`{}` and `dict(...)` literals disappear** from framework source modulo `to_wire()` factories. Easier to grep for boundaries.

### 7.2 Negative

- **One extra line at every factory.** `to_wire()` adds the wire conversion step. Worth it for the discriminator narrowing.
- **`ToolResult` is structural only at the wire.** Two services that exchange a `ToolResult` over Redis still need the wire schema doc; the dataclass on the consumer side has no compile-time link to the producer. The on-wire path is unchanged.
- **Slight type-erasure at boundaries.** `Mapping[str, JsonValue]` from `Event.data` loses the dataclass guarantee that `value` is present when `status == "ok"`. The contract lives in the docstring; ADR-067 §1.1 already lives with this trade-off for the JSON wire.

### 7.3 Compatibility

- `ToolResult` is internal framework state; no external caller can depend on `result_dict["status"]` style because the call signature changes.
- Saga `state.step_results` is part of the saga's internal API; the data was already immutable; only the `MappingProxyType` wrap is gone (and `Mapping` returned is what `MappingProxyType` was wrapping).
- `_DERIVED_COMPONENT_KEYS` set type narrows: callers using `frozenset[Any]` see a tighter type, which is upstream-compatible.

## 8. Open questions

1. **`ToolResult` is currently public via `tools/manager.py`'s `ExecutorFactory` type alias.** A vertical implementing a `ProcessPoolFactory` today sees `Mapping[str, str | JsonValue]`. Under the migration the alias becomes `ToolResult`. Is that OK, or should the alias stay `Mapping[...]` to keep the factory contract shape-stable across boundaries? The pragmatic answer is "alias becomes `ToolResult` — verticals should import the dataclass" but flagging.
2. **`to_wire()` and cross-language clients.** Today `result_dict` is wire-compatible by construction. `to_wire()` returns a fresh dict each call; for hot-path Redis writes the cost is one dict allocation per event. Negligible at our event rates; flagging for review.
3. **Discriminated union over multiple `ok` shapes.** A future ADR may need a `ToolResult.success_ok(value)` vs `ToolResult.success_no_content()` distinction; the `Literal["ok", "err"]` is the minimum.

## 9. References

- Evans, E. *Domain-Driven Design*, 2003 — Value Objects (the dataclass-with-`to_wire` form is the canonical Value Object).
- [`kntgraph-type-discipline` skill](../.agents/skills/kntgraph-type-discipline/SKILL.md).
- [ADR-001](./ADR-001-Arquitetura.md) — pure ECS.
- [ADR-019](./ADR-019-Redis-Adapter-Typing.md) — typed adapters.
- [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — `ToolCallRequest` / `ToolCallCompletion` (the precedent).
- [ADR-042](./ADR-042-Agents-Memory-Model-usage.md) — frozen memory components.
- [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) — `DomainComponent`.
- [ADR-067](./ADR-067-derived-component-ownership-and-implicit-materialisation.md) — `JsonValue`.
- PEP 589 — `TypedDict` (rejected here).
- `types.MappingProxyType` — Python stdlib (rejected here).

---

## 10. Revision history

- **2026-10-10** — Refined §5 anti-patterns. The previous version banned `r = {"key": value, ...}` wholesale; the refined version splits "shape-known factory return" (banned — use dataclass) from "wire-boundary literal" (allowed in `to_wire()`) and "empty sentinel" (allowed: `return {}`). The `dict()` call ban (`return dict(K=V, ...)`) is preserved. Removes the friction between ADR-079 §5 and ruff C408.
- **2026-10-09** — Initial draft. Picks frozen dataclass over `TypedDict` and `Mapping` over `MappingProxyType`. No CI gate included per "no allowlist yet".
