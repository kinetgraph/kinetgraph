<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-081: Vertical discipline — narrow `Any` in `cli/`, `memory/`, `agents/` to canonical primitives

- **Status:** Proposed
- **Date:** 2026-10-10
- **Author:** kinetgraph architecture team
- **Supersedes:** (none)
- **Related to:**
  - [`kntgraph-type-discipline`](../.agents/skills/kntgraph-type-discipline/SKILL.md) — the §0 TL;DR's four primitives, §2 decision tree, and §3.5 ECS-bag `Any` exception apply to the vertical with one twist: the `Any` rule is *soft* (the vertical may use `Any` when justified) but the *canonical patterns* are *hard* — the same `JsonValue` / `Mapping[K, V]` / frozen dataclass rules hold.
  - [ADR-067](./ADR-067-derived-component-ownership-and-implicit-materialisation.md) — `JsonValue` discipline; the wire primitive anchor.
  - [ADR-079](./ADR-079-Frozen-Dataclass-In-Place-Of-TypedDict.md) — frozen dataclass + drop `MappingProxyType`; the pattern this ADR extends to the vertical.
  - [ADR-080](./ADR-080-Drop-Type-Any-In-ECS-Bag.md) — drop `type[Any]` in the ECS bag key; the precedent for "the value `Any` stays, the gratuitous `Any` does not".
  - [ADR-042](./ADR-042-Agents-Memory-Model-usage.md) — the memory vertical's `ProfileState` / `SessionState` are mutable fold state, the framework's `ProfileComponent` / `SessionComponent` are the read-only projection (see also skill §4 on the `memory/` confusion).
  - [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — `ToolCallRequest` / `ToolCallCompletion` as the typed component classes that occupy the role-system bag.
  - [ADR-061](./ADR-061-litellm-integration-review.md) — the LiteLLM adapter, which the LLM `Any` exception (§3.2 below) is grounded in.

---

## 1. Context

The `Any` audit of `src/kntgraph/` (run 2026-10-10) shows the
framework reduced `Any` count to ~117 from ~130 (PR #64).
The vertical still carries **82 `Any`** across five
directories:

```
src/kntgraph/agents/      28 (10 files)
src/kntgraph/cli/         19 (1 file:  cli/commands/upgrade.py)
src/kntgraph/memory/      17 (2 files:  profile.py, session.py)
src/kntgraph/api/          9
src/kntgraph/knowledge/    9
src/kntgraph/events/       0
```

The skill's current position (§1.2) is that the vertical
"may use `Any` and `object` (today) but the canonical
patterns from §3 still apply". That is too soft — the
canonical patterns are not just suggestions, they are
the same contracts the framework uses, and the
vertical is *the* caller of those contracts.

This ADR scopes the discipline to the three target
directories (`agents/`, `cli/`, `memory/`) where the
cluster of `Any` is concentrated in **concrete,
reducible** patterns. The remaining `api/`,
`knowledge/`, `events/` are deferred (they have
already-typed `Mapping[str, JsonValue]` in most cases;
the leftover `Any` is per-tool transport envelopes and
deserves its own ADR per pattern).

The migration is **mechanical**: 19 `dict[str, Any]`
in `cli/commands/upgrade.py` → `dict[str, JsonValue]`
(Jinja2 context is JSON-shaped); 17 `dict[str, Any]`
in `memory/profile.py` + `session.py` fold state →
`Mapping[str, JsonValue]` (fold state is a read-only
view); 4-5 `dict[str, Any]` returns in
`agents/tools/llm.py` and `agents/role_systems/_base.py`
→ `dict[str, JsonValue]`.

The hard cases (LLM response shape, tool-completion
envelope) stay as `Any` with a documented rationale
per file. The hard cases are **out of scope** for this
ADR — the rule is "the *canonical patterns* apply", not
"every `Any` is a bug".

## 2. Decision

> **Apply the same `JsonValue` / `Mapping[K, V]` / frozen
> dataclass discipline to the vertical's three target
> directories. The `Any` exception for the vertical is
> narrower than the framework's: it is reserved for
> genuinely dynamic shapes (LLM responses, tool-completion
> envelopes) and is documented per file. The
> `cli/` Jinja2 contexts and `memory/` fold state have
> no such excuse — they are JSON-shaped, and `Any` is
> a leak.**

The vertical discipline has **one** legitimate
exception (narrower than the framework's `AgentView.components`
exception): the **LLM response shape**. The
`agents/tools/llm.py` adapter accepts a `ModelResponse`
from litellm (a Pydantic model with model-specific
fields) and emits `dict[str, JsonValue]`-shaped data
through `_dump_response` and `_convert_to_raw_dict`.
The input side of the adapter stays `Any` (the LLM
response is dynamic; the type system cannot fully
describe it); the output side tightens to `JsonValue`.

## 3. The patterns

### 3.1 Jinja2 template contexts (`cli/commands/upgrade.py`)

The `knt upgrade` CLI builds Python dicts that the
Jinja2 template engine consumes. The dicts are
**JSON-shaped by construction** (they map a template
variable name to a string, a list, or a nested dict
of the same). Replacing `dict[str, Any]` with
`dict[str, JsonValue]` is mechanical: every site is
either a single-key dict (`{"project_name": ...,
"package": ...}`) or a literal `{}` (sentinel) or a
result of JSON-decoding.

The migration touches **19 sites** in a single file
(per the audit). After: zero `Any` in
`cli/commands/upgrade.py`.

### 3.2 The LLM adapter (`agents/tools/llm.py`)

Two `Any` patterns:

- **Input (`response: Any`):** the litellm
  `ModelResponse` (or `CustomStreamWrapper` for streams).
  The type system cannot fully describe the response
  shape — the fields vary by model and the framework
  reads a small subset (`.choices[0].message.content`,
  `.usage`). `Any` stays. This is the **one** vertical
  exception.
- **Output (`-> dict`):** the adapter coerces the
  response to a JSON-serialisable dict. The return type
  narrows to `dict[str, JsonValue]`.

Three functions narrow: `_dump_response`,
`_convert_to_raw_dict`, and `_attempt` (all return
`dict` today; should be `dict[str, JsonValue]`).

### 3.3 Memory fold state (`memory/profile.py`, `memory/session.py`)

The two `_run_*_handlers` folds carry a heterogeneous
`state: dict[str, Any]` that mirrors the `AgentView.components`
shape (per skill §3.5). The state is **JSON-shaped**:
every field on the fold is a slot written from an event
payload, and the payloads are `JsonValue` by the wire
discipline (ADR-067).

Migration:
- `state: dict[str, Any]` → `Mapping[str, JsonValue]`
  (the fold state is read-only at the handler call site).
- The handler `Callable[[Event, dict[str, Any]], None]`
  type alias tightens to `Callable[[Event, Mapping[str, JsonValue]], None]`.
- `value: Any` in the slot updates tightens to
  `JsonValue`.

The mutable construction site (the dataclass default
`field(default_factory=dict)`) stays `dict` because
`Mapping` is read-only at the type level and the
fold mutates the dict internally (the `field(default_factory=dict)`
mirrors `AgentView.components`).

### 3.4 Role-system bag (`agents/role_systems/_base.py`)

The `_BaseRoleSystem._consume_completion` method returns
`list[tuple[str, Any]]` — a list of `(slot_name,
value)` pairs to install on the agent's view. The
slot values are `Any` because they include both JSON
payloads (from `ToolCallCompletion.result`) and
ECS components (the role-system's own state).
Per skill §3.5, this is the **same heterogeneous-bag
pattern** as `AgentView.components`. The legitimate
exception applies: the `Any` value is documented
inline. The `last_eid: Any` parameter is
**gratuitous** (it's an event_id) and tightens to
`str | None`.

The `output_payload: dict[str, Any]` and
`def _dump_response` (in `llm.py`) tighten to
`Mapping[str, JsonValue]`.

## 4. Migration plan

### 4.1 Commit 1 — `cli/commands/upgrade.py` (the easy win)

Single file, **19 sites**, mechanical `dict[str, Any]` →
`dict[str, JsonValue]`. No ADR in commit message; this
ADR is the rationale. **~30 lines diff.**

### 4.2 Commit 2 — `memory/profile.py` + `memory/session.py`

Two files, **17 sites**. `state: dict[str, Any]` →
`Mapping[str, JsonValue]`, the `_ProfileHandler` and
`_SESSION_HANDLERS` type aliases tighten, the `value:
Any` slot updates tighten to `JsonValue`. The
`field(default_factory=dict)` defaults stay `dict`
(read-only typing at the field site, write-only at the
constructor). **~20 lines diff.**

### 4.3 Commit 3 — `agents/tools/llm.py` (partial)

`-> dict` returns (`_dump_response`, `_convert_to_raw_dict`,
`_attempt`) → `dict[str, JsonValue]`. The `Any` parameters
**stay**: `ModelResponse` is a litellm-internal
Pydantic model whose full shape is model-specific. The
file's module docstring is updated to spell out the
exception with a one-paragraph rationale. **~10
lines diff.**

### 4.4 Commit 4 — `agents/role_systems/_base.py` (partial)

`last_eid: Any` → `str | None`. `comp: Any` stays (a
tool-completion envelope). `output_payload: dict[str, Any]`
→ `dict[str, JsonValue]`. The `_consume_completion` return
type `list[tuple[str, Any]]` keeps the `Any` value
(heterogeneous bag exception) but the docstring
references skill §3.5. **~5 lines diff.**

### 4.5 Out of scope (deferred)

- `src/kntgraph/api/` and `src/kntgraph/knowledge/` carry
  the remaining 18 `Any` in transport-envelope shapes
  (request models, response models). These are
  per-endpoint types; the migration is best done
  endpoint-by-endpoint with a per-vertical ADR. Tracked
  under the "vertical cleanup" follow-up.

## 5. Consequences

### 5.1 Positive

- **Vertical `Any` count drops from 82 to ~40** in this
  cycle. The remaining 40 are in `api/` (9),
  `knowledge/` (9), `agents/tools/llm.py` (8 — the
  LLM exception), `agents/role_systems/_base.py`
  (4 — the bag exception), and a handful of stragglers
  in `agents/memory/`.
- **Skill §3 alignment.** The vertical now follows the
  same canonical patterns as the framework. A new
  contributor who reads the skill does not need a
  second mental model for the vertical.
- **Self-documenting.** The `from kntgraph.core._typing
  import JsonValue` imports in the vertical point to
  the framework as the source of truth. The skill's
  cross-references (§6 in the skill) replace the
  duplicated rationale in the vertical.

### 5.2 Negative

- **Narrower LLM exception.** A future contributor who
  wants to use `Any` somewhere in `agents/` will be
  asked "is this the LLM exception?" — they may need to
  read this ADR before they get an LGTM. That is the
  intended friction; a "Any is OK in vertical" mental
  model was hiding the canonical-pattern contract.
- **Module docstring updates.** Each touched file gets
  a one-paragraph rationale for the surviving `Any`
  (LLM response shape, tool-completion envelope, the
  `_consume_completion` return). Total: 3 doc updates.
- **Vertical CI allowlist.** The verticals gate currently
  compares the *overall* branch-coverage number, not
  per-path (the gate's known design weakness; see
  reliability-gate commentary). The migration does not
  regress coverage (no tests added, no branches
  removed), but the per-vertical rebalance means the
  gate's noise floor shifts; no `--update-verticals-baseline`
  is needed.

### 5.3 Compatibility

- **Public API of the touched files:** unchanged. Every
  return type narrowing is from `dict` to
  `dict[str, JsonValue]` (a subtype widening in the
  pyright sense) or from `Any` to `str | None` (a
  narrowing that the existing callers already pass
  strings into).
- **Internal API:** unchanged. The fold state is
  consumed in-process; no wire format changes.

## 6. Out of scope

- **`api/` and `knowledge/` Any cleanup.** Deferred
  until a per-endpoint audit is feasible.
- **The "vertical CI allowlist" project.** A separate
  follow-up to introduce per-path coverage in the
  verticals gate (so per-vertical rebalances don't
  re-trigger the overall-number regression). Tracked
  under DEBT §2.40 follow-up.
- **`MappingProxyType` in `knowledge/extraction/argument/_finder.py`.** Vertical code; out of scope for the
  framework migration per ADR-079 §6.4. Tracked under
  the "vertical cleanup" follow-up.

## 7. References

- Evans, E. *Domain-Driven Design*, 2003 — Bounded contexts; the framework/vertical split is the project's version of the layering.
- [`kntgraph-type-discipline`](../.agents/skills/kntgraph-type-discipline/SKILL.md) §0, §2, §3.5.
- [ADR-001](./ADR-001-Arquitetura.md) — pure ECS.
- [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — `ToolCallRequest` / `ToolCallCompletion`.
- [ADR-042](./ADR-042-Agents-Memory-Model-usage.md) — `ProfileComponent` / `SessionComponent`.
- [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) — `DomainComponent` superclass.
- [ADR-061](./ADR-061-litellm-integration-review.md) — the LiteLLM adapter, cited for the LLM `Any` exception.
- [ADR-067](./ADR-067-derived-component-ownership-and-implicit-materialisation.md) — `JsonValue` discipline.
- [ADR-079](./ADR-079-Frozen-Dataclass-In-Place-Of-TypedDict.md) — frozen dataclass + drop `MappingProxyType`.
- [ADR-080](./ADR-080-Drop-Type-Any-In-ECS-Bag.md) — drop `type[Any]` in the ECS bag key.

---

## 8. Revision history

- **2026-10-10** — Initial draft. Proposes a vertical discipline that mirrors the framework's primitives (`JsonValue`, `Mapping[K, V]`, frozen dataclass) with one narrower exception (LLM response shape). Migration: 4 commits, 47 → ~9 `Any` in the three target directories.
