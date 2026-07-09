# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the embedding provider (no FalkorDB needed).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kntgraph.knowledge.embedding.provider import (
    DEFAULT_PARAPHRASE_MULTILINGUAL_DIM,
    DEFAULT_PARAPHRASE_MULTILINGUAL_MODEL,
    EmbeddingProvider,
    OllamaEmbeddingAdapter,
)
from kntgraph.testing import FakeEmbeddingProvider


class TestFakeEmbeddingProvider:
    """The ``FakeEmbeddingProvider`` (in ``kntgraph.testing``)
    is the canonical test double for ``EmbeddingProvider``,
    mirroring the role of ``HashEmbeddingProvider`` before
    Iter 9. It is intentionally trivial (zero vector by
    default) — tests that need a deterministic vector
    should set ``make_vector`` in the constructor.
    """

    @pytest.mark.asyncio
    async def test_dimension_default(self):
        p = FakeEmbeddingProvider()
        assert p.dimension == 256

    @pytest.mark.asyncio
    async def test_dimension_override(self):
        p = FakeEmbeddingProvider(dimension=768)
        assert p.dimension == 768

    @pytest.mark.asyncio
    async def test_embed_returns_vector_of_correct_dim(self):
        p = FakeEmbeddingProvider()
        v = await p.embed("hello world")
        assert len(v) == 256

    @pytest.mark.asyncio
    async def test_embed_default_is_zero_vector(self):
        p = FakeEmbeddingProvider()
        v = await p.embed("test")
        assert all(x == 0.0 for x in v)

    @pytest.mark.asyncio
    async def test_embed_is_deterministic(self):
        p = FakeEmbeddingProvider()
        v1 = await p.embed("hello")
        v2 = await p.embed("hello")
        assert v1 == v2

    @pytest.mark.asyncio
    async def test_embed_with_make_vector(self):
        # Tests that need deterministic non-zero vectors
        # pass a custom ``make_vector`` callable.
        def echo(text: str) -> list[float]:
            return [float(len(text))] * 4

        p = FakeEmbeddingProvider(dimension=4, make_vector=echo)
        v = await p.embed("hello")
        assert v == [5.0, 5.0, 5.0, 5.0]

    @pytest.mark.asyncio
    async def test_embed_batch(self):
        p = FakeEmbeddingProvider()
        out = await p.embed_batch(["a", "b", "c"])
        assert len(out) == 3
        assert all(len(v) == 256 for v in out)

    @pytest.mark.asyncio
    async def test_protocol_satisfied(self):
        p = FakeEmbeddingProvider()
        assert isinstance(p, EmbeddingProvider)


# ---------------------------------------------------------------------------
# Fake Ollama client — mimics the `ollama.Client.embeddings`
# synchronous call without requiring a running Ollama server.
# ---------------------------------------------------------------------------


class _FakeOllamaClient:
    def __init__(self, dim: int = 768) -> None:
        self.dim = dim
        self.calls: list[dict] = []

    def embeddings(self, model: str, prompt: str) -> dict:
        self.calls.append({"model": model, "prompt": prompt})
        # Deterministic, length-`dim` vector derived from the
        # prompt. The exact values are not meaningful — we
        # only care about dimension and ordering.
        seed = abs(hash((model, prompt))) % (2**32)
        return {
            "embedding": [((seed + i) % 1024) / 1024.0 - 0.5 for i in range(self.dim)]
        }


class TestOllamaEmbeddingAdapterDefaults:
    def test_default_model_and_dimension(self):
        p = OllamaEmbeddingAdapter()
        assert p._model == DEFAULT_PARAPHRASE_MULTILINGUAL_MODEL
        assert p._model == "paraphrase-multilingual"
        assert OllamaEmbeddingAdapter.dimension == DEFAULT_PARAPHRASE_MULTILINGUAL_DIM
        assert OllamaEmbeddingAdapter.dimension == 768

    def test_default_dimension_constant(self):
        assert DEFAULT_PARAPHRASE_MULTILINGUAL_DIM == 768
        assert DEFAULT_PARAPHRASE_MULTILINGUAL_MODEL == ("paraphrase-multilingual")

    def test_custom_model_and_dimension(self):
        p = OllamaEmbeddingAdapter(model="nomic-embed-text", dimension=768)
        assert p._model == "nomic-embed-text"
        assert p.dimension == 768


class TestOllamaEmbeddingAdapterBehavior:
    @pytest.mark.asyncio
    async def test_embed_returns_vector_of_default_dim(self):
        client = _FakeOllamaClient(dim=768)
        p = OllamaEmbeddingAdapter(client=client)
        v = await p.embed("olá mundo")
        assert len(v) == 768
        assert client.calls == [
            {"model": "paraphrase-multilingual", "prompt": "olá mundo"}
        ]

    @pytest.mark.asyncio
    async def test_embed_uses_injected_client(self):
        client = _FakeOllamaClient(dim=768)
        p = OllamaEmbeddingAdapter(model="paraphrase-multilingual", client=client)
        v = await p.embed("hello")
        assert isinstance(v, list)
        assert all(isinstance(x, float) for x in v)

    @pytest.mark.asyncio
    async def test_embed_batch(self):
        client = _FakeOllamaClient(dim=768)
        p = OllamaEmbeddingAdapter(client=client)
        out = await p.embed_batch(["a", "b", "c"])
        assert len(out) == 3
        assert all(len(v) == 768 for v in out)
        assert [c["prompt"] for c in client.calls] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_embed_rejects_non_string(self):
        p = OllamaEmbeddingAdapter(client=_FakeOllamaClient())
        with pytest.raises(TypeError):
            await p.embed(123)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_dimension_mismatch_raises(self):
        # Fake client returns 256-d vectors, but provider expects 768.
        client = _FakeOllamaClient(dim=256)
        p = OllamaEmbeddingAdapter(client=client)
        with pytest.raises(ValueError, match="dimension mismatch"):
            await p.embed("hi")

    @pytest.mark.asyncio
    async def test_missing_embedding_field_raises(self):
        class _BadClient:
            def embeddings(self, model: str, prompt: str) -> dict:
                return {"wrong_field": [1.0, 2.0]}

        p = OllamaEmbeddingAdapter(client=_BadClient())
        with pytest.raises(ValueError, match="embedding"):
            await p.embed("hi")

    @pytest.mark.asyncio
    async def test_response_object_with_attribute(self):
        # Some ollama client forks return objects, not dicts.
        class _ObjClient:
            def embeddings(self, model: str, prompt: str):
                return SimpleNamespace(
                    embedding=[0.1] * DEFAULT_PARAPHRASE_MULTILINGUAL_DIM
                )

        p = OllamaEmbeddingAdapter(client=_ObjClient())
        v = await p.embed("hi")
        assert len(v) == DEFAULT_PARAPHRASE_MULTILINGUAL_DIM

    @pytest.mark.asyncio
    async def test_protocol_satisfied(self):
        p = OllamaEmbeddingAdapter(client=_FakeOllamaClient())
        assert isinstance(p, EmbeddingProvider)

    @pytest.mark.asyncio
    async def test_close_clears_client(self):
        client = _FakeOllamaClient()
        p = OllamaEmbeddingAdapter(client=client)
        await p.close()
        assert p._client is None

    @pytest.mark.asyncio
    async def test_missing_ollama_package_raises(self, monkeypatch):
        """
        When the `ollama` package is not installed, the
        first call to `embed()` raises `RuntimeError`
        with a message mentioning the missing package
        and the install command.

        Implemented as an async test (rather than the
        legacy `asyncio.get_event_loop().run_until_complete`
        pattern) so that pytest-asyncio's loop management
        is consistent with the other tests in this file
        — the legacy pattern raises "There is no current
        event loop in thread 'MainThread'" on Python 3.12+
        when a loop has already been bound to a different
        thread by the test runner.
        """
        import builtins

        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name == "ollama" or name.startswith("ollama."):
                raise ImportError("No module named 'ollama'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        p = OllamaEmbeddingAdapter(host="http://localhost:11434")
        with pytest.raises(RuntimeError, match="ollama"):
            await p.embed("x")

    @pytest.mark.asyncio
    async def test_lazy_client_is_reused_across_calls(self):
        """
        The provider lazily creates the Ollama client on
        first use. After that, the same client must be
        reused — no new Client() on every embed call.

        This is the contract that makes the lock + cache
        meaningful: a fresh client per call would defeat
        connection pooling.
        """
        client = _FakeOllamaClient(dim=768)
        p = OllamaEmbeddingAdapter(client=client)
        # First call: client was injected, no lazy init.
        await p.embed("a")
        assert p._client is client
        await p.embed("b")
        assert p._client is client
        # Concurrent calls must see the same client.
        import asyncio

        await asyncio.gather(*(p.embed(c) for c in ["c", "d", "e"]))
        assert p._client is client

    @pytest.mark.asyncio
    async def test_lazy_client_creation_under_concurrent_load(self, monkeypatch):
        """
        Under concurrent first-use, only one Ollama client
        must be created. The provider's lazy-init path
        currently has no `await` between the ``is None``
        check and the ``Client()`` call, so the asyncio
        scheduler serialises the construction naturally
        (a single Client() is created per concurrent burst).

        This test pins that property: a single
        ``Client()`` construction per concurrent first-use
        burst, no matter how many coroutines race.
        """
        fake_module_calls: list = []

        class _CountingClient:
            def __init__(self, *args, **kwargs):
                fake_module_calls.append((args, kwargs))
                self._inner = _FakeOllamaClient(dim=768)

            def embeddings(self, model, prompt):
                return self._inner.embeddings(model, prompt)

        class _StubOllama:
            Client = _CountingClient

        import sys

        monkeypatch.setitem(sys.modules, "ollama", _StubOllama)
        p = OllamaEmbeddingAdapter(host="http://localhost:11434")
        import asyncio

        await asyncio.gather(*(p.embed(str(i)) for i in range(10)))
        assert len(fake_module_calls) == 1, (
            f"expected exactly one Client() construction, got {len(fake_module_calls)}"
        )

    def test_dimension_mismatch_message_is_english(self):
        """
        The error message raised on a dimension mismatch
        must be readable by an English speaker. Earlier
        versions contained a stray Spanish word
        ("retornou") that was clearly a typo.
        """
        import inspect

        source = inspect.getsource(OllamaEmbeddingAdapter.embed)
        assert "retornou" not in source, (
            "dimension mismatch message still contains the "
            "Spanish word 'retornou' — should be 'returned' "
            "or similar English text."
        )

    def test_host_default_documented_correctly(self):
        """
        The constructor accepts ``host=None`` and the
        underlying Ollama client then reads the
        ``OLLAMA_HOST`` environment variable. The docstring
        used to say the default is
        ``http://localhost:11434``; this was inaccurate.
        """
        import inspect

        source = inspect.getsource(OllamaEmbeddingAdapter)
        # The docstring may NOT promise a specific default
        # URL that the underlying client does not honour.
        # It must mention OLLAMA_HOST so the actual
        # behaviour is documented.
        if "Defaults to" in source:
            assert "OLLAMA_HOST" in source, (
                "host default documented inconsistently: the "
                "docstring should clarify that OLLAMA_HOST is "
                "honoured when host is left as None."
            )


class TestOllamaEmbeddingAdapterTimeout:
    """Iter 23: ``embed()`` wraps the synchronous Ollama
    call with ``asyncio.wait_for(timeout=self._timeout_s)``.
    On timeout, the call raises ``EmbeddingTimeoutError``
    (a typed error from the embedding Protocol module).
    """

    @pytest.mark.asyncio
    async def test_slow_call_raises_embedding_timeout_error(self, monkeypatch):
        """
        A client whose ``embeddings`` call sleeps past
        the adapter's ``_timeout_s`` must surface as
        ``EmbeddingTimeoutError`` — not the bare
        ``asyncio.TimeoutError`` and not the
        ``RuntimeError`` the adapter raises when the
        ``ollama`` package is missing.

        The new error type lives in the embedding
        Protocol module so callers can ``except`` it
        without importing the adapter.
        """
        from kntgraph.knowledge.embedding._protocol import (
            EmbeddingTimeoutError,
        )

        class _SlowClient:
            def embeddings(self, model, prompt):
                import time as _time

                _time.sleep(2.0)
                return {"embedding": [0.0] * 768}

        adapter = OllamaEmbeddingAdapter(client=_SlowClient(), timeout_s=0.05)
        with pytest.raises(EmbeddingTimeoutError):
            await adapter.embed("hi")

    @pytest.mark.asyncio
    async def test_timeout_error_message_mentions_timeout(self, monkeypatch):
        """The error message must include the effective
        timeout so operators can debug ``why`` the call
        was bounded."""
        from kntgraph.knowledge.embedding._protocol import (
            EmbeddingTimeoutError,
        )

        class _SlowClient:
            def embeddings(self, model, prompt):
                import time as _time

                _time.sleep(2.0)
                return {"embedding": [0.0] * 768}

        adapter = OllamaEmbeddingAdapter(client=_SlowClient(), timeout_s=0.05)
        with pytest.raises(EmbeddingTimeoutError) as exc_info:
            await adapter.embed("hi")
        # Message must surface the configured timeout so
        # operators can correlate against Settings.
        assert "0.05" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_timeout_error_includes_text(self, monkeypatch):
        """The error message should mention the input
        text so the operator can identify which call
        timed out (matters for batched callers)."""
        from kntgraph.knowledge.embedding._protocol import (
            EmbeddingTimeoutError,
        )

        class _SlowClient:
            def embeddings(self, model, prompt):
                import time as _time

                _time.sleep(2.0)
                return {"embedding": [0.0] * 768}

        adapter = OllamaEmbeddingAdapter(client=_SlowClient(), timeout_s=0.05)
        with pytest.raises(EmbeddingTimeoutError) as exc_info:
            await adapter.embed("hello world")
        # Text is truncated by the error class; the prefix
        # must be present so it remains identifiable.
        assert "hello" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_fast_call_does_not_raise(self):
        """A call that completes within the timeout
        returns a vector normally — the ``wait_for``
        wrapper is transparent on the happy path."""
        adapter = OllamaEmbeddingAdapter(
            client=_FakeOllamaClient(dim=768),
            timeout_s=5.0,
        )
        v = await adapter.embed("hi")
        assert len(v) == 768

    @pytest.mark.asyncio
    async def test_timeout_preserves_one_client_per_burst(self, monkeypatch):
        """Iter 23 must NOT break the concurrent
        first-use invariant: a burst that times out
        still leaves exactly one Client() constructed.

        This is the test that pins the interaction
        between ``asyncio.wait_for`` and the existing
        ``_client_lock`` + lazy init path.
        """
        from kntgraph.knowledge.embedding._protocol import (
            EmbeddingTimeoutError,
        )

        fake_module_calls: list = []

        class _SlowClient:
            def __init__(self, *args, **kwargs):
                fake_module_calls.append((args, kwargs))
                self._inner = _FakeOllamaClient(dim=768)

            def embeddings(self, model, prompt):
                import time as _time

                _time.sleep(2.0)
                return self._inner.embeddings(model, prompt)

        class _StubOllama:
            Client = _SlowClient

        import sys
        import asyncio

        monkeypatch.setitem(sys.modules, "ollama", _StubOllama)
        adapter = OllamaEmbeddingAdapter(host="http://localhost:11434", timeout_s=0.05)
        # All 5 calls will time out, but only one client
        # should have been constructed.
        results = await asyncio.gather(
            *(adapter.embed(str(i)) for i in range(5)),
            return_exceptions=True,
        )
        # Every call must raise EmbeddingTimeoutError.
        for r in results:
            assert isinstance(r, EmbeddingTimeoutError)
        # And exactly one Client() must have been built.
        assert len(fake_module_calls) == 1, (
            f"expected exactly one Client() construction "
            f"under a timeout-bounded burst, "
            f"got {len(fake_module_calls)}"
        )
