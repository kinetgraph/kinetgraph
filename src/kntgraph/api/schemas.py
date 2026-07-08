# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic models for the HTTP gateway (ADR-012).

These models are the wire format. The router validates
incoming requests against `IntentRequest` and serialises
responses through `IntentResponse` and `StatusResponse`.

Validation is intentional: an `IntentRequest` that names
a tool or role not registered in the `ToolRegistry` for
the caller's `agent_id` is rejected at the HTTP boundary
(404), without ever entering the `EventLog`. This keeps
the log clean of attempts that could never succeed.
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _check_json_value(value: Any) -> None:
    """
    Recursively assert that ``value`` is a valid
    :data:`JsonValue` (scalar, dict of JsonValue, list of
    JsonValue). Raises ``ValueError`` on the first
    non-JSON-serialisable leaf. Used by
    :class:`IntentRequest` to validate ``args`` at the
    HTTP boundary so the downstream ``Event.data``
    invariant (typed ``Mapping[str, JsonValue]``) is
    never violated by an untrusted client.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, MappingABC):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(
                    f"JSON object keys must be str, got {type(k).__name__}"
                )
            _check_json_value(v)
        return
    if isinstance(value, list):
        for v in value:
            _check_json_value(v)
        return
    raise ValueError(f"value is not JSON-serialisable: type {type(value).__name__}")


class IntentRequest(BaseModel):
    """
    The shape of an external call to a Tool or Role.

    `type` discriminates between the two flows. The router
    looks up the corresponding entry in the `ToolRegistry`
    or `RoleRegistry`; missing entries return 404.

    `args` is forwarded as the `data` of the resulting
    `tool.{name}.requested` event. The Tool/Roles see the
    same kwargs they would see in a system-driven flow.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool.invoke", "role.invoke"]
    tool: Optional[str] = Field(
        default=None,
        description=(
            "Tool name. Required when `type='tool.invoke'`. "
            "Convention: `provider.action` in lower-snake-case "
            "(e.g. 'invoice.issue')."
        ),
    )
    role: Optional[str] = Field(
        default=None,
        description=(
            "Role name. Required when `type='role.invoke'`. "
            "Examples: 'planner', 'summarizer'."
        ),
    )
    args: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Forwarded as the `data` payload of the "
            "resulting `tool.{name}.requested` event."
        ),
    )

    @field_validator("args")
    @classmethod
    def _validate_args_json_value(cls, value: dict[str, Any]) -> dict[str, Any]:
        """
        The wire type is `dict[str, Any]` so the OpenAPI
        schema stays permissive, but the runtime value
        must be JSON-serialisable: ``args`` flows into
        ``Event.data`` (typed ``Mapping[str, JsonValue]``)
        and gets ``json.dumps``'d on the wire. Failing
        closed at the HTTP boundary prevents
        ``TypeError: Object of type X is not JSON
        serializable`` from escaping into the
        ``EventLog.append`` call.
        """
        _check_json_value(value)
        return value


class IntentResponse(BaseModel):
    """
    The 202 Accepted response. The client polls
    `status_url` for the terminal outcome.
    """

    event_id: str = Field(
        description=(
            "Deterministic UUID5 of "
            "(agent_id, type, tool_or_role, args, "
            "idempotency_key_or_empty). Two identical "
            "requests produce the same `event_id`; the "
            "EventLog dedupes."
        )
    )
    status: Literal["accepted"] = "accepted"
    status_url: str = Field(
        description=(
            "GET endpoint that long-polls the EventLog "
            "for the terminal event (`.completed` or "
            "`.failed`) with `causation_id == event_id`."
        )
    )


class StatusResponse(BaseModel):
    """
    Outcome of a previously-accepted intent.

    `pending` — no terminal event yet. The router is
    blocking on the EventLog; the client should keep
    polling (or use SSE in a future ADR).

    `completed` / `failed` — terminal. The `result` /
    `error` is the same payload that the Tool/Roles
    published.

    `rejected` — the router rejected the request AFTER
    emitting the event (rare race; see ADR-012 §2.3).
    """

    status: Literal["pending", "completed", "failed", "rejected"]
    event_id: str
    result: Optional[Any] = None
    error: Optional[str] = None


class RejectionResponse(BaseModel):
    """
    404 / 401 / 403 response shape. No `event_id` is
    returned because no event was emitted.
    """

    error: str
    detail: Optional[str] = None


class ToolDescriptor(BaseModel):
    """
    A `Tool` as exposed by the gateway. Mirrors
    `kntgraph.agents.tools.descriptors.ToolDescriptor` for
    the `/tools` listing endpoint.
    """

    name: str
    description: str
    input_schema_json: str


class HealthResponse(BaseModel):
    """Liveness/readiness probe."""

    status: Literal["ok"] = "ok"
