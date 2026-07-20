# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``HttpClientLike`` -- framework-level Protocol for an
async HTTP client (ADR-047 §2.2.2).

Per AGENTS.md §1, the framework never imports
third-party libraries at the boundary. Every HTTP
call site (the ``@tool_worker`` classes that need
to call an external REST API) accepts a
``HttpClientLike`` Protocol; the concrete
implementation (``HttpxHttpClientAdapter``) is hidden
behind the adapter.

The Protocol is shaped by the **only** call site the
framework has today: ``OpenMeteoApi`` in
``examples/knt-cli/weather_platform/.../tools/open_meteo_api.py``.
That worker needs a single ``get`` returning a
response with ``status_code`` and a ``json()`` method.
The Protocol is intentionally narrow: a Protocol
that returns the third-party's native shape (httpx
``Response``) is not actually abstracting the
third-party.

Why ``@runtime_checkable``
--------------------------

Same rationale as ``RedisLike``: callers can do
``isinstance(client, HttpClientLike)`` defensively
(e.g. to detect a misconfigured mock in tests).
The check is structural (duck typing), not nominal.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class HttpResponseLike(Protocol):
    """Subset of an HTTP response used by the
    framework's HTTP-bound tool workers.

    Mirrors the parts of ``httpx.Response`` the
    framework actually reads: the status code and a
    ``json()`` helper. Adding a new read at a call
    site (e.g. ``text()``) must be followed by
    declaring the method here.
    """

    @property
    def status_code(self) -> int: ...

    def raise_for_status(self) -> "HttpResponseLike | None": ...

    def json(self) -> Any: ...


@runtime_checkable
class HttpClientLike(Protocol):
    """Async HTTP client -- framework-level view.

    A ``@tool_worker`` that needs to call an external
    REST API receives an ``HttpClientLike`` via its
    ``__init__`` (the ADR-047 §2.2.3 constructor
    template). The concrete ``HttpxHttpClientAdapter``
    wraps ``httpx.AsyncClient``; test doubles provide
    an in-memory implementation (no network, no
    ``httpx`` import).
    """

    async def get(self, url: str) -> HttpResponseLike: ...


class HttpxHttpClientAdapter:
    """``httpx.AsyncClient`` adapter.

    The ``httpx`` import is **lazy** (inside
    ``__init__``) so the framework's import graph
    stays clean for operators that do not need HTTP.
    The adapter is cheap to instantiate; the underlying
    ``AsyncClient`` is created on first ``get`` and
    reused for the lifetime of the adapter.

    The adapter satisfies ``HttpClientLike`` by
    structural typing (the methods are the same
    shape). ``isinstance(client, HttpClientLike)``
    works via the ``@runtime_checkable`` Protocol.
    """

    def __init__(self) -> None:
        # Lazy import: ``httpx2`` is an optional
        # dependency for the framework's I/O path.
        # The vertical that uses this adapter
        # (``weather_platform``) declares ``httpx``
        # as a runtime dep; the framework itself
        # does not.
        import httpx2 as httpx

        self._client = httpx.AsyncClient()

    async def get(self, url: str) -> HttpResponseLike:
        response = await self._client.get(url)
        return response

    async def aclose(self) -> None:
        await self._client.aclose()
