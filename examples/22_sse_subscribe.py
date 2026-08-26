# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
22 — SSE subscribe to a request's result.

Demonstrates the new event-driven HTTP endpoint
``GET /agents/{agent_id}/events`` (ADR-065 §3.1 /
§4.1). The endpoint is the event-driven replacement
for the legacy long-poll
``GET /agents/{agent_id}/events/{event_id}/status``
endpoint.

The example runs against the framework's
``create_app`` factory with the in-process
``FakeEventLog`` (the production code path is
identical; Redis-backed EventLog just stores the
events in Redis Streams instead of an in-memory
list). Three scenarios:

  1. **Subscribe to the result of one POST.**
     Post an intent, capture the ``event_id`` from
     the 202 response, then open an SSE connection
     filtered by ``causation_id=<event_id>``. Read
     the SSE frames and print each event as it
     lands.
  2. **Reconnect with ``Last-Event-ID``.** After the
     server has emitted some frames, drop the
     connection. Reconnect with ``Last-Event-ID``
     set to the last ``id`` you received; the
     server replays the gap automatically.
  3. **The legacy endpoint emits
     ``DeprecationWarning``.** Hit the long-poll
     ``/status`` endpoint and confirm the
     ``DeprecationWarning`` is raised (the framework
     keeps the endpoint for one minor cycle so
     callers have time to migrate).

The example does NOT spin a uvicorn server — it
exercises the request flow end-to-end via FastAPI's
``TestClient`` (the SSE endpoint is exercised via
``TestClient.stream``).

Pre-requisites
--------------

  - ``pip install 'kntgraph[api]'`` for FastAPI.
  - ``pip install httpx`` (FastAPI's TestClient
    transport).

Run
---

    python examples/22_sse_subscribe.py
"""

from __future__ import annotations

import json
import warnings

from fastapi.testclient import TestClient

from kntgraph.agents.tools.protocol import Tool, ToolRegistry
from kntgraph.api import create_app
from kntgraph.api.auth import AuthError
from kntgraph.core.event import Event
from kntgraph.core.result import Err, Ok, Result


# ---------------------------------------------------------------------------
# In-process EventLog with cursor support. Mirrors the
# test fixture in `tests/unit/api/_fake_log.py`; lives
# here so the example is self-contained.
# ---------------------------------------------------------------------------


class InMemoryEventLog:
    """In-process EventLog for examples.

    Mirrors the production ``EventLog.read(start, end)``
    contract: ``start="-"`` reads from the beginning;
    ``start="("`` reads strictly after the cursor
    (Redis Stream exclusive-id convention).
    """

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def append(self, event: Event) -> Result:
        self.events.append(event)
        return Ok(None)

    async def read(
        self,
        agent_id: str,
        start: str = "-",
        end: str = "+",
        count: int | None = None,
    ) -> list[Event]:
        out = [e for e in self.events if e.agent_id == agent_id]
        if start.startswith("("):
            cursor_id = start[1:]
            out = [e for e in out if str(e.event_id) > cursor_id]
        if count is not None:
            out = out[:count]
        return out


# ---------------------------------------------------------------------------
# A minimal echo tool. Production code would import
# from ``fmh_agents.tools`` or register an
# ``@tool_worker``-decorated class via
# ``WorkerManager``. The example uses the legacy
# ``Tool`` Protocol to keep the wiring minimal.
# ---------------------------------------------------------------------------


class EchoTool(Tool):
    """Echoes the input back to the caller."""

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
# production ``RedisAPIKeyVerifier`` looks the key up
# in Redis; this stand-in lets the example run without
# any external state.
# ---------------------------------------------------------------------------


class StaticAPIKeyVerifier:
    """Demo verifier: maps one key to one Principal.

    Returns ``Result[Principal, AuthError]`` rather
    than a bare agent_id, so the EventLog tenant
    check (ADR-017) sees a Principal and not a
    string.
    """

    def __init__(self, key: str, agent_id: str) -> None:
        from kntgraph.security import Principal, Role

        self._key = key
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
# Build the test client. Use the test hook that
# closes the SSE generator after the first batch of
# events has been yielded, so the test client
# receives a complete response (the production code
# never sets this; the SSE endpoint streams forever
# until the client disconnects).
# ---------------------------------------------------------------------------


def build_client() -> tuple[TestClient, InMemoryEventLog]:
    registry = ToolRegistry()
    registry.register(EchoTool())
    log = InMemoryEventLog()
    verifier = StaticAPIKeyVerifier(key="demo-key", agent_id="demo-agent")
    app = create_app(log=log, registry=registry, verifier=verifier)
    return TestClient(app), log


def _activate_test_hook() -> None:
    """Set the SSE test hook so the generator closes
    after the first batch of events has been yielded
    (lets the TestClient collect a complete response).
    Production code never sets this."""
    from kntgraph.api.intent_router import routes

    routes._sse_test_close_after_first_batch = True


def _read_sse_frames(response) -> list[dict]:
    """Read the SSE response body and parse the frames.

    Each frame is ``event: <type>\\nid: <event_id>\\ndata:
    <json>\\n\\n``. Returns a list of ``{"event", "id",
    "data"}`` dicts; ``data`` is the parsed JSON
    payload.
    """
    buf = b""
    for chunk in response.iter_bytes():
        buf += chunk
    out: list[dict] = []
    for frame in buf.decode("utf-8").split("\n\n"):
        frame = frame.strip()
        if not frame or frame.startswith(":"):
            # Heartbeat comment or empty frame.
            continue
        record: dict = {}
        data_lines: list[str] = []
        for line in frame.split("\n"):
            if line.startswith(":") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            value = value.lstrip()
            if key == "data":
                data_lines.append(value)
            else:
                record[key] = value
        if data_lines:
            record["data"] = json.loads("".join(data_lines))
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# Scenarios.
# ---------------------------------------------------------------------------


def scenario_subscribe_to_one_request() -> None:
    """Scenario 1: subscribe to the result of one POST.

    Post an intent, capture the ``event_id`` from the
    202 response, then simulate the worker's
    ``tool.<name>.completed`` event (the worker would
    emit it in a later tick). Open an SSE connection
    filtered by ``causation_id=<event_id>``; the
    server replays the completion event as a frame.

    The demo doesn't run a real worker pool; it
    synthesises the completion event directly into
    the EventLog so the SSE stream has something to
    surface.
    """
    print("=" * 70)
    print("Scenario 1: subscribe to the result of one POST")
    print("=" * 70)
    client, log = build_client()

    # 1. POST the intent. The server emits a
    # `tool.echo.requested` event into the EventLog and
    # returns 202 + event_id.
    r = client.post(
        "/agents/demo-agent/intents",
        headers={"X-API-Key": "demo-key"},
        json={
            "type": "tool.invoke",
            "tool": "echo",
            "args": {"msg": "hello, SSE"},
        },
    )
    assert r.status_code == 202
    event_id = r.json()["event_id"]
    print(f"[1] POST /intents → 202, event_id={event_id}")

    # 2. Simulate the worker emitting
    # `tool.echo.completed` (in production, the
    # ``WorkerManager`` does this in a later tick). The
    # completion's ``causation_id`` is the request's
    # ``event_id``, so a subscriber filtered by that
    # ``causation_id`` will receive the completion.
    from kntgraph.core.event import CorrelationContext

    log.events.append(
        Event.domain_from(
            agent_id="demo-agent",
            type="tool.echo.completed",
            data={
                "request_id": event_id,
                "result": {"echo": "hello, SSE"},
            },
            correlation=CorrelationContext.new(
                correlation_id=f"corr-{event_id}",
            ),
            event_id="evt-completed-1",
            causation_id=event_id,
        )
    )

    # 3. Subscribe to the SSE stream filtered by the
    # request's event_id. Each frame carries the
    # canonical payload (event, id, data).
    with client.stream(
        "GET",
        "/agents/demo-agent/events",
        params={"causation_id": event_id},
        headers={"X-API-Key": "demo-key"},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        assert r.headers["x-accel-buffering"] == "no"
        frames = _read_sse_frames(r)
    print(f"[2] SSE /events → {len(frames)} frame(s)")
    for i, frame in enumerate(frames, start=1):
        ev_type = frame["event"]
        ev_id = frame["id"]
        print(f"    [{i}] type={ev_type!r} id={ev_id!r}")


def scenario_reconnect_with_last_event_id() -> None:
    """Scenario 2: reconnect with cursor advance.

    After some events have been streamed, drop the
    connection. Reconnect passing the last ``id`` as
    the ``from_`` query parameter; the server replays
    only the events strictly after that cursor.

    Browser EventSource does this automatically via
    the SSE standard's ``Last-Event-ID`` header
    (ADR-065 §4.3). Programmatic clients using
    ``httpx`` (or any HTTP client) pass it
    explicitly as the ``from_`` query parameter —
    the gateway does not auto-translate
    ``Last-Event-ID`` to ``from_`` (the gateway
    exposes the cursor through the URL so the
    contract is the same for browser clients and
    programmatic clients).
    """
    print()
    print("=" * 70)
    print("Scenario 2: reconnect with cursor advance")
    print("=" * 70)
    client, log = build_client()

    # 1. Seed 5 events so the stream has something to
    # replay on reconnect.
    from kntgraph.core.event import CorrelationContext

    for i in range(5):
        log.events.append(
            Event.domain_from(
                agent_id="demo-agent",
                type=f"tool.echo.step{i}",
                data={"i": i},
                correlation=CorrelationContext.new(),
                event_id=f"evt-{i:04d}",
            )
        )

    # 2. First connection: full replay (``from_=0``).
    with client.stream(
        "GET",
        "/agents/demo-agent/events",
        params={"from_": "0"},
        headers={"X-API-Key": "demo-key"},
    ) as r:
        assert r.status_code == 200
        frames = _read_sse_frames(r)
    assert len(frames) == 5, f"expected 5 frames, got {len(frames)}"
    last_id = frames[-1]["id"]
    print(f"[1] first connection → {len(frames)} frame(s), last_id={last_id!r}")

    # 3. Reconnect passing ``from_=<last_id>``. The
    # server replays only events strictly after the
    # cursor (Redis Stream "exclusive" semantics, the
    # ``start="("`` convention). With the test hook
    # closing after the first batch, the reconnect
    # yields 0 events (the FakeEventLog has none
    # beyond the 5 seeded ones).
    with client.stream(
        "GET",
        "/agents/demo-agent/events",
        params={"from_": last_id},
        headers={"X-API-Key": "demo-key"},
    ) as r:
        assert r.status_code == 200
        frames = _read_sse_frames(r)
    print(f"[2] reconnect with from_={last_id!r} → {len(frames)} frame(s)")
    print("    (no new events past the cursor; gap-replay honoured)")


def scenario_legacy_status_emits_deprecation() -> None:
    """Scenario 3: the legacy long-poll emits
    ``DeprecationWarning``.

    The framework keeps the legacy
    ``/events/{event_id}/status`` endpoint for one
    minor cycle. Hitting it produces a
    ``DeprecationWarning`` with a clear migration
    message (point at the SSE endpoint).
    """
    print()
    print("=" * 70)
    print("Scenario 3: legacy /status emits DeprecationWarning")
    print("=" * 70)
    client, _ = build_client()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        r = client.get(
            "/agents/demo-agent/events/some-pending-id/status",
            headers={"X-API-Key": "demo-key"},
        )
    assert r.status_code == 200, "legacy endpoint still works"
    deprecations = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    assert deprecations, "expected DeprecationWarning"
    print(f"[1] /status still returns 200")
    print(f"[2] DeprecationWarning: {deprecations[0].message}")


def main() -> None:
    _activate_test_hook()
    scenario_subscribe_to_one_request()
    scenario_reconnect_with_last_event_id()
    scenario_legacy_status_emits_deprecation()
    print()
    print("=" * 70)
    print("To run the server instead of the test client:")
    print("  uvicorn examples.factory:app --port 8000")
    print("    (you'll need to write a `factory.py` that")
    print("     wires the same `create_app` call.)")
    print()
    print("Full docs: docs/sse_subscribe.md")


if __name__ == "__main__":
    main()