# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""Verticals gate — branch coverage on the vertical packages.

Parallel to ``scripts/reliability_gate.py``: same machinery
(coverage.py, branch coverage, regression vs baseline), different
scope and different ownership. The reliability gate measures the
safety-critical framework subset (``stream/``, ``runner/``,
``security/``); the verticals gate measures the domain packages
the verticals own per the type-discipline skill (§1.2):

    src/kntgraph/agents/     — agent runtime, role systems, memory, tools
    src/kntgraph/api/        — FastAPI surface, intent router, schemas
    src/kntgraph/cli/        — `knt` CLI commands
    src/kntgraph/events/     — event taxonomy
    src/kntgraph/knowledge/  — extraction, graph adapter
    src/kntgraph/memory/     — continuity, principals, sessions

The two gates are deliberately separate so that:

  - A failure in the reliability gate is an MC/DC-floor signal
    ("framework safety-critical path regressed"). The action is
    add a test or fix the code.
  - A failure in the verticals gate is a domain-quality signal
    ("a vertical path dropped coverage below its baseline"). The
    action belongs to the vertical owner, not the framework team.

Conflating them under one TARGET_PATHS list would dilute both
signals: vertical refactors (which legitimately remove code
paths) would fail a gate whose design rationale is "framework
MC/DC floor", and the overall branch-coverage number would be
dominated by the much larger vertical surface, making per-path
regressions in either scope invisible.

Branch coverage is the metric for the same reason as the
reliability gate: it's the floor for any future MC/DC work, and
the schema is designed to extend with mutation data without a
breaking change.

Usage:
    uv run python scripts/verticals_gate.py            # gate (fail on regression)
    uv run python scripts/verticals_gate.py --update   # refresh baseline

Exit codes:
    0  pass (or baseline updated)
    1  regression: branch coverage dropped below baseline
    2  hard fail: no baseline and branch coverage < 100
    3  no test ran (pytest exit 5 with "no tests ran")
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / ".verticals-baseline.json"
COVERAGE_JSON = ROOT / ".coverage-verticals.json"

# Vertical packages per the type-discipline skill (§1.2).
# These are the domain packages the framework gate deliberately
# excludes; the verticals gate owns their coverage signal.
TARGET_PATHS = (
    "src/kntgraph/agents/",
    "src/kntgraph/api/",
    "src/kntgraph/cli/",
    "src/kntgraph/events/",
    "src/kntgraph/knowledge/",
    "src/kntgraph/memory/",
)

# Tests that exercise the vertical paths. Same set the
# `tests` step in `scripts/ci.py` runs — narrower test
# surfaces produce misleading coverage numbers, and we want
# "branch coverage of the verticals under the project's
# full unit test suite", not a curated subset.
PYTEST_TARGETS = (
    "tests/unit/",
    "tests/agents/unit/",
    "tests/scripts/",
)


def _run_coverage() -> int:
    """Run pytest under coverage and emit the JSON report.

    Returns the pytest exit code. The verticals gate inherits
    the same pytest exit-5 ("no tests ran") tolerance as the
    main `tests` step in `scripts/ci.py` (see `_run_step`
    there) — the skip is a known case for optional-dependency
    test directories.
    """
    cmd = [
        "uv",
        "run",
        "pytest",
        *PYTEST_TARGETS,
        "-q",
        "--no-header",
        "--cov=src/kntgraph",
        "--cov-branch",
        f"--cov-report=json:{COVERAGE_JSON}",
    ]
    return subprocess.call(cmd, cwd=ROOT)


def _branch_summary(data: dict, path: str) -> dict[str, int]:
    """Aggregate per-file branch counts under `path`.

    `data` is the `coverage.py` JSON. Returns
    `{covered_branches, num_branches}` summed over every file
    whose path starts with `path`.
    """
    covered = 0
    total = 0
    for file_path, info in data.get("files", {}).items():
        if not file_path.startswith(path):
            continue
        summary = info.get("summary", {})
        covered += summary.get("covered_branches", 0)
        total += summary.get("num_branches", 0)
    return {"covered_branches": covered, "num_branches": total}


def _snapshot(data: dict) -> dict:
    """Build the baseline payload from a coverage JSON.

    Keys are stable; the mutmut step (Phase 2 of the MC/DC
    work) will add `mutation_score`, `mutation_killed`,
    `mutation_survived`, `mutation_skipped` without breaking
    this schema.
    """
    per_path: dict[str, dict[str, int]] = {}
    for path in TARGET_PATHS:
        per_path[path] = _branch_summary(data, path)
    overall = _branch_summary(
        data, "src/kntgraph/"
    )  # covers everything, not just targets
    return {
        "schema_version": 1,
        "tool": "coverage.py",
        "metric": "branch_coverage_percent",
        "scope": "verticals",
        "overall": overall,
        "target_paths": per_path,
    }


def _percent(covered: int, total: int) -> float:
    return round(100.0 * covered / total, 2) if total else 100.0


def _print_summary(snapshot: dict) -> None:
    overall = snapshot["overall"]
    overall_pct = _percent(overall["covered_branches"], overall["num_branches"])
    print(
        f"  overall branch coverage: {overall_pct}% "
        f"({overall['covered_branches']}/{overall['num_branches']} branches)"
    )
    for path, counts in snapshot["target_paths"].items():
        pct = _percent(counts["covered_branches"], counts["num_branches"])
        print(
            f"  {path:<32} {pct:>6}% "
            f"({counts['covered_branches']}/{counts['num_branches']})"
        )


def _check(snapshot: dict, baseline: dict | None) -> int:
    """Compare snapshot to baseline.

    Returns 0 on pass, 1 on regression, 2 on hard fail.
    The hard-fail-without-baseline threshold (100% of overall
    branches) is intentionally generous: until the team has
    seen a baseline, the gate should record numbers, not block
    the build. Once `.verticals-baseline.json` is committed,
    any drop below it is a regression and the gate fails.
    """
    overall = snapshot["overall"]
    overall_pct = _percent(overall["covered_branches"], overall["num_branches"])

    if baseline is None:
        print(
            f"\nNo verticals baseline found — {overall_pct}% branch coverage. "
            f"Run `uv run python scripts/verticals_gate.py --update` "
            f"to freeze the current state."
        )
        return 0 if overall_pct >= 100.0 else 2

    base_overall = baseline["overall"]
    base_pct = _percent(base_overall["covered_branches"], base_overall["num_branches"])
    delta = round(overall_pct - base_pct, 2)

    print(
        f"\nBranch coverage: {overall_pct}% "
        f"(baseline: {base_pct}%, delta: {delta:+.2f}pp)"
    )

    if delta < 0:
        regressed = []
        for path in TARGET_PATHS:
            cur = snapshot["target_paths"][path]
            base = baseline["target_paths"].get(path)
            if not base:
                continue
            cur_pct = _percent(cur["covered_branches"], cur["num_branches"])
            base_path_pct = _percent(base["covered_branches"], base["num_branches"])
            if cur_pct < base_path_pct:
                regressed.append(f"  {path}: {base_path_pct}% -> {cur_pct}%")
        if regressed:
            print("Regressed paths:")
            print("\n".join(regressed))
        print(
            "\nRegression: branch coverage dropped below baseline. "
            "Add tests or run `--update` if the drop is intentional."
        )
        return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="verticals gate")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Regenerate .verticals-baseline.json from the current run",
    )
    args = parser.parse_args()

    print(
        ">>> verticals (branch coverage on agents + api + cli + events + knowledge + memory)"
    )
    rc = _run_coverage()
    if rc == 5:
        print(
            "  >>> tolerated: pytest exit 5 ('no tests ran') — "
            "no target tests ran. Run `uv sync` to enable them."
        )
        return 3
    if rc != 0:
        print(f"pytest failed (exit {rc}); verticals gate cannot run.")
        return rc

    if not COVERAGE_JSON.exists():
        print(f"coverage JSON not found at {COVERAGE_JSON}; cannot proceed.")
        return 2

    data = json.loads(COVERAGE_JSON.read_text())
    snapshot = _snapshot(data)
    _print_summary(snapshot)

    if args.update:
        BASELINE_PATH.write_text(json.dumps(snapshot, indent=2) + "\n")
        print(f"\nBaseline written to {BASELINE_PATH.relative_to(ROOT)}")
        return 0

    baseline: dict | None = None
    if BASELINE_PATH.exists():
        baseline = json.loads(BASELINE_PATH.read_text())

    return _check(snapshot, baseline)


if __name__ == "__main__":
    sys.exit(main())
