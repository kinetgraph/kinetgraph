# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
bump_version.py -- cut a release by creating a git tag
locally (ADR-051).

Reads the current version from ``git describe --tags
--abbrev=0`` (the latest annotated tag matching
``vX.Y.Z``), computes the next version per the
requested level (major / minor / patch), and creates
the tag locally with ``git tag -a vX.Y.Z -m "Release
vX.Y.Z"``.

The script does **not** push the tag (``AGENTS.md
§11.3``: no agent-driven pushes). The operator
runs ``git push origin vX.Y.Z`` after the release
workflow (PR 4 of ADR-051) opens the GitHub Release.

Usage:

    uv run python scripts/bump_version.py --level <major|minor|patch>
    uv run python scripts/bump_version.py --level minor --dry-run

Output (in order):

    current: <X.Y.Z>
    next:    <A.B.C>
    tag:     vA.B.C
    (dry-run: tag not created)
    # or
    created tag vA.B.C locally; push with: git push origin vA.B.C

Exit code: 0 on success, non-zero on error (no
tag in history, tag already exists, malformed
input).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from packaging.version import InvalidVersion, Version


# Tag scheme. Must match the ``tag_regex`` in
# ``[tool.setuptools_scm]`` (``pyproject.toml``).
_TAG_PREFIX = "v"
_TAG_FORMAT = "v{major}.{minor}.{micro}"


def _current_tag() -> str:
    """Return the latest released version, stripped
    of the leading ``v``. ``latest`` is "the most
    recently **created** tag", not the most recent
    ancestor of HEAD — the two are equal in a
    linear history (the normal case) but diverge
    when a tag was cut from a side branch and the
    side branch was then rebase-merged into ``main``
    as a different SHA. The version published on
    PyPI is the side-branch tag, so the bump must
    read that one to avoid colliding with an
    already-released version.

    Fails with a non-zero exit if no tag exists.
    """
    # ``for-each-ref`` sorted by creation date
    # returns the tags in the order they were cut
    # (``taggerdate`` for annotated tags, the
    # commit date for lightweight ones). This is
    # the same order PyPI's "latest release" uses,
    # so the bump and the publish stay in sync.
    result = subprocess.run(
        [
            "git",
            "for-each-ref",
            "--sort=-creatordate",
            "--format=%(refname:short)",
            "refs/tags",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"bump_version: 'git for-each-ref refs/tags' "
            f"failed; cannot read the tag list.\nstderr: "
            f"{result.stderr}"
        )
    tags = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not tags:
        raise SystemExit(
            "bump_version: no tags found in refs/tags. "
            "Run 'git tag -a vX.Y.Z' first, or apply "
            "the retroactive tags from ADR-051 PR 1."
        )
    tag = tags[0]
    if not tag.startswith(_TAG_PREFIX):
        raise SystemExit(
            f"bump_version: latest tag {tag!r} does not "
            f"start with the expected prefix "
            f"{_TAG_PREFIX!r}; the project's tag scheme "
            f"(ADR-051) requires the ``v`` prefix; check "
            f"[tool.setuptools_scm] in pyproject.toml."
        )
    return tag[len(_TAG_PREFIX) :]


def _next_version(current: Version, level: str) -> Version:
    """Compute the next version per the bump
    level.

    ``Version`` in modern ``packaging`` is a
    string-only constructor (it parses the PEP 440
    string at ``__init__``). We construct the next
    version as a string and re-parse for type
    safety; the parse round-trip catches a typo in
    the version format at script-time.
    """
    if level == "major":
        raw = f"{current.major + 1}.0.0"
    elif level == "minor":
        raw = f"{current.major}.{current.minor + 1}.0"
    elif level == "patch":
        raw = f"{current.major}.{current.minor}.{current.micro + 1}"
    else:
        raise SystemExit(  # pragma: no cover (argparse rejects first)
            f"bump_version: unknown level {level!r}"
        )
    return Version(raw)


def _tag_exists(tag: str, *, cwd: "Path | None" = None) -> bool:
    """True when ``tag`` already exists locally.

    ``cwd`` is the directory to run ``git`` in; it
    defaults to ``None`` (the script's cwd). Tests
    pass a temporary git repo so the production
    repo is not mutated.
    """
    result = subprocess.run(
        ["git", "tag", "--list", tag],
        capture_output=True,
        text=True,
        check=True,
        cwd=cwd,
    )
    return tag in result.stdout.splitlines()


def _create_tag(tag: str, *, cwd: "Path | None" = None) -> None:
    """Create an annotated tag. Raises ``SystemExit``
    if the tag already exists locally (the operator
    must decide whether to delete the existing
    tag or pick a different level).

    ``cwd`` is forwarded to ``_tag_exists`` and the
    ``git tag`` subprocess; see ``_tag_exists``.
    """
    if _tag_exists(tag, cwd=cwd):
        raise SystemExit(
            f"bump_version: tag {tag} already exists "
            f"locally. Refusing to re-create it. If "
            f"this is a mistake, run "
            f"'git tag -d {tag}' to remove the old "
            f"one (use with care -- tags are a "
            f"release artifact)."
        )
    result = subprocess.run(
        [
            "git",
            "tag",
            "-a",
            tag,
            "-m",
            f"Release {tag}",
        ],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise SystemExit(f"bump_version: 'git tag -a {tag}' failed: {result.stderr}")


def main() -> int:
    """Parse args, read the current tag, compute the
    next version, and (unless ``--dry-run``) create
    the new tag locally.
    """
    parser = argparse.ArgumentParser(
        prog="bump_version",
        description=("Cut a release by creating the next git tag (ADR-051)."),
    )
    parser.add_argument(
        "--level",
        required=True,
        choices=("major", "minor", "patch"),
        help="Which segment to bump (PEP 440).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the next version and exit 0 "
            "without creating the tag. Used by the "
            "CI step ``bump-dry-run`` to assert the "
            "bump logic is sane."
        ),
    )
    args = parser.parse_args()

    current_str = _current_tag()
    try:
        current = Version(current_str)
    except InvalidVersion as e:
        raise SystemExit(
            f"bump_version: latest tag v{current_str} "
            f"is not a valid PEP 440 version: {e}"
        ) from e

    next_version = _next_version(current, args.level)
    next_tag = _TAG_FORMAT.format(
        major=next_version.major,
        minor=next_version.minor,
        micro=next_version.micro,
    )

    # Fixed-width output so the operator can
    # ``grep`` for the new tag in CI logs. The
    # values are right-aligned to 8 chars.
    print(f"current: {current.major}.{current.minor}.{current.micro}")
    print(f"next:    {next_version.major}.{next_version.minor}.{next_version.micro}")
    print(f"tag:     {next_tag}")

    if args.dry_run:
        print("(dry-run: tag not created)")
        return 0

    _create_tag(next_tag)
    print(f"created tag {next_tag} locally; push with: git push origin {next_tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
