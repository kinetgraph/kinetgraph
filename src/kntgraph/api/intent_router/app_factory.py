# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
intent_router.app_factory -- The FastAPI app builder.

Three layers:

  - `create_app` (public): wraps the internal
    factory with a stable signature.
  - `_create_app` (internal): the FastAPI import
    boundary (the `[api]` extra is required). Builds
    the `bind_principal_dependency` closure once at
    app construction.
  - `_build_app` (private closure): instantiates the
    FastAPI app, calls the middleware + route
    installers, and returns it.

The four `register_*` installers live in
`routes.py`; the middleware installer lives in
`middleware_setup.py`. The helpers
(`_sanitize_idempotency_key`, `_deterministic_event_id`)
live in `helpers.py`.

`fastapi` is an optional dependency (the ``[api]``
extra). The import is performed in `_create_app`,
lazily, so `import kntgraph.api` itself does not
require FastAPI to be installed — only the call to
``create_app`` / ``_create_app`` does. The
:func:`kntgraph._optional.require_optional` helper
raises a clear ``ImportError`` pointing at
``kntgraph[api]`` if it is missing.
"""

from __future__ import annotations

from kntgraph.tools.registry import ToolRegistry

from ..._optional import require_optional
from ...infra.config import fresh_settings
from ...security import Principal
from ...stream.event_log import EventLog
from ..auth import APIKeyVerifier
from .middleware_setup import configure_middlewares
from .routes import (
    register_get_status,
    register_healthz,
    register_list_tools,
    register_post_intent,
)


def _create_app(
    *,
    log: EventLog,
    registry: ToolRegistry,
    verifier: APIKeyVerifier,
):
    """
    Build the FastAPI app with explicit dependencies.
    Exposed as `create_app` so tests can inject mocks.

    `fastapi` is an optional dependency (the ``[api]``
    extra). The import is performed here, lazily, so
    `import kntgraph.api` itself does not require
    FastAPI to be installed — only the call to
    ``create_app`` / ``_create_app`` does. The
    :func:`kntgraph._optional.require_optional`
    helper raises a clear ``ImportError`` pointing at
    ``kntgraph[api]`` if it is missing.
    """
    fastapi = require_optional(
        "fastapi",
        "kntgraph[api]",
        purpose="The HTTP gateway (kntgraph.api.create_app)",
    )
    FastAPI = fastapi.FastAPI
    Header = fastapi.Header
    Depends = fastapi.Depends
    HTTPException = fastapi.HTTPException

    # Build the FastAPI dependency for ``X-API-Key`` →
    # ``Principal`` once at app construction. The factory
    # is defined in ``kntgraph.api.auth`` so the same
    # closure is available to any HTTP gateway (the
    # ``fmh_office`` dedicated gateway uses an analogous
    # pattern but bound to its own verifier, not the
    # framework's).
    from ..auth import bind_principal_dependency

    auth = bind_principal_dependency(verifier)

    def _build_app():
        # The return type is intentionally left untyped:
        # ``FastAPI`` here is a module-level binding
        # captured lazily; static typing the return
        # would force a top-level ``from fastapi``
        # which the framework explicitly avoids.
        expose = fresh_settings().expose_docs
        app = FastAPI(
            title="FMH Intent Gateway",
            version="0.6.0",
            description=(
                "HTTP gateway for the FMH framework. "
                "Produces `tool.{name}.requested` events "
                "into the EventLog; consults the "
                "ToolRegistry to reject unknown tools "
                "before they enter the log. See "
                "ADR-012 for the contract."
            ),
            docs_url="/docs" if expose else None,
            redoc_url="/redoc" if expose else None,
            openapi_url="/openapi.json" if expose else None,
        )

        configure_middlewares(app)
        register_healthz(app, FastAPI)
        register_list_tools(
            app,
            FastAPI,
            Depends=Depends,
            Principal=Principal,
            registry=registry,
            auth=auth,
        )
        register_post_intent(
            app,
            FastAPI,
            Depends=Depends,
            Header=Header,
            HTTPException=HTTPException,
            Principal=Principal,
            log=log,
            registry=registry,
            auth=auth,
        )
        register_get_status(
            app,
            FastAPI,
            Depends=Depends,
            Principal=Principal,
            log=log,
            auth=auth,
        )

        return app

    return _build_app()


def create_app(
    *,
    log: EventLog,
    registry: ToolRegistry,
    verifier: APIKeyVerifier,
):
    """
        Public factory. Wraps the internal `_create_app`
        so the call site reads naturally.

        `fastapi` is an optional dependency — see
        :func:`_create_app` for the import strategy. The
        return type is left unannotated so the module is
        importable without FastAPI; production callers see
        a ``fastapi.FastAPI`` instance at runtime.

        Usage
        -----

        ```python
        from kntgraph.api import create_app
        from kntgraph.api.auth import RedisAPIKeyVerifier
    from kntgraph.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(my_tool)
        verifier = RedisAPIKeyVerifier(redis_client)
        app = create_app(
            log=event_log,
            registry=registry,
            verifier=verifier,
        )
        # uvicorn.run(app, host="0.0.0.0", port=8000)
        ```
    """
    return _create_app(log=log, registry=registry, verifier=verifier)


__all__ = [
    "_create_app",
    "create_app",
]
