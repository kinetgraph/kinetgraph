# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
testing.embedding -- ``FakeEmbeddingProvider`` for unit tests.

A minimal, deterministic ``EmbeddingProvider`` impl that:

  - Returns a fixed-dimension vector of all-zeros
    (default dimension = 256).
  - Is async-safe and stateless.
  - Satisfies the ``EmbeddingProvider`` Protocol at
    runtime (verifiable via ``isinstance``).

The fake is intentionally trivial: tests that need a
specific vector shape can subclass or set
``FakeEmbeddingProvider.dimension``. Tests that need a
deterministic, hash-derived vector (the legacy
``HashEmbeddingProvider`` behaviour) should construct the
provider with a custom ``make_vector`` callable — or use
the framework's :mod:`kntgraph.knowledge.embedding`
tests for the production Ollama path.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Callable


class FakeEmbeddingProvider:
    """
    Minimal ``EmbeddingProvider`` for tests that need a
    vector-producing object without Ollama.

    Default behaviour: returns a list of ``dimension``
    zeros. Override ``make_vector`` to produce deterministic
    non-zero vectors (e.g. seeded by text).
    """

    dimension: int = 256

    def __init__(
        self,
        *,
        dimension: int = 256,
        make_vector: Callable[[str], list[float]] | None = None,
    ) -> None:
        self.dimension = dimension
        self._make_vector = make_vector or self._zeros

    @staticmethod
    def _zeros(_text: str) -> list[float]:
        return [0.0] * FakeEmbeddingProvider.dimension

    async def embed(self, text: str) -> list[float]:
        return [float(x) for x in self._make_vector(text)]

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]

    async def close(self) -> None:
        return None

    def __class_getitem__(cls, dim: int) -> type["FakeEmbeddingProvider"]:
        """
        Allow subscripting ``FakeEmbeddingProvider[768]`` for
        type-level annotation. Returns the class unchanged
        (a typing-style idiom).
        """
        return cls


__all__ = ["FakeEmbeddingProvider"]
