# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests — GlinerModelRegistry wiring in adapters (ADR-055 §2.2).

These tests verify that the two adapters that previously called
``GLiNER2.from_pretrained`` directly now delegate to
``GlinerModelRegistry.get``. No real model is loaded; the registry is
mocked or source-inspected throughout.

What is covered:
  1. ``gliner_intent.py`` source contains no ``from_pretrained`` call
     (source-code inspection, same pattern as
     ``test_gliner_argument_no_leak.py``).
  2. ``_gliner_finder.py`` source contains no ``from_pretrained`` call.
  3. ``gliner.py`` framework code contains no ``from_pretrained`` call
     (the Template Method remains clean; subclasses carry the wiring).
  4. ``GlinerIntentAdapter.__init__`` calls ``GlinerModelRegistry.get``
     with the expected arguments (mock-based).
  5. ``GlinerFieldFinder.__init__`` calls ``GlinerModelRegistry.get``
     with the expected arguments (mock-based).
  6. Both adapters constructed with the same (model, device) share the
     same model object returned by the registry.
  7. ``KNT_MODEL_CACHE_DIR`` is forwarded from Settings to the registry
     call.

What is NOT covered here (see tests/integration/knowledge/extraction/):
  - Registry caching semantics with a real model.
  - Model functional behaviour (``batch_extract``, ``extract_entities``).
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

import kntgraph.knowledge.extraction.argument._gliner_finder as _finder_mod
import kntgraph.knowledge.extraction.gliner as _entity_mod
import kntgraph.knowledge.extraction.gliner_intent as _intent_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_registry(sentinel: object = "sentinel-model"):
    """Return a patcher that replaces ``GlinerModelRegistry.get`` with a
    MagicMock returning ``sentinel``. Used in adapter-construction tests
    to avoid a real model load while still exercising the wiring."""
    return patch(
        "kntgraph.knowledge.extraction._gliner_model_registry.GlinerModelRegistry.get",
        return_value=sentinel,
    )


# ---------------------------------------------------------------------------
# Source-code inspection tests
# ---------------------------------------------------------------------------


class TestNoDirectFromPretrained:
    """Verify that the framework modules no longer call
    ``GLiNER2.from_pretrained`` directly. The Template Method
    (``GlinerEntityAdapter``) is the remaining legitimate location for
    subclass-authored calls; the framework base itself must not."""

    def test_gliner_intent_has_no_from_pretrained(self) -> None:
        """``gliner_intent.py`` must not contain a live call to
        ``.from_pretrained(`` after the ADR-055 refactor. Docstring
        references to the old API are allowed; call sites are not."""
        source = inspect.getsource(_intent_mod)
        assert ".from_pretrained(" not in source, (
            "kntgraph.knowledge.extraction.gliner_intent still calls "
            "from_pretrained directly (ADR-055 regression)."
        )

    def test_gliner_finder_has_no_from_pretrained(self) -> None:
        """``_gliner_finder.py`` must not contain a live call to
        ``.from_pretrained(`` after the ADR-055 refactor."""
        source = inspect.getsource(_finder_mod)
        assert ".from_pretrained(" not in source, (
            "kntgraph.knowledge.extraction.argument._gliner_finder still "
            "calls from_pretrained directly (ADR-055 regression)."
        )

    def test_gliner_entity_framework_has_no_from_pretrained(self) -> None:
        """The ``GlinerEntityAdapter`` Template Method base must not call
        ``.from_pretrained(`` — that is the subclass's responsibility.
        Docstring references to the old API pattern are allowed."""
        source = inspect.getsource(_entity_mod)
        assert ".from_pretrained(" not in source, (
            "kntgraph.knowledge.extraction.gliner (the Template Method "
            "base) calls from_pretrained directly. "
            "Subclass examples in docstrings must use "
            "GlinerModelRegistry.get instead (ADR-055)."
        )


# ---------------------------------------------------------------------------
# Adapter delegation tests
# ---------------------------------------------------------------------------


class TestIntentAdapterUsesRegistry:
    """``GlinerIntentAdapter.__init__`` must call
    ``GlinerModelRegistry.get`` (not ``from_pretrained``) with the
    resolved model name, device, and cache_dir from Settings."""

    def test_registry_get_is_called_on_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Constructing ``GlinerIntentAdapter`` calls
        ``GlinerModelRegistry.get`` exactly once with the expected
        keyword arguments."""
        # Arrange: isolate Settings so model_cache_dir is None.
        from kntgraph.infra.config import fresh_settings

        fresh_settings.cache_clear()
        monkeypatch.delenv("KNT_MODEL_CACHE_DIR", raising=False)

        sentinel = MagicMock()
        with _patch_registry(sentinel) as mock_get:
            from kntgraph.knowledge.extraction.gliner_intent import (
                GlinerIntentAdapter,
            )

            adapter = GlinerIntentAdapter(model_name="gliner2-base", threshold=0.5)

        mock_get.assert_called_once_with(
            "gliner2-base",
            device=None,
            cache_dir=None,
        )
        assert adapter._model is sentinel
        fresh_settings.cache_clear()

    def test_cache_dir_forwarded_from_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``KNT_MODEL_CACHE_DIR`` is read from Settings and forwarded
        as ``cache_dir`` to ``GlinerModelRegistry.get``."""
        monkeypatch.setenv("KNT_MODEL_CACHE_DIR", "/data/models")
        from kntgraph.infra.config import fresh_settings

        fresh_settings.cache_clear()

        with _patch_registry() as mock_get:
            from kntgraph.knowledge.extraction.gliner_intent import (
                GlinerIntentAdapter,
            )

            GlinerIntentAdapter(model_name="gliner2-base")

        _, kwargs = mock_get.call_args
        assert kwargs.get("cache_dir") == "/data/models"
        fresh_settings.cache_clear()


class TestFieldFinderUsesRegistry:
    """``GlinerFieldFinder.__init__`` must call
    ``GlinerModelRegistry.get`` with the resolved model name, device,
    and cache_dir from Settings."""

    def test_registry_get_is_called_on_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Constructing ``GlinerFieldFinder`` calls
        ``GlinerModelRegistry.get`` exactly once."""
        from kntgraph.infra.config import fresh_settings

        fresh_settings.cache_clear()
        monkeypatch.delenv("KNT_MODEL_CACHE_DIR", raising=False)

        sentinel = MagicMock()
        with _patch_registry(sentinel) as mock_get:
            from kntgraph.knowledge.extraction.argument._gliner_finder import (
                GlinerFieldFinder,
            )

            finder = GlinerFieldFinder(model_name="gliner2-base")

        mock_get.assert_called_once_with(
            "gliner2-base",
            device=None,
            cache_dir=None,
        )
        assert finder._model is sentinel
        fresh_settings.cache_clear()

    def test_cache_dir_forwarded_from_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``KNT_MODEL_CACHE_DIR`` is forwarded as ``cache_dir``."""
        monkeypatch.setenv("KNT_MODEL_CACHE_DIR", "/opt/weights")
        from kntgraph.infra.config import fresh_settings

        fresh_settings.cache_clear()

        with _patch_registry() as mock_get:
            from kntgraph.knowledge.extraction.argument._gliner_finder import (
                GlinerFieldFinder,
            )

            GlinerFieldFinder(model_name="gliner2-base")

        _, kwargs = mock_get.call_args
        assert kwargs.get("cache_dir") == "/opt/weights"
        fresh_settings.cache_clear()


class TestAdaptersShareModel:
    """When two adapters are constructed with the same
    ``(model_name, device)``, the registry returns the same model
    object to both."""

    def test_intent_adapter_and_field_finder_share_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GlinerIntentAdapter`` and ``GlinerFieldFinder`` built with
        the same model name receive the same model object from the
        registry — one cold start, two references."""
        from kntgraph.infra.config import fresh_settings

        fresh_settings.cache_clear()
        monkeypatch.delenv("KNT_MODEL_CACHE_DIR", raising=False)

        shared_model = MagicMock(name="shared-gliner2-model")
        with _patch_registry(shared_model):
            from kntgraph.knowledge.extraction.argument._gliner_finder import (
                GlinerFieldFinder,
            )
            from kntgraph.knowledge.extraction.gliner_intent import (
                GlinerIntentAdapter,
            )

            intent = GlinerIntentAdapter(model_name="gliner2-base")
            finder = GlinerFieldFinder(model_name="gliner2-base")

        # Both received the same sentinel from the (mocked) registry.
        assert intent._model is shared_model
        assert finder._model is shared_model
        fresh_settings.cache_clear()
