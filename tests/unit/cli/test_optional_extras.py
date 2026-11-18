# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""The CLI must work with the ``[cli]`` extra alone.

The installer (ADR — the ``curl | sh`` script) installs
``kntgraph[cli]`` and nothing else. Every command must
therefore import without the optional extras -- the
``[crypto]`` extra in particular, which only the ``keys``
command needs.

The probe runs in a **subprocess** so the block on
``cryptography`` cannot leak into the rest of the suite.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# A probe that hides ``cryptography`` from the import system
# and then imports the CLI.
_PROBE = """
import sys


class _BlockCryptography:
    \"\"\"A meta-path finder that hides the optional dependency.\"\"\"

    def find_spec(self, name, path=None, target=None):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ImportError(f"{name} is blocked by the probe")
        return None


for _name in [n for n in sys.modules if n.startswith("cryptography")]:
    del sys.modules[_name]
sys.meta_path.insert(0, _BlockCryptography())

from kntgraph.cli.main import app  # noqa: F401

print("imported")
"""


def _run_probe() -> subprocess.CompletedProcess[str]:
    """Run the probe in a clean subprocess."""
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(REPO_ROOT),
    )


def test_cli_imports_without_the_crypto_extra() -> None:
    """``kntgraph.cli.main`` must import without ``cryptography``.

    The released 0.19.0 imported ``cryptography`` at the top
    of the ``keys`` command module, which made **every** ``knt``
    command fail on a CLI-only install -- the very install the
    installer script performs.
    """
    result = _run_probe()
    assert result.returncode == 0, result.stderr
    assert "imported" in result.stdout


@pytest.mark.parametrize(
    "command",
    ["--version", "--help"],
)
def test_cli_runs_without_the_crypto_extra(command: str) -> None:
    """The commands that need no key must answer without it."""
    probe = f"""
import sys


class _BlockCryptography:
    def find_spec(self, name, path=None, target=None):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ImportError(f"{{name}} is blocked by the probe")
        return None


sys.meta_path.insert(0, _BlockCryptography())

from typer.testing import CliRunner

from kntgraph.cli.main import app

result = CliRunner().invoke(app, [{command!r}])
print("exit", result.exit_code)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "exit 0" in result.stdout, result.stdout
