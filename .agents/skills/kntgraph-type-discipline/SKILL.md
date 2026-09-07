---
name: kntgraph-type-discipline
description: Use when writing or reviewing Python types in kntgraph framework code (src/kntgraph/core/, tools/, infra/, stream/, security/, runner/). Covers the no-Any / no-bare-object rule, the JsonValue union, the framework-never-imports-from-vertical boundary, the Event/Result/JsonValue public types, frozen-dataclass components, and TYPE_CHECKING for type-only imports. Trigger keywords: Any, object, JsonValue, AgentView, Event.data, TYPE_CHECKING, framework import, vertical dependency.
---

<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->


# Type discipline

## 1.1 No `Any` and no bare `object` in framework code

`Any` and bare `object` are forbidden in framework code:

- `src/kntgraph/core/`
- `src/kntgraph/tools/`
- `src/kntgraph/infra/`
- `src/kntgraph/stream/`
- `src/kntgraph/security/`
- `src/kntgraph/runner/`

Use the `JsonValue` union for JSON-shaped data, defined in `src/kntgraph/core/_typing.py`:

```python
from kntgraph.core._typing import JsonValue

data: dict[str, JsonValue] = {"text": "hi", "n": 1}
```

There are **two legitimate exceptions**:

- **`AgentView.components`**: the heterogeneous bag of slots (some are JSON payloads, others are frozen ECS dataclasses like `ToolCallRequest` / `ToolCallCompletion`). Encoding it as `Mapping[str, JsonValue]` is wrong — the ECS components live in-memory, not on the wire — and a Union per slot would force dispatch at every read. `Mapping[str, Any]` is the right call.
- **`Event.data`**: the public-facing event payload. It is `Mapping[str, JsonValue]` (tightened from `Any`); tests and examples that do not need JSON discipline may pass `Any`.

## 1.2 Framework never depends on vertical

Framework code is everything under:

- `src/kntgraph/core/`
- `src/kntgraph/tools/`
- `src/kntgraph/infra/`
- `src/kntgraph/stream/`
- `src/kntgraph/security/`
- `src/kntgraph/runner/`
- `src/kntgraph/resilience/`

Framework MUST NOT import from the verticals:

- `src/kntgraph/agents/`
- `src/kntgraph/api/`
- `src/kntgraph/cli/`
- `src/kntgraph/knowledge/`
- `src/kntgraph/events/`
- `src/kntgraph/memory/`

The verticals own the domain semantics; the framework owns the primitives.

### The `memory/` confusion

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
projection mirrors the vertical state shape), but
the path is the source of truth. When in doubt,
open both files and read the docstring: framework
files say "ECS component (ADR-042)"; vertical
files say "session state / Redis adapter".

## 1.3 `Event` / `Result` / `JsonValue` are public framework types

The only shapes that cross the framework/vertical boundary are:

- `kntgraph.core.event.Event`
- `kntgraph.core.result.Result`
- `kntgraph.core.result.ToolError`
- `kntgraph.core._typing.JsonValue`

Adapters translate vertical-specific shapes into these primitives at the vertical/framework seam.

## 1.4 Frozen dataclasses for components

`AgentView` and the components on the `AgentView.components` bag are **frozen** dataclasses. The `Mapping` / `dict` semantics allow `view.components["key"]` to return a mutable value (frozen only blocks reassignment of the field, not mutation of the dict's contents); the convention is "the framework treats this as a discipline enforced by project rules" (see `core/world/view.py`).

## 1.5 `TYPE_CHECKING` for type-only imports

Use `if TYPE_CHECKING:` to import types that are only used in annotations (e.g. `WorldSystem`, `Result`, `JsonValue`). This is the canonical way to break import cycles without paying a runtime cost. The `py_compile` and `pyright` gates enforce this.
