<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-080: Drop the gratuitous `Any` in `dict[str | type[Any], Any]`; keep the value `Any` (heterogeneous ECS bag)

- **Status:** Proposed
- **Date:** 2026-10-10
- **Author:** kinetgraph architecture team
- **Related to:**
  - [`kntgraph-type-discipline`](../.agents/skills/kntgraph-type-discipline/SKILL.md) — the no-`Any` / no-bare-`object` rule in framework code (§1.1). The two legitimate exceptions listed there are **`AgentView.components`** and **`Event.data`**. This ADR is the implementation of the *first* of those exceptions: it makes the exception explicit (the value side of the bag) while eliminating the *gratuitous* `Any` in the *key* side (the `type[Any]` that means "any class").
  - [ADR-067](./ADR-067-derived-component-ownership-and-implicit-materialisation.md) — `JsonValue` discipline. The narrow contract between JSON wire and the fold projection.
  - [ADR-079](./ADR-079-Frozen-Dataclass-In-Place-Of-TypedDict.md) — frozen dataclass + drop `MappingProxyType`; the precedent for "shape-known" components. The heterogeneous bag treated by this ADR is *not* shape-known (key can be class OR string, value is heterogeneous), so the dataclass form does not apply; the `Mapping[K, V]` discipline (ADR-079 §3.2) is what the bag inherits.
  - [ADR-001](./ADR-001-Arquitetura.md) — pure ECS, `World = fold(events)`. The bag is the fold's output projection.
  - [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — `ToolCallRequest` / `ToolCallCompletion` as the canonical precedent for typed component classes stored under class-keyed slots.
  - [ADR-042](./ADR-042-Agents-Memory-Model-usage.md) — `SessionComponent` / `ProfileComponent` / `ContinuityComponent` as typed memory components stored under class-keyed slots (the same heterogeneous bag pattern).
  - [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) — `DomainComponent` superclass; the protocol that the fold projection honours when installing typed components.
  - [AGENTS.md](../AGENTS.md) §1 — the skill loaded by `kntgraph-type-discipline`; the same `Mapping[str, Any]` exception for `AgentView.components` is restated in the *file* `core/world/view.py:73-93` (the audit reference).

---

## 1. Context

The `Any` audit of `src/kntgraph/` (run 2026-10-10) shows 14 occurrences of the pattern `dict[str | type[Any], ComponentT]` in `core/storage.py`, 7 in `core/world/projection.py`, and 1 in `core/world/view.py` (`Mapping[str | type[Any], Any]`). All three locations are the **ECS component bag** that the framework hands to systems and tools:

```python
# core/storage.py:67 (representative)
self._archetypes: dict[
    ArchetypeId, dict[str, dict[str | type[Any], ComponentT]]
] = {}
```

The pattern has two distinct parts:

1. **Key: `str | type[Any]`.** Slots are addressed by *string* (overlay-owned: `"tool_requests"`, `"tool_completions"`) **or by *class*** (typed component classes: `ToolCallRequest`, `SessionComponent`, `DomainComponent` subclasses). The `Any` in `type[Any]` is gratuitous — the framework never instantiates a `type[Any]` from runtime input; the `type` itself is the constraint.
2. **Value: `Any`.** Slots are heterogeneous: some are JSON-serialisable payloads (`Event.data`), others are frozen dataclass ECS components (`ToolCallRequest`, `SessionComponent`). This is the *legitimate* exception documented in skill §1.1 and in `view.py:73-93`: encoding it as `JsonValue` would force callers to serialise the ECS components just to satisfy the type checker, and a per-slot Union would force dispatch at every read.

The current code carries the cost of the *legitimate* `Any` (the value) **and** the cost of the *gratuitous* `Any` (the key). The former is non-negotiable; the latter is a typo of convenience that has propagated.

The audit also surfaced the per-type-count breakdown (`tools/ Any: 22`, `core/ Any: 54`, etc.); this ADR is scoped to the **ECS bag pattern only**. The legitimate `Any` for the **value** stays; the ADR does not propose a `JsonValue | FrozenComponent` Union (that would be a future ADR with backward-compat implications, deferred).

## 2. Decision

> **Replace every `dict[str | type[Any], Any]` and `dict[str | type[Any], ComponentT]` in framework core (`storage.py`, `world/projection.py`, `world/view.py`) with the same shape minus the inner `Any`.** The key becomes `str | type`; the value stays `Any` (or `ComponentT` where the surrounding generic already binds the value type). No runtime change.

The `Any` in `type[Any]` is a **style / discipline** violation, not a safety one: pyright treats `type[Any]` and `type` identically at the call site (both accept any class). The migration is mechanical, but each occurrence also carries a comment in the *spirit* of the rule, so the next person reading the file does not add the `Any` back.

### 2.1 The two distinct positions

- **Value position** — `Any` stays. This is the documented `Mapping[str, Any]` exception in skill §1.1 and `view.py:73-93`. The migration in §3 leaves this `Any` untouched.
- **Key position** — `Any` drops. The framework never relies on `type[Any]` being a special form; `type` (a `type` object) is the precise constraint. The migration in §3 makes this explicit.

### 2.2 Why not introduce a new Union for the value

The right long-term answer is `Mapping[str, JsonValue | ComponentT]` (or a `FrozenComponent` Protocol) — at the cost of a runtime check on each access to discriminate JSON vs component. The runtime cost is acceptable for small views, but the *backward-compat* cost (every caller that does `view.components["x"]["nested"]` would need a discriminator) is the blocker. The Union migration is a separate ADR (deferred); this ADR removes only the `Any` in the *key*.

## 3. The pattern in three files

### 3.1 `core/storage.py` (14 sites)

`ArchetypeStorage[ComponentT]` already parameterises the value type via the class TypeVar. The 14 sites break down as:

- `self._archetypes` declaration (line 67)
- `get_components` return type (line 87)
- `add_entity` parameter (line 105)
- `move_entity` parameter (line 128)
- `add_component` `name` parameter (line 152) and local `new_components` (line 158)
- `remove_component` `name` parameter (line 164)
- `query` yield annotation (line 174)
- `query_one` return type (line 195)
- `to_map` return type and intermediate `result` dict (lines 203-204)
- `_derive_archetype` parameter (line 211)
- `clone_with_entity` parameter (line 223)

Every `dict[str | type[Any], ComponentT]` becomes `dict[str | type, ComponentT]`. The `name: str | type[Any]` parameter in `add_component` and `remove_component` becomes `name: str | type` (no `ComponentT` bound here because the value of the *current* component is irrelevant to add/remove).

### 3.2 `core/world/projection.py` (7 sites)

`_extract_components_from_event` returns a freshly-built components dict. `_apply_event` keeps the same shape in `new_components`. `_preserve_derived_components` mutates the same shape in place. The `key: Any` in `_is_derived_component_key` is the runtime classification entry point — it receives either a `str` (overlay key) or a `type` (class key), nothing else.

The 7 sites:

- `_is_derived_component_key(key: Any)` parameter (line 88) — narrow to `key: str | type`.
- `new_components: dict[Any, Any]` in `_apply_event` (line 203) — narrow to `dict[str | type, Any]` (the projection does not know the value's class until the @domain_component registry resolves it; `Any` stays).
- `new_components: dict[Any, Any]` in `_preserve_derived_components` (line 226) — same.
- `_extract_components_from_event` return type (line 346) — same.
- Plus the docstring in `_DERIVED_COMPONENT_CLASSES` (line 85) already typed `frozenset[type]`; the existing ADR-079 §6.3 comment about the merged set was a leftover from the pre-narrowing state — the comment is updated to match the new state.

### 3.3 `core/world/view.py` (1 site)

`AgentView.components: Mapping[str | type[Any], Any]` (line 97) — narrow to `Mapping[str | type, Any]`. The docstring at lines 73-93 documents the legitimate-value-`Any` exception (this ADR's decision); the docstring is updated to acknowledge the `Any` for the *value* only.

The class does **not** become generic (`AgentView[ComponentT]`) — every public API surface that returns an `AgentView` (the projection, the World, the dispatcher) would have to thread the TypeVar, and the `Any` value exception would still apply for JSON payloads. The mechanical narrow is enough for the discipline gate.

## 4. Migration plan (one commit, three files)

The transformation is `s/type[Any]/type/g` scoped to the three files; no behavioural change. CI must remain green.

| Step | File | Substitutions |
|---|---|---|
| 1 | `src/kntgraph/core/storage.py` | 14× `type[Any]` → `type` (parameter / return / annotation) |
| 2 | `src/kntgraph/core/world/projection.py` | 7× `Any` (parameter / annotation) → `str \| type` where appropriate, plus update of the `Any` value annotations to the same shape with a narrow key |
| 3 | `src/kntgraph/core/world/view.py` | 1× `type[Any]` → `type` on `components`; the docstring at lines 73-93 references the new state |

Test coverage:

- The `_is_derived_component_key` branches in `tests/unit/core/test_projection*.py` already exercise the `str` and `type` paths; the new annotation is a pure-narrowing with no test delta required.
- `test_storage.py` (if it exists; the audit did not surface a dedicated file — search `tests/unit/` for `ArchetypeStorage`) covers the existing shape; no new test needed.

## 5. Consequences

### 5.1 Positive

- **−15 `Any` in framework core.** `storage.py` −14, `projection.py` −6 (one is the docstring-only mention, so the actual count is 6 in code, 7 in audit), `view.py` −1. The total `Any` count in `core/` drops from 54 to ~38.
- **Skill compliance.** The key side of the bag no longer carries the discipline-`Any` smell; only the *value* `Any` remains, which is the documented exception. The next audit pass is shorter.
- **Static check honesty.** `type[Any]` and `type` are semantically identical in pyright today, but `type` is the *honest* annotation: it says "any class" without the inner `Any` suggesting we accept arbitrary values.

### 5.2 Negative

- **Comment updates required.** The docstring at `view.py:73-93` references the old `Mapping[str, Any]` exception; the new shape keeps the same exception but the *key* is now narrowed. The docstring is updated to acknowledge the new state.
- **No runtime effect.** The migration is discipline-only. A future audit that mistakes the `value: Any` for a regression would re-introduce `Any` in the key; the docstring update is the long-term guard.

### 5.3 Compatibility

- **Public API:** unchanged. `AgentView.components: Mapping[str | type, Any]` is a *narrower* type than the prior `Mapping[str | type[Any], Any]`; any caller that relied on `type[Any]` (which means nothing at runtime) is unaffected.
- **Internal API:** unchanged. `ArchetypeStorage[ComponentT]` keeps its generic parameter; the inner dict types narrow.
- **Wire format:** unchanged. The bag never appears on the wire — it is a fold projection's output (ADR-067).

## 6. Out of scope

- The `value: Any` itself. A `JsonValue | FrozenComponent` Union (or a runtime tag) is the right long-term shape, but the backward-compat cost (every caller that does `view.components["x"]["nested"]` needs a discriminator) is a separate ADR with stakeholder review. This ADR is the *interim* state: discipline-clean keys, `Any` values.
- `AgentView` becoming generic (`AgentView[ComponentT]`). Same backward-compat cost.
- Vertical `agents/`, `memory/`, `cli/` carrying `Any` in their own shape (the audit showed `agents/ Any: 28`, `cli/ Any: 19`, `memory/ Any: 17`). These are vertical scopes; the rule is the same but the migration is a per-vertical ADR (or a coordinated batch). Tracked under the "vertical cleanup" follow-up.

## 7. References

- Evans, E. *Domain-Driven Design*, 2003 — Repository pattern. The fold-projection / storage split is the framework's repository.
- [`kntgraph-type-discipline`](../.agents/skills/kntgraph-type-discipline/SKILL.md) §1.1, §1.3, §1.4.
- [ADR-001](./ADR-001-Arquitetura.md) — pure ECS.
- [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — typed component classes.
- [ADR-042](./ADR-042-Agents-Memory-Model-usage.md) — memory components in the bag.
- [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) — `DomainComponent` superclass.
- [ADR-067](./ADR-067-derived-component-ownership-and-implicit-materialisation.md) — `JsonValue` discipline.
- [ADR-079](./ADR-079-Frozen-Dataclass-In-Place-Of-TypedDict.md) — frozen dataclass + `Mapping[K, V]` for shape-known data; the precedent for "shape-known" components.
- DEBT §2.40 follow-up: "ECS bag key typing with TypeVar" (placeholder; this ADR supersedes it).

---

## 8. Revision history

- **2026-10-10** — Initial draft. Proposes the mechanical narrow `type[Any] → type` in the three core files. The `value: Any` stays per skill §1.1 exception.
