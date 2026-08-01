# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for ``scripts/check_version.py`` (ADR-051).

The script fails the build when the installed
``kntgraph.__version__`` disagrees with the latest
git tag (``git describe --tags --abbrev=0``). The
two cases:

  - **In sync** (HEAD == the latest tag): exit 0.
  - **Drift** (HEAD is N commits past the tag, or
    the installed package was built before the
    latest tag was created): exit non-zero with a
    remediation message ("run ``uv sync``").

These tests cover the contract by invoking the
script as a subprocess against a fake git
environment (no real git operations on the test
host's repo).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "check_version.py"


def _run_script(*, fake_git_describe: str | None) -> subprocess.CompletedProcess:
    """Invoke ``check_version.py`` with a stub
    ``git describe`` so the test does not depend on
    the test host's actual git history.

    Args:
        fake_git_describe: the value the stub
            ``git describe --tags --abbrev=0`` should
            print. ``None`` means the stub fails
            with non-zero exit (simulating "no tag
            found").
    """
    if not SCRIPT_PATH.exists():
        pytest.skip(f"{SCRIPT_PATH} not found")
    # Build a tiny shim script that replaces ``git``
    # in PATH and re-invokes the real Python with
    # the same args. The shim prints
    # ``fake_git_describe`` and exits 0 (or fails
    # if ``fake_git_describe`` is None).
    # The shim lives OUTSIDE the repo (in /tmp) so
    # the REUSE license scan does not pick it up.
    import tempfile

    shim_dir = Path(tempfile.mkdtemp(prefix="knt-check-version-"))
    (shim_dir / "git").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "describe" ]; then\n'
        '  if [ -z "$FAKE_GIT_DESCRIBE" ]; then\n'
        "    echo 'fatal: No names found, cannot describe anything.'\n"
        "    exit 128\n"
        "  else\n"
        '    echo "$FAKE_GIT_DESCRIBE"\n'
        "    exit 0\n"
        "  fi\n"
        "else\n"
        '  exec /usr/bin/env git "$@"\n'
        "fi\n"
    )
    (shim_dir / "git").chmod(0o755)
    import os

    env = dict(os.environ)
    env["PATH"] = f"{shim_dir}:{env.get('PATH', '')}"
    # Disable bytecode cache: the test invokes the
    # ``check_version.py`` script as a subprocess,
    # which would otherwise create
    # ``tests/scripts/__pycache__/check_version.cpython-312.pyc``
    # and trip the REUSE license check. The test
    # only runs the script for the side effect; we
    # do not need the bytecode cache.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if fake_git_describe is not None:
        env["FAKE_GIT_DESCRIBE"] = fake_git_describe
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(SCRIPTS_DIR.parent),
    )


class TestCheckVersion:
    """The contract: ``check_version.py`` exits 0
    when the installed version matches the latest
    tag, and non-zero with a remediation message
    when it does not.
    """

    def test_in_sync_exits_zero(self) -> None:
        """When the installed version and the
        latest tag agree, the script exits 0.
        """
        import kntgraph
        from packaging.version import Version

        if kntgraph.__version__ == "0.0.0+unknown":
            pytest.skip("no _version.py generated; cannot exercise the in-sync path")
        v = Version(kntgraph.__version__.split("+", 1)[0])
        # The base version is e.g. "0.10.0" or
        # "0.10.1.dev0". The script accepts the
        # devN form too (we test with the devN
        # form, which is what the install reports
        # when HEAD is past the latest tag).
        # Round to the tag's base: drop the devN
        # suffix.
        tag = f"v{v.base_version}"
        result = _run_script(fake_git_describe=tag)
        assert result.returncode == 0, (
            f"check_version.py failed with "
            f"installed={kntgraph.__version__!r} and "
            f"tag={tag!r}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_drift_exits_nonzero(self) -> None:
        """When the installed version is older than
        the latest tag, the script exits non-zero
        with a remediation message.
        """
        import kntgraph
        from packaging.version import Version

        if kntgraph.__version__ == "0.0.0+unknown":
            pytest.skip("no _version.py generated; cannot exercise the drift path")
        installed_base = Version(kntgraph.__version__.split("+", 1)[0]).base_version
        # Construct a tag that is strictly ahead
        # of the installed base. e.g. if
        # installed_base is "0.10.1", use
        # "v0.10.2" (next patch). This is the only
        # guaranteed-ahead tag; bumping a minor or
        # major would also work but a patch bump
        # is the minimum case the script must
        # catch.
        major, minor, micro = installed_base.split(".")
        next_patch = f"v{major}.{minor}.{int(micro) + 1}"
        result = _run_script(fake_git_describe=next_patch)
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "version drift" in combined.lower(), (
            f"expected a 'version drift' diagnostic; got: {combined}"
        )

    def test_no_tag_exits_nonzero(self) -> None:
        """When ``git describe`` fails (no tag in
        history), the script exits non-zero (a
        missing tag is a configuration error, not
        a pass condition).
        """
        result = _run_script(fake_git_describe=None)
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "git describe" in combined.lower() or "no tag" in combined.lower(), (
            f"expected a 'no tag' diagnostic; got: {combined}"
        )
