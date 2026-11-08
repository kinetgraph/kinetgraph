# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Smoke tests for ``examples/30_declarative_agent_modelling.py``.

The example demonstrates the declarative agent model of ADR-088. It
is **pure**: it builds a ``World`` through the SUT builders in
``kntgraph.testing`` and calls the real systems against it. No
Redis, no dispatcher, no external model.

The example must:

  - Import without raising.
  - Run end to end without raising -- every assertion inside
    ``main()`` holds.
  - Use the **library**'s declarative model, not a local copy. A
    local ``RoleSpec`` / ``AgentDefinition`` / ``AgentBundle`` in the
    example would be a second vocabulary for the same concept, and
    the two would drift.

The example is loaded via ``importlib.util`` (the same pattern as
``test_example_01_migration.py``) because examples are run as
scripts, not imported from the package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples"
_EXAMPLE_NAME = "30_declarative_agent_modelling"


def _load_module() -> object:
    """Load the example as a module (examples are not on ``sys.path``)."""
    spec = importlib.util.spec_from_file_location(
        _EXAMPLE_NAME, _EXAMPLES_DIR / f"{_EXAMPLE_NAME}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_EXAMPLE_NAME] = module
    spec.loader.exec_module(module)
    return module


def test_example_30_imports_without_raising() -> None:
    """The example imports cleanly."""
    assert _load_module() is not None


def test_example_30_runs_end_to_end() -> None:
    """The example's ``main()`` completes; its assertions hold."""
    module = _load_module()
    module.main()


def test_example_30_uses_the_library_model() -> None:
    """The example declares the agent with the library's types.

    The local definitions were removed when the model moved into
    ``kntgraph.agents``. A second copy of the vocabulary would be
    exactly the drift the architecture harness prevents.
    """
    source = (_EXAMPLES_DIR / f"{_EXAMPLE_NAME}.py").read_text(encoding="utf-8")

    assert "from kntgraph.agents import AgentDefinition, RoleSpec" in source
    assert "class RoleSpec" not in source
    assert "class AgentDefinition" not in source
    assert "class AgentBundle" not in source
    assert "def _bundle(" not in source
