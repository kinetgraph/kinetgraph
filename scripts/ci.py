# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
#
# /// script
# requires-python = ">=3.12"
# ///
"""
ci — kntgraph quality gates (single source of truth).

Usage:
    uv run scripts/ci.py                   # run all gates (mandatory)
    uv run scripts/ci.py --baseline        # generate complexity baseline
    uv run scripts/ci.py --update-baseline # regenerate baseline after refactor
    uv run scripts/ci.py --only <step>     # run one step only
    uv run scripts/ci.py --verbose         # show offenders on failure

All gates are MANDATORY. There is no best-effort mode in the
canonical run; the only way to bypass a step is to pass
``--only <step>`` (e.g. ``--only lint``) which selects that
step to the exclusion of all others. The pre-commit hook
runs the full set without flags.

Steps (in order):
    syntax      py_compile on src/**/*.py + tests/**/*.py
    lint        ruff check on src/
    format      ruff format --check (zero diffs required)
    complexity  radon cc/mi hard gates + regression vs .radon-baseline.json
    reuse       REUSE 3.3 license compliance (480+ files)
    pyright     static type check
    tests       pytest unit tests
    bandit      security scan
    audit       pip-audit CVE scan

The complexity gate (ADR-019):
    CC ≤ 10 (radon grade B) per block — hard fail without baseline
    MI ≥ 20 (radon grade A) per file  — hard fail without baseline
    No regression vs .radon-baseline.json — hard fail with baseline
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / ".radon-baseline.json"
PYRIGHT_BASELINE_PATH = ROOT / ".pyright-baseline.json"

KNT_KNITGRAPH_SRC = ROOT / "src" / "kntgraph"
KNT_KNITGRAPH_TESTS = ROOT / "tests"


@dataclass
class Step:
    name: str
    cmd: tuple[str, ...]

    def run(self, *, capture: bool = True) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            " ".join(self.cmd),
            shell=True,
            cwd=ROOT,
            check=False,
            capture_output=capture,
            text=True,
        )


def _resolve(cmd_name: str) -> str:
    """Return the path to an executable, falling back to
    ``uv run`` for project deps. Raises if not found."""
    direct = shutil.which(cmd_name)
    if direct:
        return direct
    return cmd_name  # let uv resolve it


def step_syntax() -> Step:
    files: list[str] = []
    for sub in (KNT_KNITGRAPH_SRC, KNT_KNITGRAPH_TESTS):
        for p in sub.rglob("*.py"):
            files.append(str(p))
    return Step(
        "syntax (py_compile)",
        (
            "uv",
            "run",
            "python",
            "-m",
            "py_compile",
            *files,
        ),
    )


def step_lint() -> Step:
    return Step(
        "lint (ruff)",
        (
            "uv",
            "run",
            "ruff",
            "check",
            "src/",
            "tests/",
        ),
    )


def step_format() -> Step:
    return Step(
        "format (ruff format --check)",
        (
            "uv",
            "run",
            "ruff",
            "format",
            "--check",
            "src/",
            "tests/",
            "scripts/",
        ),
    )


def step_reuse() -> Step:
    """REUSE 3.3 license compliance check.

    The ``reuse`` tool is a dev-time install (not a
    runtime dep of the framework). We invoke it
    via ``uv run --with reuse`` to keep the project
    pyproject lean.

    Note: ``reuse lint`` does not honour the
    process cwd unless ``--root`` is given. We pass
    ``--root .`` (the Step already runs in
    ``$ROOT`` = the kntgraph/ root) so the tool
    only scans the kntgraph/ tree, not the parent
    monorepo.
    """
    return Step(
        "license (REUSE 3.3)",
        (
            "uv",
            "run",
            "--with",
            "reuse",
            "reuse",
            "--root",
            ".",
            "lint",
        ),
    )


def step_pyright() -> Step:
    """Static type check. Reports errors to stdout
    (JSON); the step fails if there are any errors
    in the ``errorCount`` summary.

    The baseline + regression policy lives in the
    monorepo's `scripts/ci.py`; the standalone
    kntgraph gate uses a strict (no baseline) policy
    until the project stabilises. New contributors
    should fix pyright errors before opening a PR.
    """
    return Step(
        "type-check (pyright)",
        (
            "uv",
            "run",
            "pyright",
            "src/kntgraph/",
        ),
    )


def step_tests() -> Step:
    return Step(
        "unit tests (pytest)",
        (
            "uv",
            "run",
            "pytest",
            "tests/unit/",
            "tests/agents/unit/",
            "-q",
        ),
    )


def step_bandit() -> Step:
    return Step(
        "security (bandit)",
        (
            "uv",
            "run",
            "bandit",
            "-r",
            "src/",
            "-q",
            "--severity-level",
            "medium",
        ),
    )


def step_audit() -> Step:
    """Run ``pip-audit`` against the resolved dep
    tree. Uses ``uv export`` to materialise a
    ``requirements.txt`` (without workspace members)
    and then ``pip-audit --strict`` to fail on any
    known CVE in the pinned versions.
    """
    return Step(
        "audit (pip-audit)",
        (
            "bash",
            "-c",
            "set -e; "
            "uv export --format requirements-txt --no-hashes "
            "--no-emit-workspace > /tmp/kntgraph-reqs.txt; "
            "uv run pip-audit --strict -r /tmp/kntgraph-reqs.txt "
            "--vulnerability-service osv",
        ),
    )


def _run_radon_cc() -> dict[str, Any]:
    result = subprocess.run(
        (
            "uv",
            "run",
            "radon",
            "cc",
            str(KNT_KNITGRAPH_SRC),
            "-s",
            "-a",
            "-j",
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"radon cc failed: exit {result.returncode}")
    return json.loads(result.stdout or "{}")


def _run_radon_mi() -> dict[str, Any]:
    result = subprocess.run(
        (
            "uv",
            "run",
            "radon",
            "mi",
            str(KNT_KNITGRAPH_SRC),
            "-s",
            "-j",
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"radon mi failed: exit {result.returncode}")
    return json.loads(result.stdout or "{}")


def _cc_snapshot(cc_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    flat: dict[str, dict[str, Any]] = {}
    for filepath, blocks in cc_data.items():
        for block in blocks:
            key = f"{filepath}:{block['type']}:{block['name']}"
            flat[key] = {
                "complexity": block["complexity"],
                "rank": block["rank"],
                "lineno": block["lineno"],
            }
    return flat


def _mi_snapshot(mi_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        filepath: {"mi": float(info["mi"]), "rank": info["rank"]}
        for filepath, info in mi_data.items()
    }


def cmd_baseline() -> int:
    print(">>> Generating radon baseline...")
    cc = _run_radon_cc()
    mi = _run_radon_mi()
    snapshot = {
        "cc": _cc_snapshot(cc),
        "mi": _mi_snapshot(mi),
    }
    BASELINE_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    cc_offenders = sum(1 for v in snapshot["cc"].values() if v["complexity"] > 10)
    mi_offenders = sum(1 for v in snapshot["mi"].values() if v["mi"] < 20)
    print(f"Baseline written: {BASELINE_PATH}")
    print(f"  Tracked: {len(snapshot['cc'])} CC blocks, {len(snapshot['mi'])} files")
    print(f"  CC offenders: {cc_offenders}, MI offenders: {mi_offenders}")
    return 0


def gate_complexity(verbose: bool = False) -> bool:
    """Radon CC + MI gate with regression detection.

    Policy:
      - Without a baseline: hard fail on CC > 10 or MI < 20.
      - With a baseline: hard fail only on regression
        (CC up, MI down). Pre-existing offenders are
        tolerated under the "baseline + regression"
        policy (AGENTS.md §11.3).
    """
    print("\n>>> complexity (radon cc/mi)")
    cc = _run_radon_cc()
    mi = _run_radon_mi()
    cc_snap = _cc_snapshot(cc)
    mi_snap = _mi_snapshot(mi)

    offenders_cc = {k: v for k, v in cc_snap.items() if v["complexity"] > 10}
    offenders_mi = {k: v for k, v in mi_snap.items() if v["mi"] < 20}

    has_baseline = BASELINE_PATH.exists()
    regressions: list[str] = []

    if has_baseline:
        baseline = json.loads(BASELINE_PATH.read_text())
        base_cc = baseline.get("cc", {})
        base_mi = baseline.get("mi", {})

        # CC regression: any block that grew.
        for key, cur in cc_snap.items():
            base = base_cc.get(key)
            if base and cur["complexity"] > base["complexity"]:
                regressions.append(
                    f"CC grew for {key}: {base['complexity']} -> {cur['complexity']}"
                )

        # MI regression: any file that dropped.
        for key, cur in mi_snap.items():
            base = base_mi.get(key)
            if base and cur["mi"] < base["mi"]:
                regressions.append(f"MI down {key}: {base['mi']} -> {cur['mi']}")

    print(
        f"CC offenders: {len(offenders_cc)} "
        f"(baseline: {len(offenders_cc) if not has_baseline else 'see .radon-baseline.json'}, "
        f"delta: {'n/a' if not has_baseline else len(regressions)})"
    )

    if not has_baseline:
        if offenders_cc or offenders_mi:
            print(
                f"  Hard fail: {len(offenders_cc)} CC > 10, "
                f"{len(offenders_mi)} MI < 20. "
                f"Run `uv run scripts/ci.py --update-baseline` "
                f"to freeze the current state."
            )
            if verbose:
                for k, v in list(offenders_cc.items())[:10]:
                    print(f"    CC {v['complexity']:>3} {k}")
                for k, v in list(offenders_mi.items())[:10]:
                    print(f"    MI {v['mi']:>5.1f} {k}")
        else:
            print("  All CC ≤ 10 and MI ≥ 20 — clean.")
        return not (offenders_cc or offenders_mi)

    if regressions:
        print(f"\nRegression gate failed: {len(regressions)} occurrence(s)")
        for r in regressions:
            print(f"  {r}")
        return False
    return True


# ---------------------------------------------------------------------------
# Step orchestration
# ---------------------------------------------------------------------------


ALL_STEPS: dict[str, Step] = {
    "syntax": step_syntax(),
    "lint": step_lint(),
    "format": step_format(),
    "complexity": Step(
        "complexity (radon cc/mi)", ("_inline_gate_complexity_",)
    ),  # placeholder
    "pyright": Step("type-check (pyright)", ("_inline_gate_pyright_",)),  # placeholder
    "tests": step_tests(),
    "bandit": step_bandit(),
    "audit": step_audit(),
}


def _run_step(step: Step, failed: list[str], *, capture: bool = True) -> str:
    """Run a step. Any non-zero exit adds the step to
    the failed list — there is no best-effort mode.
    When ``capture=True`` (default), the step's
    stdout/stderr are suppressed on success and
    printed on failure. When ``capture=False``, all
    output is streamed live.
    """
    r = step.run(capture=capture)
    if r.returncode != 0:
        failed.append(step.name)
        if r.stdout:
            print(r.stdout)
        if r.stderr:
            print(r.stderr, file=sys.stderr)
    return r.stdout or ""


def gate_pyright() -> bool:
    """Pyright type-check gate with regression detection.

    Mirrors ``gate_complexity``:
      - Without a baseline: hard fail on any error.
      - With a baseline: hard fail only on regression
        (new errors vs the baseline). Pre-existing
        debt is tolerated until a refactor lowers it.

    Generate the baseline with
    ``uv run scripts/ci.py --update-pyright-baseline``.
    """
    print("\n>>> type-check (pyright)")
    result = subprocess.run(
        (
            "uv",
            "run",
            "pyright",
            "src/kntgraph/",
            "--outputjson",
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"pyright output not valid JSON: {e}") from e

    by_rule, by_file = _pyright_snapshot(data)
    total = sum(by_rule.values())
    has_baseline = PYRIGHT_BASELINE_PATH.exists()

    if not has_baseline:
        print(
            f"\nNo pyright baseline found — {total} errors. "
            f"Run `uv run scripts/ci.py --update-pyright-baseline` "
            f"to freeze the current state."
        )
        return total == 0

    baseline = json.loads(PYRIGHT_BASELINE_PATH.read_text())
    baseline_total = baseline["total_errors"]
    delta = total - baseline_total

    print(f"\nPyright errors: {total} (baseline: {baseline_total}, delta: {delta:+d})")
    if delta > 0:
        for rule in sorted(set(by_rule) | set(baseline["by_rule"])):
            cur = by_rule.get(rule, 0)
            prev = baseline["by_rule"].get(rule, 0)
            if cur != prev:
                print(f"  {rule}: {prev} -> {cur} ({cur - prev:+d})")
        print(f"\nRegression: {delta} new error(s). Fix before lowering the baseline.")
        return False
    return True


def _pyright_snapshot(
    data: dict[str, Any],
) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]]]:
    diag = [
        d for d in data.get("generalDiagnostics", []) if d.get("severity") == "error"
    ]
    by_rule: dict[str, int] = {}
    by_file: dict[str, list[dict[str, Any]]] = {}
    for d in diag:
        rule = d.get("rule", "unknown")
        fp = d.get("file", "")
        if str(ROOT) + "/" in fp:
            fp = fp.split(str(ROOT) + "/", 1)[1]
        by_rule[rule] = by_rule.get(rule, 0) + 1
        by_file.setdefault(fp, []).append(
            {
                "line": d["range"]["start"]["line"],
                "col": d["range"]["start"]["character"],
                "rule": rule,
                "message": d.get("message", "")[:200],
            }
        )
    return by_rule, by_file


def main() -> int:
    parser = argparse.ArgumentParser(description="kntgraph quality gates")
    parser.add_argument(
        "--baseline", action="store_true", help="Generate complexity baseline"
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Update complexity baseline after intentional refactor",
    )
    parser.add_argument(
        "--update-pyright-baseline",
        action="store_true",
        help="Update pyright baseline (run scripts/update_pyright_baseline.py)",
    )
    parser.add_argument(
        "--only",
        choices=list(ALL_STEPS.keys()),
        help="Run one step only (skips all others)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream each step's output live (default: silent on success)",
    )
    args = parser.parse_args()

    if args.update_pyright_baseline:
        return subprocess.call(
            ["uv", "run", "python", "scripts/update_pyright_baseline.py"],
            cwd=ROOT,
        )
    if args.baseline or args.update_baseline:
        return cmd_baseline()

    selected = [args.only] if args.only else list(ALL_STEPS.keys())
    failed: list[str] = []
    capture = not args.verbose

    for name in selected:
        if name == "complexity":
            print(f"\n>>> {name}")
            if not gate_complexity(verbose=args.verbose):
                failed.append("complexity")
            continue
        if name == "pyright":
            if not gate_pyright():
                failed.append("pyright")
            continue
        print(f"\n>>> {name}")
        step = ALL_STEPS[name]
        _run_step(step, failed, capture=capture)

    print()
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("All gates passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
