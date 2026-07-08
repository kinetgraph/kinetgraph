# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``kntgraph._optional``.

These tests pin the import-guard contract: every optional
dependency must be importable via the framework's public
modules even when the underlying package is NOT installed.
The tests use a ``sys.meta_path`` blocker to simulate the
absence of ``fastapi``, ``gliner2``, ``falkordb``, ``ollama``
and ``litellm`` without actually uninstalling them.

The canonical error message wording is also pinned, so
accidental rewording in the helper is caught.
"""

from __future__ import annotations

import importlib
import sys

import pytest


class _Blocker:
    """
    `sys.meta_path` blocker that raises ImportError when
    the requested module (or any submodule of it) is
    imported. Used to simulate a missing optional dep.
    """

    def __init__(self, *names: str) -> None:
        self._names = names

    def find_spec(self, fullname, path=None, target=None):  # noqa: ARG002
        for n in self._names:
            if fullname == n or fullname.startswith(n + "."):
                # Returning a spec that raises on load_module
                # is the canonical "import failed" trick.
                from importlib.machinery import (
                    ModuleSpec,
                )

                class _Loader:
                    def create_module(self, spec):
                        raise ImportError(f"simulated: {fullname} not installed")

                    def exec_module(self, module):  # noqa: ARG002
                        raise ImportError(f"simulated: {fullname} not installed")

                return ModuleSpec(fullname, _Loader())

        return None

    def find_module(self, fullname, path=None):  # pragma: no cover
        # py3.12+ uses find_spec; kept for py3.11 if added.
        return None


@pytest.fixture
def block_optional(monkeypatch):
    """
    Install a meta_path blocker for the optional deps the
    framework guards. Yields the blocker for tests that
    want to inspect what was blocked.
    """
    blocked = (
        "fastapi",
        "uvicorn",
        "falkordb",
        "ollama",
        "gliner2",
        "litellm",
    )
    blocker = _Blocker(*blocked)
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    # Drop anything already imported under those names so
    # the blocker's find_spec is reached on the next import.
    for m in list(sys.modules.keys()):
        if any(m == b or m.startswith(b + ".") for b in blocked):
            monkeypatch.delitem(sys.modules, m)
    yield blocker
    # Cleanup: drop anything that was re-imported during
    # the test so the next test sees a clean state.
    for m in list(sys.modules.keys()):
        if any(m == b or m.startswith(b + ".") for b in blocked):
            sys.modules.pop(m, None)


# ---------------------------------------------------------------------------
# require_optional
# ---------------------------------------------------------------------------


class TestRequireOptional:
    def test_imports_stdlib_module(self):
        from kntgraph._optional import require_optional

        # ``json`` is always available; sanity check.
        mod = require_optional("json", "irrelevant", purpose="test")
        assert mod.__name__ == "json"

    def test_raises_with_canonical_message(self):
        from kntgraph._optional import require_optional

        with pytest.raises(ImportError) as exc_info:
            require_optional(
                "definitely_not_a_real_package_xyz",
                "kntgraph[fake-extra]",
                purpose="unit test",
            )
        msg = str(exc_info.value)
        assert "unit test" in msg
        assert "definitely_not_a_real_package_xyz" in msg
        assert "kntgraph[fake-extra]" in msg
        assert "uv add" in msg
        assert "pip install" in msg

    def test_default_purpose_when_omitted(self):
        from kntgraph._optional import require_optional

        with pytest.raises(ImportError) as exc_info:
            require_optional(
                "definitely_not_a_real_package_xyz",
                "kntgraph[fake-extra]",
            )
        # ``purpose`` defaults to "this feature".
        assert "this feature" in str(exc_info.value)


# ---------------------------------------------------------------------------
# try_import
# ---------------------------------------------------------------------------


class TestTryImport:
    def test_returns_module_when_present(self):
        from kntgraph._optional import try_import

        assert try_import("json") is not None

    def test_returns_none_when_missing(self):
        from kntgraph._optional import try_import

        assert try_import("definitely_not_a_real_package_xyz") is None

    def test_extra_argument_is_accepted(self, block_optional):
        from kntgraph._optional import try_import

        # The ``extra`` kwarg is documented as accepted but
        # not used in the return path. Passing it must not
        # raise.
        assert try_import("falkordb", "kntgraph[falkordb]") is None


# ---------------------------------------------------------------------------
# Framework imports do not require optional deps
# ---------------------------------------------------------------------------


class TestFrameworkImportability:
    """
    Pin the contract: ``import kntgraph`` (and the
    public submodules) must succeed on a Python environment
    where every optional dep is absent. The integration
    tests that DO need the optional deps install them via
    the corresponding extra; the bare framework must be
    importable in a fresh venv with no extras.
    """

    def test_import_kntgraph_top_level(self, block_optional):
        # If the framework's top-level imports a guarded
        # dep eagerly, this will raise ImportError.
        importlib.import_module("kntgraph")

    def test_import_kntgraph_api_module(self, block_optional):
        # The module itself is importable (create_app is
        # the lazy boundary).
        importlib.import_module("kntgraph.api")

    def test_import_kntgraph_api_intent_router(self, block_optional):
        importlib.import_module("kntgraph.api.intent_router")

    def test_import_kntgraph_knowledge_falkordb(self, block_optional):
        importlib.import_module("kntgraph.knowledge.falkordb")

    def test_import_kntgraph_knowledge_embedding(self, block_optional):
        importlib.import_module("kntgraph.knowledge.embedding")

    def test_import_kntgraph_knowledge_extraction_gliner_intent(self, block_optional):
        importlib.import_module("kntgraph.knowledge.extraction.gliner_intent")


# ---------------------------------------------------------------------------
# The guard fires on USE, not on import
# ---------------------------------------------------------------------------


class TestGuardFiresOnUse:
    """
    The optional dep must be importable as a module, but
    concrete CLASSES / FUNCTIONS that need it must raise
    a clear error at the point of use. This is the
    "lazy / eager-in-constructor" pattern: the package is
    importable, but constructing an instance (or invoking
    the function) requires the dep.
    """

    def test_create_app_raises_with_canonical_message(self, block_optional):
        """
        ``kntgraph.api.intent_router.create_app`` is the
        single point that materialises FastAPI. With fastapi
        blocked, calling it must raise ImportError pointing
        at ``kntgraph[api]``.
        """
        from kntgraph.api.intent_router import create_app

        with pytest.raises(ImportError) as exc_info:
            create_app(
                log=None,  # type: ignore[arg-type]
                registry=None,  # type: ignore[arg-type]
                verifier=None,  # type: ignore[arg-type]
            )
        msg = str(exc_info.value)
        assert "fastapi" in msg
        assert "kntgraph[api]" in msg
        assert "uv add" in msg

    def test_gliner_intent_classifier_raises_with_canonical_message(
        self, block_optional
    ):
        """
        Constructing ``GlinerIntentAdapter`` with gliner2
        blocked must raise ImportError pointing at the
        correct extra. The class is importable (so a user
        can write ``from kntgraph.knowledge.extraction
        import GlinerIntentAdapter`` and catch the error
        at instantiation time) — only the constructor
        itself touches the dep.
        """
        from kntgraph.knowledge.extraction.gliner_intent import (
            GlinerIntentAdapter,
        )

        with pytest.raises(ImportError) as exc_info:
            GlinerIntentAdapter()
        msg = str(exc_info.value)
        assert "gliner2" in msg
        assert "kntgraph[gliner]" in msg
        assert "uv add" in msg

    def test_gliner_field_finder_raises_with_canonical_message(self, block_optional):
        from kntgraph.knowledge.extraction.argument import (
            GlinerFieldFinder,
        )

        with pytest.raises(ImportError) as exc_info:
            GlinerFieldFinder()
        msg = str(exc_info.value)
        assert "gliner2" in msg
        assert "kntgraph[gliner]" in msg
