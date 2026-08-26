# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
intent_router.routes -- FastAPI route installers.

Five installers, each taking the FastAPI primitives
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
    ``GET /agents/{id}/events/{eid}/status`
    (the long-poll status endpoint, **deprecated**
    in v0.14 per ADR-065 §3.1 / §5.1; use
    `register_sse_events` for new code).
  - `register_sse_events(app, ..., log, auth)`:
    `GET /agents/{id}/events` — the SSE subscribe
    endpoint (ADR-065 §3.1 / §4.1). Streams the
    agent's EventLog from the cursor supplied by
    the client; each event carries the canonical
    payload, the `id` is the Redis Stream cursor
    (so the SSE standard `Last-Event-ID` reconnect
    semantics work out of the box).

The `auth` argument is the closure produced by
`bind_principal_dependency(verifier)`; it maps the
`X-API-Key` header to a `Principal`. Each installer
asserts that the principal's `agent_id` matches the
URL's `agent_id` (rejects 403 otherwise) — the same
pattern inlined 3x before the helper was extracted
(see `api.auth.check_agent_binding`).
"""

from __future__ import annotations

import asyncio
import json
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Optional
from uuid import UUID

import structlog

from kntgraph.tools.registry import ToolRegistry

from ...core._typing import (
    Dependable,
    HeaderParam,
    HTTPExceptionLike,
    RouterApp,
)
from ...core.event import Event
from ...core.event.codec import event_to_dict
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


# Test-only hook: when set, the SSE generator
# closes after the first batch of events has been
# yielded. Production never sets this; the
# generator keeps polling indefinitely until the
# client disconnects (``CancelledError`` propagates
# and FastAPI cleans up the response). The hook
# exists so unit tests can drive the generator
# end-to-end without blocking on the long-lived
# poll loop.
_sse_test_close_after_first_batch: bool = False


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
            # The gateway emits the
            # ``tool.<name>.requested`` event
            # **unconditionally** — even for tools
            # not in the ``ToolRegistry``. The
            # registration check (gate 1 of the
            # three-gate ACL model, ADR-060 §3.0)
            # moves to the dispatcher (the
            # ``IntentResolutionSystem``,
            # ADR-039 §2.2 step 2) where it is
            # enforced alongside the RBAC and
            # persona checks. On rejection the
            # dispatcher emits
            # ``intent.validation_failed`` with the
            # canonical reason string
            # (``tool_not_registered``); the client
            # learns of the failure via the SSE
            # subscribe stream.
            #
            # This change closes the gap flagged by
            # ADR-065 §3.2 / §5.1: previously the
            # gateway returned 404 with no event
            # emitted (ADR-012 §2.3), so the
            # EventLog stayed clean of attempts
            # that could never succeed and the
            # client learned of the failure
            # synchronously. The new model
            # (always emit, dispatcher validates)
            # gives the operator an audit trail,
            # makes idempotency work (two calls
            # with the same body dedupe on
            # ``event_id``), and unifies the
            # failure surface across all three
            # gates.
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
        # is the entry point of an external flow.
        # ``correlation_id`` is **derived from
        # ``event_id``** (ADR-065 §2.3, ADR-037 §2
        # requirement): the event_id is a UUID5 hash
        # of the deterministic request fields
        # (``agent_id``, ``type``, ``target``,
        # ``args``, ``idempotency_key``); using the
        # same hash for the correlation_id makes a
        # retry of the same request produce the same
        # correlation_id, so the audit trail stitches
        # the retry back to the original intent.
        #
        # ADR-037 §2 requires the caller to supply a
        # correlation context; we build it here at the
        # entry point instead of letting the framework
        # default it. The default would mint a fresh
        # UUID per call (the pre-fix bug) and break
        # the retry-audit-trail contract.
        from kntgraph.core.event import CorrelationContext

        correlation = CorrelationContext.new(
            correlation_id=UUID(event_id),
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
            # Pin the Event's ``event_id`` to the
            # deterministic hash so the
            # ``correlation_id == event_id`` invariant
            # holds end-to-end (the Event's default
            # would mint a fresh UUID4 and the audit
            # trail would break on the first emit
            # without this pin).
            event_id=UUID(event_id),
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


def _get_streaming_response() -> type:
    """
    Lazily import :class:`fastapi.responses.StreamingResponse`.

    The ``fastapi`` package is an optional dependency
    (the ``[api]`` extra). The other ``register_*``
    installers defer the FastAPI imports to the
    FastAPI wrapper module (``_create_app``); SSE
    reuses the same import strategy via this helper.

    The runtime call from ``register_sse_events``
    raises ``ImportError`` with a clear message
    pointing at ``kntgraph[api]`` if FastAPI is
    missing.
    """
    from fastapi.responses import StreamingResponse

    return StreamingResponse


def register_sse_events(
    app: RouterApp,
    FastAPI: type | None = None,
    *,
    Depends: Dependable,
    Principal: type | None = None,
    log: EventLog | None = None,
    auth: PrincipalDep,
) -> None:
    """
    Install ``GET /agents/{agent_id}/events`` — the SSE
    subscribe endpoint (ADR-065 §3.1 / §4.1).

    Behaviour
    ---------

    The endpoint streams the agent's EventLog to the
    client as Server-Sent Events. The client controls
    the start point via ``from=<stream_id>`` (default
    ``"0"`` = replay the whole visible window). The
    server reads new events with a poll-and-yield loop
    while the connection is held. Optional filters
    narrow the stream:

      - ``causation_id=<event_id>`` — only events whose
        ``causation_id`` matches the value (the
        canonical use case: subscribe to the result of
        one POST ``/intents`` request).
      - ``event_class=<domain|lifecycle|tool>`` —
        only events of the given class.

    Each SSE frame carries the canonical payload
    (``event: <type>``, ``id: <stream_id>``,
    ``data: <json>``). The ``id`` is the Redis Stream
    cursor, so the SSE standard ``Last-Event-ID``
    reconnect semantics work out of the box: a client
    that disconnects and reconnects with the last
    ``id`` it received gets the gap replayed.

    A ``:heartbeat\\n\\n`` SSE comment is emitted
    every 15 s when no events have arrived; this
    keeps proxies / load balancers from closing idle
    connections.

    The endpoint REPLACES the long-poll
    ``GET /agents/{agent_id}/events/{event_id}/status``
    (now deprecated). The old endpoint is kept for
    one minor cycle with a ``DeprecationWarning`` at
    request time (ADR-065 §5.1).

    Why poll-and-yield and not real subscribe
    ----------------------------------------

    The EventLog does not yet expose a
    ``subscribe(agent_id)`` primitive (see DEBT
    §2.X for the planned Redis Pub/Sub channel).
    Polling is good enough for v0.14: the poll
    interval is 100 ms (matches the legacy long-poll
    cadence); the EventLog's Redis Stream read is
    cheap; and the SSE framing only adds a few bytes
    per event. When the volume justifies it, the
    internals of this endpoint swap to a real
    Pub/Sub channel without changing the public
    contract.
    """

    StreamingResponse = _get_streaming_response()

    @app.get("/agents/{agent_id}/events")
    async def sse_events(
        agent_id: str,
        principal: Principal = Depends(auth),  # type: ignore[valid-type]
        from_: str = "0",
        causation_id: "str | None" = None,
        event_class: "str | None" = None,
    ) -> "StreamingResponse":  # type: ignore[valid-type]
        """
        Subscribe to the agent's EventLog.

        Query parameters (all optional):
          - ``from_``: the Redis Stream cursor to
            start from (``"0"`` = from the
            beginning). On reconnect, the SSE client
            sends the last ``id`` it received as
            ``Last-Event-ID``; the gateway translates
            that into ``from_``.
          - ``causation_id``: filter to events whose
            ``causation_id`` matches (use the
            ``event_id`` returned by ``POST /intents``
            to subscribe to one specific request).
          - ``event_class``: filter by event class
            (``"domain"``, ``"lifecycle"``, ``"tool"``).
        """
        check_agent_binding(principal, agent_id)

        # SSE filters are validated once at the entry
        # point; the inner generator is a pure
        # poll-and-yield loop and trusts the inputs.
        causation_filter: Optional[str] = causation_id
        class_filter: Optional[str] = event_class

        async def _stream() -> AsyncIterator[bytes]:
            cursor = from_
            # Tracks the last time we emitted a
            # heartbeat so idle connections stay alive.
            last_heartbeat = asyncio.get_event_loop().time()
            heartbeat_interval_s = 15.0
            poll_interval_s = DEFAULT_POLL_INTERVAL_S
            try:
                while True:
                    events: list[Event] = []
                    try:
                        # ``read(agent_id, start="(",
                        # end="+")`` returns events
                        # strictly after ``from_``. When
                        # ``from_ == "0"``, we want the
                        # whole history, so ``start="-"
                        # end="+"``. Otherwise the
                        # ``start="("`` is the Redis
                        # Stream exclusive-id convention
                        # (``(`` + cursor = strictly after
                        # the cursor); the EventLog's
                        # ``read(start, end)`` honours it.
                        start = "-" if cursor == "0" else f"({cursor}"
                        events = await log.read(
                            agent_id,
                            start=start,
                            end="+",
                        )
                    except Exception as e:
                        logger.warning(
                            "intent_router.sse_read_failed",
                            agent_id=agent_id,
                            error=str(e),
                        )
                        await asyncio.sleep(poll_interval_s)
                        continue
                    if not events:
                        # Test hook: when the
                        # ``_sse_test_close_after_first_batch``
                        # global is set, the generator
                        # closes after the first batch
                        # of events has been yielded.
                        # Production never sets this; the
                        # generator keeps polling
                        # indefinitely until the client
                        # disconnects.
                        if _sse_test_close_after_first_batch:
                            return
                        now = asyncio.get_event_loop().time()
                        if now - last_heartbeat >= heartbeat_interval_s:
                            yield b":heartbeat\n\n"
                            last_heartbeat = now
                        await asyncio.sleep(poll_interval_s)
                        continue
                    for ev in events:
                        if causation_filter is not None:
                            if str(ev.causation_id or "") != causation_filter:
                                continue
                        if class_filter is not None:
                            if ev.event_class != class_filter:
                                continue
                        payload = event_to_dict(ev)
                        frame = (
                            f"event: {ev.event_type}\n"
                            f"id: {ev.event_id}\n"
                            f"data: {json.dumps(payload, default=str)}\n\n"
                        ).encode("utf-8")
                        yield frame
                        cursor = str(ev.event_id)
                        last_heartbeat = asyncio.get_event_loop().time()
                    # Test hook: close after the first
                    # batch of events has been yielded.
                    if _sse_test_close_after_first_batch:
                        return
            except asyncio.CancelledError:
                raise

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
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

    **Deprecated in v0.14** (ADR-065 §5.1). Use
    ``GET /agents/{agent_id}/events?causation_id=<event_id>``
    (the SSE subscribe endpoint, ``register_sse_events``)
    instead. The endpoint stays in place for one
    minor cycle; emits ``DeprecationWarning`` at
    request time.
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

        **Deprecated:** prefer
        ``GET /agents/{agent_id}/events?causation_id={event_id}``
        (SSE). The new endpoint streams the same
        terminal events without the per-request
        polling overhead; ``Last-Event-ID`` on
        reconnect handles gap replay automatically.
        """
        warnings.warn(
            (
                f"GET /agents/{agent_id}/events/{event_id}/status "
                f"is deprecated; use GET /agents/{agent_id}/events?"
                f"causation_id={event_id} (SSE) instead. See ADR-065 §5.1."
            ),
            DeprecationWarning,
            stacklevel=2,
        )
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
    "register_sse_events",
]
