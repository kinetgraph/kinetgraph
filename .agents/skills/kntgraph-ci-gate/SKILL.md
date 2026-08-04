---
name: kntgraph-ci-gate
description: Use when running or debugging the kntgraph CI gate (scripts/ci.py) — running the full 10-step gate, iterating on a single step with --only, understanding which tool each step uses, and reading what counts as a pass/fail. Covers py_compile, ruff check, ruff format --check, radon cc/mi, REUSE 3.3, pyright, pytest, branch coverage, bandit, and pip-audit. Trigger keywords: ci.py, scripts/ci.py, --only, syntax, lint, format, complexity, radon, REUSE, pyright, tests, reliability, coverage, branch coverage, bandit, pip-audit, KNT_REDIS_FAKE, ci gate, gate, baseline, regression.
---

# The single CI gate (`scripts/ci.py`)

The mandatory gate is one command:

```bash
uv run scripts/ci.py
```

It runs **10 steps in order**. All 10 must pass. There is no best-effort mode; the only way to skip a step is `uv run scripts/ci.py --only <step>` (e.g. `--only lint`), which selects that step to the exclusion of the others (for local iteration). The pre-commit hook runs the full set without flags.

| Step          | Tool                  | Description                                       |
| ------------- | --------------------- | ------------------------------------------------- |
| `syntax`      | `py_compile`          | Compiles every `.py` in `src/` and `tests/`       |
| `lint`        | `ruff check`          | Lints with the rules `E, F, W, I, UP, B, A, C4, SIM` |
| `format`      | `ruff format --check` | Verifies the canonical format (zero diffs)        |
| `complexity`  | `radon cc/mi`         | CC ≤ 10 per block, MI ≥ 20 per file, no regression vs `.radon-baseline.json` |
| `reuse`       | `REUSE 3.3`           | License compliance (SPDX headers + `LICENSES/`)   |
| `pyright`     | `pyright`             | Static type check (against the existing baseline) |
| `tests`       | `pytest`              | Unit tests; integration tests when Redis is available |
| `reliability` | `coverage.py`         | Branch coverage on `stream/`, `runner/`, `security/`; regression vs `.reliability-baseline.json` |
| `bandit`      | `bandit`              | Security scan (`B110` filtered at severity medium) |
| `audit`       | `pip-audit`           | Vulnerability scan of the resolved dep tree      |

The reliability gate mirrors the `pyright` and `complexity` patterns:

- **Without** `.reliability-baseline.json`: hard fail only if branch coverage is below 100% on the overall metric (the gate records the current numbers and tells the operator how to freeze them).
- **With** a baseline: hard fail on regression (a drop in overall or any per-path branch coverage).
- The target paths are the safety-critical framework subset: `src/kntgraph/stream/`, `src/kntgraph/runner/`, `src/kntgraph/security/`. Verticals (`agents/`, `knowledge/`, `events/`, `memory/`, `api/`, `cli/`) are deliberately excluded per the type-discipline §1.2 (the framework never depends on verticals; structural coverage on vertical code would couple the gate to domain semantics).
- The metric is **branch coverage** (not line coverage) because branch coverage is the floor for any future MC/DC work. The follow-up mutmut step will ride on the same baseline JSON; the schema is already designed to extend without a breaking change.
- The test surface is the same set the `tests` step runs (`tests/unit/`, `tests/agents/unit/`, `tests/scripts/`). `KNT_REDIS_FAKE` does not change the gate's behaviour: both modes run the unit suite, and the unit suite uses fakeredis by default. The integration tests in `tests/integration/` are out of scope (run separately, not in the main gate).

Update the baseline after intentional refactors:

```bash
uv run scripts/ci.py --update-reliability-baseline
```

## Iteration recipes

```bash
# Quick local check (no integration, no bandit, no audit)
KNT_REDIS_FAKE=1 uv run scripts/ci.py --only lint
KNT_REDIS_FAKE=1 uv run scripts/ci.py --only format
KNT_REDIS_FAKE=1 uv run scripts/ci.py --only tests
KNT_REDIS_FAKE=1 uv run scripts/ci.py --only reliability

# Fast pytest loop
KNT_REDIS_FAKE=1 uv run pytest tests/unit/ -q

# Reliability-only loop (rebuilds the coverage JSON)
KNT_REDIS_FAKE=1 uv run python scripts/reliability_gate.py

# Refresh the reliability baseline (after intentional refactor)
KNT_REDIS_FAKE=1 uv run python scripts/reliability_gate.py --update

# Quick linter loop
uv run ruff check . && uv run ruff format --check .
```
