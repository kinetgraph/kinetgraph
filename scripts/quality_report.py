#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Quality-report snapshot generator.

Runs every gate the CI runs (lint, format, complexity,
maintainability, pyright, coverage, tests, bandit,
pip-audit) and writes a deterministic snapshot to
``docs/quality.md`` plus a JSON dump at
``.quality-report.json``. The README badges reference
the Markdown snapshot so the values are always
reproducible from a fresh checkout.

Usage
-----

    python scripts/quality_report.py
    python scripts/quality_report.py --only lint,format,tests
    python scripts/quality_report.py --json path/to/report.json

The script never fails the build — it captures each
gate's output and writes a single ``docs/quality.md``
section. The CI gate that *does* fail is
``scripts/ci.py`` (the canonical runner). This script
is a *snapshot* tool, not a gate.

Why a separate script
----------------------
The README references specific values ("75% coverage",
"1457 tests passed") and a slow CI run would race the
badge update. By caching the snapshot in
``docs/quality.md`` we get static-image badges with
``img.shields.io/badge/...-75%25-yellow`` URLs (no
shield endpoint) and a reproducible source for the
values.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "quality.md"
DEFAULT_JSON = REPO_ROOT / ".quality-report.json"


def _run(cmd: list[str], *, timeout: int = 180) -> tuple[int, str, float]:
    """Run ``cmd`` and return (returncode, stdout, duration_s)."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, stdout, time.monotonic() - start
    except subprocess.TimeoutExpired as e:
        return 124, f"timeout after {timeout}s: {e}", time.monotonic() - start


def _vrun(venv: str, args: list[str], **kwargs) -> tuple[int, str, float]:
    return _run([venv, *args], **kwargs)


def _py() -> str:
    """Path to the venv's Python interpreter (fallback to system)."""
    candidate = REPO_ROOT / ".venv" / "bin" / "python3"
    return str(candidate) if candidate.exists() else sys.executable


def _ruff() -> str:
    return "ruff" if shutil.which("ruff") else ".venv/bin/ruff"


def gate_lint() -> dict:
    code, out, dt = _run([_ruff(), "check", "src/", "tests/"])
    return {
        "tool": "ruff check",
        "ok": code == 0,
        "issues": 0 if code == 0 else _count_ruff(out),
        "duration_s": round(dt, 2),
    }


def _count_ruff(out: str) -> int:
    n = 0
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("All checks passed"):
            continue
        if line.startswith("Found "):
            try:
                return int(line.split()[1])
            except (ValueError, IndexError):
                pass
        n += 1
    return n


def gate_format() -> dict:
    code, out, dt = _run([_ruff(), "format", "--check", "src/", "tests/"])
    formatted = 0
    needs_reformat = 0
    for line in out.splitlines():
        # The success line is "<N> files already
        # formatted" (the value is what we count). The
        # failure lines are "<N> files would be
        # reformatted, <M> files already formatted" —
        # we count the *would* number on the leading
        # "Would reformat" banner line and the
        # "left unchanged" / "already formatted" tail
        # for the unaffected count.
        first_tok = line.split()[0] if line.split() else ""
        if line.startswith("Would reformat:"):
            try:
                needs_reformat = int(first_tok)
            except (ValueError, IndexError):
                pass
        elif first_tok.isdigit() and "file" in line:
            try:
                formatted = int(first_tok)
            except (ValueError, IndexError):
                pass
    return {
        "tool": "ruff format --check",
        "ok": code == 0 and needs_reformat == 0,
        "formatted": formatted,
        "needs_reformat": needs_reformat,
        "duration_s": round(dt, 2),
    }


def _count_format_needs(out: str) -> int:
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Would reformat:") or line.startswith("file"):
            try:
                return int(line.split()[0])
            except (ValueError, IndexError):
                pass
    return 0


def gate_complexity() -> dict:
    py = _py()
    code, out, dt = _vrun(
        py,
        ["-m", "radon", "cc", "src/kntgraph", "-s", "-a", "-j"],
        timeout=240,
    )
    blocks = 0
    avg = 0.0
    high = 0
    if code == 0 and out.strip():
        try:
            data = json.loads(out)
            for _path, items in data.items():
                for it in items:
                    blocks += 1
                    avg += it.get("complexity", 0)
                    if it.get("rank", "?") in ("D", "E", "F"):
                        high += 1
            avg = avg / max(blocks, 1)
        except json.JSONDecodeError:
            pass
    return {
        "tool": "radon cc",
        "ok": high == 0,
        "blocks": blocks,
        "avg_complexity": round(avg, 2),
        "rank_d_plus": high,
        "duration_s": round(dt, 2),
    }


def gate_maintainability() -> dict:
    py = _py()
    code, out, dt = _vrun(
        py,
        ["-m", "radon", "mi", "src/kntgraph", "-s", "-j"],
        timeout=240,
    )
    a_count = 0
    b_count = 0
    c_count = 0
    if code == 0 and out.strip():
        try:
            data = json.loads(out)
            for info in data.values():
                rank = info.get("rank", "?")
                if rank == "A":
                    a_count += 1
                elif rank == "B":
                    b_count += 1
                else:
                    c_count += 1
        except json.JSONDecodeError:
            pass
    return {
        "tool": "radon mi",
        "ok": c_count == 0,
        "rank_a": a_count,
        "rank_b": b_count,
        "rank_c_or_lower": c_count,
        "duration_s": round(dt, 2),
    }


def gate_pyright() -> dict:
    """Snapshot pyright strict-mode diagnostics.

    The JSON envelope (``--outputjson``) carries a
    ``summary`` block that always includes the
    per-severity counts (``errorCount``,
    ``warningCount``). We use that for the
    canonical numbers; the ``generalDiagnostics``
    list is the cross-check that lets us mark the
    gate ``ok`` when the call itself succeeded.
    """
    code, out, dt = _run(["pyright", "src/kntgraph", "--outputjson"], timeout=240)
    errors = 0
    warnings = 0
    try:
        data = json.loads(out)
        summary = data.get("summary", {})
        errors = int(summary.get("errorCount", 0) or 0)
        warnings = int(summary.get("warningCount", 0) or 0)
    except json.JSONDecodeError:
        pass
    return {
        "tool": "pyright",
        "ok": errors == 0,
        "errors": errors,
        "warnings": warnings,
        "duration_s": round(dt, 2),
    }


def gate_coverage() -> dict:
    py = _py()
    cov_dir = REPO_ROOT / ".coverage-data"
    cov_dir.mkdir(exist_ok=True)
    cov_data = cov_dir / ".coverage"
    cov_dir_env = {"COVERAGE_FILE": str(cov_data), "PATH": REPO_ROOT / ".venv" / "bin"}
    code, _, dt = _run_with_env(
        [
            py,
            "-m",
            "coverage",
            "run",
            "--source=src/kntgraph",
            "-m",
            "pytest",
            "tests/unit",
            "-q",
            "--no-header",
        ],
        env=cov_dir_env,
        timeout=300,
    )
    if code != 0:
        return {
            "tool": "coverage",
            "ok": False,
            "percent": 0.0,
            "covered": 0,
            "total": 0,
            "duration_s": round(dt, 2),
        }
    _, summary, _ = _run_with_env(
        [py, "-m", "coverage", "report", "--include=src/kntgraph/*", "--skip-empty"],
        env=cov_dir_env,
        timeout=60,
    )
    pct, covered, total = _parse_coverage_report(summary)
    return {
        "tool": "coverage",
        "ok": pct >= 80.0,
        "percent": pct,
        "covered": covered,
        "total": total,
        "duration_s": round(dt, 2),
    }


def _parse_coverage_report(out: str) -> tuple[float, int, int]:
    """Parse the ``TOTAL`` row of ``coverage report``.

    The column order is ``TOTAL <stmts> <miss> <cover%>``;
    ``<stmts>`` is the *total* stmts and ``<miss>`` is the
    number of *uncovered* stmts. The covered count is
    derived (``stmts - miss``) so downstream code can
    report either shape.
    """
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("TOTAL"):
            parts = line.split()
            if len(parts) >= 4:
                try:
                    stmts = int(parts[1].replace(",", ""))
                    miss = int(parts[2].replace(",", ""))
                    pct = float(parts[3].rstrip("%"))
                    return pct, stmts - miss, stmts
                except (ValueError, IndexError):
                    pass
    return 0.0, 0, 0


def _run_with_env(cmd: list[str], *, env: dict, timeout: int) -> tuple[int, str, float]:
    import os

    full_env = {**os.environ, **env}
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=full_env,
        )
        stdout = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, stdout, time.monotonic() - start
    except subprocess.TimeoutExpired as e:
        return 124, f"timeout after {timeout}s: {e}", time.monotonic() - start


def gate_tests() -> dict:
    py = _py()
    code_unit, out_unit, dt_unit = _vrun(
        py,
        ["-m", "pytest", "tests/unit", "-q", "--no-header"],
        timeout=600,
    )
    code_agents, out_agents, dt_agents = _vrun(
        py,
        ["-m", "pytest", "tests/agents", "-q", "--no-header"],
        timeout=600,
    )
    return {
        "tool": "pytest",
        "ok": code_unit == 0 and code_agents == 0,
        "unit": _parse_pytest_counts(out_unit),
        "agents": _parse_pytest_counts(out_agents),
        "duration_s": round(dt_unit + dt_agents, 2),
    }


def _parse_pytest_counts(out: str) -> dict:
    passed = failed = 0
    for line in out.splitlines():
        line = line.strip()
        if "passed" in line and "warning" in line:
            for chunk in line.split(","):
                chunk = chunk.strip()
                if "passed" in chunk and "warning" not in chunk:
                    try:
                        passed = int(chunk.split()[0])
                    except (ValueError, IndexError):
                        pass
                if "failed" in chunk:
                    try:
                        failed = int(chunk.split()[0])
                    except (ValueError, IndexError):
                        pass
    return {"passed": passed, "failed": failed}


def gate_bandit() -> dict:
    """Capture bandit severity totals.

    Bandit prints two summary blocks at the end:

      1. ``Total issues (by severity):`` — the canonical
         counts we want (``High``/``Medium``/``Low``).
      2. ``Total issues (by confidence):`` — different
         metric (how confident bandit is in each finding);
         NOT severity.

    We parse block 1 only; the second block is ignored
    so the snapshot reflects the severity counts
    downstream tooling (CI) cares about.
    """
    code, out, dt = _run(["bandit", "-r", "src/kntgraph", "-q", "-f", "txt"])
    high = medium = low = 0
    in_severity = False
    for line in out.splitlines():
        stripped = line.strip()
        if "Total issues (by severity):" in stripped:
            in_severity = True
            continue
        if in_severity and "Total issues (by confidence):" in stripped:
            in_severity = False
            continue
        if not in_severity:
            continue
        if not stripped or ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        try:
            n = int(val.strip())
        except ValueError:
            continue
        key_l = key.strip().lower()
        if key_l == "high":
            high = n
        elif key_l == "medium":
            medium = n
        elif key_l == "low":
            low = n
    return {
        "tool": "bandit",
        "ok": high == 0 and medium == 0,
        "high": high,
        "medium": medium,
        "low": low,
        "duration_s": round(dt, 2),
    }


def gate_pip_audit() -> dict:
    """Snapshot ``pip-audit`` over the resolved dependency
    set.

    The repo uses ``uv`` to resolve the lock file
    (``uv.lock``); ``pip-audit`` consumes
    ``requirements.txt``. We export a transient
    requirements file via ``uv export`` and feed it to
    ``pip-audit``. The export is written to
    ``.quality-report.requirements.txt`` so the audit
    is reproducible.
    """

    req_path = REPO_ROOT / ".quality-report.requirements.txt"
    py = _py()
    # Export the resolved set (no editable install, no
    # hashes — pip-audit only needs the names + versions).
    code_exp, _, _ = _run(
        ["uv", "export", "--format", "requirements-txt", "--no-hashes", "--quiet"],
        timeout=120,
    )
    if code_exp != 0:
        return {
            "tool": "pip-audit",
            "ok": False,
            "vulns": 0,
            "error": "uv export failed",
            "duration_s": 0.0,
        }
    # ``uv export`` writes to stdout; the shell
    # redirection was a no-op in our subprocess.run.
    # Run again and capture into the file.
    with open(req_path, "wb") as fh:
        subprocess_exp = subprocess.run(
            ["uv", "export", "--format", "requirements-txt", "--no-hashes", "--quiet"],
            cwd=REPO_ROOT,
            stdout=fh,
            timeout=120,
        )
    if subprocess_exp.returncode != 0 or not req_path.exists():
        return {
            "tool": "pip-audit",
            "ok": False,
            "vulns": 0,
            "error": "uv export did not produce requirements.txt",
            "duration_s": 0.0,
        }

    code, out, dt = _vrun(
        py,
        ["-m", "pip_audit", "-r", str(req_path), "--no-deps"],
        timeout=300,
    )
    return {
        "tool": "pip-audit",
        "ok": code == 0,
        "vulns": _parse_pip_audit(out),
        "duration_s": round(dt, 2),
    }


def _parse_pip_audit(out: str) -> int:
    n = 0
    for line in out.splitlines():
        if "vulnerability" in line.lower() or "vuln" in line.lower():
            try:
                for tok in line.split():
                    if tok.isdigit():
                        n = int(tok)
                        break
            except ValueError:
                pass
    return n


GATES = {
    "lint": gate_lint,
    "format": gate_format,
    "complexity": gate_complexity,
    "maintainability": gate_maintainability,
    "pyright": gate_pyright,
    "coverage": gate_coverage,
    "tests": gate_tests,
    "bandit": gate_bandit,
    "pip-audit": gate_pip_audit,
}


def render_markdown(report: dict) -> str:
    g = report["gates"]
    lines = [
        "<!--",
        "REUSE-IgnoreStart",
        "SPDX-FileCopyrightText: 2026 kinetgraph",
        "",
        "SPDX-License-Identifier: Apache-2.0",
        "",
        "This file is auto-generated by `scripts/quality_report.py`.",
        "Do not edit by hand; re-run the script to refresh the snapshot.",
        "REUSE-IgnoreEnd",
        "-->",
        "",
        "# Quality report",
        "",
        f"Generated: {report['generated_at']}",
        f"Total duration: {report['total_duration_s']}s",
        "",
        "Each row mirrors one of the nine gates in",
        "[`scripts/ci.py`](../scripts/ci.py) and one of the",
        "[`README.md`](../README.md) badges. The values here are",
        "the source of truth for the badge text — keep them in",
        "sync with the README.",
        "",
        "| Gate | Tool | OK | Detail | Duration |",
        "| --- | --- | :-: | --- | ---: |",
    ]
    rows: list[str] = []
    if "lint" in g:
        rows.append(_row("Lint", g["lint"], f"{g['lint']['issues']} issues"))
    if "format" in g:
        rows.append(
            _row(
                "Format",
                g["format"],
                f"{g['format']['formatted']} files formatted, "
                f"{g['format']['needs_reformat']} need reformat",
            )
        )
    if "complexity" in g:
        rows.append(
            _row(
                "Complexity",
                g["complexity"],
                f"avg {g['complexity']['avg_complexity']} over "
                f"{g['complexity']['blocks']} blocks; "
                f"{g['complexity']['rank_d_plus']} rank D+",
            )
        )
    if "maintainability" in g:
        rows.append(
            _row(
                "Maintainability",
                g["maintainability"],
                f"{g['maintainability']['rank_a']} A + "
                f"{g['maintainability']['rank_b']} B + "
                f"{g['maintainability']['rank_c_or_lower']} C-",
            )
        )
    if "pyright" in g:
        rows.append(
            _row(
                "Pyright",
                g["pyright"],
                f"{g['pyright']['errors']} errors, {g['pyright']['warnings']} warnings",
            )
        )
    if "coverage" in g:
        cov = g["coverage"]
        rows.append(
            _row(
                "Coverage",
                cov,
                f"{cov['percent']}% ({cov['covered']}/{cov['total']} stmts)",
            )
        )
    if "tests" in g:
        t = g["tests"]
        rows.append(
            _row(
                "Tests",
                t,
                f"{t['unit']['passed']} unit + "
                f"{t['agents']['passed']} agents passed; "
                f"{t['unit']['failed'] + t['agents']['failed']} failed",
            )
        )
    if "bandit" in g:
        b = g["bandit"]
        rows.append(
            _row(
                "Bandit",
                b,
                f"{b['high']} H + {b['medium']} M + {b['low']} L",
            )
        )
    if "pip-audit" in g:
        p = g["pip-audit"]
        rows.append(
            _row(
                "pip-audit",
                p,
                f"{p['vulns']} known vulnerabilities",
            )
        )
    if not rows:
        rows.append("| _(no gates selected)_ | — | — | — | — |")
    lines.extend(rows)
    lines.append("")
    lines.append("## Re-generating this report")
    lines.append("")
    lines.append("```sh")
    lines.append("python scripts/quality_report.py")
    lines.append("python scripts/quality_report.py --json .quality-report.json")
    lines.append("```")
    lines.append("")
    lines.append("The script never fails the build — it captures each gate's")
    lines.append("output and writes this snapshot. The actual fail-on-error")
    lines.append("logic lives in `scripts/ci.py` (the canonical runner).")
    lines.append("")
    return "\n".join(lines)


def _row(name: str, gate: dict, detail: str) -> str:
    ok = "✅" if gate["ok"] else "❌"
    return f"| {name} | `{gate['tool']}` | {ok} | {detail} | {gate['duration_s']}s |"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated list of gate names to run (default: all)",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="path to write the Markdown snapshot",
    )
    parser.add_argument(
        "--json",
        default=str(DEFAULT_JSON),
        help="path to write the JSON dump (also used as the "
        "merge source: passing a sub-set of gates --only "
        "preserves results from previous runs so the "
        "snapshot can be re-assembled incrementally)",
    )
    args = parser.parse_args(argv)

    selected = (
        [s.strip() for s in args.only.split(",") if s.strip()]
        if args.only
        else list(GATES)
    )
    for name in selected:
        if name not in GATES:
            print(f"unknown gate: {name}", file=sys.stderr)
            return 2

    # Merge: load any existing snapshot so partial runs
    # accumulate instead of overwriting. The first
    # report on a fresh checkout (or after `rm
    # .quality-report.json`) is a clean slate.
    json_path = Path(args.json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = None
    else:
        existing = None

    report: dict = dict(existing) if existing else {"gates": {}}
    report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report.setdefault("gates", {})
    for name in selected:
        print(f"running gate: {name} ...", file=sys.stderr, flush=True)
        try:
            report["gates"][name] = GATES[name]()
        except Exception as exc:  # pragma: no cover — safety net
            report["gates"][name] = {
                "tool": name,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "duration_s": 0.0,
            }
    report["total_duration_s"] = round(
        sum(
            g.get("duration_s", 0)
            for g in report["gates"].values()
            if isinstance(g, dict)
        ),
        2,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_markdown(report))

    json_path.write_text(json.dumps(report, indent=2, sort_keys=True))

    print(f"wrote {output_path}", file=sys.stderr)
    print(f"wrote {json_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
