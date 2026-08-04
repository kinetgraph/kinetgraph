---
name: kntgraph-ci-gate
description: Use when running or debugging the kntgraph CI gate (scripts/ci.py) — running the full 9-step gate, iterating on a single step with --only, understanding which tool each step uses, and reading what counts as a pass/fail. Covers py_compile, ruff check, ruff format --check, radon cc/mi, REUSE 3.3, pyright, pytest, bandit, and pip-audit. Trigger keywords: ci.py, scripts/ci.py, --only, syntax, lint, format, complexity, radon, REUSE, pyright, tests, bandit, pip-audit, KNT_REDIS_FAKE, ci gate, gate.
---

# The single CI gate (`scripts/ci.py`)

The mandatory gate is one command:

```bash
uv run scripts/ci.py
```

It runs **9 steps in order**. All 9 must pass. There is no best-effort mode; the only way to skip a step is `uv run scripts/ci.py --only <step>` (e.g. `--only lint`), which selects that step to the exclusion of the others (for local iteration). The pre-commit hook runs the full set without flags.

| Step         | Tool                  | Description                                       |
| ------------ | --------------------- | ------------------------------------------------- |
| `syntax`     | `py_compile`          | Compiles every `.py` in `src/` and `tests/`       |
| `lint`       | `ruff check`          | Lints with the rules `E, F, W, I, UP, B, A, C4, SIM` |
| `format`     | `ruff format --check` | Verifies the canonical format (zero diffs)        |
| `complexity` | `radon cc/mi`         | CC ≤ 10 per block, MI ≥ 20 per file, no regression vs `.radon-baseline.json` |
| `reuse`      | `REUSE 3.3`           | License compliance (SPDX headers + `LICENSES/`)   |
| `pyright`    | `pyright`             | Static type check (against the existing baseline) |
| `tests`      | `pytest`              | Unit tests; integration tests when Redis is available |
| `bandit`     | `bandit`              | Security scan (`B110` filtered at severity medium) |
| `audit`      | `pip-audit`           | Vulnerability scan of the resolved dep tree      |

## Iteration recipes

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
