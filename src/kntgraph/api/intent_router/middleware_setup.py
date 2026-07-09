# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
intent_router.middleware_setup -- Install B5 (rate limit) + B4 (CORS / TrustedHost / HTTPSRedirect).

`configure_middlewares(app)` installs the per-deployment
middleware stack on the FastAPI app. Each factory
returns ``None`` when its setting is empty/false,
signalling "do not install".

The middleware order matters:
  - B5 (rate limit) is installed first, so it
    intercepts every request before B4.
  - B4 (CORS / TrustedHost / HTTPSRedirect) is
    installed in the order it appears in
    ``app.add_middleware`` calls; FastAPI processes
    middleware in reverse-registration order at
    request time.

The module imports `fresh_settings()` lazily so that
tests can monkeypatch settings without a circular
import.
"""

from __future__ import annotations

from ...core._typing import RouterApp
from ...infra.config import fresh_settings
from ...resilience.edge import (
    build_cors_middleware,
    build_https_redirect_middleware,
    build_trusted_host_middleware,
)
from ...resilience.rate_limit import build_rate_limit_middleware


def configure_middlewares(app: RouterApp) -> None:
    """
    Install B5 (rate limit) + B4 (CORS / TrustedHost /
    HTTPSRedirect) on `app`.

    Each factory returns ``None`` when its setting is
    empty/false, signalling "do not install". The
    opt-in policy is per-deployment.
    """
    settings = fresh_settings()
    # B5: rate limiting (in-process, per-IP+route).
    # Healthchecks, docs, and OpenAPI are bypassed by
    # the rate-limit middleware itself.
    rpm = settings.http_rate_limit_rpm
    app.add_middleware(build_rate_limit_middleware(requests_per_minute=rpm))
    # B4: CORS / TrustedHost / HTTPSRedirect.
    # Each is opt-in via Settings (per the user's
    # requirement that operators control this
    # per-deployment rather than a hard-coded
    # default). Each factory returns ``None`` when
    # the corresponding setting is empty/false,
    # signalling "do not install this middleware".
    cors_factory = build_cors_middleware(allow_origins=settings.http_cors_allow_origins)
    if cors_factory is not None:
        app.add_middleware(cors_factory)
    th_factory = build_trusted_host_middleware(allowed_hosts=settings.trusted_hosts)
    if th_factory is not None:
        app.add_middleware(th_factory)
    https_factory = build_https_redirect_middleware(
        enabled=settings.https_redirect_enabled,
        status_code=settings.https_redirect_status,
        hsts_max_age=settings.https_redirect_hsts_max_age,
    )
    if https_factory is not None:
        app.add_middleware(https_factory)


__all__ = ["configure_middlewares"]
