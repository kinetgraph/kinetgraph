# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``kntgraph.api.auth.bind_principal_dependency``.

The dependency factory is what makes
``Depends(bind_principal_dependency(verifier))`` work in
the HTTP gateway. It does three things in order:

  1. Read the ``X-API-Key`` header.
  2. Call ``verifier.verify(api_key)``.
  3. Bind the returned ``Principal`` to ``principal_ctx``
     and return it.

This module pins the contract for the factory itself
(separate from the router integration tests in
``test_intent_router.py``).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from kntgraph.api.auth import (  # noqa: E402
    AuthError,
    bind_principal_dependency,
)
from kntgraph.core.result import Err, Ok  # noqa: E402
from kntgraph.security import Principal, Role, principal_ctx  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _OkVerifier:
    """Returns ``Ok(Principal)`` for any non-empty key."""

    def __init__(self, principal: Principal) -> None:
        self._principal = principal
        self.calls: list[str] = []

    async def verify(self, api_key: str):
        self.calls.append(api_key)
        return Ok(self._principal)


class _ErrVerifier:
    """Returns ``Err(AuthError)`` for any key."""

    def __init__(self, kind: str, message: str) -> None:
        self._kind = kind
        self._message = message

    async def verify(self, api_key: str):
        return Err(AuthError(self._kind, self._message))


def _principal(agent_id: str = "tenant-a.agent-1") -> Principal:
    return Principal(
        agent_id=agent_id,
        role=Role.agent,
        tenant_id=agent_id.partition(".")[0],
        key_id="test",
    )


# ---------------------------------------------------------------------------
# Direct calls (no FastAPI app)
# ---------------------------------------------------------------------------


class TestBindPrincipalDependencyDirect:
    """
    The factory returns a zero-arg dependency that FastAPI
    can call with the ``X-API-Key`` header. Direct calls
    simulate FastAPI by passing the header explicitly.
    """

    @pytest.mark.asyncio
    async def test_happy_path_binds_principal(self):
        verifier = _OkVerifier(_principal())
        auth = bind_principal_dependency(verifier)

        result = await auth(x_api_key="tenant-a.agent-1")

        assert isinstance(result, Principal)
        assert result.agent_id == "tenant-a.agent-1"
        # The dependency MUST bind to principal_ctx so
        # downstream readers (EventLog.append,
        # ToolInvoker) see it.
        assert principal_ctx.get() is result

    @pytest.mark.asyncio
    async def test_missing_key_raises_401(self):
        verifier = _ErrVerifier("missing", "X-API-Key is required")
        auth = bind_principal_dependency(verifier)

        with pytest.raises(HTTPException) as exc:
            await auth(x_api_key="")
        assert exc.value.status_code == 401
        assert "required" in exc.value.detail

    @pytest.mark.asyncio
    async def test_forbidden_key_raises_403(self):
        verifier = _ErrVerifier("forbidden", "key not recognised")
        auth = bind_principal_dependency(verifier)

        with pytest.raises(HTTPException) as exc:
            await auth(x_api_key="bad-key")
        assert exc.value.status_code == 403
        assert "not recognised" in exc.value.detail

    @pytest.mark.asyncio
    async def test_malformed_key_raises_403(self):
        """The ``malformed`` kind is a specialisation of
        ``forbidden`` from the client perspective; we
        preserve the framework's contract that only
        ``missing`` is 401.
        """
        verifier = _ErrVerifier("malformed", "binding corrupt")
        auth = bind_principal_dependency(verifier)

        with pytest.raises(HTTPException) as exc:
            await auth(x_api_key="x")
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_none_x_api_key_treated_as_empty(self):
        """When the header is absent, FastAPI passes
        ``None``. The dependency should call the verifier
        with an empty string (which the verifier then
        maps to ``AuthError(kind="missing")`` → 401).
        """
        verifier = _OkVerifier(_principal())
        auth = bind_principal_dependency(verifier)

        # A permissive verifier would return Ok and bind;
        # this test pins the contract that ``None`` is
        # forwarded as the empty string.
        result = await auth(x_api_key=None)
        assert isinstance(result, Principal)
        # The verifier saw the empty string, not None.
        assert verifier.calls == [""]


# ---------------------------------------------------------------------------
# FastAPI integration: the dependency must work as `Depends(...)`
# ---------------------------------------------------------------------------


class TestBindPrincipalDependencyInFastAPI:
    """
    End-to-end check: build a small FastAPI app, declare
    the dependency via ``Depends(...)``, and assert the
    wiring is correct (status code, response body, and
    ``principal_ctx`` side effect).
    """

    def _build_app(self, verifier):
        from fastapi import Depends, FastAPI

        from kntgraph.api.auth import bind_principal_dependency

        auth = bind_principal_dependency(verifier)
        app = FastAPI()

        @app.get("/whoami")
        async def whoami(
            principal: Principal = Depends(auth),
        ):
            return {"agent_id": principal.agent_id}

        return app

    def test_authenticated_request(self):
        client = TestClient(
            self._build_app(_OkVerifier(_principal("tenant-a.agent-1")))
        )
        r = client.get("/whoami", headers={"X-API-Key": "tenant-a.agent-1"})
        assert r.status_code == 200
        assert r.json() == {"agent_id": "tenant-a.agent-1"}

    def test_missing_key_401(self):
        """A missing ``X-API-Key`` returns 401 (the
        ``kind="missing"`` branch in the dependency)."""
        client = TestClient(
            self._build_app(_ErrVerifier("missing", "X-API-Key is required"))
        )
        r = client.get("/whoami")
        assert r.status_code == 401

    def test_forbidden_key_403(self):
        client = TestClient(self._build_app(_ErrVerifier("forbidden", "bad")))
        r = client.get("/whoami", headers={"X-API-Key": "anything"})
        assert r.status_code == 403
