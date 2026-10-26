# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
update_version_badge.py -- keep the README release badge
in sync with ``kntgraph.__version__`` (ADR-051).

The badge is a shields.io image with the format
``[![release](https://img.shields.io/badge/release-vX.Y.Z-blue?style=for-the-badge&logo=github&logoColor=white)](https://github.com/kinetgraph/kntgraph/releases/tag/vX.Y.Z)``.

The badge lives in the README's badge row
(adjacent to the CC / MI / pyright badges). This
script is intentionally separate from
``readme_stats.py`` (which regenerates the project
metrics block) so the version update can run
independently -- e.g. as part of the release
workflow (PR 4 of ADR-051), not on every CI run.

The script locates the existing ``[![release](...)``
line and replaces it. If the line does not exist,
it appends the badge after the pyright badge
(the canonical row in the current README).
"""

from __future__ import annotations

import argparse
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
from readme_stats import _version_badge

# The badge line. Only the badges whose shields.io **label**
# is ``version`` (the pre-0.17.1 form) or ``release`` (the
# current form) are matched — the sibling badges (``pypi``,
# ``cc``, ``mi``, ``pyright``, ``coverage``, ``tests``,
# ``security``, ``audit``) are left alone.
#
# The version and the trailing ``style``/``logo`` parameters
# are replaced wholesale by the renderer, so the regex only
# has to locate the line.
#
# Three shapes are matched, so a README that still carries an
# older form is migrated to the current one instead of gaining
# a second, duplicate badge:
#
#   1. ``[![release](.../badge/release-...)](link)`` --
#      the current form.
#   2. ``![Version](.../badge/version-...)`` -- the
#      pre-0.17.1 form: no link wrapper, alt text
#      ``Version``.
#   3. ``[![Version](.../badge/version-...)](link)`` --
#      a linked variant of the old form.
_VERSION_BADGE_RE = re.compile(
    r"^\[!\[(?:release|Version)\]\(https://img\.shields\.io/"
    r"badge/(?:release|version)-[^)]+\)\]"
    r"(?:\([^)]*\))?[ \t]*$"
    r"|^!\[Version\]\(https://img\.shields\.io/badge/version-[^)]+\)[ \t]*$",
    re.MULTILINE,
)

# The PyPI badge — a **sibling** of the release badge, not a
# replacement for it. The release badge tracks the git tag;
# the PyPI badge tracks the version published on the index.
# They are updated together by this script so they cannot
# drift (the pypi badge was hand-edited at v0.16.0 and
# stayed stale through v0.17.0).
_PYPI_BADGE_RE = re.compile(
    r"^\[!\[pypi\]\(https://img\.shields\.io/badge/pypi-[^)]+\)\]"
    r"(?:\([^)]*\))?[ \t]*$",
    re.MULTILINE,
)


def _render_pypi_badge(base: str) -> str:
    """Render the README PyPI badge for a version triple.

    Same row convention as the release badge: the
    ``for-the-badge`` style, the PyPI logo, and a link to
    the project page.
    """
    return (
        f"[![pypi](https://img.shields.io/badge/pypi-{base}-blue"
        f"?style=for-the-badge&logo=pypi&logoColor=white)]"
        f"(https://pypi.org/project/kntgraph/)"
    )


def _sync_pypi_badge(text: str, version: str | None) -> str:
    """Refresh the PyPI badge in ``text`` to the given version.

    The badge is replaced when present. When it is absent,
    it is inserted right after the release badge (the two
    travel together). A missing badge is not an error: a
    README without the PyPI row simply does not get one.
    """
    base = _version_triple(version)
    if base is None:
        return text
    badge = _render_pypi_badge(base)
    if _PYPI_BADGE_RE.search(text):
        return _PYPI_BADGE_RE.sub(badge, text)
    # Insert after the release badge, keeping the pair
    # adjacent.
    release_re = re.compile(
        r"^(\[\!\[release\]\([^\n]*)",
        re.MULTILINE,
    )
    m = release_re.search(text)
    if m is None:
        return text
    return text[: m.end()] + f"\n{badge}" + text[m.end() :]


def _version_triple(version: str | None) -> str | None:
    """Return the clean version triple for the badge, or None.

    ``None`` means "the version could not be derived" — the
    caller leaves the README untouched rather than writing a
    wrong version into it.
    """
    from packaging.version import Version

    if version:
        return Version(version.lstrip("v")).base_version
    badge = _version_badge()
    if not badge:
        return None
    # The rendered badge carries the version in its path:
    # ``.../badge/release-vX.Y.Z-blue...``.
    m = re.search(r"/badge/(?:release|version)-v?([0-9][^-]*)-", badge)
    if m is None:
        return None
    return m.group(1)


def main() -> int:
    """Read the README, find (or insert) the
    version badge, replace it with the current
    version, and write back.

    ``--version`` overrides the installed
    ``kntgraph.__version__``. The release workflow needs
    it: the badge update runs **before** the tag is
    created, so the install still reflects the previous
    version at that point.
    """
    parser = argparse.ArgumentParser(
        prog="update_version_badge",
        description=(
            "Keep the README version badge in sync with the released version (ADR-051)."
        ),
    )
    parser.add_argument(
        "--version",
        default=None,
        help=(
            "The version to write into the badge (e.g. "
            "'0.17.0'). Defaults to the installed "
            "kntgraph.__version__."
        ),
    )
    args = parser.parse_args()

    new_badge = _version_badge(args.version)
    if not new_badge:
        print(
            "update_version_badge: could not derive a "
            "version badge. Run 'uv sync' first, or pass "
            "--version explicitly.",
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

    # The PyPI badge is refreshed alongside the release
    # badge. It was hand-edited until now, which is how it
    # drifted to ``0.16.0`` while the release badge tracked
    # ``0.17.0`` — two badges claiming to be "the version",
    # disagreeing. The version triple is the same for both
    # (the release publishes the wheel), so they are
    # rendered from the same value.
    new_text = _sync_pypi_badge(new_text, args.version)
    if new_text == text:
        print("update_version_badge: README is up to date.")
        return 0
    README.write_text(new_text, encoding="utf-8")
    print(f"update_version_badge: updated {README}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
