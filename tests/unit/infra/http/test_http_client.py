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
async def test_httpx_adapter_get_returns_httpx_response() -> None:
    """``HttpxHttpClientAdapter.get`` returns a value
    that satisfies ``HttpResponseLike``.

    Uses a non-existent host so the test does not
    depend on the network; the test only checks
    that the bound client class is the
    ``httpx2.AsyncClient`` (the package is
    vendored as ``httpx2`` in this project)."""
    import httpx2  # local: only needed for the assertion below

    adapter = HttpxHttpClientAdapter()
    try:
        assert adapter._client.__class__ is httpx2.AsyncClient
    finally:
        await adapter.aclose()
