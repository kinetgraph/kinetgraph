# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Knowledge subsystem (F8.3).

Exposes:

  - GraphPool           : multi-tenant connection (Iter 24).
  - FalkorDBProjector     : EventLog → FalkorDB projection.
  - GraphRAGRetriever     : hybrid vector + graph search.
  - EmbeddingProvider     : plugable embedding interface (Protocol).
  - EmbeddingClient       : default facade; lazy ``import ollama``.
  - OllamaEmbeddingAdapter : low-level Ollama adapter (advanced).

FalkorDB is OPTIONAL — it is a derived projection. The
EventLog is the source of truth. Applications that don't
need GraphRAG can ignore this entire package.

Iter 24: ``FalkorDBClient`` was renamed to ``GraphPool``
and moved to the ``knowledge.graph`` package. The
``graph_name_for_tenant`` helper is re-exported from the
same path. The new ``GraphPool.graph(tenant_id)`` returns
a ``GraphAdapter`` (Protocol) instead of a raw native
``Graph | AsyncGraph`` — every existing call site
(projectors, retriever) continues to work without changes.
"""

from .embedding.provider import (
    EmbeddingClient,
    EmbeddingProvider,
    OllamaEmbeddingAdapter,
)
from .falkordb.adapter import FalkorDBProjector
from .graphrag.retriever import GraphRAGRetriever, RetrievalResult

__all__ = [
    # embedding
    "EmbeddingClient",
    "EmbeddingProvider",
    "OllamaEmbeddingAdapter",
    # graph (Iter 24: GraphPool replaces FalkorDBClient)
    "FalkorDBProjector",
    # graphrag
    "GraphRAGRetriever",
    "RetrievalResult",
]
