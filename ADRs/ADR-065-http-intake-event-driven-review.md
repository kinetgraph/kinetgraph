<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-065: HTTP intent ingestion — review against event-driven patterns

- **Status:** Proposed
- **Date:** 2026-08-26
- **Author:** kinetgraph architecture team
- **Supersedes (in part):** [ADR-012](./ADR-012-IntentRouter-HTTP-Gateway.md) — specifically §2.4 (status endpoint / long-poll) and §3 Cons (long-poll coupling)
- **Related to:**
  - [ADR-002](./ADR-002-Replay-Puro.md) — canonical replay; EventLog as source of truth
  - [ADR-005](./ADR-005-Checkpoints-Idempotency.md) — idempotency_key contract
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — Full Payload Fan-Out
  - [ADR-037](./ADR-037-Mandatory-Correlation-Propagation.md) — correlation_id at boundaries
  - [ADR-039](./ADR-039-Role-rethinking-and-intentions-routing.md) — `IntentResolutionSystem`, `RoleComponent`
  - [ADR-046](./ADR-046-CLI-Intent-Routing-Scaffold.md) — routing modes (`external` / `autonomous` / `collaborate`)
  - [ADR-060](./ADR-060-fmh-office-v2-pillars.md) — three-gate model (RBAC × persona × handoff)
  - [ADR-061](./ADR-061-litellm-integration-review.md) — ACL gaps in unauthenticated tools

> **Scope.** This ADR evaluates the **current HTTP
> intent ingestion path** (`src/kntgraph/api/intent_router/`,
> per ADR-012) against the framework's event-driven
> patterns. The diagnosis is the same as the user's
> observation: request-reply HTTP is an abstraction
> layer that does not match the rest of the framework,
> and the long-poll status endpoint is a symptom of
> that mismatch. This ADR proposes the
> **event-via-HTTP** pattern (emit + subscribe, not
> emit + poll) and identifies the specific changes
> needed.

## 1. Context

ADR-012 (June 2026) shipped the `IntentRouter` HTTP
gateway. It produces `tool.<name>.requested` events
into the EventLog (correct, per ADR-002) and exposes
two patterns to the client:

```| | | Pattern | Endpoint | Behaviour |
|---|---|---|
| **Ingest** | `POST /agents/{agent_id}/intents` | Returns **202 Accepted** with `event_id` + `status_url` |
| **Status** | `GET /agents/{agent_id}/events/{event_id}/status` | **Long-polls** the EventLog for the terminal event (`completed` / `failed`) whose `causation_id == event_id` |
| Health | `GET /healthz` | Liveness |
| List tools | `GET /agents/{agent_id}/tools` | Tool registry snapshot |

The **ingest** half is correct. The gateway emits
the event into the EventLog (the source of truth,
per ADR-002); the EventLog's idempotency dedupes
(per ADR-005); the `WorkerManager` consumes the
event out-of-process (per ADR-036); the result lands
in the EventLog as `tool.<name>.completed` /
`tool.<name>.failed`.

The **status** half is the mismatch. A request-reply
HTTP client does:

```
POST /intents  ──▶ 202 + event_id
GET  /status?  ──▶ 200 pending
GET  /status?  ──▶ 200 pending
GET  /status?  ──▶ 200 completed | failed
```

The framework underneath is **not** request-reply. It
is:

- **Pure ECS** (`World = fold(events)`, ADR-002).
- **Event-sourced** (the EventLog is the source of
  truth, not the response of any one Tool call).
- **Out-of-process workers** (ADR-036 — the LLM runs
  in a `ProcessPoolExecutor`; result lands **later**
  via the EventLog).
- **Cross-pod** (ADR-035 — multiple dispatchers run;
  a `ToolCallRequest` can be answered by a pod the
  client never spoke to).

The long-poll is a **client-side translation layer**
that re-implements, badly, what the framework already
provides natively: **subscribe to the EventLog and
get notified when the terminal event lands.** The
client is paying for the abstraction twice — once
for the HTTP request, once for the polling loop.

## 2. The diagnosis

### 2.1 Long-poll is a leaky abstraction

The status endpoint (`GET /agents/{agent_id}/events/{event_id}/status`)
is a **synchronous query against an asynchronous
event log**. It works because Redis Streams are fast
(`XREAD` is sub-millisecond); it does not work
*cleanly* because:

- **Each poll holds a connection.** For 1000
  concurrent clients awaiting 1000 different
  `event_id`s, the gateway holds 1000 connections
  and runs 1000 `XREAD` loops. The cost is amortised over
  the `timeout_s` window (5s default), but the
  gateway becomes a connection-multiplexer that
  the framework never asked for.
- **The poll is "blocking on time", not "blocking
  on event".** The client gets `pending` and has to
  decide whether to poll again. There is no
  push-style notification; the client has to invent
  backoff and dedup logic on top of the polling.
- **The `causation_id == event_id` walk is
  inefficient.** A poll reads the stream from `0`
  (every time), filters by `causation_id`, and walks
  forward. There is no Redis index for
  `causation_id` — this is O(N) per poll, where N is
  the agent's stream length. For agents with
  long histories, the cost grows linearly.

### 2.2 The gateway's auth/registration checks belong to the dispatcher

The current `POST /intents` handler validates **at
ingest time**:

| Check | Where it lives today | Where it should live |
|---|---|---|
| `agent_id` under `principal.tenant_id` | `check_agent_binding(...)` | **stays at gateway** (gate 1 of ADR-060 §3.0; the request must carry a valid `agent_id`) |
| `tool in ToolRegistry` | `registry.get(body.tool)` → 404 | **moves to dispatcher** (`IntentResolutionSystem` already does this, ADR-039 §2.2 step 2) |
| `Idempotency-Key` shape | `_sanitize_idempotency_key` | **stays at gateway** (HTTP transport concern) |
| `principal.role` vs `tool.required_role` | `default_acl().check(principal)` | **not done today** (ADR-061 §5 flag) — must move to dispatcher (gate 1) |
| `role.allowed_tools` | not checked today | **must move to dispatcher** (gate 2, ADR-061 §5 flag) |
| `handoff_targets` for cross-agent transitions | not applicable | not applicable at gateway |

The **two registration checks** (`tool in registry`,
`tool in role.allowed_tools`) are **business logic**
that belong in the dispatcher. The current gateway
duplicates the first check (its own `registry.get`)
and **misses the second** entirely (ADR-061 §5 item
6/7 — `chat_llm` is unauthenticated and
`ChatRoleSystem` does not gate on persona). The fix
is not to add the second check to the gateway; it is
to remove the first check and let the dispatcher do
both.

### 2.3 Correlation is set per-event, not per-request

The current handler mints a `correlation_id` from
`uuid4()` on every request (line 261 of `routes.py`):

```python
flow_id = uuid4()
correlation = CorrelationContext.new(
    correlation_id=flow_id,
)
```

This means **two retries of the same request produce
two different `correlation_id`s**, even though the
`event_id` is stable (per the deterministic hash).
ADR-037 §2 requires `correlation_id` to be
deterministic from the request input — otherwise the
event-sourced audit trail cannot stitch the retry
back to the original intent. Today the audit trail
**breaks on retry**: the first POST and the second
POST share an `event_id` (deduped) but have
different `correlation_id`s, so the second response
event lands under a different correlation.

The fix is to derive `correlation_id` from the same
hash that drives `event_id` (or to use `event_id`
as the `correlation_id` — they have the same
provenance: agent + tool + args + idempotency_key).

## 3. The decision

### 3.1 Replace the status endpoint with subscribe

The `POST /agents/{agent_id}/intents` endpoint
**stays** (it is the correct ingest path). The
`GET /agents/{agent_id}/events/{event_id}/status`
endpoint is **removed**. The replacement is a
**subscribe endpoint** that streams the agent's
EventLog to the client over a long-lived HTTP
connection.

**SSE is the default** (Server-Sent Events,
unidirectional server→client, no extra infra).
**WebSocket is opt-in** for clients that need
bidirectional control (e.g. cancel a request).

```| | | Method | Path | Purpose | Stack |
|---|---|---|---|
| `POST` | `/agents/{agent_id}/intents` | Ingest: emit `tool.<name>.requested`, return 202 + Location | HTTP/1.1 + JSON |
| `GET` | `/agents/{agent_id}/events` | Subscribe: stream the agent's EventLog to the client | **SSE** (default) |
| `GET` | `/agents/{agent_id}/events/ws` | Subscribe with bidirectional control (cancel, pause) | **WebSocket** (opt-in) |
| `GET` | `/healthz` | Liveness | HTTP/1.1 + JSON |
| `GET` | `/agents/{agent_id}/tools` | Tool registry snapshot (still useful for clients building UI) | HTTP/1.1 + JSON |
```

The subscribe endpoint **streams the agent's
EventLog from the cursor the client supplied**
(default: `0` = from the beginning of the visible
window). Each SSE event carries the canonical
`tool.<name>.requested` / `.completed` / `.failed`
event from the EventLog. The client filters by
`causation_id == <the event_id from ingest>` if it
only wants the result for one request; otherwise it
streams the whole agent's history.

### 3.2 Gateway is an **adapter** — no business logic

The gateway's responsibilities are now strictly
transport:

| Responsibility | Belongs to |
|---|---|
| Validate HTTP shape (`agent_id`, `body`, headers) | gateway |
| Mint **deterministic** `event_id` and `correlation_id` from `(agent_id, type, target, args, idempotency_key)` | gateway |
| Append `Event` to `EventLog` (or `EventLog.append`) | gateway |
| Bind `agent_id` to `principal.tenant_id` (gate 1's tenant check) | gateway |
| Validate `tool in ToolRegistry` | **dispatcher** (gate 1 ACL + gate 2 persona) |
| Validate `tool in role.allowed_tools` | **dispatcher** (gate 2 persona, per ADR-060 §3.0) |
| Validate cross-agent handoff (`handoff_targets`) | **dispatcher** (gate 3, per ADR-060 §3.0) |
| Resolve the tool's worker pool | **dispatcher** (`ToolRouter` + `WorkerManager`) |
| Emit `tool.<name>.requested` from the dispatcher's tick | **dispatcher** |

The gateway **emits exactly one event**: the
`tool.<name>.requested` (or `role.invoke`-derived
event). It does not validate registration, does not
emit success/failure events (those come from the
worker), and does not hold state across requests.

### 3.3 Three-gate model is enforced downstream

With the registration check moved to the dispatcher,
the three-gate model (ADR-060 §3.0) becomes the
**only** validation point for tool calls:

```
intake request  ──▶  EventLog (tool.<name>.requested)
                          │
                          ▼
                  dispatcher tick
                          │
                          ├── Gate 1: ToolACL.check(principal)        [RBAC]
                          │              └── pass → next gate
                          │              └── fail → emit intent.validation_failed
                          │
                          ├── Gate 2: target_tool in role.allowed_tools [persona]
                          │              └── pass → next gate
                          │              └── fail → emit intent.validation_failed
                          │
                          ├── Gate 3: handoff_targets ⊇ destination    [v2 only]
                          │              └── pass → next gate
                          │              └── fail → emit intent.validation_failed
                          │
                          └── emit tool.<name>.requested → WorkerManager
```

The **client learns of validation failures** via
the subscribe stream: the dispatcher emits
`intent.validation_failed` (ADR-039 §2.2 step 2/4),
which lands in the EventLog and is streamed to the
client over SSE/WS. The client never sees a 404 from
the gateway; it sees an `intent.validation_failed`
event with `data.reason`.

This is the same pattern ADR-039 already documents.
ADR-065 does not introduce new event types — it
removes the gateway's premature checks.

### 3.4 Correlation_id is derived, not minted

The gateway derives `correlation_id` from the same
input as `event_id`:

```python
event_id = uuid5(
    namespace=AGENT_NS,
    name=f"{agent_id}|{type}|{target}|{args_hash}|{idempotency_key}",
)
correlation_id = event_id   # SAME value
```

ADR-037 §2 requires `correlation_id` to be
deterministic. Using `event_id` directly satisfies
that (they share the same hash input). Two retries
produce the same `correlation_id`; the audit trail
is correct.

For **multi-event flows** (where one request
eventually fans out into multiple downstream events
via handoff), `causation_id` carries the per-step
provenance; `correlation_id` stays the request-level
identifier. This is exactly what ADR-037 §3
documents — no change to the framework's contract.

## 4. Subscribe semantics

### 4.1 SSE — `GET /agents/{agent_id}/events`

```text
GET /agents/{agent_id}/events?from=1715000000-0&causation_id=<event_id>&event_class=domain

200 OK
Content-Type: text/event-stream
Cache-Control: no-cache

event: tool.invoice.issue.requested
id: 1715000000-0
data: {"event_id":"1715000000-0","agent_id":"nf-001","type":"tool.invoice.issue.requested","correlation_id":"1715000000-0","data":{...}}

event: tool.invoice.issue.completed
id: 1715000001-2
data: {"event_id":"1715000001-2","agent_id":"nf-001","type":"tool.invoice.issue.completed","correlation_id":"1715000000-0","causation_id":"1715000000-0","data":{"result":{...}}}

[connection held; new events stream as they land]
```

The client can filter by `causation_id` (just the
events for one request) or by `event_class`
("domain" only, "tool" only, etc.). The
`from=<stream_id>` cursor is the canonical Redis
Stream cursor — clients can resume from any point
(per ADR-002 §3 idempotence on `event_id`).

### 4.2 WebSocket — `GET /agents/{agent_id}/events/ws`

Same payload, bidirectional control channel.
Clients can send `{"type": "cancel", "causation_id":
"<event_id>"}` to request a downstream cancel
(per ADR-047 §6.1 partial-completion model). The
server replies with `{"type": "cancel_ack",
"event_id": "..."}` and emits the canonical
`tool.<name>.cancelled` event when the worker
honours the cancel.

### 4.3 Connection lifecycle

- **Open**: client connects, server starts streaming
  from the cursor (`from` parameter or current tip).
- **Heartbeat**: server sends `:heartbeat\n\n` every
  15 s (SSE comment line; WebSocket ping). Prevents
  proxy / LB timeouts on idle connections.
- **Reconnect**: client disconnects (network error,
  LB churn, app reload); on reconnect, client
  passes the last `id` it received as `from`. The
  server replays the gap. SSE's built-in `Last-Event-ID`
  header on reconnect matches this — no client-side
  bookkeeping needed beyond "remember the last `id`".
- **Close**: server closes the connection on
  agent termination (`agent.terminated`, ADR-003
  §2.1) or on explicit client `close` frame.

## 5. Migration

### 5.1 What changes in v0.15 (deprecation wave)

| Change | Status | Notes |
|---|---|---|
| `GET /agents/{agent_id}/events/{event_id}/status` deprecated | `DeprecationWarning` at request time | body: "use `GET /agents/{agent_id}/events?causation_id=<event_id>` (SSE)" |
| `POST /agents/{agent_id}/intents` keeps 202 + `event_id` + `Location` header | unchanged | `Location` header now points at the **subscribe endpoint** (SSE) instead of the status endpoint |
| `POST /agents/{agent_id}/intents` no longer 404s on unknown tool | **404 removed** | the dispatcher emits `intent.validation_failed`; client sees it in the SSE stream |
| Gateway stops computing `correlation_id = uuid4()` | **deterministic from hash** | fixes the retry-audit-trail bug (§2.3) |
| `GET /agents/{agent_id}/events` (SSE) added | new | streams the agent's EventLog |
| `GET /agents/{agent_id}/events/ws` (WebSocket) added | new | opt-in bidirectional |

### 5.2 What changes in v0.16 (`PrincipalLevel` canonical)

The `Principal` lookup in the gateway uses
`PrincipalLevel` (ADR-060 §2.0). No protocol-level
change; the headers (`X-Tenant-Id`, `Authorization`)
are unchanged.

### 5.3 What changes in v1.0 (cleanup)

- `GET /agents/{agent_id}/events/{event_id}/status`
  **removed** (per AGENTS.md §7 one-minor-cycle
  deprecation).
- The `default_acl()` for `tool.invoke` events
  enforces gate-1 RBAC at the dispatcher (per
  ADR-061 §5).
- `RoleComponent.allowed_tools` enforcement is
  wired for all `chat_llm`-derived intents (per
  ADR-061 §5).

## 6. Why this is **not** a refactor

The change touches three semantic levels, not just
the code shape:

1. **Transport-level**: long-poll → SSE/WS. The
   gateway loses the `status` endpoint and gains
   subscribe endpoints.
2. **Validation-level**: gateway loses the
   registration check; dispatcher gains a complete
   three-gate validation. The ADR-061 §5 gaps
   (`chat_llm` unauthenticated; persona not
   consulted on `chat_llm` emission) become
   structurally impossible — the dispatcher emits
   `intent.validation_failed` for any unregistered
   combination.
3. **Audit-level**: `correlation_id` becomes
   deterministic; retry stops breaking the audit
   trail. ADR-037 §2 is satisfied end-to-end at the
   ingest boundary.

The change is a **closure of two design mistakes**
(the long-poll and the registration-at-gateway
duplication) plus the **closure of two latent bugs**
(the correlation_id derivation and the
`chat_llm` ACL gap).

## 7. Open questions

1. **SSE vs. WebSocket at the LB.** Reverse proxies
   (nginx, AWS ALB, Cloudflare) buffer SSE responses
   by default; the heartbeat comment line is the
   workaround, but operators must configure the LB
   to disable buffering on the `/events` endpoint.
   Tracked as runbook content.
2. **Multi-tenant subscribe.** Today a client
   subscribes to one agent's events; a multi-tenant
   dashboard would subscribe to many. Whether to
   expose a `/agents/{agent_id}/events/batch` or
   rely on the client opening N connections is
   open. Tracked for a follow-up ADR if dashboards
   become common.
3. **Backpressure.** SSE has no client-side flow
   control. A slow client blocks the gateway's
   stream forwarder (Redis `XREAD`). The framework's
   existing circuit breaker (`worker resilience`,
   ADR-005) covers retries; for subscribe, a
   per-connection buffer cap with disconnect-on-full
   is the right policy. Tracked.
4. **Cursor replay on reconnect.** The `from=<id>`
   parameter assumes the EventLog retains the event
   at that cursor. ADR-057 §4.11.6 per-class
   retention may **trim** events older than the TTL
   — the client must accept "I cannot resume before
   this point" and re-subscribe from `0`. Tracked in
   the client SDK documentation.
5. **WebSocket auth.** The SSE path reuses the HTTP
   `Authorization` header. The WebSocket upgrade
   request carries it too, but the auth verifier
   runs **before** the upgrade — same code path. If
   a deployment proxies through a layer that strips
   headers on upgrade, the WebSocket may need a
   query-param token. Tracked for a future revision.

## 8. Decision

This ADR **proposes** the SSE-default + WebSocket-
opt-in subscribe pattern, with the gateway
demoted to a pure adapter (no registration checks;
deterministic `correlation_id`; ACL gates in the
dispatcher).

**Recommended next step:** open **ADR-066**
*"SSE subscribe implementation in the HTTP
gateway"* — owns the v0.15 changes for the gateway
(demote to adapter; add `/events` SSE endpoint;
deprecate `/status`). ADR-066 is the actionable
unit; ADR-065 is the audit and prioritisation that
informs it.