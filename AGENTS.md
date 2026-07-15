<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# AGENTS.md — Conventions for AI agents and human contributors

This document is the **single source of truth** for
the conventions AI agents (Claude, opencode, etc.)
and human contributors must follow when working on
the kntgraph codebase. It is referenced from
`CONTRIBUTING.md`, the test files, and the ADRs.

The goal is to keep the project coherent across
generations of contributors and AI sessions: every
decision documented here is **enforced by a gate**
in `scripts/ci.py` (lint, format, complexity,
pyright, tests, bandit, audit, reuse). No convention
in this file is a "nice to have".

The companion `CONTRIBUTING.md` covers the
PR workflow; this document covers **how to write
code that the gates accept**.

---

## 1. Type discipline

### 1.1 No `Any` and no bare `object` in framework code

`Any` and bare `object` are forbidden in framework
code (`src/kntgraph/core/`, `src/kntgraph/tools/`,
`src/kntgraph/infra/`, `src/kntgraph/stream/`,
`src/kntgraph/security/`, `src/kntgraph/runner/`).
Use the `JsonValue` union for JSON-shaped data
(defined in `src/kntgraph/core/_typing.py`):

```python
from kntgraph.core._typing import JsonValue

data: dict[str, JsonValue] = {"text": "hi", "n": 1}
```

There are **two legitimate exceptions**:

  - **`AgentView.components`**: the heterogeneous
    bag of slots (some are JSON payloads, others
    are frozen ECS dataclasses like `ToolCallRequest`
    / `ToolCallCompletion`). Encoding it as
    `Mapping[str, JsonValue]` is wrong (the ECS
    components live in-memory, not on the wire) and
    a Union per slot would force dispatch at every
    read. `Mapping[str, Any]` is the right call.
  - **`Event.data`**: the public-facing event
    payload. It is `Mapping[str, JsonValue]`
    (tightened from `Any`); tests / examples that
    do not need JSON discipline may pass `Any`.

### 1.2 Framework never depends on vertical

`src/kntgraph/core/`, `src/kntgraph/tools/`,
`src/kntgraph/infra/`, `src/kntgraph/stream/`,
`src/kntgraph/security/`, `src/kntgraph/runner/`
must **NOT** import from `src/kntgraph/agents/`,
`src/kntgraph/api/`, `src/kntgraph/cli/`,
`src/kntgraph/knowledge/`, `src/kntgraph/events/`,
`src/kntgraph/memory/`. The verticals own the
domain semantics; the framework owns the
primitives.

### 1.3 `Event` / `Result` / `JsonValue` are public framework types

`kntgraph.core.event.Event`,
`kntgraph.core.result.Result`,
`kntgraph.core.result.ToolError`, and
`kntgraph.core._typing.JsonValue` are the only
shapes that cross the framework/vertical
boundary. Adapters translate vertical-specific
shapes into these primitives at the
vertical/framework seam.

### 1.4 Frozen dataclasses for components

`AgentView` and the components on the
`AgentView.components` bag are **frozen**
dataclasses. The `Mapping` / `dict` semantics
allow `view.components["key"]` to return a
mutable value (frozen only blocks reassignment
of the field, not mutation of the dict's
contents); the convention is "the framework
treats this as a discipline enforced by project
rules" (see `core/world/view.py`).

### 1.5 `TYPE_CHECKING` for type-only imports

Use `if TYPE_CHECKING:` to import types that are
only used in annotations (e.g. `WorldSystem`,
`Result`, `JsonValue`). This is the canonical way
to break import cycles without paying a runtime
cost. The `py_compile` and `pyright` gates enforce
this.

---

## 2. No compat shims

### 2.1 When an API changes, update all call sites in the same commit

Do **not** add kwargs-optional branches that
detect the old API at runtime. Do **not** keep
deprecated classes / functions / modules alive
past their documented removal target. The
"Deprecation removal" pattern (issue a
`DeprecationWarning` for one minor cycle, then
`git rm` the deprecated code in the next) is
the framework's standard lifecycle.

### 2.2 Removal targets are not optional

A `Removal target: v0.9.0` in a deprecation warning
is a **commitment**. The deprecated code MUST be
removed in the major version that follows the
deprecation. Exceptions require an ADR.

### 2.3 `kntgraph.agents.roles` is a precedent

The package was deprecated in v0.8.0 (ADR-041)
with a removal target of v1.0.0; the cleanup
commit moved all internal usages to the
ECS-shaped `kntgraph.agents.role_systems`
module and then removed the package. The pattern
is the reference for any future deprecation
(this is what was applied in the v0.9.0
breaking change that removed `LiteLLMTool`,
`ToolInvoker`, and `agents/roles/`).

---

## 3. File size and module layout

### 3.1 500-line guideline

Files > 500 lines should be split into
sub-modules. The split is private (prefix
`_private.py`); the public `__init__.py`
re-exports the API.

### 3.3 Private modules are private

A module whose name starts with `_` (e.g.
`core/event/_codec.py`) is **internal to its
parent package**. External code MUST NOT import
from private modules. The linter (ruff rule
`F401` — unused imports; `SLF001` — private
attribute access) catches some of the symptoms;
the gate that enforces the public API is the
`__all__ = [...]` declaration in the
`__init__.py`.

---

## 4. Style

### 4.6 Prose in English; identifiers follow the domain

Docstrings, comments, and module-level docs are
in **English**. Identifiers follow the domain
(English where natural: `ReactiveDispatcher`,
`SolutionExtractor`; PT-BR-derived names where
the domain uses them: `ContinuityManager`,
`CNPJ` in the schema, `PIX` in the example
tools).

---

## 6. Errors are typed (`Result[T, E]`)

### 6.1 Never `raise Exception`

Domain errors are typed. The framework's
`Result[T, E]` (in `kntgraph.core.result`)
encodes the expected-failure path; typed
`*Error` classes (`ToolError`, `GraphError`,
`CheckpointError`, etc) are the per-domain
"raised to crash the process" signal for
unexpected failures.

### 6.2 Mutating operations return `Result`

All mutating operations on framework / vertical
storage adapters (`RedisEventLogAdapter`,
`RedisCheckpointStorage`,
`RedisSessionStorage`, `RedisProfileStorage`,
`RedisContinuityStorage`, the `DLQ` adapters,
the graph adapters, etc.) return `Result[T,
ToolError]` / `Result[T, GraphError]` / etc.
Tests document the contract (e.g.
`tests/unit/infra/redis/_memory/test_session.py`).

---

## 7. Testing

### 7.1 Behaviour tests, not mock-heavy unit tests

Use the real `EventLog`, the real `World`, the
real `WorkerManager`. Mock only when the
external system is unavailable in CI (e.g.
GLiNER2 with a GPU, real Ollama with a local
LLM, real FalkorDB with a graph). The
`KNT_REDIS_FAKE=1` env var switches the
`EventLog` to an in-process `fakeredis` client
so unit tests do not need a Redis container.

### 7.2 Cover the happy path + at least one failure mode per public function

Per `CONTRIBUTING.md`. This is the standard gate
for new code; the test files document the
expected shape.

### 7.3 `pytest.mark.asyncio` only on `async def` tests

The project's `pyproject.toml` sets
`asyncio_mode = "strict"`, which requires an
explicit mark on every `async def test_*` and
rejects stray marks on sync `def test_*`. The
gate is the `pytest -W error::pytest.PytestWarning`
in `scripts/ci.py`.

---

## 9. The single CI gate (`scripts/ci.py`)

The mandatory gate is one command:

```bash
uv run scripts/ci.py
```

It runs 9 steps in order. **All 9 must pass.**
There is no best-effort mode; the only way to
skip a step is `uv run scripts/ci.py --only
<step>` (e.g. `--only lint`), which selects that
step to the exclusion of the others (for local
iteration). The pre-commit hook runs the full
set without flags.

| Step         | Tool                 | Description                              |
| ------------ | -------------------- | ---------------------------------------- |
| `syntax`     | `py_compile`         | Compiles every `.py` in `src/` and `tests/` |
| `lint`       | `ruff check`         | Lints with the rules `E, F, W, I, UP, B, A, C4, SIM` |
| `format`     | `ruff format --check`| Verifies the canonical format (zero diffs) |
| `complexity` | `radon cc/mi`        | CC ≤ 10 per block, MI ≥ 20 per file, no regression vs `.radon-baseline.json` |
| `reuse`      | `REUSE 3.3`          | License compliance (SPDX headers + `LICENSES/`) |
| `pyright`    | `pyright`            | Static type check (against the existing baseline) |
| `tests`      | `pytest`             | Unit tests; integration tests when Redis is available |
| `bandit`     | `bandit`             | Security scan (`B110` filtered at severity medium) |
| `audit`      | `pip-audit`          | Vulnerability scan of the resolved dep tree |

For iteration, the most common subset is:

```bash
# Quick local check (no integration, no bandit, no audit)
KNT_REDIS_FAKE=1 uv run scripts/ci.py --only lint
KNT_REDIS_FAKE=1 uv run scripts/ci.py --only format
KNT_REDIS_FAKE=1 uv run scripts/ci.py --only tests

# Fast pytest loop
KNT_REDIS_FAKE=1 uv run pytest tests/unit/ -q

# Quick linter loop
uv run ruff check . && uv run ruff format --check .
```

---

## 10. Prose language

All new prose (docstrings, comments, module
docs, ADRs, design notes, error messages
visible to the user) is in **English**.
Existing PT-BR content remains as historical
records; do NOT translate it. The exception is
the CLI's user-facing messages (terminal output
to Brazilian operators), which keep the PT-BR
domain language where it carries semantic value
(e.g. `LICENÇA`, `CONFIGURAR` in the `knt`
CLI).

---

## 11. Branch policy

### 11.3 Don't create new branches without checking with the human

AI agents MUST NOT push to `main` directly and
MUST NOT create new long-lived branches
(`feat/...`, `fix/...`, `chore/...`) without
explicit human direction. The canonical
workflow is:

  1. The human creates the branch.
  2. The AI agent works on it, accumulates
     commits (the AI does NOT commit either;
     the human reviews and commits).
  3. The human opens the PR.

The agent's only git operations during
iteration are `git add`, `git diff`, `git
status`, and `git checkout` (between branches
the human already created). Pushing and PR
creation are the human's.

---

## 13. Environment

### Required env vars

```bash
# Switch the EventLog / Redis adapters to
# in-process fakeredis (no Redis container
# required for unit tests).
export KNT_REDIS_FAKE=1

# Default model for the LLM worker
# (LiteLLMToolWorker). Default: gpt-4o-mini.
# Local dev typically uses Ollama:
export KNT_LLM_DEFAULT_MODEL="ollama/qwen3.5:4b"
```

### Local services (integration tests)

```bash
# Redis (port 6379, password "redispassword")
docker run -d -p 6379:6379 --name kntgraph-redis \
    -e REDIS_PASSWORD=redispassword redis:7 \
    --requirepass redispassword

# FalkorDB (port 16379)
docker run -d -p 16379:16379 --name kntgraph-falkordb \
    falkordb/falkordb:latest

# Ollama (port 11434) with the qwen3.5:4b model
docker run -d -p 11434:11434 --name kntgraph-ollama \
    ollama/ollama:latest
ollama pull qwen3.5:4b
```

### Build artifacts (do NOT commit)

The `.gitignore` already excludes the build
artifacts (the `build/`, `dist/`, `.venv/`,
`.pytest_cache/`, `.ruff_cache/`, `.coverage`,
`.egg-info/`, etc. patterns are
[setuptools' defaults](https://github.com/github/gitignore/blob/main/Python.gitignore)
plus a few project-specific entries).

Scratch / debug scripts at the repo root
(`scratch_*.py`) are NOT part of the build
artifact set; they are removed from tracking
(2026-07-14) because they were one-off debug
helpers, not production code. Add new scratch
scripts inside `scripts/` (or
`/tmp/opencode/`) so the project's
`__init__.py` layout and the gate's test
discovery stay clean.

---

## See also

  - `CONTRIBUTING.md` — the PR workflow.
  - `DEBT.md` — the project's tech-debt log
    (per-ADR close notes; the canonical
    place to surface a follow-up before it
    becomes a bug).
  - `CHANGELOG.md` — the version history; the
    `[Unreleased]` section is where new work
    lands before a release.
  - `ADRs/` — Architecture Decision Records;
    every significant change is documented
    here before it lands in code.
