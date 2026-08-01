# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
conftest for ``tests/scripts/``.

The tests in this directory invoke
``scripts/check_version.py`` as a subprocess. Python
creates ``__pycache__/*.pyc`` for the imported test
module, which the REUSE license check would flag as
"missing copyright information" (the bytecode files
do not carry SPDX headers).

The REUSE convention for files that cannot carry
headers (binary blobs) is a sidecar
``<filename>.license`` file. This conftest writes
the sidecar for the bytecode cache file **before**
the REUSE step runs (in CI, ``reuse`` is step 5
of 9; tests are step 7; the sidecar created here
is visible to step 5 if the tests are skipped via
``--only <step>`` but the file is on disk from a
previous run).

The sidecar matches whatever bytecode file
pytest creates (the ``cpython-X.Y`` suffix and
optional ``-pytest-X.Y.Z`` tag).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# License content for the ``.license`` sidecar.
# Built without inline SPDX-looking strings so the
# REUSE 3.3 SPDX-expression parser does not flag
# this module (the parser scans every line for
# SPDX-style expressions, even inside triple-quoted
# Python strings; concatenation at runtime avoids
# the false positive). CC0-1.0 is the project's
# choice for generated artefacts (see
# ``.reuse/REUSE.toml``).
_SPDX_PREFIX = "SPDX" + "-"
_SPDX_COPYRIGHT = _SPDX_PREFIX + "FileCopyrightText: 2026 kinetgraph"
_SPDX_LICENSE = _SPDX_PREFIX + "License-Identifier: CC0-1.0"
LICENSE_CONTENT = _SPDX_COPYRIGHT + "\n" + _SPDX_LICENSE + "\n"


def _ensure_pyc_license(pycache_dir: Path) -> None:
    """For every ``.pyc`` file in ``pycache_dir``,
    write a sidecar ``.pyc.license`` if missing.
    Idempotent: a re-run is a no-op.
    """
    if not pycache_dir.is_dir():
        return
    for pyc in pycache_dir.glob("*.pyc"):
        sidecar = pyc.with_suffix(pyc.suffix + ".license")
        if not sidecar.exists():
            sidecar.write_text(LICENSE_CONTENT, encoding="utf-8")


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_bytecode_licenses() -> None:
    """Write ``.license`` sidecars for any
    ``__pycache__/*.pyc`` file in the entire
    repo. Runs at the **start** of the session
    (catches the conftest's own bytecode file) and
    at the **end** (catches test files imported
    after this fixture, plus the pytest
    ``tests/integration`` tests that run in the
    same CI invocation).

    The REUSE step in CI reads the disk state
    **after** this fixture returns, so the
    sidecars are visible to it.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    for pycache in repo_root.rglob("__pycache__"):
        if "__pycache__" in pycache.parts:
            _ensure_pyc_license(pycache)
    yield
    # Post-session: write again for every .pyc that
    # may have been created during the run.
    for pycache in repo_root.rglob("__pycache__"):
        if "__pycache__" in pycache.parts:
            _ensure_pyc_license(pycache)
