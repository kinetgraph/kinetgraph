# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
HTTP gateway sub-config (mixin).

Holds the knobs the FastAPI/Starlette middleware uses
for CORS, trusted-hosts, HTTPS redirect, HSTS, and rate
limiting.

Each knob is feature-flagged so operators can tune
per-deployment. The framework does NOT default to any
closed or open policy — operators decide based on
whether an API gateway sits in front of the framework.
"""

from __future__ import annotations

from pydantic import Field

from kntgraph.infra.config._base import BaseSettings


class HttpSettingsMixin(BaseSettings):
    """CORS, trusted hosts, HTTPS redirect, HSTS, rate limit."""

    # Whether the framework exposes the OpenAPI surface
    # at ``/docs`` and ``/openapi.json``. Production
    # deployments should set ``KNT_EXPOSE_DOCS=0`` so
    # the tool registry cannot be enumerated by
    # anyone who reaches the host.
    expose_docs: bool = Field(default=True)
    http_rate_limit_rpm: int = Field(default=60, ge=1)
    # ``cors_allow_origins`` : CSV. Empty string disables
    #   CORS entirely (same-origin only). Use a single
    #   ``*`` for any origin (debugging only); use a
    #   comma-separated list of origins for production.
    # ``trusted_hosts`` : CSV. Empty string allows ANY Host
    #   header (dev convenience). Set to a
    #   comma-separated list of expected Host values
    #   to enable the guard.
    # ``https_redirect_enabled`` : when True, GET/HEAD
    #   requests with X-Forwarded-Proto=http are
    #   redirected to https.
    # ``https_redirect_status`` : HTTP status code for
    #   the redirect (301, 302, 307, or 308).
    # ``https_redirect_hsts_max_age`` : ``Strict-Transport-Security``
    #   max-age (seconds). 0 means no HSTS header.
    http_cors_allow_origins: str = Field(default="")
    trusted_hosts: str = Field(default="")
    https_redirect_enabled: bool = Field(default=False)
    https_redirect_status: int = Field(default=308)
    https_redirect_hsts_max_age: int = Field(default=0, ge=0)
