# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the SSE subscribe endpoint
(ADR-065 §3.1 / §4.1).

The endpoint (``GET /agents/{agent_id}/events``) is the
event-driven replacement for the long-poll
``GET /agents/{agent_id}/events/{event_id}/status``
endpoint. It streams the agent's EventLog to the
client as Server-Sent Events:

  - Each event carries the canonical payload
    (``event: <type>``, ``id: <event_id>``,
    ``data: <json>``).
  - The client filters by ``causation_id`` (one
    specific request) or ``event_class`` (one slice
    of the stream).
  - The server keeps the connection open and
    poll-and-yields new events as they land.
  - Disconnect / reconnect semantics rely on the
    SSE standard ``Last-Event-ID`` header (the
    server translates it to the cursor for the
    next read).

The poll-and-yield implementation is good enough
for v0.14: 100 ms poll cadence (matches the legacy
long-poll cadence), no real Redis Pub/Sub
subscription. The endpoint's contract is the
same regardless of the internal transport; when
the volume justifies it, the internals swap to a
real subscribe primitive without changing the
public surface.

Test hook
---------

The generator uses the module-level
``_sse_test_close_after_first_batch`` flag in
``routes.py`` to close after the first batch of
events has been yielded. Production never sets
this; tests set it to drive the generator
end-to-end without blocking on the long-lived
poll loop. The flag is reset after each test.
"""

from __future__ import annotations

import json

import pytest


pytest.importorskip("fastapi")


def _build_app_with_log(log):
    """Build a FastAPI app wired to ``log`` so
    tests can append events and observe the SSE
    generator's output.
    """
    from kntgraph.api import create_app
    from kntgraph.api.auth import AuthError
    from kntgraph.core.result import Err, Ok
    from kntgraph.agents.tools.protocol import Tool
    from kntgraph.security import Principal, PrincipalLevel
    from kntgraph.tools.registry import ToolRegistry

    class _FakeTool(Tool):
        name = "fake.echo"
        description = "Echoes the input."
        input_schema: dict = {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
        }

        async def invoke(self, *, idempotency_key: str, **kwargs):
            raise NotImplementedError

    class _FakeVerifier:
        def __init__(self, bindings):
            self._bindings = bindings
            self._principals = {
                k: Principal(
                    agent_id=v,
                    level=PrincipalLevel.agent,
                    tenant_id=v.partition(".")[0] or v,
                    key_id="test",
                )
                for k, v in bindings.items()
            }

        async def verify(self, api_key):
            if not api_key:
                return Err(AuthError("missing", "X-API-Key required"))
            if api_key not in self._bindings:
                return Err(AuthError("forbidden", "key not recognised"))
            return Ok(self._principals[api_key])

    registry = ToolRegistry()
    registry.register(_FakeTool())
    verifier = _FakeVerifier({"key-for-a1": "agent-1"})
    app = create_app(
        log=log,  # type: ignore[arg-type]
        registry=registry,
        verifier=verifier,
    )
    return app


def _seed_events(log, agent_id: str, n: int) -> list:
    """Append ``n`` synthetic domain events to
    ``log`` for ``agent_id`` and return them. The
    events have sequential event_ids so the cursor
    logic in the SSE endpoint can advance
    deterministically.
    """
    from kntgraph.core.event import CorrelationContext, Event

    seeded = []
    for i in range(n):
        ev = Event.domain_from(
            agent_id=agent_id,
            type=f"tool.fake.echo.step{i}",
            data={"i": i},
            correlation=CorrelationContext.new(
                correlation_id=f"corr-{i}",  # type: ignore[arg-type]
            ),
            event_id=f"evt-{i:04d}",  # type: ignore[arg-type]
        )
        log.events.append(ev)
        seeded.append(ev)
    return seeded


def _parse_sse_frames(body: bytes) -> list[dict]:
    """Parse an SSE response body into a list of
    ``{"event": ..., "id": ..., "data": <dict>}``
    records. SSE frames are separated by a
    blank line (``\\n\\n``); each frame is a
    sequence of ``key: value`` lines.
    """
    out: list[dict] = []
    for frame in body.decode("utf-8").split("\n\n"):
        frame = frame.strip()
        if not frame or frame.startswith(":"):
            # Heartbeat comment or empty frame.
            continue
        record: dict = {}
        data_lines: list[str] = []
        for line in frame.split("\n"):
            if line.startswith(":"):
                continue
            if ":" not in line:
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


@pytest.fixture
def close_after_first_batch():
    """Activate the SSE test hook for the
    duration of one test. The generator closes
    after the first batch of events has been
    yielded so the response terminates cleanly
    in the test runner.
    """
    from kntgraph.api.intent_router import routes

    routes._sse_test_close_after_first_batch = True
    try:
        yield
    finally:
        routes._sse_test_close_after_first_batch = False


class TestSseSubscribe:
    """End-to-end tests of the SSE subscribe
    endpoint via ``TestClient.stream``.

    The test hook closes the generator after the
    first batch of events so the response
    terminates cleanly.
    """

    def _collect(
        self,
        client,
        agent_id: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        n_frames: int = 3,
    ) -> bytes:
        """Drive the SSE stream and return the
        first ``n_frames`` worth of bytes. The
        generator closes via the test hook; the
        ``TestClient.stream`` context manager
        cleans up.
        """
        with client.stream(
            "GET",
            f"/agents/{agent_id}/events",
            params=params or {},
            headers=headers or {"X-API-Key": "key-for-a1"},
        ) as r:
            assert r.status_code == 200
            buf = b""
            for chunk in r.iter_bytes():
                buf += chunk
                if buf.count(b"\n\n") >= n_frames:
                    break
        return buf

    def test_subscribe_from_0_streams_all_events(self, close_after_first_batch):
        """A subscriber connecting with no cursor
        (``from_=0``) gets every event currently
        in the agent's log, framed as SSE."""
        from fastapi.testclient import TestClient

        from ._fake_log import FakeEventLog

        log = FakeEventLog()
        app = _build_app_with_log(log)
        _seed_events(log, "agent-1", 3)
        client = TestClient(app)
        buf = self._collect(client, "agent-1", n_frames=3)
        frames = _parse_sse_frames(buf)
        assert len(frames) == 3
        assert frames[0]["event"] == "tool.fake.echo.step0"
        assert frames[1]["event"] == "tool.fake.echo.step1"
        assert frames[2]["event"] == "tool.fake.echo.step2"
        # Each frame's ``id`` is the event's own
        # event_id; clients use it as
        # ``Last-Event-ID`` on reconnect.
        assert frames[0]["id"] == "evt-0000"
        assert frames[1]["id"] == "evt-0001"
        assert frames[2]["id"] == "evt-0002"
        # Each frame's ``data`` is the canonical
        # ``event_to_dict`` payload.
        for i, frame in enumerate(frames):
            assert frame["data"]["agent_id"] == "agent-1"
            assert frame["data"]["data"]["i"] == i

    def test_causation_id_filter_narrows_stream(self, close_after_first_batch):
        """``causation_id=<event_id>`` only yields
        events whose ``causation_id`` matches.
        Two parallel flows are seeded; only one
        flows through the filter.
        """
        from fastapi.testclient import TestClient

        from kntgraph.core.event import CorrelationContext, Event

        from ._fake_log import FakeEventLog

        log = FakeEventLog()
        app = _build_app_with_log(log)
        # Two parallel flows: flow-A has 2 events,
        # flow-B has 1 event.
        for i, ev_id in enumerate(["flow-a-0", "flow-a-1"]):
            log.events.append(
                Event.domain_from(
                    agent_id="agent-1",
                    type=f"tool.fake.echo.a{i}",
                    data={"i": i},
                    correlation=CorrelationContext.new(
                        correlation_id="corr-a",  # type: ignore[arg-type]
                    ),
                    event_id=ev_id,  # type: ignore[arg-type]
                    causation_id="root-a",  # type: ignore[arg-type]
                )
            )
        log.events.append(
            Event.domain_from(
                agent_id="agent-1",
                type="tool.fake.echo.b0",
                data={"i": 0},
                correlation=CorrelationContext.new(
                    correlation_id="corr-b",  # type: ignore[arg-type]
                ),
                event_id="flow-b-0",  # type: ignore[arg-type]
                causation_id="root-b",  # type: ignore[arg-type]
            )
        )
        client = TestClient(app)
        buf = self._collect(
            client,
            "agent-1",
            params={"causation_id": "root-a"},
            n_frames=2,
        )
        frames = _parse_sse_frames(buf)
        # Only flow-A's 2 events pass the filter.
        assert len(frames) == 2
        assert frames[0]["event"] == "tool.fake.echo.a0"
        assert frames[1]["event"] == "tool.fake.echo.a1"

    def test_event_class_filter_narrows_stream(self, close_after_first_batch):
        """``event_class=domain`` only yields
        domain events; ``tool`` and ``lifecycle``
        events are filtered out.
        """
        from fastapi.testclient import TestClient

        from kntgraph.core.event import CorrelationContext, Event
        from kntgraph.core.event.operational import OperationalEventType

        from ._fake_log import FakeEventLog

        log = FakeEventLog()
        app = _build_app_with_log(log)
        # 1 domain, 1 lifecycle, 1 domain.
        log.events.append(
            Event.domain_from(
                agent_id="agent-1",
                type="user.intent",
                data={"intent": "hi"},
                correlation=CorrelationContext.new(),
                event_id="evt-domain-1",  # type: ignore[arg-type]
            )
        )
        log.events.append(
            Event.operation_from(
                agent_id="agent-1",
                type=OperationalEventType.IDLE,
                data={},
                correlation=CorrelationContext.new(),
                event_id="evt-lifecycle-1",  # type: ignore[arg-type]
            )
        )
        log.events.append(
            Event.domain_from(
                agent_id="agent-1",
                type="chat.reply.generated",
                data={"reply": "hello"},
                correlation=CorrelationContext.new(),
                event_id="evt-domain-2",  # type: ignore[arg-type]
            )
        )
        client = TestClient(app)
        buf = self._collect(
            client,
            "agent-1",
            params={"event_class": "domain"},
            n_frames=2,
        )
        frames = _parse_sse_frames(buf)
        assert len(frames) == 2
        assert frames[0]["event"] == "user.intent"
        assert frames[1]["event"] == "chat.reply.generated"

    def test_sse_response_uses_no_cache_headers(self, close_after_first_batch):
        """The SSE response carries the canonical
        headers: ``Content-Type:
        text/event-stream``, ``Cache-Control:
        no-cache``, and ``X-Accel-Buffering: no``
        (disables nginx response buffering so
        events stream immediately).
        """
        from fastapi.testclient import TestClient

        from ._fake_log import FakeEventLog

        log = FakeEventLog()
        app = _build_app_with_log(log)
        _seed_events(log, "agent-1", 1)
        client = TestClient(app)
        with client.stream(
            "GET",
            "/agents/agent-1/events",
            headers={"X-API-Key": "key-for-a1"},
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith(
                "text/event-stream"
            )
            assert r.headers["cache-control"] == "no-cache"
            assert r.headers["x-accel-buffering"] == "no"
            # Drain to close cleanly.
            buf = b""
            for chunk in r.iter_bytes():
                buf += chunk
                if buf.count(b"\n\n") >= 1:
                    break

    def test_sse_mismatched_agent_id_returns_403(self):
        """The endpoint enforces the same
        principal-agent binding as the rest of
        the gateway (``check_agent_binding``):
        the authenticated principal's ``agent_id``
        must match the URL's ``agent_id``.
        """
        from fastapi.testclient import TestClient

        from ._fake_log import FakeEventLog

        log = FakeEventLog()
        app = _build_app_with_log(log)
        client = TestClient(app)
        # key-for-a1 → agent-1; asking for
        # agent-2 must 403.
        with client.stream(
            "GET",
            "/agents/agent-2/events",
            headers={"X-API-Key": "key-for-a1"},
        ) as r:
            assert r.status_code == 403

    def test_legacy_status_endpoint_emits_deprecation_warning(self):
        """The legacy long-poll ``/status``
        endpoint emits a ``DeprecationWarning``
        on each call (ADR-065 §5.1). It still
        returns the ``StatusResponse`` payload
        so callers have a one-minor cycle to
        migrate to the SSE endpoint.
        """
        import warnings

        from fastapi.testclient import TestClient

        from ._fake_log import FakeEventLog

        log = FakeEventLog()
        app = _build_app_with_log(log)
        client = TestClient(app)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            r = client.get(
                "/agents/agent-1/events/some-pending-id/status",
                headers={"X-API-Key": "key-for-a1"},
            )
        assert r.status_code == 200
        deprecations = [
            w
            for w in caught
            if issubclass(w.category, DeprecationWarning)
        ]
        assert deprecations, "expected DeprecationWarning"
        msg = str(deprecations[0].message)
        assert "/events/some-pending-id/status" in msg
        assert "SSE" in msg or "subscribe" in msg