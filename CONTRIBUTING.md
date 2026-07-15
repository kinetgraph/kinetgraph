<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Contributing to kntgraph

Thank you for your interest in contributing to
kntgraph! This document covers the development
setup, the gates that run in CI, and the pull
request workflow.

## Development setup

kntgraph is a Python project (3.12+). We use
[`uv`](https://docs.astral.sh/uv/) for dependency
management and [pytest](https://docs.pytest.org/)
for tests.

```bash
# Clone
git clone https://github.com/kinetgraph/kntgraph.git
cd kntgraph

# Install all extras (dev, falkordb, ollama, gliner, api, crypto, llm)
uv sync --all-extras

# Run the gate (mandatory before any commit)
uv run scripts/ci.py
```

The gate runs 9 steps in order:

| Step        | Tool             | Description                              |
| ----------- | ---------------- | ---------------------------------------- |
| `syntax`    | `py_compile`     | Compiles every `.py` in `src/` and `tests/` |
| `lint`      | `ruff check`     | Lints with the rules `E, F, W, I, UP, B, A, C4, SIM` |
| `format`    | `ruff format --check` | Verifies the canonical format (zero diffs) |
| `complexity` | `radon cc/mi`  | CC ≤ 10 per block, MI ≥ 20 per file, no regression |
| `reuse`     | `REUSE 3.3`      | License compliance (SPDX headers + `LICENSES/`) |
| `pyright`   | `pyright`        | Static type check (against the existing baseline) |
| `tests`     | `pytest`         | Unit tests; integration tests when Redis is available |
| `bandit`    | `bandit`         | Security scan (`B110` filtered at severity medium) |
| `audit`     | `pip-audit`      | Vulnerability scan of the resolved dep tree |

All 9 steps are mandatory. There is no
best-effort mode in the canonical run; the only
way to skip a step is `uv run scripts/ci.py
--only <step>`, which selects that step and
excludes the others (useful for local iteration).

## License compliance

The project follows the
[REUSE 3.3 specification](https://reuse.software/spec-3.3/)
for license and copyright metadata. Every file
carries an SPDX header at the top:

```python
# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
```

When you add a new file, run:

```bash
uv run --with reuse reuse annotate \
  --copyright "kinetgraph" \
  --license "Apache-2.0" \
  --year 2026 \
  path/to/new_file.py
```

The `LICENSES/` directory at the root holds the
full text of every license referenced in any
SPDX header (downloaded via `reuse download
<SPDX-ID>`). The `.reuse/REUSE.toml` declares
per-path overrides for generated artefacts
(`htmlcov/`, `__pycache__/`, `*.json`
baselines) that are not part of the source
distribution.

Verify with `uv run --with reuse reuse --root
. lint`. The project is compliant when this
prints "Congratulations".

## Architecture Decision Records

We document significant decisions in
[`ADRs/`](ADRs/). If your change touches the
public API, the data model, the concurrency
model, or the storage layout, write a new ADR
first and link it in the PR description.

ADRs follow this template:

```markdown
# ADR-NNN: Title

**Status:** Proposed | Accepted | Deprecated
**Date:** DD de mês de AAAA
**Related:** [ADR-XXX](./ADR-XXX-titulo.md)

## 1. Contexto
## 2. Decisão
## 3. Consequências
## 4. Migration (se aplicável)
## 5. Decisões relacionadas
## 6. Referências
```

ADRs are written in **English** (per
AGENTS.md §10). Existing ADRs in PT-BR remain
as historical records.

## Style guide

- **Prose in English** (docstrings, comments,
  docs). Identifiers follow the domain
  (English where natural: `ReactiveDispatcher`,
  `SolutionExtractor`; PT-BR-derived names
  where the domain uses them: `ContinuityManager`).
- **Type hints everywhere**; `Any` and bare
  `object` are forbidden in framework code
  (AGENTS.md §1). Use the
  `JsonValue = Union[str, int, float, bool, None,
  dict[str, "JsonValue"], list["JsonValue"]]`
  union for JSON-shaped data.
- **Async-first**: every I/O is async. The
  `WorldSystem.__call__` interface is **pure**
  (no I/O, no `await`, no `time.sleep`); tools
  that need I/O run in the `WorkerManager`
  process, not in the dispatcher.
- **Idempotency**: operations must be idempotent.
  The `event_id` is a UUID5 over stable inputs;
  the `EventLog.append` is idempotent
  (`dedup_keys`); the `World.fold` is idempotent
  (re-applying the same event produces the same
  World).
- **Errors are typed**: never `raise Exception`.
  Use the framework's `Result[T, E]` for
  expected failures, typed `*Error` classes for
  domain errors.
- **No compat shims** (AGENTS.md §2). When an
  API changes, update all call sites in the
  same commit. Do not add kwargs-optional
  branches that detect the old API at runtime.
- **File size**: files > 500 lines should be
  split into sub-modules per AGENTS.md §3.
  Sub-modules use the `_private.py` prefix;
  the public `__init__.py` re-exports the API.

## Testing

```bash
# Unit tests (fast, no external deps)
uv run --package kntgraph pytest kntgraph/tests/unit/

# Integration tests (require Redis on localhost:6379)
docker run -d -p 6379:6379 --name kntgraph-redis redis:latest
uv run --package kntgraph pytest kntgraph/tests/integration/
```

When adding a new feature:

- Cover the happy path and at least one
  failure mode per public function.
- Prefer behaviour tests over mock-heavy unit
  tests (AGENTS.md §7). Use the real
  `EventLog`, the real `World`, etc. Mock
  only when the external system is unavailable
  in CI (e.g. GLiNER2 with GPU).

## Pull request workflow

1. **Branch** from `main`:
   - `feat/<topic>` for new features
   - `fix/<topic>` for bug fixes
   - `docs/<topic>` for documentation only
2. **Commit** atomically. One commit = one
   logical change. Reference the ADR when
   applicable (`feat(agents): ... (ADR-013)`).
3. **Run the gate locally**:
   `uv run scripts/ci.py`. All 8 steps must
   pass.
4. **Open a PR** against `main`. CI will run
   the same gate; PRs that fail any step are
   blocked from merge.
5. **Review**: at least one human approval is
   required before merge.
6. **Merge**: squash or rebase. No merge
   commits.

## Reporting bugs

Use the GitHub issue tracker. Include:

- A minimal reproduction (script or snippet).
- The output of `uv run --package kntgraph
  python -c "import kntgraph; print(kntgraph.__version__)"`
  (or the version you ran).
- The Python version (`python --version`).
- The OS and architecture.
- The relevant log output (use `structlog`
  JSON output if possible).

## Security vulnerabilities

See [SECURITY.md](SECURITY.md). **Do not**
open a public issue for security bugs — use
the private disclosure channel documented there.
