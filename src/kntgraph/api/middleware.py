# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
HTTP middleware that authenticates each request and
populates the ``principal_ctx`` ContextVar (ADR-017 §3.3).

The middleware is intentionally light-weight -- it does
NOT raise on auth failure. The route handler decides
how to surface 401/403 (via the
``bind_principal_dependency`` factory in ``api.auth``,
which reads ``X-API-Key``, calls the verifier, and
binds the result to ``principal_ctx``). The
``principal_ctx`` is left at ``None`` on failure, so
``EventLog.append`` and ``ToolInvoker`` will treat the
operation as unauthorised and refuse.

This split (auth-failure-as-ContextVar-empty vs
auth-failure-as-raise) lets read-only public endpoints
(/healthz, /readyz, /docs) skip auth entirely while
authorised endpoints still get the principal bound.
"""

from __future__ import annotations

from typing import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from kntgraph.security import Principal, principal_ctx

logger = structlog.get_logger()


class PrincipalBindingMiddleware(BaseHTTPMiddleware):
    """
    Starlette/FastAPI middleware that:

      1. Reads ``X-API-Key``.
      2. Calls the configured ``APIKeyVerifier``.
      3. On success, sets ``principal_ctx`` for the
         duration of the request.
      4. On failure, leaves ``principal_ctx`` at None.

    The middleware does NOT itself raise 401/403; that
    is the responsibility of the per-route
    ``bind_principal_dependency(verifier)`` closure
    passed to ``Depends(...)``. This split allows
    health checks, docs, and metrics endpoints to skip
    authentication while still flowing through the
    same middleware chain.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        # Lazy verifier factory to keep this module
        # importable even when the verifier backend
        # (e.g. a custom OAuth2 one) isn't installed.
        # ``verifier_for`` is called once per request
        # with the raw ``X-API-Key`` value; it returns
        # an awaitable that resolves to a Principal
        # or raises AuthError.
        verifier_for: Callable[[str], Awaitable[Principal]],
        # Public paths that skip authentication entirely.
        public_paths: tuple[str, ...] = (
            "/healthz",
            "/readyz",
            "/docs",
            "/redoc",
            "/openapi.json",
        ),
    ) -> None:
        super().__init__(app)
        self._verifier_for = verifier_for
        self._public_paths = public_paths

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in self._public_paths):
            return await call_next(request)  # type: ignore[no-any-return]
        api_key = request.headers.get("x-api-key", "")
        if not api_key:
            # No header → principal_ctx stays None.
            # The route's Depends (or absence thereof)
            # decides whether this is a 401.
            return await call_next(request)  # type: ignore[no-any-return]
        try:
            principal = await self._verifier_for(api_key)
        except Exception as e:
            # Verifier failure is logged at WARN with
            # the sha256 prefix only — never the raw
            # key. The route's Depends surfaces the
            # appropriate 4xx.
            logger.warning(
                "auth.verifier_failed",
                error_type=type(e).__name__,
                error=type(e).__name__,
            )
            return await call_next(request)  # type: ignore[no-any-return]
        token = principal_ctx.set(principal)
        try:
            return await call_next(request)  # type: ignore[no-any-return]
        finally:
            principal_ctx.reset(token)
