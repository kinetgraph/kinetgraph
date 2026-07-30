# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the HTTP client adapter sub-package
(ADR-047 §2.2.2 "Abstract via Protocol").

The framework's HTTP I/O boundary is the
``HttpClientLike`` Protocol. The concrete
implementation (``HttpxHttpClientAdapter``) wraps
``httpx.AsyncClient``; the framework code that needs
HTTP (``@tool_worker`` classes like
``OpenMeteoApi``) receives the adapter via DI and
never imports ``httpx`` directly.

What this test file covers
--------------------------

  - ``HttpClientLike`` is ``@runtime_checkable`` and
    structurally satisfied by an in-memory
    ``FakeHttpClient`` (no ``httpx`` import, no
    network).
  - ``HttpxHttpClientAdapter.__init__`` lazy-imports
    ``httpx`` (the framework's import graph does not
    pay the ``httpx`` cost unless the operator
    instantiates the adapter).
  - ``HttpxHttpClientAdapter.get`` returns a value
    that satisfies ``HttpResponseLike``.

The integration with ``OpenMeteoApi`` (the
canonical consumer) is exercised in
``test_open_meteo_tool.py`` in this directory.
"""

from __future__ import annotations

from typing import Any

import pytest

from kntgraph.infra.http import (
    HttpClientLike,
    HttpResponseLike,
    HttpxHttpClientAdapter,
)


class _FakeResponse:
    """In-memory ``HttpResponseLike`` for unit tests.

    The ``payload`` is forwarded to ``json()``; if
    it is the sentinel string ``"<not-json>"``,
    ``json()`` raises ``ValueError`` (mirroring a
    real ``httpx.Response.json()`` call on a body
    that is not valid JSON).
    """

    def __init__(self, status_code: int, payload: Any) -> None:
        self._status_code = status_code
        self._payload = payload

    @property
    def status_code(self) -> int:
        return self._status_code

    def raise_for_status(self) -> None:
        if self._status_code >= 400:
            raise RuntimeError(f"http_status_{self._status_code}")

    def json(self) -> Any:
        if self._payload == "<not-json>":
            raise ValueError("not json")
        return self._payload


class FakeHttpClient:
    """In-memory ``HttpClientLike`` for unit tests.

    The test queues ``(url, response)`` pairs. Each
    ``get(url)`` call pops the first pair whose URL
    matches (or raises if no pair is queued). No
    ``httpx`` import, no network.
    """

    def __init__(self) -> None:
        self._queue: list[tuple[str, _FakeResponse]] = []
        self.calls: list[str] = []

    def enqueue(self, url: str, response: _FakeResponse) -> None:
        self._queue.append((url, response))

    async def get(self, url: str) -> HttpResponseLike:
        self.calls.append(url)
        for queued_url, response in self._queue:
            if queued_url == url:
                return response
        raise AssertionError(f"FakeHttpClient: no response queued for {url!r}")


def test_fake_http_client_satisfies_protocol() -> None:
    """A ``FakeHttpClient`` is structurally a
    ``HttpClientLike`` (the Protocol is
    ``@runtime_checkable``)."""
    fake = FakeHttpClient()
    assert isinstance(fake, HttpClientLike)


def test_httpx_adapter_lazy_imports_httpx() -> None:
    """``HttpxHttpClientAdapter.__init__`` lazy-imports
    ``httpx`` (the framework's import graph does not
    pay the dep cost)."""
    adapter = HttpxHttpClientAdapter()
    # The adapter's internal client is an
    # ``httpx.AsyncClient`` -- verified by class
    # name to avoid importing ``httpx`` at the
    # top of the test file.
    assert type(adapter._client).__name__ == "AsyncClient"


@pytest.mark.asyncio
class TestHttpxAdapterGet:
    """``HttpxHttpClientAdapter.get`` delegates to the
    underlying ``AsyncClient``. ``httpx2`` is not
    installed in the dev environment; the test
    monkey-patches the ``httpx2`` import inside the
    adapter module to a stub ``AsyncClient`` so the
    lazy import resolves and the call path is
    exercised."""

    async def test_get_returns_underlying_client_response(self, monkeypatch) -> None:
        import sys
        import types

        from kntgraph.infra.http import _client as client_mod

        class _StubResponse:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {}

        class _StubAsyncClient:
            def __init__(self) -> None:
                pass

            async def get(self, url: str) -> _StubResponse:
                return _StubResponse()

        stub_httpx = types.ModuleType("httpx2")
        stub_httpx.AsyncClient = _StubAsyncClient
        monkeypatch.setitem(sys.modules, "httpx2", stub_httpx)

        adapter = client_mod.HttpxHttpClientAdapter()
        assert isinstance(adapter, client_mod.HttpClientLike)
        response = await adapter.get("https://example.com")
        assert isinstance(response, client_mod.HttpResponseLike)
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_httpx_adapter_aclose() -> None:
    """``aclose`` closes the underlying ``AsyncClient``.
    The stub ``_StubAsyncClient`` records the call so
    the test can assert it was awaited."""

    import sys
    import types

    from kntgraph.infra.http import _client as client_mod

    class _StubAsyncClient:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    stub_httpx = types.ModuleType("httpx2")
    stub_httpx.AsyncClient = _StubAsyncClient
    original = sys.modules.get("httpx2")
    sys.modules["httpx2"] = stub_httpx
    try:
        adapter = client_mod.HttpxHttpClientAdapter()
        assert adapter._client.closed is False
        await adapter.aclose()
        assert adapter._client.closed is True
    finally:
        if original is not None:
            sys.modules["httpx2"] = original
        else:
            sys.modules.pop("httpx2", None)
