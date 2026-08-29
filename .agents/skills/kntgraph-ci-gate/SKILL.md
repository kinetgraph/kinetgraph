---
name: kntgraph-ci-gate
description: Use when running or debugging the kntgraph CI gate (scripts/ci.py) — running the full 12-step gate, iterating on a single step with --only, understanding which tool each step uses, and reading what counts as a pass/fail. Covers py_compile, ruff check, ruff format --check, radon cc/mi, REUSE 3.3, pyright, pytest (unit + integration), branch coverage on framework and verticals, bandit, and pip-audit. Trigger keywords: ci.py, scripts/ci.py, --only, syntax, lint, format, complexity, radon, REUSE, pyright, tests, integration, reliability, verticals, coverage, branch coverage, bandit, pip-audit, KNT_REDIS_FAKE, ci gate, gate, baseline, regression.
---

<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->


# The single CI gate (`scripts/ci.py`)

The mandatory gate is one command:

```bash
uv run scripts/ci.py
```

It runs **12 steps in order**. All 12 must pass. There is no best-effort mode; the only way to skip a step is `uv run scripts/ci.py --only <step>` (e.g. `--only lint`), which selects that step to the exclusion of the others (for local iteration). The pre-commit hook runs the full set without flags.

| Step          | Tool                  | Description                                       |
| ------------- | --------------------- | ------------------------------------------------- |
| `syntax`      | `py_compile`          | Compiles every `.py` in `src/` and `tests/`       |
| `lint`        | `ruff check`          | Lints with the rules `E, F, W, I, UP, B, A, C4, SIM` |
| `format`      | `ruff format --check` | Verifies the canonical format (zero diffs)        |
| `complexity`  | `radon cc/mi`         | CC ≤ 10 per block, MI ≥ 20 per file, no regression vs `.radon-baseline.json` |
| `reuse`       | `REUSE 3.3`           | License compliance (SPDX headers + `LICENSES/`)   |
| `pyright`     | `pyright`             | Static type check (against the existing baseline) |
| `tests`       | `pytest`              | Unit tests; fakeredis by default (`KNT_REDIS_FAKE=1`) |
| `integration` | `pytest`              | Framework integration tests (opt-in via `--only`); Redis + FalkorDB + LLM |
| `reliability` | `coverage.py`         | Branch coverage on `stream/`, `runner/`, `security/`; regression vs `.reliability-baseline.json` |
| `verticals`   | `coverage.py`         | Branch coverage on `agents/`, `api/`, `cli/`, `events/`, `knowledge/`, `memory/`; regression vs `.verticals-baseline.json` |
| `bandit`      | `bandit`              | Security scan (`B110` filtered at severity medium) |
| `audit`       | `pip-audit`           | Vulnerability scan of the resolved dep tree      |

### The two coverage gates

The `reliability` and `verticals` gates are deliberately parallel: same machinery (coverage.py, branch coverage, regression vs baseline), different scope and different ownership. They write to separate JSON files (`.coverage-reliability.json` and `.coverage-verticals.json`) and separate baseline files, so neither pollutes the other.

**`reliability`** measures the safety-critical framework subset: `src/kntgraph/stream/`, `src/kntgraph/runner/`, `src/kntgraph/security/`. A regression here is an MC/DC-floor signal; the framework team owns the action.

**`verticals`** measures the vertical packages per the type-discipline skill (§1.2): `src/kntgraph/agents/`, `api/`, `cli/`, `events/`, `knowledge/`, `memory/`. A regression here is a domain-quality signal; the vertical owner owns the action.

Conflating them under one `TARGET_PATHS` list would dilute both signals: vertical refactors (which legitimately remove code paths) would fail a gate whose design rationale is "framework MC/DC floor", and the overall branch-coverage number would be dominated by the much larger vertical surface, making per-path regressions in either scope invisible.

Both gates mirror the `pyright` and `complexity` patterns:

- **Without** a baseline: hard fail only if branch coverage is below 100% on the overall metric (the gate records the current numbers and tells the operator how to freeze them).
- **With** a baseline: hard fail on regression (a drop in overall or any per-path branch coverage).

The metric is **branch coverage** (not line coverage) because branch coverage is the floor for any future MC/DC work. The follow-up mutmut step will ride on the same baseline JSON; the schema is already designed to extend without a breaking change.

The test surface for both gates is the same set the `tests` step runs (`tests/unit/`, `tests/agents/unit/`, `tests/scripts/`). `KNT_REDIS_FAKE` does not change the gates' behaviour: both modes run the unit suite, and the unit suite uses fakeredis by default. The framework integration tests (`tests/integration/test_dlq.py`, `test_event_log.py`, `test_reactive_dispatcher.py`, `test_runner.py`) are a separate opt-in step: `uv run scripts/ci.py --only integration`. They run without coverage flags — neither gate measures them.

Update the baselines after intentional refactors:

```bash
uv run scripts/ci.py --update-reliability-baseline
uv run scripts/ci.py --update-verticals-baseline
```

## Iteration recipes

```bash
# Quick local check (no integration, no bandit, no audit)
KNT_REDIS_FAKE=1 uv run scripts/ci.py --only lint
KNT_REDIS_FAKE=1 uv run scripts/ci.py --only format
KNT_REDIS_FAKE=1 uv run scripts/ci.py --only tests
KNT_REDIS_FAKE=1 uv run scripts/ci.py --only reliability
KNT_REDIS_FAKE=1 uv run scripts/ci.py --only verticals

# Framework integration tests (requires real Redis / FalkorDB / LLM)
uv run scripts/ci.py --only integration

# Fast pytest loop
KNT_REDIS_FAKE=1 uv run pytest tests/unit/ -q

# Reliability-only loop (rebuilds the coverage JSON)
KNT_REDIS_FAKE=1 uv run python scripts/reliability_gate.py

# Verticals-only loop (rebuilds the coverage JSON)
KNT_REDIS_FAKE=1 uv run python scripts/verticals_gate.py

# Refresh the baselines (after intentional refactor)
KNT_REDIS_FAKE=1 uv run python scripts/reliability_gate.py --update
KNT_REDIS_FAKE=1 uv run python scripts/verticals_gate.py --update

# Quick linter loop
uv run ruff check . && uv run ruff format --check .
```
