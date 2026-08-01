# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for ``scripts/quality_report.py`` (ADR-051).

The script generates ``docs/quality.md`` from a
``report`` dict. After ADR-051 the project version
is the **git tag**, not a field in
``pyproject.toml``. The report must include the
version (so the badge in the README and the
snapshot in ``docs/quality.md`` stay in sync).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "quality_report.py"


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location("quality_report", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# A minimal ``report`` shape; the script only reads
# the keys it needs in ``render_markdown``.
def _minimal_report(*, version: str) -> dict:
    return {
        "generated_at": "2026-08-01T00:00:00Z",
        "total_duration_s": 0.0,
        "version": version,
        "gates": {
            "lint": {
                "ok": True,
                "tool": "ruff",
                "issues": 0,
                "duration_s": 0.0,
            },
            "format": {
                "ok": True,
                "tool": "ruff",
                "formatted": 0,
                "needs_reformat": 0,
                "duration_s": 0.0,
            },
        },
    }


class TestReportIncludesVersion:
    """The Markdown report must include the
    installed version (so the README badge and the
    quality snapshot stay in sync with the git
    tag). The version is the **base** of
    ``kntgraph.__version__`` (no ``devN`` /
    ``+g<sha>`` suffix).
    """

    def test_markdown_contains_version(self) -> None:
        module = _load_module()
        md = module.render_markdown(_minimal_report(version="0.10.0"))
        # The version appears in the generated
        # report (the canonical source of truth
        # for the README badge).
        assert "0.10.0" in md
        # The label is descriptive enough for
        # someone reading the file cold.
        assert "Version" in md or "version" in md

    def test_markdown_strips_devN_suffix(self) -> None:
        """A ``0.10.1.dev0+gc0adc4211`` (the
        ``setuptools_scm`` form when HEAD is past
        the latest tag) is rendered as ``0.10.1``
        in the report -- the devN suffix is not
        user-facing in the badge / report.
        """
        module = _load_module()
        # The script reads ``kntgraph.__version__``
        # which produces the devN form locally; the
        # report must derive the base before
        # rendering.
        md = module.render_markdown(_minimal_report(version="0.10.1.dev0+gc0adc4211"))
        assert "0.10.1" in md
        # The dev suffix is not in the rendered
        # report.
        assert ".dev0" not in md
        assert "+g" not in md

    def test_markdown_handles_zero_zero_zero_unknown(
        self,
    ) -> None:
        """When the install is missing
        ``_version.py`` (the ``0.0.0+unknown``
        fallback), the report shows the raw value
        verbatim -- the script does not crash.
        This is the contract for source installs
        without ``setuptools_scm``.
        """
        module = _load_module()
        md = module.render_markdown(_minimal_report(version="0.0.0+unknown"))
        # The raw value is present (we do not
        # hide it -- the developer should know
        # their install is mis-configured).
        assert "0.0.0+unknown" in md


class TestReportVersionSource:
    """The script's ``main()`` reads the version
    from ``kntgraph.__version__`` (via the same
    helper used by ``update_version_badge.py``),
    not from ``pyproject.toml::version`` (which
    was removed in ADR-051 PR 1).
    """

    def test_get_version_reads_kntgraph(self) -> None:
        module = _load_module()
        version = module.get_version()
        # The helper returns the raw value (the
        # caller is responsible for stripping
        # devN if needed).
        import kntgraph

        assert version == kntgraph.__version__
        # When the install is missing
        # ``_version.py`` (the ``0.0.0+unknown``
        # fallback), the helper returns the raw
        # value verbatim.
        assert isinstance(version, str)
        assert version != ""
