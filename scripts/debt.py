# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
#
# /// script
# requires-python = ">=3.12"
# ///
"""debt — query the inventoried technical debt of a file or directory.

This is the query tool of the ``fix-on-touch`` policy: before editing
a file, discover what is already wrong **in it**, so that the debt is
corrected in the same pass.

The inventory itself lives in ``tests/architecture/_debt.py``; this
script is only its command-line interface.

Usage:
    uv run python scripts/debt.py src/kntgraph/core/event/event.py
    uv run python scripts/debt.py src/kntgraph/core/
    uv run python scripts/debt.py --all
    uv run python scripts/debt.py --all --kind broad-except
    uv run python scripts/debt.py --update-baseline

Exit codes:
    0 — no debt found for the requested target
    1 — debt found (useful for a pre-commit hook)
    2 — bad invocation
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT))

from tests.architecture._debt import (  # noqa: E402
    DEBT_KINDS,
    Debt,
    collect_debts,
    write_baseline,
)


def _matches(target: Path, debt_path: str) -> bool:
    """True when the debt's path is the target or lives under it."""
    resolved = (ROOT / debt_path).resolve()
    return resolved == target or target in resolved.parents


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``debt`` command."""
    parser = argparse.ArgumentParser(
        description=(
            "Query the inventoried technical debt of a file or directory "
            "(fix-on-touch support)."
        )
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="File or directory to inspect. Omit with --all.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Show the entire inventory.",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Rewrite .debt-baseline.json with the current inventory.",
    )
    parser.add_argument(
        "--kind",
        action="append",
        default=[],
        help="Restrict the output to the given debt kind (repeatable).",
    )
    args = parser.parse_args(argv)

    if args.update_baseline:
        debts = collect_debts()
        write_baseline(debts)
        print(
            f"Baseline updated: {len(debts)} item(s) -> {ROOT / '.debt-baseline.json'}"
        )
        return 0

    if args.all:
        debts = collect_debts()
    elif args.path:
        target = Path(args.path)
        if not target.is_absolute():
            target = (ROOT / target).resolve()
        if not target.exists():
            print(f"Path not found: {args.path}", file=sys.stderr)
            return 2
        debts = [debt for debt in collect_debts() if _matches(target, debt.path)]
    else:
        parser.print_help()
        return 2

    if args.kind:
        debts = [debt for debt in debts if debt.kind in args.kind]

    if not debts:
        print("No debt found.")
        return 0

    by_kind: dict[str, list[Debt]] = {}
    for debt in debts:
        by_kind.setdefault(debt.kind, []).append(debt)

    for kind in sorted(by_kind):
        print(f"\n{kind} — {DEBT_KINDS.get(kind, 'unknown kind')}")
        for debt in sorted(by_kind[kind], key=lambda item: (item.path, item.line)):
            location = f"{debt.path}:{debt.line}" if debt.line else debt.path
            print(f"  {location}  {debt.detail}")

    print(f"\n{len(debts)} debt item(s) in {len(by_kind)} kind(s).")
    return 1


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main())
