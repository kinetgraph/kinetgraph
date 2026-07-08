# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.embedding._protocol -- typed Protocol for embedding providers.

Defines the framework-level boundary for any embedding backend.
All concrete implementations (currently only
``OllamaEmbeddingAdapter``) must satisfy this Protocol.

The Protocol is ``@runtime_checkable`` so ``isinstance`` works
in defensive type checks (e.g. tests confirming the public
API surface, factories building adapters).

The ``OllamaClient`` and ``OllamaEmbeddingResponse`` adapter
types are part of the framework boundary for the
``ollama`` Python client. They translate the library's
loose response shapes (dict vs object, ``.embeddings()`` vs
``__call__()``) into a single concrete contract that the
rest of the framework consumes.

Iter 23 adds ``EmbeddingTimeoutError`` — a typed error raised
by adapters whose call sites are bounded by
``settings.embedding_timeout_seconds``. Callers that need
to distinguish "transport hung" from "bad input" or
"missing dependency" catch this concrete type at the
adapter boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Typed errors raised at the adapter boundary.
# ---------------------------------------------------------------------------


class EmbeddingTimeoutError(Exception):
    """
    Raised by an ``EmbeddingProvider`` adapter when a call
    exceeds the configured per-call timeout
    (``settings.embedding_timeout_seconds``).

    Concrete adapters translate the framework-level
    ``asyncio.TimeoutError`` (raised by ``asyncio.wait_for``)
    into this typed error so callers can ``except`` it
    without importing the adapter module.

    Parameters
    ----------
    text:
        The first 80 characters of the input that timed
        out. Truncated so error messages stay readable when
        the input is large; full text is not needed for
        operator debugging.
    timeout_s:
        The effective timeout (seconds) that was applied.
        Surfaced in the message so operators can correlate
        against Settings.
    """

    def __init__(self, *, text: str, timeout_s: float) -> None:
        prefix = text if len(text) <= 80 else text[:77] + "..."
        super().__init__(
            f"embedding call timed out after {timeout_s}s "
            f"for input starting with {prefix!r}"
        )
        self.text = prefix
        self.timeout_s = timeout_s


# ---------------------------------------------------------------------------
# Ollama adapter types (the framework-level boundary for
# the third-party ``ollama`` Python client).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OllamaEmbeddingResponse:
    """
    Framework-level adapter for the ``ollama`` Python client
    response.

    The library returns either a dict
    ``{"embedding": [float, ...], ...}`` or an object with
    an ``.embedding`` attribute. Both shapes are converted
    to this concrete type at the boundary so the rest of
    the pipeline sees a single, well-defined shape.
    """

    embedding: list[float]
    model: str = ""
    total_duration_ns: int = 0


@runtime_checkable
class OllamaClient(Protocol):
    """
    Framework-level adapter for the ``ollama`` Python client.

    The library is duck-typed at runtime via ``isinstance``
    checks on this Protocol — no static import of ``ollama``
    keeps the framework importable without the optional
    extra.
    """

    def embeddings(  # noqa: D401 - protocol method
        self,
        *,
        model: str,
        prompt: str,
    ) -> OllamaEmbeddingResponse: ...


# ---------------------------------------------------------------------------
# EmbeddingProvider — the public, plugable boundary.
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingProvider(Protocol):
    """
    A plugable embedding provider. Implementations turn text
    into a fixed-dimension vector. The dimension is a class
    attribute.

    The framework uses ``isinstance(obj, EmbeddingProvider)``
    defensively in factories and tests, so the Protocol must
    be ``@runtime_checkable``.
    """

    dimension: int

    async def embed(self, text: str) -> list[float]:
        """Embed a single piece of text."""
        ...

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed multiple texts. Default impl calls embed() in a loop."""
        ...

    async def close(self) -> None:
        """Release resources (HTTP sessions, model handles). Optional."""
        ...


__all__ = [
    "DEFAULT_PARAPHRASE_MULTILINGUAL_DIM",
    "DEFAULT_PARAPHRASE_MULTILINGUAL_MODEL",
    "EmbeddingProvider",
    "EmbeddingTimeoutError",
    "OllamaClient",
    "OllamaEmbeddingResponse",
]


# Default model and dimension. The
# ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2``
# model exposes 768-dimensional embeddings, supports 50+
# languages, and is the default for multilingual GraphRAG.
# These constants live in ``_protocol`` because they describe
# the protocol's canonical dimension; concrete impls import
# them from here.
DEFAULT_PARAPHRASE_MULTILINGUAL_MODEL = "paraphrase-multilingual"
DEFAULT_PARAPHRASE_MULTILINGUAL_DIM = 768
