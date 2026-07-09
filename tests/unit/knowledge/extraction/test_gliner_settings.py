# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests verifying that ``GlinerIntentAdapter`` and
``SLMIntentClassifier`` (Iter 21 facade) read their
default model name from ``Settings``.

Iter 21 split the GLiNER2 wrapper into:

  - ``GlinerIntentAdapter`` (low-level, renamed from
    ``GlinerIntentClassifier``) — the concrete GLiNER2
    adapter. Lazy-imports ``gliner2`` (optional extra).
  - ``SLMIntentClassifier`` (NEW facade) — IS-A
    ``IntentClassifier`` (Protocol), holds a reference
    to a low-level adapter (default:
    ``GlinerIntentAdapter``) and delegates every call.

    The ``SLM`` prefix (``Small Language Model``)
    covers GLiNER2 today but is **not tied** to it —
    a future ``TinyLLMIntentAdapter`` or
    ``FastTextIntentAdapter`` can be slotted in via
    ``SLMIntentClassifier(adapter=...)`` without
    changing the facade's public API.

Before Iter 21, the GLiNER2 classifier hard-coded
``model_name: str = "gliner2-base"`` in ``__init__``.
The framework also had a
``Settings.arg_extractor_model_id`` field (default
``"default"``) but **nothing read it** — the
constructor and the Settings field were drifting.

After Iter 21:
  - The default ``model_name`` is read from
    ``Settings.arg_extractor_model_id`` (default
    updated from ``"default"`` to ``"gliner2-base"``).
  - An explicit ``model_name=`` arg still wins.
  - The Settings field default is now the real model
    name (was a placeholder that didn't match any
    real model).

The tests mock ``GLiNER2.from_pretrained`` to avoid
loading the real model (which would require the
``gliner2`` package and a network call).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from kntgraph.infra.config import fresh_settings


class _MockGLiNER2Module:
    """Mock of the ``gliner2`` module exposing
    ``GLiNER2.from_pretrained`` as a MagicMock that
    returns a sentinel. The ``require_optional`` call
    in the classifier uses an attribute access chain
    (``require(...).GLiNER2``), so we mock the chain."""

    def __init__(self):
        self.GLiNER2 = MagicMock()
        self.GLiNER2.from_pretrained = MagicMock(return_value="sentinel-model")


def _build_classifier(model_name: str | None = None):
    """
    Construct a ``GlinerIntentAdapter`` with
    ``gliner2`` mocked. The model load is bypassed; we
    just want to verify the ``_model_name`` attribute
    that the constructor stored.
    """
    mock_module = _MockGLiNER2Module()
    # ``require_optional(name, extra, purpose)`` returns
    # an object that has ``GLiNER2`` as an attribute.
    # We mock it as a MagicMock with the right attribute.
    mock_require_result = MagicMock()
    mock_require_result.GLiNER2 = mock_module.GLiNER2

    with patch(
        "kntgraph._optional.require_optional",
        return_value=mock_require_result,
    ):
        from kntgraph.knowledge.extraction import (
            gliner_intent,
        )

        kwargs = {}
        if model_name is not None:
            kwargs["model_name"] = model_name
        return gliner_intent.GlinerIntentAdapter(**kwargs)


class TestGLiNERReadsSettings:
    def test_default_model_from_settings(self, monkeypatch):
        """
        ``GlinerIntentAdapter()`` (no args) reads
        the model from Settings. With the default
        Settings, ``arg_extractor_model_id="gliner2-base"``
        so the adapter's ``_model_name`` matches.
        """
        fresh_settings.cache_clear()
        classifier = _build_classifier()
        assert classifier._model_name == "gliner2-base"
        fresh_settings.cache_clear()
        classifier = _build_classifier()
        assert classifier._model_name == "gliner2-base"
        fresh_settings.cache_clear()

    def test_env_override_changes_model(self, monkeypatch):
        """
        When ``KNT_ARG_EXTRACTOR_MODEL_ID`` is set, the
        classifier's default changes. Operators can
        point the framework at a private HF checkpoint
        or a local path.
        """
        monkeypatch.setenv(
            "KNT_ARG_EXTRACTOR_MODEL_ID",
            "urchen/gliner-multi-pii-base",
        )
        fresh_settings.cache_clear()
        classifier = _build_classifier()
        assert classifier._model_name == ("urchen/gliner-multi-pii-base")
        fresh_settings.cache_clear()


class TestGLiNERExplicitOverrides:
    def test_explicit_model_wins_over_settings(self, monkeypatch):
        """
        Passing ``model_name=`` to the constructor
        must still win over Settings. The default is
        "Settings unless told otherwise".
        """
        monkeypatch.setenv(
            "KNT_ARG_EXTRACTOR_MODEL_ID",
            "urchen/gliner-multi-pii-base",
        )
        fresh_settings.cache_clear()
        classifier = _build_classifier(model_name="custom/local/model")
        assert classifier._model_name == "custom/local/model"
        fresh_settings.cache_clear()

    def test_explicit_model_no_settings(self):
        """When the caller passes ``model_name=`` and
        no env override is set, the explicit arg wins."""
        fresh_settings.cache_clear()
        classifier = _build_classifier(model_name="explicit-only-model")
        assert classifier._model_name == "explicit-only-model"
        fresh_settings.cache_clear()
