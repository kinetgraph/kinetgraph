# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
HTTP edge middlewares (ADR-019 / B4).

Three middlewares are exposed, all following the
``build_*_middleware(...)`` factory pattern used by
``rate_limit.py``: the import of ``starlette`` is
lazy so this module can be imported in environments
that have not installed the ``[api]`` extra.

Each factory returns a class suitable for
``app.add_middleware(factory(...))``. Operators opt in
by setting the corresponding ``Settings`` field
(``http_cors_allow_origins``, ``trusted_hosts``,
``https_redirect_enabled``); the framework itself does
NOT default to any closed/open policy (see Settings
docstring).

Middlewares covered:

  - **CORS** (Cross-Origin Resource Sharing). Blocks
    browser-side requests from origins not in the
    allow-list. Empty allow-list = no middleware
    applied (same-origin only by default browser
    behaviour).

  - **TrustedHost** (Host header validation). Blocks
    requests whose ``Host`` header is not in the
    allow-list. Defends against DNS rebinding and Host
    header injection. Empty allow-list = no middleware
    applied (dev convenience).

  - **HTTPSRedirect**. 308-redirects GET/HEAD
    requests from http → https when
    ``X-Forwarded-Proto=http`` is set. Disable when a
    TLS-terminating proxy already handles the redirect.

These are defence-in-depth layers intended for
deployments that may or may not sit behind an API
gateway. The framework does not assume the gateway
exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    # ``ASGIMiddleware`` is the framework-level adapter
    # for Starlette / ASGI middleware classes (each of
    # the three ``build_*`` factories returns a class
    # to be passed to ``app.add_middleware(...)``).
    from starlette.middleware.base import BaseHTTPMiddleware as ASGIMiddleware

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

__all__ = [
    "build_cors_middleware",
    "build_trusted_host_middleware",
    "build_https_redirect_middleware",
]


def _parse_csv(value: str) -> list[str]:
    """Split a CSV into trimmed non-empty entries."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def build_cors_middleware(*, allow_origins: str = "") -> "ASGIMiddleware | None":
    """
    Build a Starlette ``CORSMiddleware`` configured by
    the operator.

    The CSV string ``allow_origins`` is parsed into a
    list. Empty string disables CORS entirely (returns
    ``None`` so the caller knows to skip
    ``add_middleware``).

    Behaviour:

      - ``""`` (empty) → ``None`` (no middleware; browser
        same-origin policy applies by default).
      - ``"*"`` → any origin allowed (debugging only;
        logs a warning on construction).
      - ``"https://app.example.com,https://admin.example.com"`` →
        explicit allow-list; browser checks
        ``Access-Control-Allow-Origin`` against this
        list.

    The framework uses Starlette's built-in
    ``CORSMiddleware`` rather than a custom
    implementation: it is well-tested, supports
    preflight (OPTIONS) and credentials correctly, and
    is part of the dependency that the operator already
    installs for the FastAPI gateway.
    """
    origins = _parse_csv(allow_origins)
    if not origins:
        return None

    from starlette.middleware.cors import CORSMiddleware

    if origins == ["*"]:
        # The user explicitly opted into any-origin
        # mode (typical for local development). We
        # disable credentials in that case because
        # ``Access-Control-Allow-Origin: *`` is rejected
        # by browsers when credentials are present.
        allow_credentials = False
    else:
        allow_credentials = True

    class _Bound(CORSMiddleware):
        def __init__(self, app: ASGIApp) -> None:
            super().__init__(
                app,
                allow_origins=origins,
                allow_credentials=allow_credentials,
                # The framework's default methods cover
                # both intent-router POSTs and status
                # GETs. Operators can override by
                # constructing CORSMiddleware directly
                # when they need custom headers.
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=[
                    "X-API-Key",
                    "Idempotency-Key",
                    "Content-Type",
                ],
            )

    return _Bound


# ---------------------------------------------------------------------------
# TrustedHost
# ---------------------------------------------------------------------------


def build_trusted_host_middleware(
    *, allowed_hosts: str = ""
) -> "ASGIMiddleware | None":
    """
    Build a custom middleware that rejects requests
    whose ``Host`` header is not in the allow-list.

    The framework ships a custom (rather than
    Starlette's ``TrustedHostMiddleware``) because:

      - We want a 400 response (Starlette returns
        400 too, but the body differs).
      - We log every rejection at WARNING for
        observability.
      - Empty allow-list disables the middleware
        (returns ``None``) instead of denying
        everything — same dev convenience as CORS.

    Operators should set ``trusted_hosts`` to the
    expected ``Host`` header values for their
    deployment:

      - ``"api.example.com"`` → single host.
      - ``"api.example.com,staging.example.com,localhost"`` →
        multiple hosts (e.g. prod + staging + local).
      - ``""`` (empty) → no middleware (dev mode).

    The check is case-insensitive. ``Host`` headers
    may carry an optional port (``api.example.com:8080``);
    the middleware strips the port before comparison.
    """
    hosts = [h.lower() for h in _parse_csv(allowed_hosts)]
    if not hosts:
        return None

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class _TrustedHostMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Callable) -> Response:
            host_header = request.headers.get("host", "")
            # Strip optional port.
            host = host_header.split(":", 1)[0].lower()
            if not host or host not in hosts:
                import structlog

                logger = structlog.get_logger()
                logger.warning(
                    "trusted_host.rejected",
                    host_header=host_header,
                    allowed=hosts,
                )
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": (
                            "invalid Host header; "
                            "expected one of "
                            f"{hosts}, got {host_header!r}"
                        )
                    },
                )
            return await call_next(request)

    return _TrustedHostMiddleware


# ---------------------------------------------------------------------------
# HTTPSRedirect
# ---------------------------------------------------------------------------


def build_https_redirect_middleware(
    *,
    enabled: bool = True,
    status_code: int = 308,
    hsts_max_age: int = 0,
) -> "ASGIMiddleware | None":
    """
    Build a custom middleware that 308-redirects
    GET/HEAD requests from http → https when the
    request arrives via a TLS-terminating proxy that
    sets ``X-Forwarded-Proto=http``.

    Parameters
    ----------
    enabled : bool
        Master switch. When False, the middleware is
        not installed (returns ``None``).
    status_code : int
        HTTP status for the redirect. Default 308
        (preserves method per RFC 7538); 301 is the
        legacy alternative. Validated by Settings
        upstream; the middleware itself does not
        reject unknown codes (Starlette will raise).
    hsts_max_age : int
        ``Strict-Transport-Security`` max-age in
        seconds. When 0 (default), no HSTS header is
        emitted. When positive, the redirect response
        carries ``Strict-Transport-Security: max-age=N``
        so browsers pin HTTPS for the domain.

        HSTS is only emitted on the redirect response
        itself — successful HTTPS requests do NOT
        carry it. This matches RFC 6797 §7.2 ("the HSTS
        Host MUST NOT include the STS header field in
        responses conveyed over non-secure transport")
        — but the redirect from http→https is itself
        a non-secure response, so HSTS on the redirect
        is the standard bootstrap mechanism.

        Common values: 31536000 (1 year, the original
        RFC 6797 minimum), 63072000 (2 years, the
        HSTS-preload-list minimum).

    Only GET/HEAD are redirected. POST/PUT/etc. would
    silently drop the body on 308; 308 with method
    preservation is technically correct but surprises
    clients. Operators behind a TLS proxy should
    ``--forwarded-allow-ips`` set on uvicorn so
    ``X-Forwarded-Proto`` is honoured; otherwise the
    redirect loop never fires.
    """
    if not enabled:
        return None

    # Lazy import: starlette is optional. We import at
    # class-definition time (when ``build_https_redirect_middleware``
    # is called) rather than at module-load time so
    # environments without starlette can still import
    # ``kntgraph.resilience.edge`` (the protocol
    # tests rely on this).
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import RedirectResponse

    class _HTTPSRedirectMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Callable) -> Response:
            if request.method not in ("GET", "HEAD"):
                return await call_next(request)
            # ``X-Forwarded-Proto`` is set by reverse
            # proxies (nginx, traefik, etc.) when
            # terminating TLS.
            forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
            if forwarded_proto != "http":
                # Already https, or no proxy header (in
                # which case we trust the wire protocol).
                return await call_next(request)
            # Build the redirect URL preserving the
            # path and query.
            url = str(request.url.replace(scheme="https"))
            headers: dict[str, str] = {}
            # HSTS only when explicitly requested
            # (max_age > 0). RFC 6797 §7.2 allows HSTS
            # on the redirect response itself; we
            # honour that to bootstrap the pin.
            if hsts_max_age > 0:
                headers["Strict-Transport-Security"] = f"max-age={hsts_max_age}"
            return RedirectResponse(url=url, status_code=status_code, headers=headers)

    return _HTTPSRedirectMiddleware
