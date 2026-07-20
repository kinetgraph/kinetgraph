# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the ``OpenMeteoApi`` ``@tool_worker``
(ADR-047 §2.2.1 "No Direct External Imports").

The worker follows the Tool-Adapter pattern: the
``httpx`` import is hidden behind the
``HttpClientLike`` Protocol. The tests inject an
in-memory ``FakeHttpClient`` (defined in
``test_http_client.py``) so the worker is exercised
end-to-end with no network and no ``httpx``
dependency on the test path.

Test matrix:

  - ``invoke`` returns ``Ok(...)`` with the
    ``current_weather`` payload when the response
    is 2xx and decodes as JSON.
  - ``invoke`` returns ``Err(ToolError)`` on a
    non-2xx response (the adapter raises via
    ``raise_for_status``).
  - ``invoke`` returns ``Err(ToolError)`` when the
    response body is not valid JSON.
  - ``invoke`` returns ``Err(ToolError)`` when the
    response body decodes but lacks the
    ``current_weather`` key.
  - The worker registers with ``@tool_worker``
    (carries ``name`` / ``description`` /
    ``input_schema``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from kntgraph.core.result import ToolError
from tests.unit.infra.http.test_http_client import (
    FakeHttpClient,
    _FakeResponse,
)


def _load_open_meteo_module() -> Any:
    """Load the worker's module from the vertical
    (the ``examples/`` tree is not on ``sys.path``).

    The worker lives at::

      examples/knt-cli/weather_platform/src/weather_platform/
        contexts/weather/tools/open_meteo_api.py

    The module path is stable; the loader injects
    the ``src`` directory onto ``sys.path`` so the
    relative import of ``kntgraph.*`` resolves.
    """
    import sys

    # Walk up until we find a directory whose
    # ``examples/`` subtree contains the vertical.
    # The repo layout is ``<root>/kinetgraph/{src,tests,examples,...}``;
    # the test lives at
    # ``<root>/kinetgraph/tests/unit/infra/http/``,
    # so ``<root>`` is ``parents[4]``.
    repo_root = Path(__file__).resolve().parents[4]
    src = repo_root / "examples" / "knt-cli" / "weather_platform" / "src"
    if not src.is_dir():
        raise FileNotFoundError(
            f"weather_platform src not found at {src!r}; repo_root={repo_root!r}"
        )
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    spec = importlib.util.spec_from_file_location(
        "open_meteo_api",
        src
        / "weather_platform"
        / "contexts"
        / "weather"
        / "tools"
        / "open_meteo_api.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def open_meteo_module() -> Any:
    return _load_open_meteo_module()


def test_worker_metadata(open_meteo_module: Any) -> None:
    """The worker carries the
    ``@tool_worker``-injected metadata (name /
    description / input_schema)."""
    cls = open_meteo_module.OpenMeteoApi
    assert cls.name == "open_meteo_api"
    assert cls.description.startswith("Fetches weather")
    schema = cls.input_schema
    assert "latitude" in schema["properties"]
    assert "longitude" in schema["properties"]


def test_default_constructor_does_not_eagerly_import_httpx(
    open_meteo_module: Any,
) -> None:
    """Importing the worker's module does NOT bring
    ``httpx2`` into ``sys.modules`` (the import is
    lazy, on the first ``HttpxHttpClientAdapter``
    instantiation)."""
    import sys

    # We test the worker module's import path:
    # the module is already loaded by the fixture;
    # we assert ``httpx2`` is not present unless
    # the operator already pulled it in.
    # (The fixture loads the module via
    # ``spec_from_file_location``, so the test
    # itself does not pull the package.)
    if "httpx2" in sys.modules:
        # If a prior test loaded it, that is fine;
        # the worker's module itself is what we
        # are asserting. Drop the package from
        # ``sys.modules`` temporarily and re-import
        # the worker to assert the lazy import.
        sys.modules.pop("httpx2", None)
        # Clear any sub-modules too.
        for k in list(sys.modules):
            if k.startswith("httpx2"):
                sys.modules.pop(k, None)
        # Re-import the worker module fresh.
        sys.modules.pop("open_meteo_api", None)
        fresh = _load_open_meteo_module()
        # The fresh module is loaded; ``httpx2``
        # should NOT be in ``sys.modules`` because
        # the worker's ``__init__`` was not
        # executed (only the module body).
        assert "httpx2" not in sys.modules, (
            "worker's module-level import pulled httpx2 "
            "(the lazy-import contract was violated)"
        )
        # Sanity: the class is still on the fresh
        # module.
        assert fresh.OpenMeteoApi is open_meteo_module.OpenMeteoApi or hasattr(
            fresh, "OpenMeteoApi"
        )


@pytest.mark.asyncio
async def test_invoke_returns_ok_with_current_weather(
    open_meteo_module: Any,
) -> None:
    """Happy path: a 2xx JSON response with the
    ``current_weather`` key is returned as ``Ok``
    with the payload dict."""
    fake_http = FakeHttpClient()
    fake_http.enqueue(
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=-22.9&longitude=-43.2&current_weather=true",
        _FakeResponse(
            status_code=200,
            payload={
                "current_weather": {
                    "temperature": 32.1,
                    "windspeed": 5.4,
                    "winddirection": 90,
                }
            },
        ),
    )
    worker = open_meteo_module.OpenMeteoApi(http=fake_http)
    r = await worker.invoke(
        latitude=-22.9,
        longitude=-43.2,
        idempotency_key="k1",
    )
    assert r.is_ok()
    assert r.unwrap() == {
        "temperature": 32.1,
        "windspeed": 5.4,
        "winddirection": 90,
    }
    assert len(fake_http.calls) == 1


@pytest.mark.asyncio
async def test_invoke_returns_err_on_http_error(
    open_meteo_module: Any,
) -> None:
    """A non-2xx response is mapped to
    ``Err(ToolError)`` (the ``raise_for_status``
    exception is caught)."""
    fake_http = FakeHttpClient()
    fake_http.enqueue(
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=0.0&longitude=0.0&current_weather=true",
        _FakeResponse(status_code=500, payload={"error": "boom"}),
    )
    worker = open_meteo_module.OpenMeteoApi(http=fake_http)
    r = await worker.invoke(
        latitude=0.0,
        longitude=0.0,
        idempotency_key="k2",
    )
    assert r.is_err()
    err = r.err_value()
    assert isinstance(err, ToolError)
    assert "open_meteo_http_error" in str(err)


@pytest.mark.asyncio
async def test_invoke_returns_err_on_invalid_json(
    open_meteo_module: Any,
) -> None:
    """A response body that does not decode as JSON
    is mapped to ``Err(ToolError)``."""
    fake_http = FakeHttpClient()
    fake_http.enqueue(
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=1.0&longitude=1.0&current_weather=true",
        _FakeResponse(status_code=200, payload="<not-json>"),
    )
    worker = open_meteo_module.OpenMeteoApi(http=fake_http)
    r = await worker.invoke(
        latitude=1.0,
        longitude=1.0,
        idempotency_key="k3",
    )
    assert r.is_err()
    err = r.err_value()
    assert isinstance(err, ToolError)
    assert "open_meteo_decode_error" in str(err)


@pytest.mark.asyncio
async def test_invoke_returns_err_on_missing_key(
    open_meteo_module: Any,
) -> None:
    """A response body that decodes as JSON but
    lacks the ``current_weather`` key is mapped
    to ``Err(ToolError)`` (the ``KeyError`` is
    caught by the third ``except Exception``)."""
    fake_http = FakeHttpClient()
    fake_http.enqueue(
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=2.0&longitude=2.0&current_weather=true",
        _FakeResponse(status_code=200, payload={"hourly": {}}),
    )
    worker = open_meteo_module.OpenMeteoApi(http=fake_http)
    r = await worker.invoke(
        latitude=2.0,
        longitude=2.0,
        idempotency_key="k4",
    )
    assert r.is_err()
    err = r.err_value()
    assert isinstance(err, ToolError)
    assert "open_meteo_missing_key" in str(err)


def test_default_constructor_uses_httpx_adapter(
    open_meteo_module: Any,
) -> None:
    """The zero-arg constructor (the
    ``WorkerManager``'s ``tool_cls()`` path) wires
    the canonical ``HttpxHttpClientAdapter``."""
    worker = open_meteo_module.OpenMeteoApi()
    # The adapter type is the framework-level
    # ``HttpxHttpClientAdapter`` (structural check
    # via class name to keep the test free of
    # ``httpx`` imports at module level).
    assert type(worker._http).__name__ == "HttpxHttpClientAdapter"
