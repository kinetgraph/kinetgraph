# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""
readme_stats — regenerate the project-metrics block in README.md.

Inspects the repo (source LOC, test count, ADR count, etc.) and
replaces the fenced block delimited by ``<!-- STATS START -->``
and ``<!-- STATS END -->`` markers in ``README.md`` with fresh
numbers. The script is intentionally read-only on the rest of the
repo; it only mutates README.md between the two markers.

Run manually::

    uv run python scripts/readme_stats.py

Or wire it into a pre-commit / ``make readme`` target.

The CI-quality badges that go ABOVE the stats block (lint,
format, type-check, tests, bandit, audit) are kept in
``README.md`` verbatim; they reflect the current ``ci.py`` gates.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

START_MARK = "<!-- STATS START -->"
END_MARK = "<!-- STATS END -->"


def _count(paths: tuple[str, ...], pattern: str = "*.py") -> int:
    """Count files under each path (relative to ROOT) that
    match ``pattern``, excluding ``__pycache__``."""
    total = 0
    for p in paths:
        full = ROOT / p
        if not full.exists():
            continue
        total += sum(1 for f in full.rglob(pattern) if "__pycache__" not in f.parts)
    return total


def _count_md(paths: tuple[str, ...]) -> int:
    return _count(paths, pattern="*.md")


def _src_loc() -> int:
    out = 0
    for f in (ROOT / "src").rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        out += sum(1 for _ in f.read_text(encoding="utf-8").splitlines())
    return out


def _tests_loc() -> int:
    out = 0
    for f in (ROOT / "tests").rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        out += sum(1 for _ in f.read_text(encoding="utf-8").splitlines())
    return out


def _version_badge() -> str:
    """Render the README version badge from
    ``kntgraph.__version__``.

    The badge is a standard shields.io image with
    the format
    ``![Version](https://img.shields.io/badge/version-X.Y.Z-blue)``.

    The version is the **base of the latest tag**
    (e.g. ``0.10.0``); the ``+g<sha>`` and ``.devN``
    suffixes that ``setuptools_scm`` adds when the
    working tree is past the latest tag are stripped
    so the badge always shows a clean semver triple
    matching the canonical release version.

    Edge case: when the working tree is **dirty**
    (uncommitted changes), ``setuptools_scm`` infers
    the next minor/patch as ``0.10.1.dev0+g...`` to
    signal "not the tagged release". The
    ``Version.base_version`` of that string is
    ``0.10.1`` -- which is wrong for the badge (the
    release is still ``0.10.0``). The fix is to
    prefer the last clean tag:

    1. Try ``git describe --tags --abbrev=0`` (the
       last tag reachable from HEAD, **without**
       ``--dirty``, which means "tag of the current
       commit").

    2. If that succeeds, parse the tag and use it.

    3. Otherwise (no tag, e.g. a fresh clone), fall
       back to ``__version__``'s ``base_version``.
    """
    try:
        # Prefer the clean tag -- "what is the
        # version this commit corresponds to".
        # ``git describe --tags --abbrev=0`` returns
        # ``v0.11.0`` when HEAD is the v0.11.0 tag
        # commit itself, and ``v0.11.0`` when HEAD
        # is past the tag (the closest tag is the
        # answer regardless of distance).
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=ROOT,
        )
        if result.returncode == 0:
            tag = result.stdout.strip()
            # Strip the ``v`` prefix and any
            # non-semver suffix.
            from packaging.version import Version

            base = Version(tag.lstrip("v")).base_version
            return f"![Version](https://img.shields.io/badge/version-{base}-blue)"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Fallback: parse ``__version__``.
    try:
        import kntgraph
    except ImportError:
        return ""
    raw = getattr(kntgraph, "__version__", "")
    if not raw or raw == "0.0.0+unknown":
        return ""
    from packaging.version import Version

    base = Version(raw).base_version
    return f"![Version](https://img.shields.io/badge/version-{base}-blue)"


def _pytest_count() -> int:
    """Return the number of unit tests collected by pytest.

    Skipped on environments where the dev-deps aren't
    installed (e.g. CI lint-only job) — falls back to 'n/a'.
    """
    try:
        result = subprocess.run(
            (
                "uv",
                "run",
                "pytest",
                "tests/unit/",
                "tests/agents/unit/",
                "--collect-only",
                "-q",
            ),
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -1
    # pytest prints a final line like "1692 tests collected in 1.81s"
    # or "no tests ran" / "errors". We only honour the
    # "N tests collected" pattern.
    m = re.search(r"(\d+) tests collected", result.stdout)
    if not m:
        return -1
    return int(m.group(1))


def _stats() -> str:
    src_files = _count(("src/kntgraph",))
    test_files = _count(("tests",))
    src_loc = _src_loc()
    test_loc = _tests_loc()
    adrs = _count_md(("ADRs",))
    docs = _count_md(("docs",))
    tests_collected = _pytest_count()
    tests_str = f"{tests_collected:,}" if tests_collected >= 0 else "n/a"
    return (
        "## Project metrics\n"
        "\n"
        "| Source modules | Test modules | ADRs | Docs |\n"
        "| --- | --- | --- | --- |\n"
        f"| {src_files} ({src_loc:,} LOC) | {test_files} ({test_loc:,} LOC, "
        f"{tests_str} tests collected) | {adrs} | {docs} pages |\n"
    )


def _render() -> str:
    block = _stats()
    return (
        f"\n{START_MARK}\n"
        "<!-- This block is regenerated by scripts/readme_stats.py. "
        "Do not edit by hand. -->\n"
        f"{block}"
        f"{END_MARK}\n"
    )


def main() -> int:
    text = README.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(START_MARK) + r".*?" + re.escape(END_MARK),
        re.DOTALL,
    )
    if not pattern.search(text):
        print(
            f"ERROR: markers {START_MARK!r} / {END_MARK!r} not found in {README}",
            file=sys.stderr,
        )
        return 1
    new_text = pattern.sub(_render().rstrip("\n"), text)
    if new_text == text:
        print("README.md is up to date.")
        return 0
    README.write_text(new_text, encoding="utf-8")
    print("README.md metrics block updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
