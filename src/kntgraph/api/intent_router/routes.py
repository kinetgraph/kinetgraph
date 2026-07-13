# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
intent_router.routes -- FastAPI route installers.

Four installers, each taking the FastAPI primitives
(`FastAPI`, `Depends`, `Header`, `HTTPException`,
`Principal`), the framework dependencies (`EventLog`,
`ToolRegistry`), and the `bind_principal_dependency`
closure as arguments. This keeps the helpers
transport-agnostic (they don't import FastAPI
themselves) and lets tests inject mocks.

  - `register_healthz(app, ...)`: ``GET /healthz``
    (the only endpoint that bypasses auth).
  - `register_list_tools(app, ..., registry, auth)`:
    ``GET /agents/{id}/tools``.
  - `register_post_intent(app, ..., log, registry, auth)`:
    ``POST /agents/{id}/intents`` — the main entry:
    validate, resolve target, emit
    `tool.<name>.requested`, return 202.
  - `register_get_status(app, ..., log, auth)`:
    ``GET /agents/{id}/events/{eid}/status`` (the
    long-poll status endpoint).

The `auth` argument is the closure produced by
`bind_principal_dependency(verifier)`; it maps the
`X-API-Key` header to a `Principal`. Each installer
asserts that the principal's `agent_id` matches the
URL's `agent_id` (rejects 403 otherwise) — the same
pattern inlined 3x before the helper was extracted
(see `api.auth.check_agent_binding`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Optional

import structlog

from kntgraph.tools.registry import ToolRegistry

from ...core._typing import (
    Dependable,
    HeaderParam,
    HTTPExceptionLike,
    RouterApp,
)
from ...core.event import Event
from ...core.long_poll import DEFAULT_POLL_INTERVAL_S, await_terminal_event
from ...security import Principal
from ...stream.event_log import EventLog
from ..auth import check_agent_binding
from ..schemas import (
    HealthResponse,
    IntentRequest,
    IntentResponse,
    RejectionResponse,
    StatusResponse,
    ToolDescriptor as ToolDescriptorSchema,
)
from .helpers import (
    _deterministic_event_id,
    _sanitize_idempotency_key,
)

logger = structlog.get_logger()


# The shape of the closure produced by
# ``api.auth.bind_principal_dependency(verifier)``:
# an async callable usable as a FastAPI ``Depends``.
PrincipalDep = Callable[..., Awaitable[Principal]]


def register_healthz(
    app: RouterApp,
    FastAPI: type | None = None,
) -> None:
    """
    Install ``/healthz`` (the only endpoint that
    bypasses auth and rate limiting).

    `FastAPI` is accepted for signature symmetry with
    the other installers; the healthz endpoint does
    not need it.
    """

    @app.get(
        "/healthz",
        response_model=HealthResponse,
    )
    async def healthz() -> HealthResponse:
        return HealthResponse()


def register_list_tools(
    app: RouterApp,
    FastAPI: type | None = None,
    *,
    Depends: Dependable,
    Principal: type | None = None,
    registry: ToolRegistry | None = None,
    auth: PrincipalDep,
) -> None:
    """
    Install ``GET /agents/{agent_id}/tools`` (list the
    ToolRegistry, gated by the agent_id binding).
    """

    @app.get(
        "/agents/{agent_id}/tools",
        response_model=list[ToolDescriptorSchema],
    )
    async def list_tools(
        principal: Principal = Depends(auth),  # type: ignore[valid-type]
        agent_id: str = "",
    ) -> list[ToolDescriptorSchema]:
        """
        List the tools registered for this agent.

        The `agent_id` in the URL is the binding
        target; the `principal.agent_id` from the API
        key is the caller's identity. They must
        match — a key for `agent-X` cannot list
        tools for `agent-Y`.
        """
        check_agent_binding(principal, agent_id)
        descriptors = registry.list_descriptors()  # type: ignore[union-attr]
        return [
            ToolDescriptorSchema(
                name=d.name,
                description=d.description,
                input_schema_json=d.input_schema_json,
            )
            for d in descriptors
        ]


def register_post_intent(
    app: RouterApp,
    FastAPI: type | None = None,
    *,
    Depends: Dependable,
    Header: HeaderParam,
    HTTPException: type[HTTPExceptionLike],
    Principal: type | None = None,
    log: EventLog | None = None,
    registry: ToolRegistry | None = None,
    auth: PrincipalDep,
) -> None:
    """
    Install ``POST /agents/{agent_id}/intents`` (the
    main entry: validate, resolve target, emit
    `tool.<name>.requested`, return 202).
    """

    @app.post(
        "/agents/{agent_id}/intents",
        response_model=IntentResponse,
        status_code=202,
        responses={
            404: {
                "model": RejectionResponse,
                "description": ("Tool or Role not registered for this agent_id."),
            },
            422: {
                "description": ("Request schema invalid."),
            },
        },
    )
    async def post_intent(
        agent_id: str,
        body: IntentRequest,
        principal: Principal = Depends(auth),  # type: ignore[valid-type]
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> IntentResponse:
        """
        Accept an intent. The router:
          1. checks the agent_id binding;
          2. resolves the tool/role;
          3. emits `tool.{name}.requested`;
          4. returns 202 + status URL.
        """
        check_agent_binding(principal, agent_id)
        # 0. Validate Idempotency-Key. Done BEFORE
        # any hashing so a malformed key cannot
        # inflate the hash input, and so the
        # 400 response is a clear client error
        # rather than a downstream 500. The helper
        # raises ``ValueError``; we convert to
        # HTTPException here so the transport
        # concern stays in the FastAPI scope.
        try:
            idempotency_key = _sanitize_idempotency_key(idempotency_key)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # 1. Resolve target.
        if body.type == "tool.invoke":
            if not body.tool:
                raise HTTPException(
                    status_code=422,
                    detail=("'tool' is required when type='tool.invoke'"),
                )
            if registry.get(body.tool) is None:
                # NO event is emitted on 404. The
                # EventLog stays clean of attempts
                # that could never succeed (ADR-012
                # §2.3).
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"Tool {body.tool!r} is not "
                        f"registered for "
                        f"agent_id={agent_id!r}"
                    ),
                )
            event_type = f"tool.{body.tool}.requested"
            target = body.tool
        else:  # role.invoke
            if not body.role:
                raise HTTPException(
                    status_code=422,
                    detail=("'role' is required when type='role.invoke'"),
                )
            # Roles live outside the ToolRegistry
            # (ADR-006). For v1 we treat any
            # `role` as a candidate; consumers
            # downstream (e.g. a Role registry)
            # validate further. We still emit a
            # `tool.{role}.requested` event so
            # downstream dispatchers don't have
            # to special-case Roles.
            event_type = f"tool.{body.role}.requested"
            target = body.role

        # 2. Deterministic event_id.
        event_id = _deterministic_event_id(
            agent_id=agent_id,
            type_=body.type,
            target=target,
            args=body.args,
            idempotency_key=idempotency_key or "",
        )

        # 3. Append to EventLog. The HTTP intent router
        # is the entry point of an external flow; the
        # request may carry a caller-supplied
        # ``X-Correlation-Id`` (passed via
        # ``idempotency_key``) or we mint a fresh
        # correlation_id. ADR-037: the caller MUST
        # supply a correlation context — we do it here
        # at the entry point instead of letting the
        # framework default it.
        from kntgraph.core.event import CorrelationContext
        from uuid import uuid4

        flow_id = uuid4()
        correlation = CorrelationContext.new(
            correlation_id=flow_id,
        )
        event = Event.domain_from(
            agent_id=agent_id,
            type=event_type,
            data={
                "request_id": event_id,
                "tool": target,
                "args": body.args,
                "source": "http.intent_router",
            },
            correlation=correlation,
        )
        append_result = await log.append(event)
        if append_result.is_err():
            logger.error(
                "intent_router.append_failed",
                event_id=event_id,
                error=str(append_result.err_value()),
            )
            raise HTTPException(
                status_code=503,
                detail="EventLog temporarily unavailable",
            )

        return IntentResponse(
            event_id=event_id,
            status="accepted",
            status_url=(f"/agents/{agent_id}/events/{event_id}/status"),
        )


def register_get_status(
    app: RouterApp,
    FastAPI: type | None = None,
    *,
    Depends: Dependable,
    Principal: type | None = None,
    log: EventLog | None = None,
    auth: PrincipalDep,
) -> None:
    """
    Install ``GET /agents/{agent_id}/events/{event_id}/status``
    (the long-poll status endpoint).
    """

    @app.get(
        "/agents/{agent_id}/events/{event_id}/status",
        response_model=StatusResponse,
    )
    async def get_status(
        agent_id: str,
        event_id: str,
        principal: Principal = Depends(auth),  # type: ignore[valid-type]
        timeout_s: float = 5.0,
    ) -> StatusResponse:
        """
        Long-poll the EventLog for the terminal
        event with `causation_id == event_id`.

        Polling window: `timeout_s` seconds
        (default 5). Returns `pending` if no
        terminal event arrived in that window;
        the client should poll again.
        """
        check_agent_binding(principal, agent_id)

        def _match(e: Event) -> bool:
            if str(e.causation_id or "") != event_id:
                return False
            return e.event_type.endswith(".completed") or e.event_type.endswith(
                ".failed"
            )

        terminal = await await_terminal_event(
            read=lambda: log.read(agent_id),
            predicate=_match,
            timeout_s=timeout_s,
            poll_interval_s=DEFAULT_POLL_INTERVAL_S,
        )
        if terminal is None:
            # Deadline reached without a terminal
            # event — the request is still in flight.
            return StatusResponse(
                status="pending",
                event_id=event_id,
            )
        if terminal.event_type.endswith(".completed"):
            return StatusResponse(
                status="completed",
                event_id=event_id,
                result=terminal.data.get("result"),
            )
        # ".failed" — the predicate filters out
        # other event types.
        return StatusResponse(
            status="failed",
            event_id=event_id,
            error=str(terminal.data.get("error", "unknown")),
        )


__all__ = [
    "register_get_status",
    "register_healthz",
    "register_list_tools",
    "register_post_intent",
]
