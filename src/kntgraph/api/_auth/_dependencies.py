# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
api._auth._dependencies -- FastAPI ``Depends`` helpers
and the per-request auth dependency factory
(ADR-017 §3.2, §3.3).

Two pieces live here:

  - ``check_agent_binding(principal, agent_id)``:
    centralises the 403 pattern that repeats in every
    endpoint of the intent router (principal's
    ``agent_id`` must match the path parameter).

  - ``bind_principal_dependency(verifier)``: the
    FastAPI adapter for the request-level auth flow.
    Reads ``X-API-Key``, calls the verifier, on
    success binds the returned ``Principal`` to
    ``principal_ctx`` (so ``EventLog.append`` and
    ``ToolInvoker`` can attribute operations --
    ADR-017 §3.3) and returns it.

Note on the deleted helpers
---------------------------

The 0.9.x cycle shipped three additional ``Depends``
helpers -- ``require_principal``, ``require_role``,
and ``require_tenant`` -- alongside
``check_agent_binding``. They were never adopted by
the intent router or by ``fmh_office``/``fmh_app``;
``check_agent_binding`` plus
``bind_principal_dependency`` cover every authenticated
endpoint in the codebase. They are removed in this
split (workflow P1 #3; tracked in ``DEBT_TECHNICAL.md``
A.4 "Duas APIs de autorização paralelas -- pago"). If
a future endpoint needs role or tenant gating,
re-introduce the helpers at the call site rather than
re-adding dead code to the auth layer.

This module is a private implementation detail of
``_auth``; the public surface is unchanged.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Callable, Optional

from ...security import Principal, principal_ctx
from ._verifier import APIKeyVerifier


def check_agent_binding(principal: Principal, agent_id: str) -> None:
    """
    Verify the principal's ``agent_id`` matches the
    path parameter. Raises 403 on mismatch.

    The 403 pattern repeats in every endpoint of the
    intent router; this helper centralises the
    response shape so future endpoints can be added
    with one import.

    The ``from fastapi import HTTPException`` is lazy
    (inside the function body) so the module remains
    importable in environments without the ``[api]``
    extra installed -- the helper raises the
    ``ImportError`` on first call in that case, which
    is the standard contract for opt-in FastAPI
    integrations.
    """
    from fastapi import HTTPException

    if principal.agent_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail=("API key is bound to a different agent_id"),
        )


def bind_principal_dependency(
    verifier: "APIKeyVerifier",
) -> "Callable[..., Awaitable[Principal]]":
    """
    Build a FastAPI ``Depends``-compatible dependency
    that:

      1. Reads the ``X-API-Key`` header.
      2. Calls ``verifier.verify(api_key)``.
      3. On error, raises ``HTTPException`` (401 for
         ``AuthError(kind="missing")``, 403 otherwise).
      4. On success, binds the returned ``Principal`` to
         ``principal_ctx`` (so ``EventLog.append`` and
         ``ToolInvoker`` can attribute operations --
         ADR-017 §3.3) and returns it.

    Usage::

        from fastapi import Depends
        from kntgraph.api.auth import (
            APIKeyVerifier,
            bind_principal_dependency,
        )

        def build_app(verifier: APIKeyVerifier):
            auth = bind_principal_dependency(verifier)
            app = FastAPI()

            @app.post("/agents/{agent_id}/intents")
            async def post_intent(
                agent_id: str,
                principal: Principal = Depends(auth),
            ): ...

    The dependency does NOT call ``principal_ctx.reset``
    on the way out. ``ContextVar.set`` returns a token
    that can be used to restore the previous value, but
    FastAPI runs each request inside its own
    ``Context.run`` (since Starlette 0.27), so the set
    is naturally scoped to the request and does not
    leak to the next request on the same worker thread.
    The middleware-equivalent (see
    ``PrincipalBindingMiddleware``) is responsible for
    cleanup when the request is not FastAPI-mediated.
    """
    from fastapi import Header, HTTPException

    async def _dependency(
        x_api_key: Optional[str] = Header(default=None),
    ) -> Principal:
        result = await verifier.verify(x_api_key or "")
        if result.is_err():
            err = result.err_value()
            if err is None:
                # Defensive: ``is_err()`` was true but the
                # error value is missing. Treat as a generic
                # 500 -- should not happen with a well-formed
                # ``Result`` implementation.
                raise HTTPException(
                    status_code=500,
                    detail="Authentication failed",
                )
            if err.kind == "missing":
                raise HTTPException(
                    status_code=401,
                    detail=err.message,
                )
            raise HTTPException(
                status_code=403,
                detail=err.message,
            )
        principal = result.ok_value()
        if principal is None:
            # Defensive: ``is_err()`` was false but the
            # principal is missing. Treat as a generic 500.
            raise HTTPException(
                status_code=500,
                detail="Authentication succeeded but no principal",
            )
        # Bind to ContextVar so EventLog.append /
        # ToolInvoker can attribute operations
        # (ADR-017 §3.3).
        principal_ctx.set(principal)
        return principal

    return _dependency


__all__ = ["bind_principal_dependency", "check_agent_binding"]
