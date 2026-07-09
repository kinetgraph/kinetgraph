# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.testing -- shared test fixtures and helpers.

This package is part of the framework's testing surface.
Code here is consumed by both unit and integration tests
to avoid each test re-implementing common fakes.

Public surface:

  - ``embedding.FakeEmbeddingProvider`` — deterministic,
    dependency-free ``EmbeddingProvider`` for tests that
    need a vector-producing object without Ollama.
"""

from .embedding import FakeEmbeddingProvider

__all__ = ["FakeEmbeddingProvider"]
