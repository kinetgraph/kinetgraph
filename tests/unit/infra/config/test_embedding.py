# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the Embedding Settings mixin.

Iter 19: the embedding adapter was hard-coding the
model and dimension. This Settings mixin centralises
the knobs.
"""

from __future__ import annotations


from kntgraph.infra.config import Settings


class TestEmbeddingDefaults:
    def test_default_model(self):
        s = Settings()
        assert s.embedding_default_model == "paraphrase-multilingual"

    def test_default_dimension(self):
        s = Settings()
        assert s.embedding_default_dimension == 768

    def test_default_dimension_matches_default_model(self):
        """The two defaults must stay in sync. If you
        change the model, the dimension MUST change to
        match (otherwise the FalkorDB vector index
        rejects mismatched vectors)."""
        s = Settings()
        # Test as a contract: if either changes, both
        # should change. Hard-coded in the test
        # (Iter 19 will not add a runtime check).
        assert s.embedding_default_dimension == 768
        assert s.embedding_default_model == "paraphrase-multilingual"

    def test_timeout_default(self):
        s = Settings()
        assert s.embedding_timeout_seconds == 5.0

    def test_timeout_is_positive(self):
        s = Settings()
        assert s.embedding_timeout_seconds > 0


class TestEmbeddingEnvOverride:
    def test_model_override(self, monkeypatch):
        monkeypatch.setenv("FMH_EMBEDDING_DEFAULT_MODEL", "nomic-embed-text")
        from kntgraph.infra.config import fresh_settings

        fresh_settings.cache_clear()
        s = fresh_settings()
        assert s.embedding_default_model == "nomic-embed-text"
        fresh_settings.cache_clear()

    def test_dimension_override(self, monkeypatch):
        monkeypatch.setenv("FMH_EMBEDDING_DEFAULT_DIMENSION", "1536")
        from kntgraph.infra.config import fresh_settings

        fresh_settings.cache_clear()
        s = fresh_settings()
        assert s.embedding_default_dimension == 1536
        fresh_settings.cache_clear()

    def test_timeout_override(self, monkeypatch):
        monkeypatch.setenv("FMH_EMBEDDING_TIMEOUT_SECONDS", "15.0")
        from kntgraph.infra.config import fresh_settings

        fresh_settings.cache_clear()
        s = fresh_settings()
        assert s.embedding_timeout_seconds == 15.0
        fresh_settings.cache_clear()


class TestEmbeddingValidation:
    def test_dimension_must_be_positive(self):
        s = Settings()
        assert s.embedding_default_dimension > 0
