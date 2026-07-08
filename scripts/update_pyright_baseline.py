#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Generate the pyright baseline for the standalone
kntgraph/ package. Mirrors the monorepo's
`scripts/ci.py` ``_pyright_snapshot`` logic.

Usage:
    python scripts/update_pyright_baseline.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / ".pyright-baseline.json"


def _run_pyright() -> dict:
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
    return json.loads(result.stdout or "{}")


def _snapshot(
    data: dict,
) -> tuple[dict[str, int], dict[str, list[dict]]]:
    diag = [
        d for d in data.get("generalDiagnostics", []) if d.get("severity") == "error"
    ]
    by_rule: dict[str, int] = {}
    by_file: dict[str, list[dict]] = {}
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
    print(">>> Generating pyright baseline...")
    data = _run_pyright()
    by_rule, by_file = _snapshot(data)
    total = sum(by_rule.values())
    snapshot = {
        "version": data.get("version", "?"),
        "total_errors": total,
        "files_with_errors": len(by_file),
        "by_rule": dict(sorted(by_rule.items(), key=lambda x: -x[1])),
        "by_file_count": {f: len(errs) for f, errs in sorted(by_file.items())},
    }
    BASELINE_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    print(f"Baseline written: {BASELINE_PATH}")
    print(f"  Tracked: {total} errors in {len(by_file)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
