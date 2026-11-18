# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
#

"""
Unit tests for the GLiNER2 backend seam.

The framework pins the SHAPE of the backend (what the
adapters call); the RUNTIME that serves it is the
application's choice. These tests pin that contract:

  - an injected model means no ``gliner2`` import and no
    checkpoint load -- the adapters work in an environment
    where ``gliner2`` is not installed at all;
  - without an injected model, the adapter asks for
    ``gliner2``, and when the package is present but its
    inference runtime is not, the error names the missing
    runtime instead of pointing back at the extra (which
    would send the operator in a circle).
"""

from __future__ import annotations

import asyncio
import sys
from importlib.machinery import ModuleSpec
from unittest.mock import MagicMock

import pytest

from kntgraph.knowledge.extraction.argument import GlinerFieldFinder
from kntgraph.knowledge.extraction.gliner_argument import GlinerArgumentAdapter
from kntgraph.knowledge.extraction.gliner_intent import GlinerIntentAdapter
from kntgraph.tools.schema import FieldSpec

# ---------------------------------------------------------------------------
# Fixtures: the two ways an optional dep can be absent
# ---------------------------------------------------------------------------


class _Blocker:
    """
    ``sys.meta_path`` blocker that makes the named module
    unimportable -- simulating "the package is not installed".
    """

    def __init__(self, *names: str) -> None:
        self._names = names

    def find_spec(self, fullname, path=None, target=None):
        for n in self._names:
            if fullname == n or fullname.startswith(n + "."):

                class _Loader:
                    def create_module(self, spec):
                        raise ImportError(f"simulated: {fullname} not installed")

                    def exec_module(self, module):
                        raise ImportError(f"simulated: {fullname} not installed")

                return ModuleSpec(fullname, _Loader())

        return None


class _MissingRuntime:
    """
    ``sys.meta_path`` blocker that simulates the OTHER
    failure: the package IS installed, but one of its own
    dependencies (the inference runtime) is missing. The
    import of the package raises ``ModuleNotFoundError``
    naming the missing runtime -- exactly what
    ``import gliner2`` does without ``torch``.
    """

    def __init__(self, package: str, missing: str) -> None:
        self._package = package
        self._missing = missing

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self._package:
            return None

        missing = self._missing

        class _Loader:
            def create_module(self, spec):
                raise ModuleNotFoundError(f"No module named {missing!r}", name=missing)

            def exec_module(self, module):  # pragma: no cover
                raise ModuleNotFoundError(f"No module named {missing!r}", name=missing)

        return ModuleSpec(fullname, _Loader())


@pytest.fixture
def no_gliner2(monkeypatch):
    """``gliner2`` is not installed at all."""
    blocker = _Blocker("gliner2")
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    for m in list(sys.modules):
        if m == "gliner2" or m.startswith("gliner2."):
            monkeypatch.delitem(sys.modules, m)
    yield


@pytest.fixture
def gliner2_without_runtime(monkeypatch):
    """``gliner2`` is installed, but its runtime (``torch``) is not."""
    blocker = _MissingRuntime("gliner2", "torch")
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    for m in list(sys.modules):
        if m == "gliner2" or m.startswith("gliner2."):
            monkeypatch.delitem(sys.modules, m)
    yield


# ---------------------------------------------------------------------------
# Fakes: the models the application would bring
# ---------------------------------------------------------------------------


class _FakeIntentModel:
    """Stands in for a remote (or local) intent classifier."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def batch_extract(self, texts, schema, *, include_confidence=False):
        self.calls.append((list(texts), dict(schema)))
        return [
            {
                "_fmh_intent_0": {"label": "emitir_nfe", "confidence": 0.9},
                "_fmh_intent_1": {"label": "none_of_the_above", "confidence": 0.95},
            }
        ]


class _FakeFieldModel:
    """Stands in for a remote (or local) entity extractor."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def extract_entities(self, text, labels, *, include_confidence=False):
        self.calls.append((text, list(labels)))
        # The canonical GLiNER2 1.3.x shape.
        return {"entities": {labels[0]: [{"text": "ACME", "confidence": 0.9}]}}


# ---------------------------------------------------------------------------
# The injected backend
# ---------------------------------------------------------------------------


class TestInjectedBackend:
    def test_intent_adapter_uses_the_injected_model(self, no_gliner2) -> None:
        """
        With a model in hand, the adapter must not need
        ``gliner2`` at all -- and must call the model it
        was given.
        """
        model = _FakeIntentModel()
        adapter = GlinerIntentAdapter(model=model)

        result = asyncio.run(adapter.classify("quero emitir a nota", ("emitir_nfe",)))

        assert result.top_label == "emitir_nfe"
        assert model.calls, "the injected model was never called"

    def test_field_finder_uses_the_injected_model(self, no_gliner2) -> None:
        """Same contract for the argument-extraction finder."""
        model = _FakeFieldModel()
        finder = GlinerFieldFinder(model=model)

        found = asyncio.run(finder.find("a empresa ACME", _field_spec("company")))

        assert found is not None
        assert found[0] == "ACME"
        assert model.calls, "the injected model was never called"

    def test_argument_adapter_accepts_an_injected_model(self, no_gliner2) -> None:
        """
        The argument adapter wires the finder; with an
        injected model the whole chain must build without
        ``gliner2``.
        """
        worker_manager = MagicMock()
        adapter = GlinerArgumentAdapter(
            worker_manager,
            model=_FakeFieldModel(),
        )
        assert adapter is not None

    def test_injected_model_skips_the_gliner2_import(
        self, gliner2_without_runtime
    ) -> None:
        """
        The decisive case: ``gliner2`` is installed but its
        runtime is missing. An injected model must work
        anyway -- that is the whole point of the seam.
        """
        adapter = GlinerIntentAdapter(model=_FakeIntentModel())
        assert adapter is not None


def _field_spec(name: str) -> FieldSpec:
    """A minimal ``FieldSpec`` for the finder tests."""
    return FieldSpec(name=name, json_type="string", required=False)


# ---------------------------------------------------------------------------
# The error message
# ---------------------------------------------------------------------------


class TestRuntimeMessage:
    def test_missing_runtime_is_named(self, gliner2_without_runtime) -> None:
        """
        The package is present, the runtime is not. The
        message must name the missing runtime and point at
        the choice the application has -- NOT back at the
        extra, which would reproduce the same error.
        """
        with pytest.raises(ImportError) as exc_info:
            GlinerIntentAdapter()
        msg = str(exc_info.value)
        assert "torch" in msg
        assert "gliner2[local]" in msg
        assert "kntgraph[gliner]" not in msg

    def test_missing_package_points_at_the_extra(self, no_gliner2) -> None:
        """
        The package itself is absent: the canonical message
        (the extra) is the right remedy.
        """
        with pytest.raises(ImportError) as exc_info:
            GlinerIntentAdapter()
        msg = str(exc_info.value)
        assert "kntgraph[gliner]" in msg
        assert "uv add" in msg
