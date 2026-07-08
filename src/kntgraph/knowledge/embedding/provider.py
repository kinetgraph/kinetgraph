# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
EmbeddingProvider — plugable interface for vector embeddings.

The implementation is split across:

  - ``_protocol`` — ``EmbeddingProvider`` (the Protocol),
    ``OllamaClient`` + ``OllamaEmbeddingResponse``
    (the framework boundary for the ``ollama`` lib).
  - ``_ollama`` — ``OllamaEmbeddingAdapter`` (low-level
    adapter, lazy ``import ollama``, requires
    ``kntgraph[ollama]``).
  - ``_client`` — ``EmbeddingClient`` (facade, default impl;
    the canonical entry point for application code).

This module re-exports the public surface so callers can
keep using ``from kntgraph.knowledge.embedding.provider
import EmbeddingClient`` (the canonical entry point
documented in the framework docs).

Iter 15 (ADR-019 epílogo): the previous name
``OllamaEmbeddingProvider`` exposed the backend concrete
in the class name. The low-level adapter was renamed to
``OllamaEmbeddingAdapter`` and a new ``EmbeddingClient``
facade now serves as the default impl.
"""

from __future__ import annotations

from ._client import EmbeddingClient
from ._ollama import (
    DEFAULT_PARAPHRASE_MULTILINGUAL_DIM,
    DEFAULT_PARAPHRASE_MULTILINGUAL_MODEL,
    OllamaEmbeddingAdapter,
)
from ._protocol import (
    EmbeddingProvider,
    EmbeddingTimeoutError,
    OllamaClient,
    OllamaEmbeddingResponse,
)


__all__ = [
    "DEFAULT_PARAPHRASE_MULTILINGUAL_DIM",
    "DEFAULT_PARAPHRASE_MULTILINGUAL_MODEL",
    "EmbeddingClient",
    "EmbeddingProvider",
    "EmbeddingTimeoutError",
    "OllamaClient",
    "OllamaEmbeddingAdapter",
    "OllamaEmbeddingResponse",
]
