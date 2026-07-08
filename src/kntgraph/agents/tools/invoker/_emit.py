# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Emit helpers for the ToolInvoker.

These functions build and append a `tool.{name}.{completed,
args_invalid, failed}` event to the EventLog, sharing the
correlation/causation context of the originating request.

They are module-level (not methods on `ToolInvoker`) so they
can be tested in isolation and reused by other adapter-side
code without instantiating a full invoker.
"""

from __future__ import annotations

import time
from typing import Optional

from kntgraph.core.event import (
    Event,
    correlation_middleware,
)
from kntgraph.core.tool_event import tool_name_of
from kntgraph.stream.event_log import EventLog
from kntgraph.agents.tools.protocol import ToolArgValue, ToolEventType


def tool_name_from_request(request: Event) -> str:
    """
    Extract the tool name from the request's event type.

    Thin wrapper around ``core.tool_event.tool_name_of`` —
    kept so the call-sites read consistently and so a
    future change to the wire format only touches one
    place (``core/tool_event.py``).
    """
    name = tool_name_of(request.event_type)
    if name is None:
        return "unknown"
    return name


def ms_since(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


async def emit_completion(
    log: EventLog,
    request: Event,
    result: "ToolArgValue",
    *,
    latency_ms: float,
) -> Event:
    """
    Emit `tool.{name}.completed` with the Tool's return value.

    Returns the appended event. Raises an `Exception` if the
    EventLog rejects the append — the caller wraps that into
    a ``Result.err``.
    """
    tool_name = tool_name_from_request(request)
    ctx = correlation_middleware.current()
    e = Event.domain_from(
        agent_id=request.agent_id,
        type=ToolEventType.completed(tool_name),
        data={
            "request_id": str(request.event_id),
            "tool": tool_name,
            "result": result,
            "latency_ms": latency_ms,
        },
        correlation=ctx,
        causation_id=request.event_id,
    )
    append_result = await log.append(e)
    if append_result.is_err():
        raise Exception(f"Failed to append completion: {append_result.err_value()}")
    return e


async def emit_args_invalid(
    log: EventLog,
    request: Event,
    *,
    reason: str,
    missing: list[str],
    type_mismatches: list[tuple[str, str, str]],
    unexpected: list[str],
    latency_ms: Optional[float] = None,
) -> Event:
    """
    Emit `tool.{name}.args_invalid` (ADR-013 §2.2).

    Emitted when the merged args (caller's +
    extractor's) do not validate against the Tool's
    `input_schema`. The event is consumed by the
    DLQ / replay path; the Tool is NOT invoked.

    The payload includes structured detail
    (`missing`, `type_mismatches`, `unexpected`)
    so an operator can see WHY validation failed
    without re-running the request.

    Raises an `Exception` if the EventLog rejects
    the append.
    """
    tool_name = tool_name_from_request(request)
    ctx = correlation_middleware.current()
    data: dict[str, str | list[str] | list[dict[str, str]] | float] = {
        "request_id": str(request.event_id),
        "tool": tool_name,
        "reason": reason,
        "missing": list(missing),
        "type_mismatches": [
            {"field": f, "expected": want, "got": got}
            for f, want, got in type_mismatches
        ],
        "unexpected": list(unexpected),
    }
    if latency_ms is not None:
        data["latency_ms"] = latency_ms
    e = Event.domain_from(
        agent_id=request.agent_id,
        type=ToolEventType.args_invalid(tool_name),
        data=data,
        correlation=ctx,
        causation_id=request.event_id,
    )
    append_result = await log.append(e)
    if append_result.is_err():
        raise Exception(f"Failed to append args_invalid: {append_result.err_value()}")
    return e


async def emit_failure(
    log: EventLog,
    request: Event,
    error_message: str,
    *,
    latency_ms: Optional[float] = None,
) -> Event:
    """
    Emit `tool.{name}.failed` with the error message.

    Raises an `Exception` if the EventLog rejects the append.
    """
    tool_name = tool_name_from_request(request)
    ctx = correlation_middleware.current()
    data: dict[str, str | float] = {
        "request_id": str(request.event_id),
        "tool": tool_name,
        "error": error_message,
    }
    if latency_ms is not None:
        data["latency_ms"] = latency_ms
    e = Event.domain_from(
        agent_id=request.agent_id,
        type=ToolEventType.failed(tool_name),
        data=data,
        correlation=ctx,
        causation_id=request.event_id,
    )
    append_result = await log.append(e)
    if append_result.is_err():
        raise Exception(f"Failed to append failure: {append_result.err_value()}")
    return e
