<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-075: Tool Task Observability and Recovery

- **Status:** Proposed
- **Date:** 2026-09-12
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — `ToolCallRequest` / `ToolCallCompletion`
  - [ADR-045](./ADR-045-Tool-Call-Request-TTL.md) — Tool Call TTL
  - [ADR-068](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md) — `EventLog.subscribe_many`
  - [ADR-070](./ADR-070-Worker-Level-Back-Pressure.md) — Worker-Level Back-Pressure (proposed)
  - [ADR-071](./ADR-071-BusinessFSM-Concordo.md) — BusinessFSM Concordo (consumer)
  - [ADR-072](./ADR-072-WorkflowSaga-Concordo.md) — WorkflowSaga Concordo (consumer)

---

## 1. Context

The framework's tool-call pipeline (ADR-034, ADR-036):

```
FSM/Saga → tool.<name>.requested → EventLog
                                    ↓
                            ToolRouter (ADR-036)
                                    ↓
                          knt:tools:<name>:queue
                                    ↓
                              Worker process
                                    ↓
                         tool.<name>.completed | failed
                                    ↓
                              EventLog (for FSM/Saga)
```

The framework has **two recovery primitives**:

- `ToolCallTTLSweeperSystem` (ADR-045): scans `tool_requests` for stale entries
  and emits `tool.<name>.failed` when `expires_at` is in the past.
- `DeadLetterQueue` (ADR-019): operator-facing sink for terminal failures.

But neither primitive answers the user's question: **"can we lose tasks
during execution?"**

### 1.1 Scenarios where tasks are lost today

| Scenario | Behavior today | Consequence |
|---|---|---|
| Worker crashes after reading the queue but before starting the tool | TTL sweeper catches (if TTL set). Without TTL → **lost**. | Agent waits forever. |
| Queue never delivers the message to a worker | **Never detected.** | Task sits in `tool_requests` slot with no completion. |
| Worker never completes, TTL=infinite (or unset) | **Never detected.** | Agent waits forever. |
| Worker completes but completion event lost in transit | **Never detected.** | Agent waits forever. |
| Dispatcher crashes mid-tick (after system emits, before `append_batch`) | **Lost.** Event never reaches the EventLog. | Recovery requires dispatcher restart with replay (deterministic). |

The most damning scenario:

```python
# FSM emits tool request with TTL=None (the current default opt-out)
Event.create(
    event_type="tool.payment_processor.requested",
    agent_id="order-1",
    event_class="domain",
    data={"order_id": "123"},
    correlation=...,
)

# Worker X receives from queue.
# Worker X starts processing.
# Worker X crashes (OOM kill, segfault, k8s eviction).

# What happens?
# 1. tool_request is in tool_requests[order-1] with expires_at=None.
# 2. TTL sweeper SKIPS it (expires_at is None).
# 3. No one knows the worker crashed.
# 4. Order-1 waits forever.
# 5. The worker will NOT re-process (the queue entry was consumed).
```

**Today: task lost. No retry. No alert.**

### 1.2 What the framework already has

- `EventLog.subscribe_many` (ADR-068): cross-stream subscription mechanism.
- `DeadLetterQueue` + `DeadLetterActions` (ADR-019): operator-facing failure sink.
- `Event.correlation` (ADR-037): request-to-completion tracing.
- `ToolRouter.route_batch` (ADR-036): fan-out from EventLog to worker queues.

What's **missing**:

1. **Mandatory default TTL**: today, `expires_at=None` is opt-out (`ToolCallTTL(default_ttl_seconds=0)`).
2. **Worker acknowledgment**: framework does not know when a worker picks up a task.
3. **Re-processing on stale**: TTL sweeper marks as failed; it does not re-dispatch.
4. **Observability primitives**: no way to query "in-flight tasks" or "stale tasks".

---

## 2. Decision

A recovery pipeline with four tiers. Each tier addresses a specific
failure mode. Tiers compose: a task that survives Tier 1 (TTL) but
fails Tier 2 (worker crash) escalates to Tier 3 (re-dispatch) and,
failing that, Tier 4 (DLQ + alert).

### 2.1 Tier 1 — Mandatory default TTL

**Today**: `ToolCallRequest.expires_at` is `Optional[datetime]`. The projection
sets it from `ToolCallTTL.per_tool_ttls` (per-tool override) or
`ToolCallTTL.default_ttl_seconds` (default).

**Decision**: make `default_ttl_seconds > 0` mandatory. The
projection refuses to materialise a request without an
`expires_at`. The opt-out `ToolCallTTL(default_ttl_seconds=0)` is
**removed** in production deployments (kept as a debug-only knob).

```python
@dataclass(frozen=True, slots=True)
class ToolCallTTL:
    """Per-tool TTL configuration (ADR-045, tightened by ADR-075).

    ``default_ttl_seconds > 0`` is enforced: the projection
    refuses requests that would materialise without
    ``expires_at``. The framework guarantees every in-flight
    request has a TTL.
    """
    default_ttl_seconds: float  # MUST be > 0 in production
    per_tool_ttls: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.default_ttl_seconds <= 0:
            raise ValueError(
                "default_ttl_seconds must be > 0 (ADR-075). "
                "Use a finite TTL; infinite TTLs are forbidden."
            )
```

### 2.2 Tier 2 — Worker acknowledgment

**Today**: a worker reads from `knt:tools:<name>:queue`, runs the tool,
emits `completed` or `failed`. The framework does not know **when** the
worker picked up the task.

**Decision**: workers emit a new event type when they start
processing.

```python
Event.create(
    agent_id="order-1",
    event_type="tool.<name>.acknowledged",  # NEW event type
    event_class="domain",
    data={
        "request_event_id": "...",  # join key
        "worker_id": "worker-7",
        "acknowledged_at": "2026-09-12T12:00:00Z",
    },
    correlation=...,
)
```

The projection materialises a `tool_acknowledgments` slot (parallel
to `tool_requests` and `tool_completions`). Each acknowledgment
carries the worker's identity (so we know which worker has the
task) and the request_event_id (join key).

**Worker-side**: the framework ships a `Worker` base class that
emits the acknowledgment automatically. Workers that implement
`Tool` (ADR-036 §2.3) inherit this behavior. Custom workers
(opt-in) can call `worker.acknowledge(...)` explicitly.

### 2.3 Tier 3 — Re-dispatch on stale

**Today**: TTL sweeper marks stale requests as `failed`. The agent
sees the failure and may retry (depending on guard / saga
config), but the framework does not re-dispatch automatically.

**Decision**: extend `ToolCallTTLSweeperSystem` (or a new
`ToolCallRecoverySystem`) with two re-dispatch paths:

```
                  request emitted at T0
                          │
                          ▼
                  ┌────────┴──────────┐
                  │ Stale detection:  │
                  │   now - T0 > TTL  │
                  └────────┬──────────┘
                           │
                  ┌────────┴──────────┐
                  │ Was acknowledged? │
                  └────┬─────────┬───┘
                       │         │
                  YES  │         │  NO
                       ▼         ▼
              ┌─────────┐  ┌─────────────┐
              │ Re-dispatch │  │ Re-dispatch │
              │ (worker died)│  │ (queue lost) │
              └──────┬──────┘  └──────┬──────┘
                     │                │
                     ▼                ▼
              tool.<name>.requested  tool.<name>.requested
              (re-emitted)            (re-emitted)
```

Both paths emit a fresh `tool.<name>.requested` event with the same
`request_event_id` (so the EventLog dedup catches true duplicates).
The TTL is reset on re-dispatch.

**Idempotency caveat**: re-dispatch is safe only for tools that are
either:
- Explicitly idempotent (declared in `ToolRegistry` or via the
  `Tool` Protocol's `idempotent: bool` flag).
- OR confirmed by the operator (configuration flag:
  `ToolRegistry.force_idempotent` for dev/test).

For non-idempotent tools, the framework **DLQs** the request
instead of re-dispatching. The operator decides via
`DeadLetterActions.reprocess(event_id)` or `discard(event_id)`.

### 2.4 Tier 4 — Observability

**Today**: no way to query "tasks in flight for >X minutes" or
"which workers are slow". Operators rely on logs and metrics.

**Decision**: add three primitives to the framework:

1. **`dispatcher.in_flight_tasks(agent_id=None)`** → list of
   `InFlightTask` records:
   ```python
   @dataclass(frozen=True)
   class InFlightTask:
       request_event_id: str
       agent_id: str
       tool_name: str
       requested_at: datetime
       acknowledged_at: datetime | None
       expires_at: datetime
       attempt: int  # 1 = original; >1 = re-dispatched
   ```

2. **`dispatcher.stale_tasks(threshold_seconds: float)`** → list of
   tasks past TTL but not yet re-dispatched (recovery race window).

3. **`dispatcher.dead_lettered_tasks()`** → list of tasks in the
   DLQ awaiting operator action.

These primitives read from the EventLog (via `subscribe_many`)
and the WorldCheckpoint. They are **read-only** — no mutations.

A dashboard layer (UI, Grafana integration, etc.) is **out of scope**
for this ADR — the primitives are enough for any visualization
layer to consume.

---

## 3. Components

### 3.1 New event type: `tool.<name>.acknowledged`

Already described in §2.2. Added to `ToolEventKind` enum (alongside
`REQUESTED`, `COMPLETED`, `FAILED`).

### 3.2 Modified: `ToolCallTTL` (ADR-045)

```diff
 @dataclass(frozen=True, slots=True)
 class ToolCallTTL:
-    default_ttl_seconds: float = 300.0
+    default_ttl_seconds: float  # MUST be > 0 (ADR-075)
     per_tool_ttls: Mapping[str, float] = field(default_factory=dict)

     def __post_init__(self) -> None:
-        if self.default_ttl_seconds <= 0:
-            raise ValueError(...)
+        # ADR-075: enforced — see ADR-075 §2.1.
+        if self.default_ttl_seconds <= 0:
+            raise ValueError(
+                "default_ttl_seconds must be > 0 (ADR-075). "
+                "Use a finite TTL; infinite TTLs are forbidden."
+            )
```

### 3.3 Modified: `ToolCallRequest` (ADR-034)

```diff
 @dataclass(frozen=True, slots=True)
 class ToolCallRequest:
     request_event_id: str
     tool_name: str
     agent_id: str
     params: Mapping[str, JsonValue]
     requested_at: datetime
     correlation_id: Optional[UUID] = None
-    expires_at: Optional[datetime] = None
+    expires_at: datetime  # ADR-075: mandatory
```

### 3.4 Modified: `ToolCallTTLSweeperSystem` (ADR-045)

Replace the `tool.<name>.failed` emit with **two paths**:

```python
# ADR-075 §2.3 — re-dispatch on stale.
if was_acknowledged:
    emit "tool.<name>.acknowledged_stale" event
    # If tool is idempotent OR force_idempotent=True:
    re_emit "tool.<name>.requested" (same request_event_id)
    # Else:
    #   route to DLQ for operator decision
else:
    emit "tool.<name>.unacknowledged_stale" event
    # If tool is idempotent OR force_idempotent=True:
    re_emit "tool.<name>.requested" (same request_event_id)
    # Else:
    #   route to DLQ for operator decision
```

Both paths log a `tool_call.recovered` (success) or
`tool_call.dlq` (DLQ) structured log for observability.

### 3.5 New: `WorkerManager.acknowledge(...)`

```python
# src/kntgraph/tools/manager.py
class WorkerManager:
    async def acknowledge(
        self,
        request_event_id: str,
        worker_id: str,
    ) -> None:
        """Emit ``tool.<name>.acknowledged`` for the
        worker's current task.

        Called by Worker base class on task start
        (after reading from queue, before running).
        """
        # ... emit event with request_event_id, worker_id,
        # timestamp, correlation (inherited from request).
```

### 3.6 New: `dispatcher.in_flight_tasks()` / `stale_tasks()` / `dead_lettered_tasks()`

Read-only queries. Implemented in `ReactiveDispatcher`:

```python
class ReactiveDispatcher:
    async def in_flight_tasks(
        self, agent_id: str | None = None
    ) -> list[InFlightTask]:
        """Read all ``tool.<name>.requested`` events that
        do not have a matching ``tool.<name>.completed``
        or ``failed``, joined with the ``acknowledged``
        event if present.
        """
        # Uses subscribe_many to scan EventLog; joins
        # request → acknowledgment → completion.
```

The implementation details (scan strategy, caching) are
deferred to PR-N. This ADR only commits to the API.

---

## 4. Backward compatibility

| Change | Backward compat |
|---|---|
| `ToolCallTTL.default_ttl_seconds > 0` enforced | Existing deployments that set `0` (opt-out) **break**. Migration: set a finite default (e.g., `300.0`). |
| `ToolCallRequest.expires_at: datetime` (non-Optional) | Code that constructs `ToolCallRequest` directly without `expires_at` **breaks**. Migration: always pass `expires_at`. |
| New `tool.<name>.acknowledged` event type | Workers that don't emit it work but are not re-dispatched on stale (treated as "never picked up" → DLQ for idempotent tools, DLQ for non-idempotent). |
| New `dispatcher.in_flight_tasks()` etc. | Pure addition. |
| Modified TTL sweeper (re-dispatch instead of failed) | Behavior change for idempotent tools: **stale → re-dispatched**, not **stale → failed**. This is the intended change but operators should be aware. |

---

## 5. Migration

### Phase 1 — Mandatory TTL (no worker changes)

- Tighten `ToolCallTTL.__post_init__` (ADR-075 §3.2).
- Tighten `ToolCallRequest` field type (ADR-075 §3.3).
- Existing deployments must set a finite default.
- No new event types, no worker changes.

### Phase 2 — Worker acknowledgment (worker library update)

- Add `tool.<name>.acknowledged` to `ToolEventKind`.
- Update `Worker` base class to emit acknowledgment on task start.
- Add `WorkerManager.acknowledge(...)` API.
- Custom workers opt-in via explicit call.
- Update projection to materialise `tool_acknowledgments` slot.

### Phase 3 — Re-dispatch (recovery logic)

- Modify `ToolCallTTLSweeperSystem` per ADR-075 §3.4.
- Add idempotency flag to `Tool` Protocol (`idempotent: bool`).
- Add `ToolRegistry.force_idempotent` config (dev/test only).

### Phase 4 — Observability (read-only)

- Add `in_flight_tasks` / `stale_tasks` / `dead_lettered_tasks` to
  `ReactiveDispatcher`.
- Subscribe-based implementation.
- Dashboard integration (separate ADR or work item).

---

## 6. Tests

| Test | Validates |
|---|---|
| `test_default_ttl_must_be_positive` | `ToolCallTTL(default_ttl_seconds=0)` raises. |
| `test_request_must_have_expires_at` | `ToolCallRequest(expires_at=None)` raises. |
| `test_acknowledged_event_lands_in_slot` | Worker emits `acknowledged`; projection materialises. |
| `test_stale_with_ack_re_dispatches` | Stale + acknowledged → idempotent → re-dispatch event emitted. |
| `test_stale_with_ack_dlqs_when_not_idempotent` | Stale + acknowledged → non-idempotent → DLQ entry. |
| `test_stale_without_ack_re_dispatches` | Stale + no ack → idempotent → re-dispatch. |
| `test_in_flight_tasks_query` | `dispatcher.in_flight_tasks()` returns expected tasks. |
| `test_worker_crash_during_execution_recovered` | Worker ack → crash → next tick → re-dispatch. |
| `test_worker_never_picked_up_recovered` | Request → no ack → next tick → re-dispatch. |
| `test_complete_event_after_stale_is_deduped` | Re-dispatch + original completion → one event effective. |

---

## 7. Open questions

1. **Where does the acknowledgment live?** In the agent's EventLog
   stream, or a separate worker-events stream? Putting it in the
   agent's stream keeps the join simple but pollutes the agent's
   event vocabulary. A separate stream (`knt:tool_acks`) keeps
   things clean but requires cross-stream joins.
   *Recommendation*: agent's stream. Polluting is minor; cross-stream
   join is complex.

2. **Idempotency registry**: how does the framework know which tools
   are idempotent? Options:
   - Per-tool flag in the `Tool` Protocol (`idempotent: bool`).
   - Operator registry (`ToolRegistry.idempotent_tools`).
   - Default: idempotent (safer — retry is the right default).
   - Escape: `ToolRegistry.force_idempotent=False` per tool.
   *Recommendation*: per-tool flag in `Tool` Protocol, defaulting
   to `True`. Operators opt out per-tool if non-idempotent.

3. **DLQ integration**: which existing DLQ, new reasons?
   The framework's `DLQReason` enum has PROCESSING_FAILED,
   MAX_RETRIES_EXCEEDED, etc. Add `TOOL_STALE` and
   `TOOL_NOT_IDEMPOTENT` reasons? Or reuse PROCESSING_FAILED?
   *Recommendation*: new `TOOL_STALE_ACKNOWLEDGED` and
   `TOOL_STALE_UNACKNOWLEDGED` reasons; existing
   `PROCESSING_FAILED` for terminal failures.

4. **In-flight task query performance**: scanning the EventLog on
   every `in_flight_tasks()` call is O(N). For high-throughput
   systems, cache the view. Cache TTL? Invalidation trigger?
   *Recommendation*: deferred to PR-N. Initial implementation is
   scan-based with a TODO for caching.

5. **Multi-dispatcher**: in a sharded deployment (ADR-035), each
   shard owns a subset of agents. Cross-shard re-dispatch is
   tricky. For v1, re-dispatch stays within the shard that
   processed the original request.
   *Recommendation*: deferred. Shard-aware re-dispatch is a
   follow-up ADR.

---

## 8. References

- Evans, E. *Domain-Driven Design*, 2003 — Repository pattern
- Richardson, C. *Microservices Patterns*, 2018 — Chapter 4, Saga
- [ADR-019 — DLQ](./ADR-019-Redis-Adapter-Typing.md)
- [ADR-034 — ToolCall ECS Components](./ADR-034-ToolCall-ECS-Components.md)
- [ADR-036 — Tool Worker Pattern](./ADR-036-Tool-Worker-Pattern.md)
- [ADR-045 — Tool Call TTL](./ADR-045-Tool-Call-Request-TTL.md)
- [ADR-068 — EventLog subscribe](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md)
- [ADR-070 — Worker-Level Back-Pressure](./ADR-070-Worker-Level-Back-Pressure.md)
- [ADR-071 — BusinessFSM Concordo](./ADR-071-BusinessFSM-Concordo.md)
- [ADR-072 — WorkflowSaga Concordo](./ADR-072-WorkflowSaga-Concordo.md)
