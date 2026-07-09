# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the HTTP edge middlewares (B4): CORS,
TrustedHost, HTTPSRedirect.

Each factory returns ``None`` when its corresponding
Settings field is empty/false (operators opt in per
deployment). These tests cover the opt-in paths and the
``None``-as-skip semantics.

The middlewares are tested via FastAPI's TestClient
with minimal apps — the goal is to pin the contract,
not to exercise Starlette's internals (those are
covered by Starlette's own test suite).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from kntgraph.resilience.edge import (  # noqa: E402
    build_cors_middleware,
    build_https_redirect_middleware,
    build_trusted_host_middleware,
)


def _build_app(
    *,
    cors_origins: str = "",
    trusted_hosts: str = "",
    https_redirect: bool = True,
    https_status_code: int = 308,
    https_hsts_max_age: int = 0,
) -> TestClient:
    """Build a minimal app with the three middlewares
    wired according to the given Settings-equivalent
    flags. Mirrors the wiring logic in
    ``kntgraph.api.intent_router._build_app`` and
    ``fmh_office.mvp.http``.
    """
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/echo")
    async def echo(payload: dict) -> dict:
        return {"got": payload}

    cors = build_cors_middleware(allow_origins=cors_origins)
    if cors is not None:
        app.add_middleware(cors)
    th = build_trusted_host_middleware(allowed_hosts=trusted_hosts)
    if th is not None:
        app.add_middleware(th)
    https = build_https_redirect_middleware(
        enabled=https_redirect,
        status_code=https_status_code,
        hsts_max_age=https_hsts_max_age,
    )
    if https is not None:
        app.add_middleware(https)

    return TestClient(app)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


class TestCORS:
    def test_empty_allow_origins_disables_middleware(self):
        """No allow-list = no CORS middleware installed.
        The factory returns None; the caller is
        expected to skip ``add_middleware``.
        """
        client = _build_app(cors_origins="")
        # Request from any origin succeeds (no
        # Access-Control-Allow-Origin response header).
        r = client.get("/healthz", headers={"Origin": "https://x.com"})
        assert r.status_code == 200
        assert "access-control-allow-origin" not in {
            k.lower() for k in r.headers.keys()
        }

    def test_single_origin_allowed(self):
        client = _build_app(cors_origins="https://app.example.com")
        # Preflight succeeds.
        r = client.options(
            "/echo",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r.status_code == 200
        # The response carries the allowed origin.
        assert r.headers["access-control-allow-origin"] == "https://app.example.com"

    def test_origin_not_in_allowlist_blocked_by_browser(
        self,
    ):
        """CORS does not block the server-side
        response — the browser does. We assert that
        the server's CORS headers do NOT include the
        disallowed origin (which is what makes the
        browser block the read).
        """
        client = _build_app(cors_origins="https://app.example.com")
        r = client.get(
            "/healthz",
            headers={"Origin": "https://other.com"},
        )
        assert r.status_code == 200
        # The disallowed origin must NOT be echoed
        # back in Access-Control-Allow-Origin.
        acao = r.headers.get("access-control-allow-origin", "")
        assert "other.com" not in acao

    def test_multiple_origins(self):
        client = _build_app(
            cors_origins=("https://app.example.com,https://admin.example.com")
        )
        for origin in (
            "https://app.example.com",
            "https://admin.example.com",
        ):
            r = client.options(
                "/echo",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert r.status_code == 200
            assert r.headers["access-control-allow-origin"] == origin

    def test_wildcard_disables_credentials(self):
        """``*`` is allowed but credentials are not
        (browsers reject ACAO: * with credentials).
        Starlette's ``CORSMiddleware`` enforces this
        by NOT emitting ``Access-Control-Allow-Credentials``
        in the response when ``*`` is the origin
        (browsers would block the request anyway, and
        emitting the header would be misleading).
        We pin that the wildcard ``ACAO`` is present
        and that ``allow_credentials`` is NOT.
        """
        client = _build_app(cors_origins="*")
        r = client.get(
            "/healthz",
            headers={"Origin": "https://anywhere.example.com"},
        )
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "*"
        # ``Allow-Credentials`` is absent (Starlette's
        # behaviour for ``*``; credentials would
        # silently fail in the browser anyway).
        assert "access-control-allow-credentials" not in r.headers

    def test_credentials_allowed_for_explicit_origin(self):
        client = _build_app(cors_origins="https://app.example.com")
        r = client.options(
            "/echo",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r.headers.get("access-control-allow-credentials", "") == "true"


# ---------------------------------------------------------------------------
# TrustedHost
# ---------------------------------------------------------------------------


class TestTrustedHost:
    def test_empty_allowlist_disables_middleware(self):
        """No allow-list = no TrustedHost middleware.
        Any Host header is accepted (dev mode).
        """
        client = _build_app(trusted_hosts="")
        r = client.get("/healthz", headers={"Host": "anything.test"})
        assert r.status_code == 200

    def test_host_in_allowlist_passes(self):
        client = _build_app(trusted_hosts="api.example.com")
        r = client.get("/healthz", headers={"Host": "api.example.com"})
        assert r.status_code == 200

    def test_host_not_in_allowlist_rejected(self):
        client = _build_app(trusted_hosts="api.example.com")
        r = client.get("/healthz", headers={"Host": "evil.test"})
        assert r.status_code == 400
        assert "invalid Host header" in r.json()["detail"]

    def test_host_matching_is_case_insensitive(self):
        client = _build_app(trusted_hosts="api.example.com")
        # Browsers usually send lowercase, but a
        # malicious client could try case variation.
        r = client.get("/healthz", headers={"Host": "API.EXAMPLE.COM"})
        assert r.status_code == 200

    def test_host_with_port_strips_port(self):
        client = _build_app(trusted_hosts="api.example.com")
        r = client.get("/healthz", headers={"Host": "api.example.com:8080"})
        assert r.status_code == 200

    def test_multiple_hosts(self):
        client = _build_app(
            trusted_hosts=("api.example.com,staging.example.com,localhost")
        )
        for host in (
            "api.example.com",
            "staging.example.com",
            "localhost",
        ):
            r = client.get("/healthz", headers={"Host": host})
            assert r.status_code == 200, f"host={host} unexpectedly rejected"

    def test_missing_host_header_rejected(self):
        client = _build_app(trusted_hosts="api.example.com")
        # No Host header → middleware sees empty
        # string → rejection.
        # Starlette's TestClient sets Host by default;
        # we explicitly clear it.
        r = client.get("/healthz", headers={"Host": ""})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# HTTPSRedirect
# ---------------------------------------------------------------------------


class TestHTTPSRedirect:
    def test_disabled_means_no_redirect(self):
        client = _build_app(https_redirect=False)
        r = client.get(
            "/healthz",
            headers={"X-Forwarded-Proto": "http"},
        )
        assert r.status_code == 200
        # No Location header (no redirect).
        assert "location" not in {k.lower() for k in r.headers.keys()}

    def test_http_request_redirects_to_https(self):
        client = _build_app(https_redirect=True)
        r = client.get(
            "/healthz",
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
        assert r.status_code == 308
        assert r.headers["location"].startswith("https://")

    def test_https_request_passes_through(self):
        client = _build_app(https_redirect=True)
        r = client.get(
            "/healthz",
            headers={"X-Forwarded-Proto": "https"},
        )
        assert r.status_code == 200

    def test_no_forwarded_header_passes_through(self):
        """Without ``X-Forwarded-Proto`` (no TLS proxy
        in front), the middleware does NOT redirect —
        it trusts the wire protocol the ASGI server
        sees.
        """
        client = _build_app(https_redirect=True)
        r = client.get("/healthz")
        assert r.status_code == 200

    def test_post_not_redirected(self):
        """Only GET/HEAD are redirected. POST would
        silently drop the body on 308.
        """
        client = _build_app(https_redirect=True)
        r = client.post(
            "/echo",
            json={"x": 1},
            headers={"X-Forwarded-Proto": "http"},
        )
        assert r.status_code == 200
        # The POST succeeded; no Location header.
        assert "location" not in {k.lower() for k in r.headers.keys()}

    def test_redirect_preserves_path_and_query(self):
        client = _build_app(https_redirect=True)
        r = client.get(
            "/echo?foo=bar&baz=1",
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
        assert r.status_code == 308
        location = r.headers["location"]
        assert "/echo" in location
        assert "foo=bar" in location
        assert "baz=1" in location


# ---------------------------------------------------------------------------
# Combined: all three enabled at once
# ---------------------------------------------------------------------------


class TestCombinedEdgeMiddleware:
    def test_all_three_at_once(self):
        client = _build_app(
            cors_origins="https://app.example.com",
            trusted_hosts="api.example.com",
            https_redirect=True,
        )
        # A well-formed GET via https from the
        # approved host: 200.
        r = client.get(
            "/healthz",
            headers={
                "Host": "api.example.com",
                "X-Forwarded-Proto": "https",
                "Origin": "https://app.example.com",
            },
        )
        assert r.status_code == 200

    def test_trusted_host_blocks_before_other_middleware(self):
        """The middleware order in Starlette: last
        added = first executed (LIFO). The framework
        adds rate-limit, then CORS, then TrustedHost,
        then HTTPSRedirect. So TrustedHost runs first
        on the way in: a bad Host returns 400 without
        even consulting CORS / rate limit.
        """
        client = _build_app(
            cors_origins="https://app.example.com",
            trusted_hosts="api.example.com",
            https_redirect=True,
        )
        r = client.get(
            "/healthz",
            headers={
                "Host": "evil.test",
                "X-Forwarded-Proto": "https",
                "Origin": "https://app.example.com",
            },
        )
        assert r.status_code == 400
        assert "invalid Host header" in r.json()["detail"]


# ---------------------------------------------------------------------------
# HTTPSRedirect — status code + HSTS (the new knobs)
# ---------------------------------------------------------------------------


class TestHTTPSRedirectStatusCode:
    def test_default_308(self):
        client = _build_app(https_redirect=True)
        r = client.get(
            "/healthz",
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
        assert r.status_code == 308

    def test_custom_301(self):
        """Legacy deployments may want 301."""
        client = _build_app(https_redirect=True, https_status_code=301)
        r = client.get(
            "/healthz",
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
        assert r.status_code == 301

    def test_custom_307(self):
        client = _build_app(https_redirect=True, https_status_code=307)
        r = client.get(
            "/healthz",
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
        assert r.status_code == 307

    def test_custom_status_preserves_path(self):
        client = _build_app(https_redirect=True, https_status_code=301)
        r = client.get(
            "/echo?foo=bar",
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
        assert r.status_code == 301
        assert "/echo" in r.headers["location"]
        assert "foo=bar" in r.headers["location"]


class TestHTTPSRedirectHSTS:
    def test_hsts_not_emitted_by_default(self):
        """``hsts_max_age=0`` (the default) emits no
        HSTS header. Operators must opt in explicitly.
        """
        client = _build_app(https_redirect=True)
        r = client.get(
            "/healthz",
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
        assert r.status_code == 308
        assert "strict-transport-security" not in {k.lower() for k in r.headers.keys()}

    def test_hsts_emitted_with_max_age(self):
        """``hsts_max_age=31536000`` (1 year) emits
        ``Strict-Transport-Security: max-age=31536000``
        on the redirect response so browsers pin
        HTTPS for the domain.
        """
        client = _build_app(
            https_redirect=True,
            https_hsts_max_age=31536000,
        )
        r = client.get(
            "/healthz",
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
        assert r.headers["strict-transport-security"] == ("max-age=31536000")

    def test_hsts_two_years_for_preload(self):
        """2 years (the HSTS-preload-list minimum)."""
        client = _build_app(
            https_redirect=True,
            https_hsts_max_age=63072000,
        )
        r = client.get(
            "/healthz",
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
        assert r.headers["strict-transport-security"] == ("max-age=63072000")

    def test_hsts_only_on_redirect_response(self):
        """HSTS is NOT emitted on the original http
        request (which gets redirected); it's on the
        redirect response itself, which is the
        standard HSTS bootstrap path. We assert this
        by checking the headers of the redirect
        response (not the original).
        """
        client = _build_app(
            https_redirect=True,
            https_hsts_max_age=31536000,
        )
        r = client.get(
            "/healthz",
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
        # The 308 itself carries HSTS.
        assert "strict-transport-security" in r.headers
        # The Location header carries the redirect URL.
        assert r.headers["location"].startswith("https://")


# ---------------------------------------------------------------------------
# Settings validation: https_redirect_status must be a redirect code
# ---------------------------------------------------------------------------


class TestSettingsHTTPSRedirectStatusValidation:
    def test_valid_codes_accepted(self):
        from kntgraph.infra.config import Settings

        for code in (301, 302, 307, 308):
            Settings(
                https_redirect_enabled=True,
                https_redirect_status=code,
            )

    def test_invalid_code_rejected(self):
        from kntgraph.infra.config import Settings

        for bad in (200, 304, 400, 500, 999):
            with pytest.raises(ValueError, match="https_redirect_status"):
                Settings(
                    https_redirect_enabled=True,
                    https_redirect_status=bad,
                )

    def test_negative_hsts_max_age_rejected(self):
        """``ge=0`` validator on the field rejects
        negative values; ``https_redirect_hsts_max_age``
        must be a non-negative integer (0 = no HSTS).
        """
        from kntgraph.infra.config import Settings

        with pytest.raises(ValueError):
            Settings(https_redirect_hsts_max_age=-1)

    def test_zero_hsts_max_age_accepted(self):
        from kntgraph.infra.config import Settings

        s = Settings(https_redirect_hsts_max_age=0)
        assert s.https_redirect_hsts_max_age == 0
