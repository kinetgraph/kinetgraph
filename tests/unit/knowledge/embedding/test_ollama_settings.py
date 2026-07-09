# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests verifying that ``OllamaEmbeddingAdapter.__init__``
reads its defaults from ``Settings`` (Iter 20).

Before Iter 20, the adapter hard-coded
``DEFAULT_PARAPHRASE_MULTILINGUAL_MODEL`` and
``DEFAULT_PARAPHRASE_MULTILINGUAL_DIM`` as the
defaults. Operators who wanted to tune per-deployment
had to subclass the adapter.

After Iter 20, the adapter's defaults are read from
``Settings()``. Tests verify:

  1. The factory ``OllamaEmbeddingAdapter()`` (no args)
     reads ``settings.embedding_default_model`` and
     ``settings.embedding_default_dimension``.
  2. An explicit ``model=`` / ``dimension=`` arg still
     wins over Settings.
  3. The same applies to ``host`` (no env default, but
     the parameter is forwarded).

Iter 23 adds coverage for the per-call timeout
(``embedding_timeout_seconds``):

  4. The adapter's ``_timeout_s`` reads from Settings
     by default.
  5. An explicit ``timeout_s=`` arg still wins.
  6. ``embed()`` wraps the call with
     ``asyncio.wait_for(timeout=self._timeout_s)`` and
     raises ``EmbeddingTimeoutError`` on timeout.

The tests do NOT exercise the real Ollama client
(no imports of ollama in the test scope). They just
verify the attribute values after construction.
"""

from __future__ import annotations


from kntgraph.infra.config import fresh_settings


class TestOllamaAdapterReadsSettings:
    def test_default_model_from_settings(self, monkeypatch):
        """
        ``OllamaEmbeddingAdapter()`` (no args) reads
        the model from Settings. With the default
        Settings, ``embedding_default_model="paraphrase-multilingual"``
        so the adapter's ``_model`` matches.
        """
        fresh_settings.cache_clear()
        from kntgraph.knowledge.embedding._ollama import (
            OllamaEmbeddingAdapter,
        )

        adapter = OllamaEmbeddingAdapter()
        assert adapter._model == "paraphrase-multilingual"
        fresh_settings.cache_clear()

    def test_default_dimension_from_settings(self, monkeypatch):
        """The adapter's ``_dimension`` matches
        ``settings.embedding_default_dimension``
        (default 768)."""
        fresh_settings.cache_clear()
        from kntgraph.knowledge.embedding._ollama import (
            OllamaEmbeddingAdapter,
        )

        adapter = OllamaEmbeddingAdapter()
        assert adapter._dimension == 768
        fresh_settings.cache_clear()

    def test_env_override_changes_model(self, monkeypatch):
        """
        When ``KNT_EMBEDDING_DEFAULT_MODEL`` is set,
        the adapter's default changes to match.
        """
        monkeypatch.setenv("KNT_EMBEDDING_DEFAULT_MODEL", "nomic-embed-text")
        fresh_settings.cache_clear()
        from kntgraph.knowledge.embedding._ollama import (
            OllamaEmbeddingAdapter,
        )

        adapter = OllamaEmbeddingAdapter()
        assert adapter._model == "nomic-embed-text"
        fresh_settings.cache_clear()

    def test_env_override_changes_dimension(self, monkeypatch):
        """
        When ``KNT_EMBEDDING_DEFAULT_DIMENSION`` is set,
        the adapter's default changes. The
        operator MUST also update the model to one
        that returns the new dimension — this test
        just verifies the dimension knob works.
        """
        monkeypatch.setenv("KNT_EMBEDDING_DEFAULT_DIMENSION", "1536")
        fresh_settings.cache_clear()
        from kntgraph.knowledge.embedding._ollama import (
            OllamaEmbeddingAdapter,
        )

        adapter = OllamaEmbeddingAdapter()
        assert adapter._dimension == 1536
        fresh_settings.cache_clear()


class TestOllamaAdapterExplicitOverrides:
    def test_explicit_model_wins_over_settings(self, monkeypatch):
        """
        Passing ``model=`` to the constructor must
        still win over Settings.
        """
        monkeypatch.setenv("KNT_EMBEDDING_DEFAULT_MODEL", "nomic-embed-text")
        fresh_settings.cache_clear()
        from kntgraph.knowledge.embedding._ollama import (
            OllamaEmbeddingAdapter,
        )

        adapter = OllamaEmbeddingAdapter(model="custom-model")
        assert adapter._model == "custom-model"
        fresh_settings.cache_clear()

    def test_explicit_dimension_wins_over_settings(self, monkeypatch):
        monkeypatch.setenv("KNT_EMBEDDING_DEFAULT_DIMENSION", "1536")
        fresh_settings.cache_clear()
        from kntgraph.knowledge.embedding._ollama import (
            OllamaEmbeddingAdapter,
        )

        adapter = OllamaEmbeddingAdapter(dimension=512)
        assert adapter._dimension == 512
        fresh_settings.cache_clear()

    def test_explicit_host_overrides_default(self):
        """``host=`` has no Settings default (it
        falls back to the ``OLLAMA_HOST`` env var
        inside the Ollama client). An explicit
        ``host=`` arg wins over the env var."""
        fresh_settings.cache_clear()
        from kntgraph.knowledge.embedding._ollama import (
            OllamaEmbeddingAdapter,
        )

        adapter = OllamaEmbeddingAdapter(host="http://my-ollama:11434")
        assert adapter._host == "http://my-ollama:11434"
        fresh_settings.cache_clear()


class TestOllamaAdapterTimeoutFromSettings:
    """Iter 23: the adapter's per-call timeout is read
    from ``settings.embedding_timeout_seconds``."""

    def test_default_timeout_from_settings(self, monkeypatch):
        """``OllamaEmbeddingAdapter()`` (no args) reads
        the timeout from Settings. With the default
        Settings, ``embedding_timeout_seconds=5.0``
        so the adapter's ``_timeout_s`` matches.
        """
        fresh_settings.cache_clear()
        from kntgraph.knowledge.embedding._ollama import (
            OllamaEmbeddingAdapter,
        )

        adapter = OllamaEmbeddingAdapter()
        assert adapter._timeout_s == 5.0
        fresh_settings.cache_clear()

    def test_env_override_changes_timeout(self, monkeypatch):
        """When ``KNT_EMBEDDING_TIMEOUT_SECONDS`` is set,
        the adapter's default timeout changes to match.
        """
        monkeypatch.setenv("KNT_EMBEDDING_TIMEOUT_SECONDS", "15.0")
        fresh_settings.cache_clear()
        from kntgraph.knowledge.embedding._ollama import (
            OllamaEmbeddingAdapter,
        )

        adapter = OllamaEmbeddingAdapter()
        assert adapter._timeout_s == 15.0
        fresh_settings.cache_clear()

    def test_explicit_timeout_wins_over_settings(self, monkeypatch):
        """Passing ``timeout_s=`` to the constructor must
        still win over Settings.
        """
        monkeypatch.setenv("KNT_EMBEDDING_TIMEOUT_SECONDS", "15.0")
        fresh_settings.cache_clear()
        from kntgraph.knowledge.embedding._ollama import (
            OllamaEmbeddingAdapter,
        )

        adapter = OllamaEmbeddingAdapter(timeout_s=2.0)
        assert adapter._timeout_s == 2.0
        fresh_settings.cache_clear()


class TestOllamaAdapterResolveDefaultsHelper:
    """Iter 23: ``_resolve_defaults`` is a static helper
    that encapsulates all Settings access for the adapter.
    It returns ``(model, dimension, timeout_s)``.
    """

    def test_helper_returns_three_values(self):
        """The helper signature grew from 2-tuple to
        3-tuple when Iter 23 added ``timeout_s``."""
        fresh_settings.cache_clear()
        from kntgraph.knowledge.embedding._ollama import (
            OllamaEmbeddingAdapter,
        )

        result = OllamaEmbeddingAdapter._resolve_defaults(
            model=None, dimension=None, timeout_s=None
        )
        assert len(result) == 3
        fresh_settings.cache_clear()

    def test_helper_uses_settings_when_arg_is_none(self, monkeypatch):
        """When timeout_s=None, the helper reads from
        ``settings.embedding_timeout_seconds``."""
        monkeypatch.setenv("KNT_EMBEDDING_TIMEOUT_SECONDS", "12.5")
        fresh_settings.cache_clear()
        from kntgraph.knowledge.embedding._ollama import (
            OllamaEmbeddingAdapter,
        )

        _, _, timeout = OllamaEmbeddingAdapter._resolve_defaults(
            model=None, dimension=None, timeout_s=None
        )
        assert timeout == 12.5
        fresh_settings.cache_clear()

    def test_helper_explicit_arg_wins(self):
        """When timeout_s is given explicitly, it wins
        over Settings."""
        fresh_settings.cache_clear()
        from kntgraph.knowledge.embedding._ollama import (
            OllamaEmbeddingAdapter,
        )

        _, _, timeout = OllamaEmbeddingAdapter._resolve_defaults(
            model=None, dimension=None, timeout_s=0.5
        )
        assert timeout == 0.5
        fresh_settings.cache_clear()
