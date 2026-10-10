# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Side-effect helpers for ``WorkerManager._process_message`` and
``_consume_loop`` — extracted from ``src/kntgraph/tools/manager.py``
to reduce its LOC (MI gate, ADR-019).

Each function takes the manager (or the specific attrs it needs)
as positional arguments. The functions do not call any
``self.<attr>`` internally — the caller passes what is needed.
This keeps the helpers testable without instantiating a Manager
and keeps the dependency direction one-way
(``manager`` → ``_message_handlers``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

import structlog
from redis import exceptions as redis_exceptions

from kntgraph.core._typing import JsonValue
from kntgraph.core.event import Event

logger = structlog.get_logger()


async def xack_and_count_failure(
    redis: Any,
    stream_key: str,
    group_name: str,
    message_id: str,
    failure_counter: list[int],
) -> None:
    """ACK a message and increment the failure counter.

    Used when the message has been consumed but cannot be
    processed (payload parse error, ACL denial, etc.). The
    counter is passed as a single-element list because Python
    integers are immutable and we want to mutate the caller's
    int — wrapping in a list gives us a mutable cell.
    """
    await redis.xack(stream_key, group_name, message_id)
    failure_counter[0] += 1


def parse_request_payload(message_data: dict) -> Event | None:
    """Decode the JSON ``payload`` field of a Redis Stream
    entry into an ``Event``. Returns ``None`` when the payload
    is malformed; the caller is expected to XACK + count the
    failure.

    Extracted from ``WorkerManager._process_message`` (ADR-019
    MI gate). Pure function — takes the dict, returns the Event
    (or None); no Redis or other I/O.
    """
    try:
        payload_str = message_data.get(b"payload", b"{}").decode()
        request_event_dict = json.loads(payload_str)
        return Event.from_dict(request_event_dict)
    except Exception as e:  # noqa: BLE001  (wire-decoded payload; contract is None on any error)
        # Caller logs the structured field; this function just
        # returns None and the message id so the caller can
        # construct the log line + xack. We do not log here
        # to keep the pure-function contract.
        _log_payload_parse_error(e)
        return None


def _log_payload_parse_error(exc: BaseException) -> None:
    logger.exception(
        "worker.payload_parse.error_helper",
        error=str(exc),
    )


def coerce_tool_params(request_event: Event) -> Mapping[str, JsonValue]:
    """Return the tool param dict from a ``tool.<name>.requested``
    event. Accepts both ``data["params"]`` (the canonical
    v0.16 shape) and ``data["args"]`` (the legacy v0.14 shape
    used by the LiteLLM tool and others that pre-date ADR-036).

    The return type mirrors ``Event.data`` (ADR-067 §1.1): both
    fields of a ``tool.<name>.requested`` event are typed
    ``Mapping[str, JsonValue]`` so the boundary between the
    framework's event bus and the tool's ``invoke(**kwargs)``
    stays tight.
    """
    raw = request_event.data.get("params") or request_event.data.get("args") or {}
    if isinstance(raw, Mapping):
        return cast("Mapping[str, JsonValue]", raw)
    return cast("Mapping[str, JsonValue]", {})


def evaluate_acl(
    *,
    tool_name: str,
    request_event: Event,
    acl_for: Any,
) -> str | None:
    """Gate-1 ACL check (ADR-060 §3.0, ADR-066 §4.1).

    Returns the ``denied_reason`` string when the request is
    rejected, or ``None`` when it is allowed to proceed.

    ``acl_for`` is a callable passed in by the caller
    (typically ``manager.acl_for``) so the helper does not
    need a reference to the Manager. This keeps the helper
    testable in isolation.
    """
    acl = acl_for(tool_name)
    if acl is None:
        # Tool registered without ``acl=`` (legacy):
        # default-allow. The v0.17 step flips the default
        # to deny (see ADR-066 §4.4).
        return None

    principal_id = request_event.producer_principal_id
    if principal_id is None:
        return "acl_denied_no_principal"

    from kntgraph.security import Principal, PrincipalLevel

    # ``producer_principal_id`` is the principal's ``agent_id``
    # (the API layer in v0.16 sets it from ``principal_ctx``).
    # Extract the tenant prefix for the ``Principal`` invariant
    # (non-admin requires a non-empty tenant); fall back to the
    # whole string as the tenant when the format is ambiguous.
    tenant_id = principal_id.partition(".")[0] or principal_id
    principal = Principal(
        agent_id=principal_id,
        level=PrincipalLevel.agent,
        tenant_id=tenant_id,
        key_id="worker",
    )
    ok, reason = acl.check(principal)
    if not ok:
        return f"acl_denied:{reason}"
    return None


def log_acl_denial(
    *,
    tool_name: str,
    message_id: str,
    denied_reason: str,
    principal_id: str | None,
) -> None:
    """Emit the structured ``worker.acl_denied`` warning. Pure
    function: just logs. The caller is responsible for the
    corresponding ``tool.<name>.failed`` event + XACK.
    """
    logger.warning(
        "worker.acl_denied",
        tool=tool_name,
        message_id=message_id,
        reason=denied_reason,
        producer_principal_id=principal_id,
    )


def build_acl_denied_event(
    *,
    tool_name: str,
    request_event: Event,
    idempotency_key: str,
    denied_reason: str,
    principal_id: str | None,
) -> Event:
    """Construct the ``tool.<name>.failed`` event for an ACL
    denial. Pure: returns the Event; the caller appends + XACKs.
    """
    return Event.create(
        event_type=f"tool.{tool_name}.failed",
        agent_id=request_event.agent_id,
        event_class="domain",
        causation_id=__import__("uuid").UUID(idempotency_key),
        data={
            "error": denied_reason,
            "request_id": idempotency_key,
        },
        correlation=request_event.correlation,
        producer_principal_id=principal_id,
    )


def build_completion_event(
    *,
    tool_name: str,
    request_event: Event,
    idempotency_key: str,
    result_dict: Mapping[str, str | JsonValue],
) -> Event:
    """Build the ``tool.<name>.completed`` domain event from a
    successful ``result_dict``. Pure: returns the Event; the
    caller appends + updates the counter + XACKs.
    """
    val = result_dict["value"]
    # ``val`` is ``str | JsonValue`` per the dispatch contract
    # (``_dispatch_to_tool`` cast). When the tool returns a JSON
    # mapping we lift it as the event payload; otherwise we wrap a
    # single-key payload so the event is still JSON-safe downstream.
    if isinstance(val, Mapping):
        evt_data: dict[str, JsonValue] = dict(val)
    else:
        evt_data = {"result": val}
    return Event.create(
        event_type=f"tool.{tool_name}.completed",
        agent_id=request_event.agent_id,
        event_class="domain",
        causation_id=__import__("uuid").UUID(idempotency_key),
        data=evt_data,
        correlation=request_event.correlation,
    )


def build_failure_event(
    *,
    tool_name: str,
    request_event: Event,
    idempotency_key: str,
    result_dict: Mapping[str, str | JsonValue],
) -> Event:
    """Build the ``tool.<name>.failed`` event from a failed
    ``result_dict``. Pure: returns the Event.
    """
    return Event.create(
        event_type=f"tool.{tool_name}.failed",
        agent_id=request_event.agent_id,
        event_class="domain",
        causation_id=__import__("uuid").UUID(idempotency_key),
        data={"error": result_dict["error"]},
        correlation=request_event.correlation,
    )


async def reconnect_redis_after_runtime_error(redis: Any) -> None:
    """Tear down the current Redis client connection and
    force a reconnect on the next ``xreadgroup``.

    A ``RuntimeError`` from ``xreadgroup`` is the asyncio-redis
    signal that the connection is in a broken state. Continuing
    to read on the same client returns the same error in a
    tight loop, so we close the client (best-effort) and
    disconnect the underlying connection pool. The next
    ``xreadgroup`` reconnects transparently.

    Extracted from ``_consume_loop`` to keep the parent's CC
    under 10 (ADR-019); the inline cleanup with two
    ``try/except`` blocks, ``isinstance`` checks, and
    ``asyncio.iscoroutine`` branches was a complexity hot spot.
    """
    close_fn = getattr(redis, "aclose", None) or getattr(redis, "close", None)
    if close_fn:
        try:
            res = close_fn()
            if __import__("asyncio").iscoroutine(res):
                await res
        except (
            redis_exceptions.RedisError,
            AttributeError,
            OSError,
            TimeoutError,
        ):
            pass
    pool = getattr(redis, "connection_pool", None)
    if pool and hasattr(pool, "disconnect"):
        try:
            dis_res = pool.disconnect()
            if __import__("asyncio").iscoroutine(dis_res):
                await dis_res
        except (
            redis_exceptions.RedisError,
            AttributeError,
            OSError,
            TimeoutError,
        ):
            pass
