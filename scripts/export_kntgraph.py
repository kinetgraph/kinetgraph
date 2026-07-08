#!/usr/bin/env python3
"""
Export ``kntgraph/`` as a tarball ready to be
unpacked into the new public repo
``github.com/kinetgraph/kinetgraph``.

What is exported
----------------

- Every file under ``kntgraph/`` EXCEPT:
  - ``.pyc`` files (``__pycache__/``)
  - ``htmlcov/`` (local coverage report)
  - ``.venv/`` (virtualenv, never committed)
  - ``.ruff_cache/``, ``.mypy_cache/`` (lint cache)
  - ``.radon-baseline.json`` and
    ``.pyright-baseline.json`` are KEPT — they
    are part of the reproducible CI.

What is generated
-----------------

- ``export_manifest.txt`` — list of paths included
  in the tarball, plus sizes and a SHA-256 of each
  file. Used to verify the export on the other
  side.

Usage
-----

    python scripts/export_kntgraph.py
    # Creates dist/kntgraph-export-<timestamp>.tar.gz

The output is intentionally a tarball (not a git
init) because the new repo is already created
on GitHub — the maintainer will clone it, untar
this archive on top of the empty tree, and
commit. The history of the public repo starts
fresh; the internal monorepo retains the full
git history.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT  # the kntgraph/ tree IS the package root
DIST = ROOT / "dist"


# Patterns to exclude. The .pyright-baseline.json
# and .radon-baseline.json are KEPT (they are
# part of the reproducible CI), so we do NOT
# exclude *.json here.
EXCLUDE_DIRS = {
    "__pycache__",
    "htmlcov",
    ".venv",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "src/kntgraph.egg-info",
}
EXCLUDE_FILES = {
    "uv.lock",
}


def _iter_files() -> list[Path]:
    out: list[Path] = []
    for p in SOURCE.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(SOURCE)
        parts = rel.parts
        if any(part in EXCLUDE_DIRS for part in parts):
            continue
        if rel.name in EXCLUDE_FILES:
            continue
        out.append(p)
    return sorted(out)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if not (SOURCE / "pyproject.toml").exists():
        print(
            f"error: {SOURCE / 'pyproject.toml'} not found; "
            f"run from the kntgraph/ root.",
            file=sys.stderr,
        )
        return 1

    DIST.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = DIST / f"kntgraph-export-{timestamp}.tar.gz"

    files = _iter_files()
    if not files:
        print("error: no files to export", file=sys.stderr)
        return 1

    manifest_lines = [
        "# kntgraph export manifest",
        f"# generated: {timestamp}",
        f"# source:    {SOURCE}",
        f"# files:     {len(files)}",
        "",
    ]

    with tarfile.open(out_path, "w:gz") as tar:
        for p in files:
            rel = p.relative_to(SOURCE)
            tar.add(p, arcname=str(rel))
            size = p.stat().st_size
            digest = _sha256(p)
            manifest_lines.append(f"{digest}  {size:>10}  {rel}")

    manifest_path = out_path.with_name("export_manifest.txt")
    manifest_path.write_text("\n".join(manifest_lines) + "\n")

    print(f"Exported {len(files)} files → {out_path}")
    print(f"Manifest written → {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
