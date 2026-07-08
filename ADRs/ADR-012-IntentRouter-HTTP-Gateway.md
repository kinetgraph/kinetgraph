<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-012: IntentRouter (HTTP gateway)

**Status:** Accepted
**Date:** 2026-06-11
**Version:** 0.6.0
**Authors:** Architecture Team
**Related:** ADR-005 (Idempotency), ADR-006 (Tool × Role), ADR-007 (LiteLLM),
ADR-008 (Caching), ADR-010 (Business Tier)

---

## 1. Context

The framework is event-driven: pure systems emit `tool.{name}.requested`
events into the `EventLog` (Redis Streams), and adapters consume them
through `ToolInvoker` (F8.2). This works well for in-process flows,
batch jobs, and replay.

External integrators (frontends, webhooks, mobile clients, third-party
systems) want to call Tools via HTTP. Today they have no entry point:
the framework is intentionally transport-agnostic, and a new HTTP layer
is a candidate for a "first-class gateway".

Three concerns are at stake:

  1. **Validation**: only Tools/Roles that are *registered* for the
     caller's `agent_id` should be callable. Unknown tools must be
     rejected early, before they pollute the EventLog.

  2. **Idempotency**: the framework already has `idempotency_key` at
     the Tool boundary (ADR-005), injected as `str(request.event_id)`.
     HTTP requests that retry must produce the same `event_id`, so the
     EventLog's dedup is effective end-to-end.

  3. **Async results**: a Tool call is a multi-event flow
     (`.requested` → optional `.failed` from validation → `.completed`
     or `.failed` from the adapter). HTTP's request/response is too
     narrow; the client needs a way to discover the outcome.

## 2. Decision

Add a thin HTTP gateway — `fmh_backend.api.intent_router` — that
**produces events** into the existing `EventLog`. It is *one* adapter,
not the canonical path.

### 2.1 Endpoints

```
POST   /agents/{agent_id}/intents
GET    /agents/{agent_id}/events/{event_id}/status
GET    /healthz
GET    /agents/{agent_id}/tools    # list registered tools
```

### 2.2 Request shape (POST /intents)

```json
{
  "type": "tool.invoke",
  "tool": "invoice.issue",
  "args": { "xml": "..." }
}
```

Or, for role-driven flows:

```json
{
  "type": "role.invoke",
  "role": "summarizer",
  "args": { "text": "..." }
}
```

Optional headers:

  - `Idempotency-Key: <client-supplied>` — becomes part of the
    `event_id` hash. Two requests with the same key produce the same
    `event_id`, and the EventLog dedupes.

### 2.3 Response shapes

Success (202 Accepted):

```json
{
  "event_id": "uuid5(...)",
  "status": "accepted",
  "status_url": "/agents/{agent_id}/events/{event_id}/status"
}
```

Rejection (404 Not Found — tool/role not registered):

```json
{
  "error": "tool_not_registered",
  "tool": "invoice.issue"
}
```

**No event is emitted on 404.** The EventLog stays clean of
attempts that could never succeed.

Auth failure (401/403): no event.

### 2.4 Status endpoint

`GET /agents/{agent_id}/events/{event_id}/status` long-polls the
EventLog for the terminal event (`.completed` or `.failed`) whose
`causation_id == event_id`. Returns:

  - `{"status": "pending", "event_id": "..."}` — no terminal yet
  - `{"status": "completed", "result": ...}` — happy path
  - `{"status": "failed", "error": "..."}` — adapter raised
  - `{"status": "rejected", "error": "tool_not_registered"}` —
    this case is rare: 404 above prevents most rejections, but a
    race between lookup and dispatch is possible.

The status endpoint is **read-only** and **transport-agnostic**; any
other consumer (CLI, batch, monitoring) can read the same status by
querying the EventLog directly.

### 2.5 Idempotency contract

The `event_id` is computed as:

```
event_id = uuid5(
    namespace=AGENT_NS,
    name=f"{agent_id}|{type}|{tool_or_role}|{args_hash}|{idempotency_key_or_empty}"
)
```

Same `(agent_id, type, tool, args, idempotency_key)` → same
`event_id`. Retry is free; the EventLog dedupes on append.

When the client does **not** send `Idempotency-Key`, the gateway
generates a stable hash from the request body (sorted JSON). Two
identical bodies → same `event_id`. This is the same determinism
the framework already uses (ADR-005).

### 2.6 What this ADR is NOT

  - **Not** a replacement for `ToolInvoker`. The Invoker still
    consumes `.requested` events from the EventLog; the HTTP layer
    only **produces** them.
  - **Not** a new persistence layer. All state stays in the
    EventLog + Redis (which is what the framework already uses).
  - **Not** mandatory. Applications that don't need HTTP don't
    install the `[api]` extra; the rest of the framework is
    unchanged.
  - **Not** for streaming. The `LiteLLMTool.astream` path is
    excluded from this gateway — SSE/WebSocket support is a
    separate ADR if/when needed.

## 3. Trade-offs

### Pros

  - **Single entry point** for external integrators; pydantic
    schemas give free OpenAPI documentation.
  - **Reuses** the existing EventLog, ToolInvoker, idempotency,
    and ToolRegistry. No new invariant.
  - **No coupling** between HTTP and Tool logic. The router
    imports `EventLog` and `ToolRegistry` from the framework;
    if the router is down, the framework still runs.
  - **Tests** are independent: router tests mock `EventLog`
    and `ToolRegistry`; the existing `ToolInvoker` tests are
    untouched.

### Cons

  - **Two places to validate** that a tool exists: ToolRegistry
    (canonical) and the router's OpenAPI schema (derived).
    Mitigation: pydantic models generated at boot from
    `ToolRegistry.list_descriptors()` (auto-sync).
  - **HTTP 404 vs. event `.failed`**: clients may confuse "tool
    not registered" with "tool ran and failed". Mitigation:
    `404` is *always* immediate (no event); `.failed` is *always*
    async (the event exists in the log). Documented in OpenAPI.
  - **Long-poll coupling**: the status endpoint blocks on
    `EventLog.read(...)`. Cheap (Redis is fast), but each request
    holds a connection. For high QPS, prefer SSE or a
    subscription model. Out of scope for v1.

### Alternatives considered

  - **Thick HTTP handler that calls `Tool.invoke` directly**:
    rejected. Loses replay (EventLog is the source of truth),
    loses idempotency, and creates two ways to invoke the same
    Tool (HTTP vs. event). Drift guaranteed.
  - **Generic webhook receiver with no validation**: rejected.
    Puts the responsibility on every caller; the framework
    should fail fast (ADR-001).
  - **gRPC instead of HTTP**: deferred. gRPC has its place
    (service-to-service), but the user-facing surface is HTTP.
    gRPC adapter can be added later; it would produce the same
    `.requested` events.

## 4. Consequences

  - New extra: `fmh-backend[api]` (fastapi + uvicorn).
  - New module: `fmh_backend.api` (intentionally opt-in; not
    imported by the rest of the framework).
  - New ADR-012 (this document).
  - No change to `ToolInvoker`, `EventLog`, `ToolRegistry`, or
    any other existing module.
  - Tests: unit (mocked `EventLog`/`ToolRegistry`); integration
    (real Redis, no FalkorDB needed).

## 5. See also

  - [ADR-005: Idempotency in Tools](../fmh_backend/ADRs/ADR-005-Checkpoints-Idempotency.md)
  - [ADR-006: Tool × Role separation](../fmh_agents/ADRs/ADR-006-Tool-Role-Separation.md)
  - [fmh_backend/docs/architecture.md](../fmh_backend/docs/architecture.md) —
    the `EventLog` is the source of truth; HTTP is an edge.
