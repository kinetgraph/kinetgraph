# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.embedding._ollama -- ``EmbeddingProvider`` adapter
backed by a local Ollama server.

This is the **low-level adapter** for Ollama. Most callers
should not construct it directly — use the
``EmbeddingClient`` facade (``knowledge.embedding._client``)
which selects the right adapter based on configuration.

Default model is ``paraphrase-multilingual``
(sentence-transformers paraphrase-multilingual-MiniLM-L12-v2,
768 dimensions, multilingual). Suitable for GraphRAG over
multilingual corpora.

Requires the ``ollama`` extra:

    uv add 'kntgraph[ollama]'
    ollama pull paraphrase-multilingual

The adapter lazily imports the ``ollama`` client so the
rest of the package remains usable without that dependency
installed. The lazy path is guarded by an ``asyncio.Lock``
to keep concurrent first-use bursts from each constructing
a client (forward-compat against future refactors that may
introduce an ``await`` in the construction path).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from ._protocol import (
    DEFAULT_PARAPHRASE_MULTILINGUAL_DIM,
    DEFAULT_PARAPHRASE_MULTILINGUAL_MODEL,
    EmbeddingTimeoutError,
    OllamaClient,
)


if TYPE_CHECKING:
    # ``ollama`` is an optional dep (the ``[ollama]`` extra).
    # The runtime import is lazy in ``_get_client``; the
    # TYPE_CHECKING import lets pyright resolve the type
    # at analysis time without requiring the package to
    # be installed in the dev env.
    import ollama  # noqa: F401  # type-only import


__all__ = [
    "DEFAULT_PARAPHRASE_MULTILINGUAL_DIM",
    "DEFAULT_PARAPHRASE_MULTILINGUAL_MODEL",
    "OllamaEmbeddingAdapter",
]


def _pick[T](override: T | None, default: T) -> T:
    """Return ``override`` if not None, else ``default``.

    Pure helper used by ``_resolve_defaults`` to keep
    the constructor's default-resolution branch-free
    (CC ≤ 1 per call site).
    """
    return default if override is None else override


class _OllamaResponse(Protocol):
    embedding: Sequence[float]


class OllamaEmbeddingAdapter:
    """
    Embedding provider backed by a local Ollama server.

    Default model is ``paraphrase-multilingual``
    (sentence-transformers paraphrase-multilingual-MiniLM-L12-v2,
    768 dimensions, multilingual). Suitable for GraphRAG
    over multilingual corpora.

    Requires the ``ollama`` extra:

        uv add 'kntgraph[ollama]'
        ollama pull paraphrase-multilingual

    The provider lazily imports the ``ollama`` client so the
    rest of the package remains usable without that
    dependency installed.

    Parameters
    ----------
    model:
        Ollama model tag. Defaults to
        ``paraphrase-multilingual``.
    host:
        Ollama server URL. If left as ``None`` (the
        default), the underlying ``ollama.Client`` reads
        the ``OLLAMA_HOST`` environment variable. Setting
        ``host`` to a URL overrides the env var for this
        provider. The default Ollama server URL
        (``http://localhost:11434``) is honoured by the
        Ollama client itself when no env var is set.
    dimension:
        Embedding dimension. Defaults to 768, matching the
        default ``paraphrase-multilingual`` model. Override
        only if you have confirmed your model produces a
        different dimension.
    client:
        Optional pre-configured ``ollama.Client`` instance.
        Primarily for tests. If ``None``, a client is created
        from ``host`` on first use.
    timeout_s:
        Per-call timeout in seconds. The ``embed`` method
        wraps the synchronous Ollama call in
        ``asyncio.wait_for(timeout=timeout_s)``. On
        timeout, ``EmbeddingTimeoutError`` is raised
        (callers catch this concrete type rather than
        the framework-level ``asyncio.TimeoutError``).
        Defaults to ``settings.embedding_timeout_seconds``
        (5.0s by default).
    """

    dimension: int = DEFAULT_PARAPHRASE_MULTILINGUAL_DIM

    def __init__(
        self,
        *,
        model: str | None = None,
        host: str | None = None,
        dimension: int | None = None,
        client: OllamaClient | None = None,
        timeout_s: float | None = None,
    ) -> None:
        # Iter 20 + 23: read defaults from Settings. The
        # caller can still override per-adapter by
        # passing the value explicitly.
        self._model, self._dimension, self._timeout_s = self._resolve_defaults(
            model=model,
            dimension=dimension,
            timeout_s=timeout_s,
        )
        self._host = host
        self._client = client
        # Protects the lazy-init path of ``_client``. The
        # current implementation has no ``await`` between
        # the ``is None`` check and the ``Client()`` call,
        # so the asyncio scheduler already serialises the
        # construction; the lock is a defensive guard for
        # future refactors that may introduce an ``await``
        # in that path.
        self._client_lock = asyncio.Lock()

    @staticmethod
    def _resolve_defaults(
        *,
        model: str | None,
        dimension: int | None,
        timeout_s: float | None,
    ) -> tuple[str, int, float]:
        """
        Resolve the effective ``model``, ``dimension`` and
        ``timeout_s`` from explicit args + Settings.

        The sentinel ``None`` means "no override; use
        Settings". Any explicit value wins. Extracted
        into a helper so the ``__init__`` body stays
        flat (CC ≤ 2) and the defaults are easy to
        test in isolation.
        """
        from kntgraph.infra.config import fresh_settings

        s = fresh_settings()
        return (
            _pick(model, s.embedding_default_model),
            _pick(dimension, s.embedding_default_dimension),
            _pick(timeout_s, s.embedding_timeout_seconds),
        )

    def _get_client(self) -> OllamaClient:
        """Return the Ollama client, constructing it lazily.

        The construction is guarded by ``_client_lock`` so
        that, even if a future change introduces an
        ``await`` between the ``is None`` check and the
        ``Client()`` call, only one client is ever created.
        """
        if self._client is not None:
            return self._client
        try:
            import ollama as _ollama
        except ImportError as e:
            raise RuntimeError(
                "OllamaEmbeddingAdapter requires the 'ollama' "
                "Python package. Install it with `uv add "
                "'kntgraph[ollama]'` or "
                "`pip install 'kntgraph[ollama]'`."
            ) from e
        if self._host is not None:
            self._client = _ollama.Client(host=self._host)
        else:
            self._client = _ollama.Client()
        return self._client  # type: ignore[return-value]

    async def embed(self, text: str) -> list[float]:
        """Embed a single piece of text via Ollama."""
        if not isinstance(text, str):
            raise TypeError(f"text must be str, got {type(text).__name__}")
        client = self._get_client()

        def _call() -> dict:
            return client.embeddings(model=self._model, prompt=text)

        async with self._client_lock:
            # Iter 23: bound the synchronous Ollama call
            # with ``asyncio.wait_for`` so a hung server
            # cannot block the event loop indefinitely.
            # ``wait_for`` cancels the inner future on
            # timeout, which propagates as
            # ``asyncio.TimeoutError``; we translate it
            # into ``EmbeddingTimeoutError`` so callers
            # can ``except`` a concrete type from this
            # module without importing asyncio.
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(_call),
                    timeout=self._timeout_s,
                )
            except asyncio.TimeoutError as e:
                raise EmbeddingTimeoutError(
                    text=text,
                    timeout_s=self._timeout_s,
                ) from e
        vec = self._extract_vector(response)
        self._check_dimension(vec)
        return vec

    def _check_dimension(self, vec: Sequence[float]) -> None:
        """Raise if the returned vector's length does not
        match the configured dimension. Kept as a
        helper to keep ``embed`` branch-free.
        """
        if len(vec) == self._dimension:
            return
        raise ValueError(
            f"embedding dimension mismatch: model returned "
            f"{len(vec)} but provider dimension is "
            f"{self._dimension}. Update `dimension=` to "
            f"match the model in use."
        )

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed multiple texts. Calls Ollama sequentially.

        Sequential (not parallel) by design: a parallel
        burst can overwhelm a local Ollama server. Switch
        to ``asyncio.gather`` if you have a cluster.
        """
        return [await self.embed(t) for t in texts]

    @staticmethod
    def _extract_vector(
        response: "dict[str, object] | _OllamaResponse",
    ) -> list[float]:
        """
        Pull the embedding vector out of the Ollama response.

        The official ``ollama`` Python client returns a dict
        like ``{"embedding": [float, ...]}`` for the
        ``embeddings`` endpoint. Some forks / older versions
        return an object with an ``.embedding`` attribute.
        """
        if response is None:
            raise ValueError("Ollama returned an empty response")
        # The official ``ollama`` Python client returns a dict
        # like ``{"embedding": [float, ...]}``; some forks /
        # older versions return an object with an ``.embedding``
        # attribute. Pull the field via either path; the
        # result is always a list of floats.
        if isinstance(response, dict):
            emb: object = response.get("embedding")
        else:
            emb = getattr(response, "embedding", None)
            if emb is None and hasattr(response, "__dict__"):
                emb = response.__dict__.get("embedding")
        if emb is None:
            raise ValueError(
                f"Could not find 'embedding' field in Ollama response: {response!r}"
            )
        return [float(x) for x in emb]

    async def close(self) -> None:
        """Release the client. The HTTP client is stateless."""
        self._client = None
