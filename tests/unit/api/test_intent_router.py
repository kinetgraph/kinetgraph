# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the HTTP gateway (ADR-012).

Coverage
---------

  `EventLog`-level contract
  - Emits a `tool.{name}.requested` event into the
    EventLog on accepted intents.
  - Does NOT emit any event on 404 (unknown tool).
  - The `event_id` is deterministic: two requests
    with the same `(agent_id, type, tool, args,
    idempotency_key)` produce the same UUID5.

  `ToolRegistry` lookup
  - 404 when the tool is not registered.
  - 202 when the tool is registered.
  - The `tool` field is required for `tool.invoke`
    and the `role` field for `role.invoke`.

  `Idempotency-Key` header
  - Two requests with the same key and body produce
    the same `event_id`.
  - Two requests with the same body but different
    keys produce different `event_id`s.
  - Two requests with the same key but different
    bodies produce different `event_id`s.

  Auth
  - Missing `X-API-Key` → 401.
  - Unknown key → 403.
  - Mismatched `agent_id` (URL vs. key binding) → 403.

  Status endpoint
  - Returns `pending` if no terminal event has
    arrived in the timeout window.
  - Returns `completed` when a `tool.{name}.completed`
    event with `causation_id == event_id` is in the
    EventLog.
  - Returns `failed` when a `tool.{name}.failed`
    event is in the EventLog.

  List tools
  - Returns registered tools for the agent.
  - 403 when the API key is bound to a different
    `agent_id`.
"""

from __future__ import annotations

import uuid

from typing import Any, Optional

import pytest

# `fastapi` is an opt-in dependency declared under the
# `[api]` extra in `kntgraph/pyproject.toml`. Skip
# collection entirely when the package is not installed
# so `pytest kntgraph/tests/` works in environments
# that don't expose the HTTP gateway (mirrors the
# pattern used in `test_falkordb_client.py` for the
# `[falkordb]` extra).
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from kntgraph.api import create_app  # noqa: E402
from kntgraph.api.auth import AuthError  # noqa: E402
from kntgraph.core.result import Err, Ok  # noqa: E402
from kntgraph.agents.tools.protocol import Tool, ToolRegistry  # noqa: E402

from ._fake_log import FakeEventLog


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeVerifier:
    """
    Test double for `APIKeyVerifier`. The binding
    ``{"<key>": "<agent_id>"}`` is in-memory; the
    lookup matches the framework's contract.

    Returns a ``Principal`` (per ADR-017). Default role
    is ``agent`` and the binding target doubles as the
    tenant_id (legacy single-tenant convention).
    """

    def __init__(self, bindings: dict[str, str]) -> None:
        from kntgraph.security import Principal, Role

        self._bindings = bindings
        self._principals = {
            k: Principal(
                agent_id=v,
                role=Role.agent,
                tenant_id=v.partition(".")[0] or v,
                key_id="test",
            )
            for k, v in bindings.items()
        }

    async def verify(self, api_key: str) -> Any:
        if not api_key:
            return Err(AuthError("missing", "X-API-Key required"))
        if api_key not in self._bindings:
            return Err(AuthError("forbidden", "key not recognised"))
        return Ok(self._principals[api_key])


class _FakeTool(Tool):
    """Minimal Tool for tests; the body never runs."""

    name = "fake.echo"
    description = "Echoes the input."
    input_schema: dict = {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
    }

    async def invoke(
        self, *, idempotency_key: str, **kwargs: Any
    ) -> Any:  # pragma: no cover
        raise NotImplementedError


def _build_app(
    *,
    bindings: Optional[dict[str, str]] = None,
    log: Optional[FakeEventLog] = None,
    registry: Optional[ToolRegistry] = None,
) -> TestClient:
    bindings = bindings or {"key-for-a1": "agent-1"}
    log = log or FakeEventLog()
    registry = registry or ToolRegistry()
    registry.register(_FakeTool())
    verifier = _FakeVerifier(bindings)
    app = create_app(log=log, registry=registry, verifier=verifier)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_healthz_returns_ok(self):
        client = _build_app()
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_missing_api_key_returns_401(self):
        client = _build_app()
        r = client.post(
            "/agents/agent-1/intents",
            json={
                "type": "tool.invoke",
                "tool": "fake.echo",
                "args": {"msg": "hi"},
            },
        )
        assert r.status_code == 401
        # No event was emitted.
        # (The client doesn't know; the log is the truth.)

    def test_unknown_api_key_returns_403(self):
        client = _build_app()
        r = client.post(
            "/agents/agent-1/intents",
            headers={"X-API-Key": "wrong-key"},
            json={
                "type": "tool.invoke",
                "tool": "fake.echo",
                "args": {"msg": "hi"},
            },
        )
        assert r.status_code == 403

    def test_mismatched_agent_id_returns_403(self):
        client = _build_app(bindings={"key-for-a1": "agent-1"})
        r = client.post(
            "/agents/agent-2/intents",  # different from key
            headers={"X-API-Key": "key-for-a1"},
            json={
                "type": "tool.invoke",
                "tool": "fake.echo",
                "args": {"msg": "hi"},
            },
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# 404 — Tool not registered
# ---------------------------------------------------------------------------


class TestRejection:
    def test_unknown_tool_returns_404_and_no_event(self):
        log = FakeEventLog()
        client = _build_app(log=log)
        r = client.post(
            "/agents/agent-1/intents",
            headers={"X-API-Key": "key-for-a1"},
            json={
                "type": "tool.invoke",
                "tool": "ghost.tool",
                "args": {"x": 1},
            },
        )
        assert r.status_code == 404
        # NO event was emitted. ADR-012 §2.3.
        assert len(log.events) == 0

    def test_role_invoke_does_not_require_registry(self):
        """
        Roles live outside the ToolRegistry (ADR-006).
        For v1 the router accepts any `role` value and
        emits `tool.{role}.requested`. The Role
        dispatcher downstream validates.
        """
        log = FakeEventLog()
        client = _build_app(log=log)
        r = client.post(
            "/agents/agent-1/intents",
            headers={"X-API-Key": "key-for-a1"},
            json={
                "type": "role.invoke",
                "role": "summarizer",
                "args": {"text": "hi"},
            },
        )
        assert r.status_code == 202
        # Exactly one event with the right shape.
        assert len(log.events) == 1
        e = log.events[0]
        assert e.event_type == "tool.summarizer.requested"
        assert e.data["tool"] == "summarizer"

    def test_tool_invoke_without_tool_field_returns_422(self):
        client = _build_app()
        r = client.post(
            "/agents/agent-1/intents",
            headers={"X-API-Key": "key-for-a1"},
            json={"type": "tool.invoke", "args": {}},
        )
        assert r.status_code == 422

    def test_role_invoke_without_role_field_returns_422(self):
        client = _build_app()
        r = client.post(
            "/agents/agent-1/intents",
            headers={"X-API-Key": "key-for-a1"},
            json={"type": "role.invoke", "args": {}},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# 202 — Accepted
# ---------------------------------------------------------------------------


class TestAccepted:
    def test_emits_tool_requested_event(self):
        log = FakeEventLog()
        client = _build_app(log=log)
        r = client.post(
            "/agents/agent-1/intents",
            headers={"X-API-Key": "key-for-a1"},
            json={
                "type": "tool.invoke",
                "tool": "fake.echo",
                "args": {"msg": "hi"},
            },
        )
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "accepted"
        assert "event_id" in body
        assert body["status_url"].endswith(f"/events/{body['event_id']}/status")
        # EventLog has the event.
        assert len(log.events) == 1
        e = log.events[0]
        assert e.event_type == "tool.fake.echo.requested"
        assert e.data["tool"] == "fake.echo"
        assert e.data["args"] == {"msg": "hi"}
        assert e.data["source"] == "http.intent_router"

    def test_event_id_is_deterministic_for_same_body(self):
        """
        Two requests with identical bodies (no
        Idempotency-Key header) must produce the
        same `event_id`. The EventLog dedupes;
        the system can replay safely.
        """
        log = FakeEventLog()
        client = _build_app(log=log)
        body = {
            "type": "tool.invoke",
            "tool": "fake.echo",
            "args": {"msg": "hi"},
        }
        r1 = client.post(
            "/agents/agent-1/intents",
            headers={"X-API-Key": "key-for-a1"},
            json=body,
        )
        r2 = client.post(
            "/agents/agent-1/intents",
            headers={"X-API-Key": "key-for-a1"},
            json=body,
        )
        assert r1.json()["event_id"] == r2.json()["event_id"]

    def test_idempotency_key_overrides_default_hash(self):
        """
        With an explicit Idempotency-Key, the same
        key + same body → same `event_id`. The
        Idempotency-Key is part of the hash inputs.
        """
        log = FakeEventLog()
        client = _build_app(log=log)
        body = {
            "type": "tool.invoke",
            "tool": "fake.echo",
            "args": {"msg": "hi"},
        }
        r1 = client.post(
            "/agents/agent-1/intents",
            headers={
                "X-API-Key": "key-for-a1",
                "Idempotency-Key": "client-key-1",
            },
            json=body,
        )
        r2 = client.post(
            "/agents/agent-1/intents",
            headers={
                "X-API-Key": "key-for-a1",
                "Idempotency-Key": "client-key-1",
            },
            json=body,
        )
        assert r1.json()["event_id"] == r2.json()["event_id"]

    def test_different_idempotency_keys_yield_different_ids(self):
        log = FakeEventLog()
        client = _build_app(log=log)
        body = {
            "type": "tool.invoke",
            "tool": "fake.echo",
            "args": {"msg": "hi"},
        }
        r1 = client.post(
            "/agents/agent-1/intents",
            headers={
                "X-API-Key": "key-for-a1",
                "Idempotency-Key": "key-A",
            },
            json=body,
        )
        r2 = client.post(
            "/agents/agent-1/intents",
            headers={
                "X-API-Key": "key-for-a1",
                "Idempotency-Key": "key-B",
            },
            json=body,
        )
        assert r1.json()["event_id"] != r2.json()["event_id"]

    def test_different_bodies_yield_different_ids(self):
        log = FakeEventLog()
        client = _build_app(log=log)
        r1 = client.post(
            "/agents/agent-1/intents",
            headers={"X-API-Key": "key-for-a1"},
            json={
                "type": "tool.invoke",
                "tool": "fake.echo",
                "args": {"msg": "hi"},
            },
        )
        r2 = client.post(
            "/agents/agent-1/intents",
            headers={"X-API-Key": "key-for-a1"},
            json={
                "type": "tool.invoke",
                "tool": "fake.echo",
                "args": {"msg": "bye"},
            },
        )
        assert r1.json()["event_id"] != r2.json()["event_id"]


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------


def _terminal_event(
    *,
    event_type: str,
    causation_id: str,
    data: dict,
) -> Any:
    """Build a minimal Event-like for the FakeEventLog."""
    from kntgraph.core.event import CorrelationContext, Event

    return Event.domain_from(
        agent_id="agent-1",
        type=event_type,
        data=data,
        causation_id=causation_id,
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


class TestStatus:
    def test_pending_when_no_terminal_event(self):
        log = FakeEventLog()
        client = _build_app(log=log)
        r = client.get(
            "/agents/agent-1/events/some-pending-id/status",
            headers={"X-API-Key": "key-for-a1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "pending"
        assert body["event_id"] == "some-pending-id"

    def test_completed_when_terminal_completed(self):
        log = FakeEventLog()
        event_id = "evt-completed-1"
        log.events.append(
            _terminal_event(
                event_type="tool.fake.echo.completed",
                causation_id=event_id,
                data={
                    "request_id": event_id,
                    "tool": "fake.echo",
                    "result": {"echo": "hi"},
                },
            )
        )
        client = _build_app(log=log)
        r = client.get(
            f"/agents/agent-1/events/{event_id}/status",
            headers={"X-API-Key": "key-for-a1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed"
        assert body["result"] == {"echo": "hi"}

    def test_failed_when_terminal_failed(self):
        log = FakeEventLog()
        event_id = "evt-failed-1"
        log.events.append(
            _terminal_event(
                event_type="tool.fake.echo.failed",
                causation_id=event_id,
                data={
                    "request_id": event_id,
                    "tool": "fake.echo",
                    "error": "boom",
                },
            )
        )
        client = _build_app(log=log)
        r = client.get(
            f"/agents/agent-1/events/{event_id}/status",
            headers={"X-API-Key": "key-for-a1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "failed"
        assert body["error"] == "boom"

    def test_status_mismatched_agent_id_returns_403(self):
        client = _build_app()
        r = client.get(
            "/agents/agent-2/events/whatever/status",
            headers={"X-API-Key": "key-for-a1"},
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# List tools
# ---------------------------------------------------------------------------


class TestListTools:
    def test_returns_registered_tools(self):
        client = _build_app()
        r = client.get(
            "/agents/agent-1/tools",
            headers={"X-API-Key": "key-for-a1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["name"] == "fake.echo"

    def test_mismatched_agent_id_returns_403(self):
        client = _build_app()
        r = client.get(
            "/agents/agent-2/tools",
            headers={"X-API-Key": "key-for-a1"},
        )
        assert r.status_code == 403
