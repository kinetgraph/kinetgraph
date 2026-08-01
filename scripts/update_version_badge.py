# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
update_version_badge.py -- keep the README version
badge in sync with ``kntgraph.__version__`` (ADR-051).

The badge is a shields.io image with the format
``![Version](https://img.shields.io/badge/version-X.Y.Z-blue)``.

The badge lives in the README's badge row
(adjacent to the CC / MI / pyright badges). This
script is intentionally separate from
``readme_stats.py`` (which regenerates the project
metrics block) so the version update can run
independently -- e.g. as part of the release
workflow (PR 4 of ADR-051), not on every CI run.

The script locates the existing
``![Version](https://img.shields.io/badge/version-...)``
line and replaces it. If the line does not exist,
it appends the badge after the pyright badge
(the canonical row in the current README).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

# Make ``scripts.readme_stats`` importable when
# the script is run as ``python scripts/...``
# (the parent dir is not on ``sys.path`` by
# default).
sys.path.insert(0, str(ROOT / "scripts"))
from readme_stats import _version_badge  # noqa: E402

# Match the existing version badge line. The
# ``.*?`` is non-greedy so the regex matches the
# whole line up to the first ``)``.
_VERSION_BADGE_RE = re.compile(
    r"^\[!\[\s*Version\s*\]\(https://img\.shields\.io/"
    r"badge/version-[^)]+\)\s*$",
    re.MULTILINE,
)


def main() -> int:
    """Read the README, find (or insert) the
    version badge, replace it with the current
    version, and write back.
    """
    new_badge = _version_badge()
    if not new_badge:
        print(
            "update_version_badge: kntgraph.__version__ "
            "is the '0.0.0+unknown' fallback; no "
            "badge to insert. Run 'uv sync' first.",
            file=sys.stderr,
        )
        return 1

    text = README.read_text(encoding="utf-8")
    if _VERSION_BADGE_RE.search(text):
        new_text = _VERSION_BADGE_RE.sub(new_badge, text)
    else:
        # Insert after the pyright badge (the
        # canonical neighbour). Match the line
        # starting with ``[![pyright``.
        pyright_re = re.compile(
            r"^(\[\!\[pyright\b[^\n]*)",
            re.MULTILINE,
        )
        m = pyright_re.search(text)
        if m is None:
            print(
                "update_version_badge: README has "
                "no pyright badge; cannot determine "
                "where to insert the version badge.",
                file=sys.stderr,
            )
            return 1
        # Insert on a new line, after the pyright
        # line.
        new_text = text[: m.end()] + f"\n{new_badge}" + text[m.end() :]
    if new_text == text:
        print("update_version_badge: README is up to date.")
        return 0
    README.write_text(new_text, encoding="utf-8")
    print(f"update_version_badge: updated {README}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
