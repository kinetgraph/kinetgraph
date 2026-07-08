# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Pin the import-guard contract for ``kntgraph.agents``.

The vertical package must be importable in an environment
that does NOT have ``litellm``, ``falkordb`` or ``ollama``
installed. These tests use a ``sys.meta_path`` blocker to
simulate the absence of those packages without actually
uninstalling them.

Companion to ``kntgraph/tests/unit/test_optional.py``.
"""

from __future__ import annotations

import importlib
import sys

import pytest


class _Blocker:
    def __init__(self, *names: str) -> None:
        self._names = names

    def find_spec(self, fullname, path=None, target=None):  # noqa: ARG002
        for n in self._names:
            if fullname == n or fullname.startswith(n + "."):
                from importlib.machinery import ModuleSpec

                class _Loader:
                    def create_module(self, spec):  # noqa: ARG002
                        raise ImportError(f"simulated: {fullname} not installed")

                    def exec_module(self, module):  # noqa: ARG002
                        raise ImportError(f"simulated: {fullname} not installed")

                return ModuleSpec(fullname, _Loader())
        return None


@pytest.fixture
def block_optional(monkeypatch):
    blocked = ("litellm", "falkordb", "ollama", "gliner2")
    blocker = _Blocker(*blocked)
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    for m in list(sys.modules.keys()):
        if any(m == b or m.startswith(b + ".") for b in blocked):
            monkeypatch.delitem(sys.modules, m)
    yield blocker
    for m in list(sys.modules.keys()):
        if any(m == b or m.startswith(b + ".") for b in blocked):
            sys.modules.pop(m, None)


class TestVerticalImportability:
    """
    ``import kntgraph.agents`` and its public submodules must
    succeed even when ``litellm`` is not installed.
    """

    def test_import_kntgraph_agents(self, block_optional):
        importlib.import_module("kntgraph.agents")

    def test_import_kntgraph_agents_tools(self, block_optional):
        # ``LiteLLMTool`` is importable; only its ``invoke``
        # method touches litellm.
        mod = importlib.import_module("kntgraph.agents.tools")
        assert hasattr(mod, "LiteLLMTool")
        assert hasattr(mod, "configure_litellm_env")

    def test_import_kntgraph_agents_roles(self, block_optional):
        importlib.import_module("kntgraph.agents.roles")

    def test_import_kntgraph_agents_config(self, block_optional):
        importlib.import_module("kntgraph.agents.config")


class TestVerticalExtrasIsolation:
    """
    With optional deps blocked, importing the vertical
    must NOT bring them in via transitive imports.

    This is the key contract that ``fmh-agents``
    ``pyproject.toml`` MUST honor: declaring ``falkordb``
    or ``ollama`` as a hard dependency would make this
    test fail (because the install step itself would
    fail; here we simulate the install being absent).
    """

    def test_no_transitive_falkordb_import(self, block_optional, monkeypatch):
        """
        If any module under ``kntgraph.agents`` did
        ``import falkordb`` at top level, the meta_path
        blocker would raise ImportError when the module is
        imported. The blocker is installed; if any of the
        public submodules pulls it in transitively, the
        fixture's cleanup would NOT be reached (the import
        would already have raised). We assert that
        ``import kntgraph.agents`` returns cleanly.
        """
        importlib.import_module("kntgraph.agents")
        importlib.import_module("kntgraph.agents.tools")
        importlib.import_module("kntgraph.agents.roles")
        importlib.import_module("kntgraph.agents.config")
        # If we got here, no transitive import of falkordb
        # / ollama / litellm / gliner2 happened at top
        # level. Explicitly re-check that those names are
        # not in sys.modules (the blocker would have raised
        # before populating them).
        for name in ("falkordb", "ollama", "litellm", "gliner2"):
            assert name not in sys.modules, (
                f"{name} leaked into sys.modules; the "
                f"vertical pulled it in transitively."
            )
