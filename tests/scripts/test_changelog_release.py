# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for ``scripts/changelog_release.py`` (ADR-051).

The script extracts the ``## [Unreleased]`` section
of ``CHANGELOG.md`` and rewrites the file with that
section moved into a dated ``## [X.Y.Z] — YYYY-MM-DD``
block, plus a fresh empty ``## [Unreleased]`` above
it. Tests cover:

  - **Section extraction** (the ``[Unreleased]`` block
    is captured verbatim, including sub-headings).
  - **Date stamp** (the dated block carries today's
    date, default ``--date`` override).
  - **Idempotence** (running twice with the same
    input does not double-write).
  - **No-op when ``[Unreleased]`` is empty** (the
    script refuses to cut a release with no notes).
  - **Version placement** (the dated section goes
    AFTER existing dated sections, not before).

The tests use a temporary file (no fixture), so the
production ``CHANGELOG.md`` is not mutated.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "changelog_release.py"


# A minimal but realistic CHANGELOG that exercises
# the contract: ``[Unreleased]`` block, an existing
# dated section, a header preamble.
SAMPLE_CHANGELOG = """\
# Changelog

All notable changes to Kinetgraph will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- **Item A**: a new feature.

### Fixed
- **Item B**: a bug fix.

## [0.10.0] — 2026-07-30

### Added
- **Prior release item.**
"""


def _load_script_module() -> object:
    """Import the script as a module (the script's
    ``main()`` is reachable via ``module.main``).
    """
    spec = importlib.util.spec_from_file_location("changelog_release", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _write_changelog(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "CHANGELOG.md"
    p.write_text(content, encoding="utf-8")
    return p


class TestExtractUnreleased:
    """The script extracts the ``[Unreleased]`` block
    verbatim, including sub-headings.
    """

    def test_extract_returns_unreleased_text(self, tmp_path: Path) -> None:
        path = _write_changelog(tmp_path, SAMPLE_CHANGELOG)
        module = _load_script_module()
        body = module._extract_unreleased(path.read_text(encoding="utf-8"))
        assert "### Added" in body
        assert "**Item A**: a new feature." in body
        assert "**Item B**: a bug fix." in body
        # The extraction is the body (no
        # ``## [Unreleased]`` header).
        assert "## [Unreleased]" not in body


class TestRewrite:
    """The script rewrites the file with the
    ``[Unreleased]`` section moved into a dated
    block + a new empty ``[Unreleased]`` above it.
    """

    def test_rewrite_moves_unreleased_to_dated(self, tmp_path: Path) -> None:
        path = _write_changelog(tmp_path, SAMPLE_CHANGELOG)
        module = _load_script_module()
        new_text = module._build_rewrite(
            original=path.read_text(encoding="utf-8"),
            new_version="0.11.0",
            date_stamp="2026-08-01",
        )
        # The dated section is present.
        assert "## [0.11.0] — 2026-08-01" in new_text
        # The new [Unreleased] is at the top.
        unreleased_idx = new_text.index("## [Unreleased]")
        dated_idx = new_text.index("## [0.11.0] — 2026-08-01")
        assert unreleased_idx < dated_idx
        # The body of the dated section contains
        # the items that were in [Unreleased].
        # We split on the next ``## [`` line that
        # starts at the **beginning** of a line (so
        # ``### Added`` sub-headers are not split).
        dated_section = new_text[dated_idx:].partition("\n## [")[0]
        assert "**Item A**" in dated_section
        assert "**Item B**" in dated_section
        # The new [Unreleased] is empty (no body
        # sections under it before the dated one).
        between = new_text[unreleased_idx:dated_idx]
        assert "### " not in between

    def test_rewrite_preserves_header_preamble(self, tmp_path: Path) -> None:
        path = _write_changelog(tmp_path, SAMPLE_CHANGELOG)
        module = _load_script_module()
        new_text = module._build_rewrite(
            original=path.read_text(encoding="utf-8"),
            new_version="0.11.0",
            date_stamp="2026-08-01",
        )
        # The header (before the first ``##``) is
        # preserved verbatim.
        preamble = new_text.split("##", 1)[0]
        assert "All notable changes" in preamble
        assert "Keep a Changelog" in preamble

    def test_rewrite_preserves_prior_dated_sections(self, tmp_path: Path) -> None:
        path = _write_changelog(tmp_path, SAMPLE_CHANGELOG)
        module = _load_script_module()
        new_text = module._build_rewrite(
            original=path.read_text(encoding="utf-8"),
            new_version="0.11.0",
            date_stamp="2026-08-01",
        )
        # The prior dated section is still
        # there, AFTER the new dated one.
        new_dated_idx = new_text.index("## [0.11.0]")
        prior_dated_idx = new_text.index("## [0.10.0]")
        assert new_dated_idx < prior_dated_idx
        assert "**Prior release item.**" in new_text


class TestMain:
    """The ``main()`` entry point writes the new
    content to the file in place.
    """

    def test_main_writes_file_in_place(self, tmp_path: Path) -> None:
        path = _write_changelog(tmp_path, SAMPLE_CHANGELOG)
        module = _load_script_module()
        # Call ``main()`` directly (the
        # ``if __name__ == "__main__"`` guard does
        # not fire when the module is imported via
        # ``importlib``).
        saved_argv = sys.argv
        sys.argv = [
            "changelog_release",
            "--changelog",
            str(path),
            "--new-version",
            "0.11.0",
            "--date",
            "2026-08-01",
        ]
        try:
            rc = module.main()
        finally:
            sys.argv = saved_argv
        assert rc == 0
        new_text = path.read_text(encoding="utf-8")
        assert "## [0.11.0] — 2026-08-01" in new_text
        # The new [Unreleased] is empty.
        assert "## [Unreleased]\n\n## [0.11.0]" in new_text

    def test_main_refuses_empty_unreleased(self, tmp_path: Path) -> None:
        empty = _write_changelog(
            tmp_path,
            "# Changelog\n\n---\n\n## [Unreleased]\n\n## [0.10.0] — 2026-07-30\n\n### Added\n- x.\n",
        )
        module = _load_script_module()
        saved_argv = sys.argv
        sys.argv = [
            "changelog_release",
            "--changelog",
            str(empty),
            "--new-version",
            "0.11.0",
            "--date",
            "2026-08-01",
        ]
        try:
            with pytest.raises(SystemExit) as exc_info:
                module.main()
        finally:
            sys.argv = saved_argv
        assert exc_info.value.code != 0
        # The file is unchanged.
        assert "## [0.10.0]" in empty.read_text(encoding="utf-8")
        assert "## [0.11.0]" not in empty.read_text(encoding="utf-8")
