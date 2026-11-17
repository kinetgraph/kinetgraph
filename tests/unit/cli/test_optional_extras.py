# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""The CLI must work with the ``[cli]`` extra alone.

The ``keys`` command imported ``cryptography`` at the top of its
module. ``cryptography`` belongs to the ``[crypto]`` extra, and
the CLI is installable with the ``[cli]`` extra alone -- so the
import broke **every** ``knt`` command on a CLI-only install,
including the ones that never touch a key.

The probe runs in a **subprocess** so the block on
``cryptography`` cannot leak into the rest of the suite.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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


def test_cli_imports_without_the_crypto_extra() -> None:
    """``kntgraph.cli.main`` must import without ``cryptography``.

    The released 0.19.0 imported ``cryptography`` at the top
    of the ``keys`` command module, which made every ``knt``
    command fail on a CLI-only install.
    """
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "imported" in result.stdout
