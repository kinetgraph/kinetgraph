# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Pytest configuration for the CLI test suite.

The CLI tests depend on the ``typer`` package, which
is an **optional** dependency of the framework
(``pyproject.toml``'s ``[cli]`` extra). The CI's
default ``uv run`` does not install the ``[cli]``
extra, so the test files in this directory fail at
collect time with ``ModuleNotFoundError: No module
named 'typer'``.

This conftest uses ``collect_ignore_glob`` to skip
the entire directory at collect time when the
``typer`` module is not importable. The pattern is
the same one the Python community uses for
optional-dependency test directories (e.g. the
``torch``-bound tests in many ML libraries):

  - When the operator runs the suite with
    ``uv sync --extra cli`` (the recommended setup
    for CLI development), ``typer`` is importable
    and the tests are collected + executed.
  - When the operator runs the suite without the
    ``[cli]`` extra, the directory is silently
    ignored at collect time and the rest of the
    suite runs unaffected.

A similar pattern is used for the resilience tests
that depend on ``fastapi`` (see
``tests/unit/resilience/test_rate_limit_middleware.py``
for the per-file ``pytest.importorskip`` variant);
we centralise the skip here for the CLI tests
because the dependency is directory-wide, not
per-test.

Why a conftest and not per-file ``importorskip``?
----------------------------------------------------

The 9 test files in this directory all do
``from typer.testing import CliRunner`` at the
module level. A per-file ``pytest.importorskip("typer")``
only works at call time, not at collect time, so
the import at module top would still raise and
pytest would still abort with
``Interrupted: 9 errors during collection``.

The conftest's ``collect_ignore_glob`` runs **before**
the module imports, so the skip is clean.
"""

from __future__ import annotations

import pytest

# ``pytest.importorskip`` is the standard helper for
# optional-dependency test directories. It returns
# ``None`` when the dep is importable and raises
# ``pytest.skip.Exception`` when it is not; we catch
# the raise and configure the collect ignore list.
try:
    pytest.importorskip("typer")
    _typer_available = True
except pytest.skip.Exception:
    _typer_available = False


# Pytest reads this list at the start of the
# collection phase. When ``typer`` is missing, we
# add a glob that matches every ``test_*.py`` in
# this directory so the suite is silently skipped.
# When ``typer`` is present, the glob is empty and
# the tests are collected as usual.
collect_ignore_glob: list[str] = ["test_*.py"] if not _typer_available else []
