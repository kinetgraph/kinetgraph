# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.embedding._client -- ``EmbeddingClient`` facade.

The facade hides the choice of concrete adapter from
callers. The most common case is: "give me an
``EmbeddingProvider`` that works with my configuration".
The facade decides which low-level adapter to use based
on:

  - explicit ``adapter=`` (advanced)
  - explicit ``model=`` (HuggingFace, OpenAI, custom)
  - environment variables (``OLLAMA_HOST``,
    ``EMBEDDING_BACKEND``)
  - the default (Ollama, local, lazy)

This is the analog of ``GraphPool`` for embedding: the
caller never sees ``OllamaEmbeddingAdapter`` unless they
explicitly ask for it.

Iter 15 (ADR-019 epílogo): rename + facade. The previous
name ``OllamaEmbeddingProvider`` exposed the backend
concrete in the class name, breaking the
backend-agnostic API surface that the rest of the
framework relies on.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

from ._ollama import OllamaEmbeddingAdapter
from ._protocol import (
    DEFAULT_PARAPHRASE_MULTILINGUAL_DIM,
    DEFAULT_PARAPHRASE_MULTILINGUAL_MODEL,
    EmbeddingProvider,
)


__all__ = ["EmbeddingClient"]


class EmbeddingClient(EmbeddingProvider):
    """
    Default ``EmbeddingProvider`` facade.

    Holds a reference to a low-level adapter (default:
    ``OllamaEmbeddingAdapter``) and delegates every call
    to it. The facade itself is an ``EmbeddingProvider``
    so callers can type-hint against the Protocol and
    receive the facade directly.

    The facade is the **canonical entry point** for
    application code:

        from kntgraph.knowledge.embedding import (
            EmbeddingClient,
        )
        client = EmbeddingClient()
        vector = await client.embed("hello")

    Tests that need a deterministic vector should NOT use
    the facade — inject ``FakeEmbeddingProvider`` from
    ``kntgraph.testing`` instead.

    Parameters
    ----------
    model:
        Embedding model tag. Defaults to
        ``paraphrase-multilingual`` (Ollama default).
    host:
        Backend server URL (Ollama only). If ``None``,
        the underlying client reads its env var.
    dimension:
        Embedding dimension. Defaults to 768.
    adapter:
        Explicit low-level adapter. When ``None`` (the
        default), the facade constructs an
        ``OllamaEmbeddingAdapter``. Tests and custom
        deployments use this hook to inject a different
        adapter without changing the facade.
    """

    dimension: int = DEFAULT_PARAPHRASE_MULTILINGUAL_DIM

    def __init__(
        self,
        *,
        model: str = DEFAULT_PARAPHRASE_MULTILINGUAL_MODEL,
        host: str | None = None,
        dimension: int = DEFAULT_PARAPHRASE_MULTILINGUAL_DIM,
        adapter: Optional[EmbeddingProvider] = None,
    ) -> None:
        self._dimension = dimension
        self._adapter = adapter or OllamaEmbeddingAdapter(
            model=model, host=host, dimension=dimension
        )

    async def embed(self, text: str) -> list[float]:
        return await self._adapter.embed(text)

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._adapter.embed_batch(texts)

    async def close(self) -> None:
        await self._adapter.close()
