# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
10 — HTTP IntentRouter (ADR-012).

Demonstrates the framework's HTTP gateway: external
integrators (frontend, webhooks, mobile) call Tools and
Roles via HTTP. The router:

  1. authenticates the caller (X-API-Key);
  2. validates the request (pydantic);
  3. rejects unknown tools at the HTTP boundary
     (404, no event emitted);
  4. emits a deterministic `tool.{name}.requested`
     event into the EventLog;
  5. returns 202 + status_url for the client to poll.

The framework's core (ToolInvoker, ToolRegistry,
EventLog) is unchanged. The router is **one** HTTP
adapter; CLI, batch, and replay still work without it.

Run with Redis available:

    docker run -d -p 6379:6379 --name fmh-redis redis
    pip install 'kntgraph[api]'
    uvicorn examples.factory:app --host 0.0.0.0 --port 8000

This example script does NOT spin a uvicorn server —
it shows the wiring and exercises the request flow
end-to-end via FastAPI's `TestClient`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from kntgraph.api import create_app
from kntgraph.api.auth import AuthError
from kntgraph.core.event import Event
from kntgraph.core.result import Err, Ok, Result
from kntgraph.agents.tools.protocol import Tool, ToolRegistry


# ---------------------------------------------------------------------------
# A minimal in-process EventLog. Production code uses
# the framework's `EventLog` (Redis Streams); for the
# example we wire one so the test client works without
# Docker.
# ---------------------------------------------------------------------------


class InMemoryEventLog:
    """Drop-in stand-in for `EventLog` for examples."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def append(self, event: Event) -> Result:
        self.events.append(event)
        return Ok(None)

    async def read(self, agent_id: str) -> list[Event]:
        return [e for e in self.events if e.agent_id == agent_id]


# ---------------------------------------------------------------------------
# A minimal tool. In production you'd import from
# `fmh_agents.tools` or your own adapter module.
# ---------------------------------------------------------------------------


class EchoTool(Tool):
    """A toy Tool that echoes its input."""

    name = "echo"
    description = "Echoes the input back to the caller."
    input_schema: dict = {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
    }

    async def invoke(self, *, idempotency_key: str, **kwargs) -> dict:
        return {"echo": kwargs.get("msg", "")}


# ---------------------------------------------------------------------------
# Auth verifier that always accepts one key. The
# production `RedisAPIKeyVerifier` looks the key up in
# Redis; this stand-in lets the example run without
# any external state.
# ---------------------------------------------------------------------------


class StaticAPIKeyVerifier:
    """
    Demo verifier: maps one key to one Principal.

    In production use ``RedisAPIKeyVerifier`` (which
    returns a full Principal from the binding table).
    This stand-in mirrors the contract: it returns
    ``Result[Principal, AuthError]`` rather than a
    bare agent_id, so the EventLog tenant check
    (ADR-017) sees a Principal and not a string.
    """

    def __init__(self, key: str, agent_id: str) -> None:
        from kntgraph.security import Principal, Role

        self._key = key
        # Use ``.`` (not ``/``) as the tenant separator:
        # the B2 regex forbids ``/`` in agent_id. If the
        # agent_id has no separator, the agent_id itself
        # is the tenant (legacy single-tenant convention).
        tenant_id = agent_id.partition(".")[0] or agent_id
        self._principal = Principal(
            agent_id=agent_id,
            role=Role.agent,
            tenant_id=tenant_id,
            key_id="demo",
        )

    async def verify(self, api_key: str):
        if not api_key:
            return Err(AuthError("missing", "X-API-Key required"))
        if api_key != self._key:
            return Err(AuthError("forbidden", "key not recognised"))
        return Ok(self._principal)


# ---------------------------------------------------------------------------
# Wire the app and exercise the flow.
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    registry = ToolRegistry()
    registry.register(EchoTool())
    log = InMemoryEventLog()
    verifier = StaticAPIKeyVerifier(key="demo-key", agent_id="demo-agent")
    app = create_app(log=log, registry=registry, verifier=verifier)
    return TestClient(app), log


def main() -> None:
    client, log = build_app()

    # 1. Health check.
    r = client.get("/healthz")
    print(f"[1] health: {r.status_code} {r.json()}")

    # 2. List registered tools.
    r = client.get(
        "/agents/demo-agent/tools",
        headers={"X-API-Key": "demo-key"},
    )
    print(f"[2] tools: {r.status_code} {r.json()}")

    # 3. Reject unknown tool (404, no event).
    r = client.post(
        "/agents/demo-agent/intents",
        headers={"X-API-Key": "demo-key"},
        json={
            "type": "tool.invoke",
            "tool": "ghost.tool",
            "args": {"x": 1},
        },
    )
    print(f"[3] ghost tool: {r.status_code} {r.json()}")
    assert len(log.events) == 0, "404 must NOT emit events"

    # 4. Accept a real tool call.
    r = client.post(
        "/agents/demo-agent/intents",
        headers={"X-API-Key": "demo-key"},
        json={
            "type": "tool.invoke",
            "tool": "echo",
            "args": {"msg": "hello, gateway"},
        },
    )
    print(f"[4] echo: {r.status_code} {r.json()}")
    assert r.status_code == 202
    assert len(log.events) == 1
    accepted = log.events[0]
    print(f"[5] event: type={accepted.event_type}")
    print(f"    data={accepted.data}")

    # 6. Idempotency: same body → same event_id.
    body = {
        "type": "tool.invoke",
        "tool": "echo",
        "args": {"msg": "hello, gateway"},
    }
    r1 = client.post(
        "/agents/demo-agent/intents",
        headers={"X-API-Key": "demo-key"},
        json=body,
    )
    r2 = client.post(
        "/agents/demo-agent/intents",
        headers={"X-API-Key": "demo-key"},
        json=body,
    )
    assert r1.json()["event_id"] == r2.json()["event_id"], (
        "Two identical requests must produce the same event_id"
    )
    print(f"[6] idempotency: same event_id {r1.json()['event_id']}")

    # 7. Replay the request: the EventLog would dedupe
    #    (in production). The HTTP layer can't know
    #    whether the request is a duplicate; the dedup
    #    is the EventLog's job.
    print("[7] HTTP layer is a producer; the EventLog dedupes.")

    print()
    print("To run the server instead of the test client:")
    print("  uvicorn examples.factory:app --port 8000")
    print("    (you'll need to write a `factory.py` that")
    print("     wires the same `create_app` call.)")


if __name__ == "__main__":
    main()
