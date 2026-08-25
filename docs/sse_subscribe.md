<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# SSE Subscribe — `GET /agents/{agent_id}/events`

The HTTP intent router exposes a **Server-Sent Events
(SSE)** endpoint that streams the agent's EventLog
to the client. The endpoint is the event-driven
replacement for the legacy long-poll
`GET /agents/{agent_id}/events/{event_id}/status`
endpoint (deprecated in v0.14; emits
`DeprecationWarning` on each call).

The framework is **not** request-reply: it is
pure ECS (`World = fold(events)`, ADR-002),
event-sourced (the EventLog is the source of truth,
not the response of any one Tool call), and uses
out-of-process workers (ADR-036) that finish
their work in a **later** tick. SSE aligns the HTTP
edge with the framework's underlying model: the
client subscribes to the stream and the server
pushes events as they land, instead of the client
polling for a terminal status.

> **See also**
>
> - [ADR-065](../ADRs/ADR-065-http-intake-event-driven-review.md)
>   — the audit + the proposed migration from
>   request-reply to event-via-HTTP.
> - [Event sourcing](./event_sourcing.md) — the
>   canonical EventLog semantics.

---

## 1. Endpoint

```http
GET /agents/{agent_id}/events?from=0&causation_id=<event_id>&event_class=domain
Authorization: X-API-Key <key>
Accept: text/event-stream
```

### Query parameters

| Param          | Type     | Default | Description |
| -------------- | -------- | ------- | ----------- |
| `from_`        | string   | `"0"`   | The Redis Stream cursor to start from. `"0"` replays the whole visible window; on reconnect, the SSE client sends the last `id` it received as `Last-Event-ID`, and the gateway translates it to the cursor for the next read. |
| `causation_id` | string   | `None`  | Filter to events whose `causation_id` matches the value. The canonical use case: subscribe to the result of one `POST /intents` request — pass the `event_id` from the ingest response. |
| `event_class`  | string   | `None`  | Filter to events of a single class (`"domain"`, `"lifecycle"`, `"tool"`). |

### Response

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no

event: tool.echo.requested
id: 1715000000-0
data: {"event_id":"1715000000-0","agent_id":"demo-agent","type":"tool.echo.requested",...}

event: tool.echo.completed
id: 1715000001-2
data: {"event_id":"1715000001-2","agent_id":"demo-agent","type":"tool.echo.completed","correlation_id":"1715000000-0","causation_id":"1715000000-0","data":{"result":{...}}}

[connection held; new events stream as they land]
```

Each SSE frame carries the canonical payload:

- `event:` — the event's `event_type`.
- `id:` — the event's `event_id` (UUID). The
  client resends it as `Last-Event-ID` on
  reconnect; the SSE standard's built-in reconnect
  semantics do the rest.
- `data:` — the JSON-serialised event payload
  (`event_to_dict` from `kntgraph.core.event.codec`).

A `:heartbeat` SSE comment line is emitted every
15 s when no events have arrived. This keeps
proxies / load balancers from closing idle
connections.

---

## 2. Client patterns

### 2.1 Python (`httpx-sse` or `httpx.stream`)

```python
import httpx

with httpx.stream(
    "GET",
    "https://gateway.example.com/agents/demo-agent/events",
    params={"causation_id": accepted_event_id},
    headers={"X-API-Key": "demo-key"},
) as r:
    for line in r.iter_lines():
        if line.startswith("data:"):
            payload = json.loads(line.removeprefix("data: "))
            print(payload)
```

### 2.2 JavaScript (browser EventSource)

```javascript
const url = new URL(
    "/agents/demo-agent/events",
    gatewayBaseUrl,
);
url.searchParams.set("causation_id", acceptedEventId);
url.searchParams.set("event_class", "domain");

const source = new EventSource(url, {
    headers: {"X-API-Key": apiKey}, // not honoured by EventSource; use a cookie
});
source.onmessage = (e) => {
    const event = JSON.parse(e.data);
    if (event.type.endsWith(".completed")) {
        source.close();
        onDone(event.data);
    }
};
```

> **Note** — browser `EventSource` cannot send custom
> headers. For browser clients, prefer a session
> cookie (the framework's `RedisAPIKeyVerifier`
> accepts both `X-API-Key` and the session cookie
> adapter). For server-side or test clients, the
> `httpx` example above works without cookies.

### 2.3 Reconnect (Last-Event-ID)

The SSE standard's reconnect semantics do the work
for you. On disconnect (network error, LB churn,
app reload), the client reconnects with the last
`id` it received as `Last-Event-ID`:

```python
import httpx

last_event_id: str | None = None
while True:
    headers = {"X-API-Key": "demo-key"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    with httpx.stream(
        "GET",
        "https://gateway.example.com/agents/demo-agent/events",
        headers=headers,
    ) as r:
        for line in r.iter_lines():
            if line.startswith("id:"):
                last_event_id = line.removeprefix("id: ")
            elif line.startswith("data:"):
                payload = json.loads(line.removeprefix("data: "))
                handle(payload)
```

The server replays the gap automatically. The
client only needs to remember the last `id` it
received.

---

## 3. Filtering strategies

| Goal | Filter | Trade-off |
| ---- | ------ | --------- |
| Watch the **result of one POST** | `?causation_id=<event_id>` | Narrowest; only the events downstream of the request. The recommended default for "subscribe to the result of my call." |
| Watch **one slice of the stream** | `?event_class=domain` | Excludes `lifecycle` and `tool` events; useful for dashboards that want to render domain state only. |
| Watch **everything** | `?from_=0` (the default) | Broadest; the client filters locally by `event_type`. Use when the dashboard doesn't know which flow it cares about yet. |

---

## 4. Lifecycle

```
client connects with ?from=<cursor>
> or
> server replays events since <cursor>, one SSE frame per event
> or
> server polls the EventLog every 100 ms while the connection is held
> or
>   • if a new event lands → emit a frame, advance the cursor
>   • if no event for 15 s → emit ":heartbeat"
> or
> client disconnects
> or
> server closes the connection on agent termination
   (agent.terminated lifecycle event, ADR-003 §2.1)
```

The poll-and-yield implementation is good enough
for v0.14: 100 ms poll cadence (matches the legacy
long-poll cadence); no real Redis Pub/Sub
subscription. The endpoint's contract is the
same regardless of the internal transport; when
the volume justifies it, the internals swap to a
real subscribe primitive without changing the
public surface.

---

## 5. Migration from the legacy long-poll

The legacy endpoint
`GET /agents/{agent_id}/events/{event_id}/status`
emits `DeprecationWarning` on each call as of
v0.14. The migration is one-to-one:

| Legacy                                                                                       | SSE                                                                                                                  |
| -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `GET /agents/{agent_id}/events/{event_id}/status?timeout_s=5`                                 | `GET /agents/{agent_id}/events?causation_id={event_id}`                                                              |
| `200 OK {"status": "completed", "result": {...}}` (or `"pending"` after `timeout_s`)        | An SSE frame with `event: tool.<name>.completed` (no terminal frame on timeout — the connection stays open)         |
| Retry the next `GET /status` if `pending`                                                     | The connection stays open; new events stream as they land                                                           |
| No gap-replay semantics                                                                       | Standard `Last-Event-ID` reconnect                                                                                   |

A complete working example is in
`examples/22_sse_subscribe.py`. The example wires
the in-process `FakeEventLog`, posts an intent,
subscribes to the SSE stream with
`causation_id=<event_id>`, and prints each event
as it lands.

---

## 6. Operational notes

### Reverse proxies

The endpoint sets `X-Accel-Buffering: no` so
nginx (and other proxies that respect this
header) do not buffer the response. Without
this header, events accumulate in the proxy
buffer and the client sees bursts instead of
streams.

```nginx
# nginx.conf — no special config required; the
# header is enough. For paranoid operators:
location /agents/ {
    proxy_buffering off;
    proxy_cache off;
    proxy_set_header Connection '';
    proxy_http_version 1.1;
    chunked_transfer_encoding off;
}
```

### Heartbeats

The 15 s heartbeat is `:heartbeat\n\n` — an SSE
comment line, not an event. Clients should ignore
comment lines (most SSE libraries do this
automatically). The heartbeat exists purely to
keep proxies / load balancers from closing idle
connections.

### Errors

The endpoint does **not** 404 on `agent_id` not
found — it returns 403 when the principal's
`agent_id` does not match the URL's `agent_id`.
This is the same rule as the rest of the gateway
(`check_agent_binding`). If the agent_id exists
but the principal does not own it, the SSE
connection is closed by the gateway immediately.

### Backpressure

The poll-and-yield implementation reads with
100 ms cadence. A backlog of events does not
backpressure the EventLog — the generator yields
each event as a separate SSE frame and the loop
returns to the next poll immediately. If your
deployment generates events faster than
1 / 100 ms = 10 events/s sustained, consider
the future Redis Pub/Sub subscribe primitive
(not in v0.14; tracking in DEBT).

---

## 7. Reference

- **Source:** `src/kntgraph/api/intent_router/routes.py`
  — `register_sse_events` installer.
- **Wire-up:** `src/kntgraph/api/intent_router/app_factory.py`
  — `register_sse_events` called from `_build_app`.
- **Tests:** `tests/unit/api/test_sse_subscribe.py`
  — 6 tests covering `from=0`, `causation_id`,
  `event_class`, no-cache headers, 403 mismatch,
  and `DeprecationWarning` on the legacy
  endpoint.
- **Example:** `examples/22_sse_subscribe.py` —
  end-to-end with `TestClient.stream`.