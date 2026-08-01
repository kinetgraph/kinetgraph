# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
changelog_release.py -- move ``[Unreleased]`` to a dated
section in ``CHANGELOG.md`` (ADR-051).

The release workflow (PR 4 of ADR-051) calls this
script between the ``bump_version.py`` step and the
``git push`` step. The script:

  1. Reads the current ``CHANGELOG.md``.
  2. Extracts the ``## [Unreleased]`` section
     (header + body, up to the next ``## [``).
  3. Validates the body is non-empty (refuses to
     cut a release with no notes).
  4. Rewrites the file with the dated section
     inserted in chronological order (after the
     latest existing dated section), plus a fresh
     empty ``## [Unreleased]`` at the top.

Usage:

    uv run python scripts/changelog_release.py \\
        --changelog CHANGELOG.md \\
        --new-version 0.11.0 \\
        --date 2026-08-01

The ``--date`` defaults to today (UTC). The
``--changelog`` defaults to ``CHANGELOG.md`` in
the project root.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# Section delimiter. Matches ``## [anything]`` --
# the canonical format for ``CHANGELOG.md``
# headers. The script finds the boundary between
# the ``[Unreleased]`` block and the next section
# by looking for the next line that starts with
# ``## [``.
_SECTION_HEADER = re.compile(r"^## \[", re.MULTILINE)
# The first ``## [Unreleased]`` header.
_UNRELEASED_HEADER = re.compile(r"^## \[Unreleased\]\s*$\n", re.MULTILINE)


def _extract_unreleased(original: str) -> str:
    """Return the **body** of the ``[Unreleased]``
    block (i.e. the text between the
    ``## [Unreleased]`` header and the next
    ``## [`` header, exclusive on both ends).

    The header line itself is **not** in the body
    (the rewrite generates a new ``## [Unreleased]``
    header with a fresh empty body).
    """
    match = _UNRELEASED_HEADER.search(original)
    if match is None:
        raise SystemExit(
            "changelog_release: CHANGELOG.md has no "
            "'## [Unreleased]' section. Add one with "
            "the release notes under it, then re-run."
        )
    body_start = match.end()
    # Find the next ``## [`` after the header.
    next_section = _SECTION_HEADER.search(original, pos=body_start)
    if next_section is None:
        # ``[Unreleased]`` is the last section; the
        # body is everything to the end of the file.
        return original[body_start:].rstrip() + "\n"
    return original[body_start : next_section.start()].rstrip() + "\n"


def _build_rewrite(*, original: str, new_version: str, date_stamp: str) -> str:
    """Return the new ``CHANGELOG.md`` content.

    The structure:

      <preamble (everything before the first
        ``## [``)>
      ## [Unreleased]
      <empty body>
      ## [new_version] — <date>
      <body that was in [Unreleased]>
      <existing dated sections (in order)>
    """
    body = _extract_unreleased(original)
    if not body.strip():
        raise SystemExit(
            "changelog_release: '## [Unreleased]' is "
            "empty. Add the release notes under it, "
            "then re-run."
        )

    # Locate the first ``## [`` -- everything
    # before it is the preamble (header, "All
    # notable changes", ``---``, etc.). The
    # preamble is preserved verbatim.
    first_section = _SECTION_HEADER.search(original)
    if first_section is None:
        raise SystemExit(
            "changelog_release: CHANGELOG.md has no "
            "section headers (no '## [...]' lines). "
            "This is a malformed file; the script "
            "refuses to rewrite it."
        )
    preamble = original[: first_section.start()].rstrip() + "\n"
    # Everything from the first ``## [`` to the
    # next ``## [`` is the existing
    # ``[Unreleased]`` block. We drop it (the body
    # was extracted by ``_extract_unreleased``; the
    # header is replaced by a fresh empty one).
    unreleased_match = _UNRELEASED_HEADER.search(original, pos=first_section.start())
    if unreleased_match is None:
        # The first section is not ``[Unreleased]``
        # (e.g. someone removed it). Fall back to
        # prepending the new ``[Unreleased]`` to
        # the existing sections.
        existing_sections = original[first_section.start() :].rstrip() + "\n"
    else:
        # Drop the existing ``[Unreleased]``
        # block: from the header through the
        # next ``## [`` (or EOF).
        body_start = unreleased_match.end()
        next_section = _SECTION_HEADER.search(original, pos=body_start)
        if next_section is None:
            existing_sections = ""
        else:
            existing_sections = original[next_section.start() :].rstrip() + "\n"

    new_dated = f"## [{new_version}] — {date_stamp}\n\n{body}"
    new_unreleased = "## [Unreleased]\n"

    if existing_sections:
        new_body = f"{new_unreleased}\n{new_dated}\n\n{existing_sections}"
    else:
        # No prior dated sections: the new dated
        # one is the only one.
        new_body = f"{new_unreleased}\n{new_dated}\n"

    return f"{preamble}\n{new_body}"


def main() -> int:
    """Parse args, extract, rewrite, and write the
    CHANGELOG file in place.
    """
    parser = argparse.ArgumentParser(
        prog="changelog_release",
        description=(
            "Move the [Unreleased] section of "
            "CHANGELOG.md into a dated section "
            "(ADR-051)."
        ),
    )
    parser.add_argument(
        "--changelog",
        type=Path,
        default=Path("CHANGELOG.md"),
        help=("Path to the CHANGELOG.md file. Defaults to ./CHANGELOG.md."),
    )
    parser.add_argument(
        "--new-version",
        required=True,
        help=(
            "The new version to stamp (e.g. "
            "'0.11.0'). Must NOT include the "
            "leading 'v'."
        ),
    )
    parser.add_argument(
        "--date",
        default=None,
        help=("The date stamp to use (YYYY-MM-DD). Defaults to today (UTC)."),
    )
    args = parser.parse_args()

    if args.date is None:
        args.date = datetime.now(timezone.utc).date().isoformat()

    original = args.changelog.read_text(encoding="utf-8")
    new_text = _build_rewrite(
        original=original,
        new_version=args.new_version,
        date_stamp=args.date,
    )
    args.changelog.write_text(new_text, encoding="utf-8")
    print(
        f"changelog_release: wrote {args.changelog} "
        f"with '## [{args.new_version}] — {args.date}'"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
