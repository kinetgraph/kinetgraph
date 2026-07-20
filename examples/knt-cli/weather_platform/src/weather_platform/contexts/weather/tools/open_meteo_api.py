# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``OpenMeteoApi`` -- ``@tool_worker`` for the Open-Meteo
public weather API (ADR-047).

The worker follows the Tool-Adapter pattern (ADR-047
§2.2): the ``httpx`` import is hidden behind the
``HttpClientLike`` Protocol; the worker receives the
adapter via ``__init__``. Tests inject a fake
``HttpClientLike`` (no network, no ``httpx``).

Why DI matters here
-------------------

The previous version imported ``httpx.AsyncClient``
directly inside ``invoke``. That coupled the worker
to a concrete I/O library, made unit tests require
``unittest.mock.patch`` on the import, and added the
``httpx`` startup cost to every process that loaded
the weather context (the vertical's ``pyproject.toml``
declared ``httpx`` as a runtime dep; the framework's
own import graph did not pay it). The refactor moves
the ``httpx`` import to ``HttpxHttpClientAdapter``
(lazy, behind the Protocol) and makes the worker
testable with an in-memory ``HttpClientLike``.

Constructor template (ADR-047 §2.3):

  - The ``WorkerManager`` instantiates the worker
    with ``tool_cls()`` (zero-arg). The default
    constructor must produce a fully functional
    instance, so the lazy default adapter is built
    here.
  - Tests instantiate the worker with
    ``OpenMeteoApi(http=fake_client)`` to inject a
    stub.
"""

from __future__ import annotations

from typing import Any

from kntgraph.core.result import Err, Ok, Result, ToolError
from kntgraph.infra.http import HttpClientLike, HttpxHttpClientAdapter
from kntgraph.tools.worker import tool_worker


@tool_worker(name="open_meteo_api", description="Fetches weather for coordinates.")
class OpenMeteoApi:
    """Fetches the current weather for a (lat, lon) pair."""

    def __init__(self, http: HttpClientLike | None = None) -> None:
        self._http = http or HttpxHttpClientAdapter()

    async def invoke(
        self,
        latitude: float,
        longitude: float,
        *,
        idempotency_key: str,
    ) -> Result[dict[str, Any], ToolError]:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={latitude}&longitude={longitude}&current_weather=true"
        )
        try:
            response = await self._http.get(url)
            response.raise_for_status()
        except Exception as e:
            return Err(ToolError(f"open_meteo_http_error: {e!r}"))
        try:
            data = response.json()
        except Exception as e:
            return Err(ToolError(f"open_meteo_decode_error: {e!r}"))
        try:
            return Ok(data["current_weather"])
        except Exception as e:
            return Err(ToolError(f"open_meteo_missing_key: {e!r}"))
