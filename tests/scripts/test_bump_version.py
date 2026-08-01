# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for ``scripts/bump_version.py`` (ADR-051).

The script reads the current version from
``git describe --tags`` and creates a new tag
corresponding to the requested level (major,
minor, or patch). The tests cover:

  - **Bump level arithmetic** (major / minor /
    patch increments; the next version is
    correct relative to the current).
  - **Dry-run mode** (no tag is created; the
    next version is printed).
  - **Idempotence** (running the script with the
    same level twice does not create a second
    tag; the second run refuses because the tag
    already exists).
  - **No-tag case** (the script fails with a
    diagnostic when there is no git tag in
    history).
  - **PEP 440 parsing** (the produced tag is a
    valid version per ``packaging.version``).

The tests run the script in a temporary git repo
(``git init`` + ``git tag -a``) so the production
git history is not mutated.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.version import Version


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "bump_version.py"


# Regex matching the script's progress output.
# The format is ``current: X.Y.Z`` / ``next:    A.B.C``
# / ``tag:     vA.B.C`` (the spaces are part of the
# fixed-width output).
RE_CURRENT = re.compile(r"^current:\s*(\S+)\s*$", re.MULTILINE)
RE_NEXT = re.compile(r"^next:\s*(\S+)\s*$", re.MULTILINE)
RE_TAG = re.compile(r"^tag:\s*(\S+)\s*$", re.MULTILINE)


def _make_temp_git_repo(tmp_path: Path, *, tag: str | None) -> Path:
    """Initialise a temporary git repo with one
    commit and (optionally) one tag.

    The script is invoked with ``cwd=tmp_path`` so
    the ``git describe`` it runs reads from the
    test repo, not the production one.
    """
    _env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        # Make annotated tags deterministic.
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=tmp_path,
        env=_env,
        check=True,
        capture_output=True,
    )
    # Disable any inherited ``commit.gpgsign`` /
    # ``tag.gpgsign`` that the test host may have
    # configured (would otherwise make the
    # ``commit`` / ``tag`` commands interactive).
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "tag.gpgsign", "false"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    # Local config: in case the test host's git
    # ``user.email`` is unset (CI may run with no
    # global git config), set a per-repo identity
    # so ``commit`` and ``tag`` do not error with
    # "fatal: unable to auto-detect email address".
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    # A trivial commit so the repo has a HEAD.
    (tmp_path / "marker.txt").write_text("test\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "marker.txt"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=tmp_path,
        env=_env,
        check=True,
        capture_output=True,
    )
    if tag is not None:
        subprocess.run(
            ["git", "tag", "-a", tag, "-m", f"Release {tag}"],
            cwd=tmp_path,
            env=_env,
            check=True,
            capture_output=True,
        )
    return tmp_path


def _run_bump(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Invoke ``bump_version.py`` in ``tmp_path``."""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


class TestBumpArithmetic:
    """The bump level increments the version
    according to semver: ``major`` / ``minor`` /
    ``patch``.
    """

    def test_bump_patch_increments_micro(self, tmp_path: Path) -> None:
        _make_temp_git_repo(tmp_path, tag="v1.2.3")
        result = _run_bump(tmp_path, "--level", "patch", "--dry-run")
        assert result.returncode == 0
        m = RE_NEXT.search(result.stdout)
        assert m is not None, f"could not parse next: {result.stdout}"
        assert Version(m.group(1)) == Version("1.2.4")

    def test_bump_minor_resets_patch(self, tmp_path: Path) -> None:
        _make_temp_git_repo(tmp_path, tag="v1.2.3")
        result = _run_bump(tmp_path, "--level", "minor", "--dry-run")
        assert result.returncode == 0
        m = RE_NEXT.search(result.stdout)
        assert m is not None
        assert Version(m.group(1)) == Version("1.3.0")

    def test_bump_major_resets_minor_and_patch(self, tmp_path: Path) -> None:
        _make_temp_git_repo(tmp_path, tag="v1.2.3")
        result = _run_bump(tmp_path, "--level", "major", "--dry-run")
        assert result.returncode == 0
        m = RE_NEXT.search(result.stdout)
        assert m is not None
        assert Version(m.group(1)) == Version("2.0.0")

    def test_tag_strips_v_prefix(self, tmp_path: Path) -> None:
        """The ``tag:`` line uses the canonical
        ``vX.Y.Z`` form (with the ``v``)."""
        _make_temp_git_repo(tmp_path, tag="v1.2.3")
        result = _run_bump(tmp_path, "--level", "minor", "--dry-run")
        assert result.returncode == 0
        m = RE_TAG.search(result.stdout)
        assert m is not None
        assert m.group(1) == "v1.3.0"


class TestDryRun:
    """``--dry-run`` does not create a tag; the
    next version is printed and the script exits 0.
    """

    def test_dry_run_does_not_create_tag(self, tmp_path: Path) -> None:
        _make_temp_git_repo(tmp_path, tag="v1.2.3")
        result = _run_bump(tmp_path, "--level", "minor", "--dry-run")
        assert result.returncode == 0
        # The dry-run output mentions the dry-run
        # intent so the operator can read it back.
        assert "dry-run" in result.stdout.lower()
        # No tag was created.
        listed = subprocess.run(
            ["git", "tag", "--list"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "v1.3.0" not in listed.stdout


class TestIdempotence:
    """The script refuses to create a tag that
    already exists locally. A second run with the
    same level computes the next version **from
    the latest tag** (which is now the one the
    first run just created) and bumps relative to
    that -- so the second run does not recreate
    the just-created tag; it bumps from it.
    """

    def test_running_twice_does_not_recreate_same_tag(self, tmp_path: Path) -> None:
        """First run: ``v1.2.3`` → next ``v1.3.0``,
        created. Second run: latest is now
        ``v1.3.0``; next is ``v1.4.0``, which is
        a different tag. The first tag (``v1.3.0``)
        is **not** recreated.
        """
        _make_temp_git_repo(tmp_path, tag="v1.2.3")
        first = _run_bump(tmp_path, "--level", "minor")
        assert first.returncode == 0, first.stdout + first.stderr
        # The tag was created.
        listed = subprocess.run(
            ["git", "tag", "--list"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "v1.3.0" in listed.stdout
        # A second run reads v1.3.0 as the
        # current, bumps to v1.4.0 -- it does
        # **not** try to re-create v1.3.0.
        second = _run_bump(tmp_path, "--level", "minor")
        assert second.returncode == 0, (
            f"second run should succeed; got: {second.stdout}\nstderr: {second.stderr}"
        )
        listed2 = subprocess.run(
            ["git", "tag", "--list"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "v1.4.0" in listed2.stdout
        # The first tag (v1.3.0) is still there.
        assert "v1.3.0" in listed2.stdout

    def test_refuses_to_recreate_existing_tag(self, tmp_path: Path) -> None:
        """The script refuses to create a tag that
        already exists locally.

        We call the script's ``_create_tag``
        helper with ``cwd=tmp_path`` (a temp
        git repo). The first invocation creates
        ``v1.3.0``; the second invocation
        refuses because the tag now exists.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location("bump_version", SCRIPT_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        _make_temp_git_repo(tmp_path, tag="v1.2.0")
        # First call creates v1.3.0.
        module._create_tag("v1.3.0", cwd=tmp_path)
        # Second call refuses: the tag exists.
        with pytest.raises(SystemExit) as exc_info:
            module._create_tag("v1.3.0", cwd=tmp_path)
        assert "already exists" in str(exc_info.value).lower()


class TestNoTag:
    """The script fails with a diagnostic when
    there is no git tag in history.
    """

    def test_no_tag_fails(self, tmp_path: Path) -> None:
        _make_temp_git_repo(tmp_path, tag=None)
        result = _run_bump(tmp_path, "--level", "patch")
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "tag" in combined.lower()
        assert "no names" in combined.lower() or "no tag" in combined.lower()


class TestBumpAfterCommits:
    """When the working tree is past the latest
    tag, the script reads the latest tag (not the
    devN version) and bumps relative to that.
    """

    def test_bump_ignores_devN_offset(self, tmp_path: Path) -> None:
        """``v0.10.0`` + 1 commit past the tag. The
        script must read ``0.10.0`` from the tag
        and bump relative to that (not relative
        to ``0.10.1.dev0``).
        """
        _make_temp_git_repo(tmp_path, tag="v0.10.0")
        # Add a commit past the tag.
        env = {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_AUTHOR_DATE": "2026-01-02T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-02T00:00:00Z",
        }
        (tmp_path / "extra.txt").write_text("extra\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "extra.txt"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "post-tag commit"],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
        )
        result = _run_bump(tmp_path, "--level", "patch", "--dry-run")
        assert result.returncode == 0
        m = RE_NEXT.search(result.stdout)
        assert m is not None
        # The next version is ``0.10.1``, not
        # ``0.10.2`` (the patch is computed
        # relative to the tag's ``0.10.0``).
        assert Version(m.group(1)) == Version("0.10.1")
