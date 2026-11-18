# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``knt`` installer script (``scripts/install.sh``).

The installer is a POSIX shell script that a user pipes from
the network into their shell. Two properties matter:

  1. It is **valid POSIX sh** -- ``sh -n`` must accept it, so
     it runs on dash (Debian), ash (Alpine) and bash-as-sh.
  2. It **decides correctly**: with ``uv`` on the PATH it
     delegates to ``uv tool install``; without it, it falls
     back to a local virtual environment and ``pip``. Both
     paths must leave a ``knt`` shim in the bin directory.

The tests stub ``uv`` and ``python3`` so nothing touches the
network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "scripts" / "install.sh"

# A stub that records how it was called and fakes the result.
_UV_STUB = """#!/bin/sh
echo "uv $*" >> "$KNT_TEST_LOG"
if [ "$1" = "tool" ] && [ "$2" = "install" ]; then
    # uv puts the shim in the bin directory it manages.
    mkdir -p "$KNT_TEST_BIN"
    printf '#!/bin/sh\\necho "knt 9.9.9-stub"\\n' > "$KNT_TEST_BIN/knt"
    chmod +x "$KNT_TEST_BIN/knt"
fi
exit 0
"""

# A stub that answers the installer's probes and fakes a venv.
_PYTHON_STUB = """#!/bin/sh
if [ "$1" = "-c" ]; then
    case "$2" in
        *version_info*) exit 0 ;;
        *python_version*) echo "3.12.0"; exit 0 ;;
    esac
    exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    mkdir -p "$3/bin"
    printf '#!/bin/sh\\necho "knt 0.0.0-stub"\\n' > "$3/bin/knt"
    chmod +x "$3/bin/knt"
    # The venv's interpreter is this same stub, so the pip
    # calls it receives are recorded too.
    cp "$0" "$3/bin/python"
    chmod +x "$3/bin/python"
    exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
    echo "pip $*" >> "$KNT_TEST_LOG"
    exit 0
fi
exit 0
"""


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_installer(
    tmp_path: Path,
    *,
    with_uv: bool,
    args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run the installer in an isolated sandbox.

    Returns:
        The completed process, the call log and the bin
        directory the installer was pointed at.
    """
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    stubs = tmp_path / "stubs"
    log = tmp_path / "calls.log"
    for directory in (home, bin_dir, stubs):
        directory.mkdir(parents=True, exist_ok=True)
    log.touch()
    _write_stub(stubs / "python3", _PYTHON_STUB)
    if with_uv:
        _write_stub(stubs / "uv", _UV_STUB)

    env = {
        # A minimal PATH: the stubs plus the system directories.
        # The real ``uv`` lives in the user's home, so it stays
        # out of the sandbox -- the fallback path is only
        # reachable when ``uv`` is genuinely absent.
        "PATH": os.pathsep.join([str(stubs), "/usr/bin", "/bin"]),
        "HOME": str(home),
        "KNT_HOME": str(home / "knt"),
        "KNT_BIN_DIR": str(bin_dir),
        "KNT_TEST_LOG": str(log),
        "KNT_TEST_BIN": str(bin_dir),
        # The interpreter is pinned so the sandbox does not
        # depend on which pythons the machine happens to have.
        "KNT_PYTHON": str(stubs / "python3"),
        "NO_COLOR": "1",
    }
    result = subprocess.run(
        ["sh", str(INSTALLER), *(args or [])],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, log, bin_dir


def test_installer_is_valid_posix_sh() -> None:
    """``sh -n`` must accept the script.

    The script is piped into whatever shell the user has; a
    bashism would break it on dash (Debian's ``/bin/sh``) or
    ash (Alpine's).
    """
    result = subprocess.run(
        ["sh", "-n", str(INSTALLER)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_installer_delegates_to_uv_when_present(tmp_path: Path) -> None:
    """With ``uv`` on the PATH, the install goes through uv."""
    result, log, bin_dir = _run_installer(tmp_path, with_uv=True)

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "uv tool install --force kntgraph[cli,crypto]" in calls
    # The pip fallback did not run.
    assert "pip" not in calls
    # The shim is where the operator expects it.
    assert (bin_dir / "knt").is_file()


def test_installer_falls_back_to_a_virtual_environment(tmp_path: Path) -> None:
    """Without ``uv``, the installer builds its own venv."""
    result, log, bin_dir = _run_installer(tmp_path, with_uv=False)

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "pip install --quiet --upgrade kntgraph[cli,crypto]" in calls
    # The shim points at the venv's entry point.
    shim = bin_dir / "knt"
    assert shim.is_file()
    assert (tmp_path / "home" / "knt" / "venv" / "bin" / "knt").is_file()


def test_installer_pins_a_requested_version(tmp_path: Path) -> None:
    """``--version`` pins the package."""
    result, log, _bin_dir = _run_installer(
        tmp_path, with_uv=True, args=["--version", "0.19.0"]
    )

    assert result.returncode == 0, result.stderr
    assert "uv tool install --force kntgraph[cli,crypto]==0.19.0" in log.read_text(
        encoding="utf-8"
    )


def test_installer_uninstalls(tmp_path: Path) -> None:
    """``--uninstall`` removes the shim and the venv."""
    result, log, bin_dir = _run_installer(tmp_path, with_uv=True)
    assert result.returncode == 0, result.stderr
    assert (bin_dir / "knt").is_file()

    result, log, bin_dir = _run_installer(tmp_path, with_uv=True, args=["--uninstall"])
    assert result.returncode == 0, result.stderr
    assert not (bin_dir / "knt").exists()
    assert not (tmp_path / "home" / "knt" / "venv").exists()


def test_installer_rejects_an_unknown_option(tmp_path: Path) -> None:
    """An unknown option fails loudly instead of installing."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "NO_COLOR": "1",
    }
    result = subprocess.run(
        ["sh", str(INSTALLER), "--nope"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert "Unknown option" in result.stderr


def test_installer_help_does_not_install(tmp_path: Path) -> None:
    """``--help`` prints the usage and exits without installing."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "NO_COLOR": "1",
    }
    result = subprocess.run(
        ["sh", str(INSTALLER), "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0
    assert "knt installer" in result.stdout
    assert "--uninstall" in result.stdout


@pytest.mark.parametrize("flag", ["--version", "--uninstall"])
def test_installer_documents_every_flag(tmp_path: Path, flag: str) -> None:
    """Every flag the script accepts is in the usage text."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "NO_COLOR": "1",
    }
    result = subprocess.run(
        ["sh", str(INSTALLER), "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert flag in result.stdout
