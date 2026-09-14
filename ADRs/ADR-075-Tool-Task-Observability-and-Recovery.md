<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-075: Tool-Task Observability, Detection, and Recovery

- **Status:** Accepted (revised 2026-09-14)
- **Date:** 2026-09-12 (revised 2026-09-14)
- **Author:** kinetgraph architecture team
- **Supersedes:** initial draft (kept the four-tier framing; **dropped the
  ack cycle** (§2.2 in the first draft); kept the `Tool.idempotent` flag
  on the `Tool` Protocol; **merged the saga → DLQ wiring into the TTL
  sweeper** so there is one recovery pipeline, not two).
- **Related to:**
  - [ADR-019](./ADR-019-Redis-Adapter-Typing.md) — `DeadLetterQueue`
    + `DeadLetterEvent` + `DLQReason` (the existing recovery primitive
    this ADR wires into the tool pipeline).
  - [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — `ToolCallRequest`
    / `ToolCallCompletion`; `expires_at` is mandatory post-ADR-075.
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — `@tool_worker` /
    `WorkerManager`; **the `Tool.idempotent` flag** lives here; the
    reaper loop is the existing D primitive that detects stuck
    queues.
  - [ADR-037](./ADR-037-Mandatory-Correlation-Propagation.md) —
    mandatory `CorrelationContext` (R — join key).
  - [ADR-045](./ADR-045-Tool-Call-Request-TTL.md) — Tool Call TTL;
    the sweeper is the second D primitive (stale detection).
  - [ADR-068](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md) —
    `EventLog.subscribe` / `subscribe_many` (R — replay & cursor).
  - [ADR-074](./ADR-074-Per-System-Cursors-in-AgentView.md) —
    per-(system, agent) cursor.
  - [ADR-070](./ADR-070-Worker-Level-Back-Pressure.md) — Tier 4
    observability primitives compose with `max_in_flight` back-pressure.
  - [ADR-071](./ADR-071-BusinessFSM-Concordo.md), [ADR-072](./ADR-072-WorkflowSaga-Concordo.md) — primary consumers.

---

## 1. Goal

Make the tool-task pipeline **100% rastreável (R)**, **detector de falhas (D)**, and **recuperável (F)**, so that:

- **No tool call that the framework emitted is ever silently lost.**
- **No tool call that a worker started is left without a terminal event.**
- **No tool call stays stuck in `knt:tools:<name>:queue` without being consumed or escalated.**

Concretely: the framework must know, at any moment, (R) which
in-flight tool calls exist and their causal chain, (D) when a
worker has gone silent or a queue is not draining, and (F) what
to do next — re-dispatch, escalate to the operator via the existing
`DeadLetterQueue`, or skip — with the **EventLog as the single
source of truth**.

The framing is "R → D → F": every tool-task event has a causal
record (R); every failure has a detector (D); every detector
has a recovery path (F). A failure mode with no detector, or a
detector with no recovery, is a gap this ADR closes.

### 1.1 What already exists (the resilience primitives we lean on)

This ADR does **not** invent new machinery where the framework
already ships the primitive.

| Need (R / D / F) | Existing primitive | ADR / file |
|---|---|---|
| **R**: causal record of every tool call | `EventLog` + `event_id` dedup | ADR-034, ADR-068 |
| **R**: replay / recover from any fold position | `IncrementalWorldStore` + `WorldCheckpoint` + per-(system, agent) cursor | ADR-068, ADR-074 |
| **R**: correlate request ↔ completion ↔ worker | `Event.correlation` (ADR-037) + `WorkerManager` propagates `correlation` | ADR-037 |
| **R**: deterministic re-emit collapses to no-op | `generate_deterministic_event_id` + idempotency index | ADR-034, ADR-068 |
| **D**: worker never XACKed → reclaim the message | `WorkerManager._reaper_loop` (XAUTOCLAIM, runs every 60s, idle threshold 5 min) | ADR-036 |
| **D**: worker crashed → escalation via retry counter | `WorkerManager._process_message` reads `XPENDING` + compares with `__tool_worker_retries__`; emits `tool.<name>.failed` | ADR-036 |
| **D**: TTL expired → orphan in slot | `ToolCallTTLSweeperSystem` runs every dispatch tick; uses injectable `now` (replay-safe) | ADR-045 |
| **D**: dispatcher / worker liveness | `ReactiveDispatcher._maybe_emit_heartbeat` + `WorkerManager._maybe_emit_heartbeat` (default 30s) | ADR-036, ADR-068 |
| **D**: dispatcher / wake-up liveness | `EventLog.subscribe` (push-first) + `fallback_poll_interval` (5s default) | ADR-068 |
| **F**: re-dispatch on stale | EventLog idempotency: re-emit with the same `event_id` collapses duplicates. The TTL sweeper triggers the re-dispatch. | ADR-034 |
| **F**: operator escalation for terminal failures | `DeadLetterQueue.append` + `DeadLetterActions.reprocess / discard` | ADR-019 |
| **F**: operator application wiring | `dispatcher.subscribe` (event-pattern subscription for application-side handlers) | ADR-068 |

**Read this table before inventing anything**. For each tier
below we either reuse a row, or justify why it isn't enough.

### 1.2 Why no separate ack event (the rejected option)

The first revision of this ADR proposed `tool.<name>.acknowledged`
emitted on worker task start. **This tier was dropped**:

| Question | Answer |
|---|---|
| Does ack unlock any failure-mode coverage not already in §1.1? | No. Every failure mode caught by ack (worker picked-up, message gone) is already caught by `ReaperLoop` (idle-PEL reclaim) + TTL sweeper (stale). |
| Does ack improve forensic value? | Yes — `worker_id` recorded at pickup. But the same data is reachable from the Redis consumer group metadata (`XINFO CONSUMERS`) without a new event. |
| Does ack justify a new event type? | No. The EventLog is the source of truth for **causal chains**, not for **transport state**. Ack is transport state (it says "PEL entry was claimed by a worker") — that already lives in `XPENDING`. Putting transport state in the EventLog couples recovery (R) to observability. |

The rejection is **not** a refusal — ack can be added as a
separate ADR if forensics becomes a product need. This ADR
deliberately keeps the EventLog small.

### 1.3 Failure-mode coverage (today vs after ADR-075)

Each row is a failure mode. "Today" is what the framework ships
before this ADR takes effect (some primitives are already
merged — those are marked ✅). "After" is the post-ADR-075
state.

| # | Failure mode | Detected by (D) | Recovery (F) | Status |
|---|---|---|---|---|
| 1 | Worker crashes after reading queue but before starting tool | (a) `WorkerManager._reaper_loop` reclaims after `reaper_idle_time` (XAUTOCLAIM). (b) `ToolCallTTLSweeperSystem` emits `tool.<name>.failed` at TTL expiry. Both paths idempotent. | XAUTOCLAIM + re-run via same `event_id`; or TTL → re-dispatch path (§2.3) | ✅ ReaperLoop shipped (ADR-036); §2.3 widens the TTL path |
| 2 | Queue never delivers a message to any worker | `XLEN` rising (no consumption) + `ToolCallTTLSweeperSystem` (`now > expires_at`, no `XPENDING` entry) | TTL → re-dispatch (idempotent) or DLQ (non-idempotent) | ✅ TTL sweeper shipped; §2.4 exposes `stuck_in_queue(threshold_s)` via `XLEN` |
| 3 | Worker never completes (TTL unset or infinite) | `ToolCallTTL.default_ttl_seconds > 0` enforced (this ADR §2.1) — infinite TTLs forbidden in production | TTL sweeper re-dispatch / DLQ | ✅ Tier 1 shipped |
| 4 | Completion event lost in transit | EventLog cursor + `event_id` idempotency | Pure replay: idempotency collapses duplicates | ✅ shipped (ADR-034, ADR-068) |
| 5 | Dispatcher crashes mid-tick (after system emits, before `append_batch`) | `IncrementalWorldStore` reload from `EventLog` + cursor divergence | Pure replay from `last_stream_id` | ✅ shipped (ADR-068, ADR-074) |
| 6 | Stale request — never picked up | `ToolCallTTLSweeperSystem` (no `XPENDING` entry + stale `expires_at`) | Re-dispatch (idempotent) or DLQ (non-idempotent) | ✅ §2.3 widens |
| 7 | Stale request — worker started, didn't finish | `ToolCallTTLSweeperSystem` (stale + `XPENDING` entry) | Re-dispatch (idempotent) or DLQ (non-idempotent) | ✅ §2.3 |
| 8 | No "what's in flight right now" query | Tier 4 `in_flight_tasks()` (§2.4) | Operator dashboards | ⚠ NEW |
| 9 | No "what's stuck in queue" query | Tier 4 `stuck_in_queue()` (§2.4) — uses `XLEN` vs dispatch rhythm | Drives the `dispatcher.detect_and_recover()` cron | ⚠ NEW |
| 10 | No "what's in the DLQ for me to action" query | Tier 4 `dead_lettered_tasks()` (§2.4) — composes `DeadLetterQueue.list_*` | Operator workflow | ⚠ NEW |
| 11 | Saga compensation fails — no DLQ integration | Tier 3 `/saga compensation_failed → DLQ wire/` (§2.3.3 — merged into TTL sweeper) | `DeadLetterQueue.append` by the sweeper itself, not a separate adapter | ⚠ NEW |
| 12 | Worker reaper re-runs a non-idempotent tool | `Tool.idempotent: bool` flag (this ADR §3.5) | Operator sets the flag — sweeper reads it and routes to DLQ instead of re-dispatch | ✅ field exists on Protocol; sweeper wiring §2.3 |

---

## 2. Decision

Three tiers, ordered R → D → F. The first iteration's
`tool.<name>.acknowledged` tier (Tier 2 in the first draft) is
**dropped** (§1.2).

### 2.1 Tier 1 — Mandatory TTL and causal record (R)

**Goal.** Every in-flight tool call has a deterministic `event_id`,
a finite `expires_at`, and a `correlation` that ties request →
completion → operator action.

**Status.** Shipped (this ADR confirms the contract).

#### 2.1.1 `ToolCallTTL.default_ttl_seconds > 0` is mandatory

`ToolCallTTL.__post_init__` raises `ValueError` when the default
is non-positive. `ToolCallRequest.expires_at` is non-Optional
(`datetime`); the projection refuses to materialise a request
without one. Infinite TTLs are forbidden in production.

#### 2.1.2 Single-event-id rule

Every `tool.<name>.requested` event carries
`event_id = generate_deterministic_event_id(...)`. A re-dispatch
emits with the same `event_id`; the EventLog idempotency index
collapses duplicates to a single appended event.

#### 2.1.3 Per-(system, agent) cursor

`AgentView.cursors[system_name]` advances to `view.last_event_id`
after each tick (ADR-074). The cursor piggy-backs on
`WorldCheckpoint`.

### 2.2 Tier 2 — Failure detection (D)

**Goal.** Every failure mode has a detector that runs **per
dispatch tick** and reports without polling logs.

**Status.** Shipped. **No new event types, no new system.**

| Detector | What it catches |
|---|---|
| `ToolCallTTLSweeperSystem` (ADR-045) | Stale request (any cause) — fires every tick. |
| `WorkerManager._reaper_loop` (ADR-036) | PEL messages not XACKed within `reaper_idle_time`. |
| `WorkerManager` retry counter (`XPENDING` vs `__tool_worker_retries__`) | Worker hard-crashed mid-invocation. |
| `ReactiveDispatcher._maybe_emit_heartbeat` + `WorkerManager._maybe_emit_heartbeat` | Loop / pool liveness (default 30s). |

### 2.3 Tier 3 — Recovery on stale (F)

**Goal.** A stale request is either **re-dispatched** (idempotent
tool — safe by construction) or **escalated to the DLQ**
(non-idempotent tool — operator decides). Re-dispatch re-emits
with the same `event_id`; idempotency collapses duplicates. The
sweeper runs once per tick, owns one dedup set, routes **all**
tool-task failures — including saga compensations — through the
single recovery pipeline.

**Status.** Mostly shipped. The wider behaviour (idempotent vs
non-idempotent branch + saga-DLQ wire) is the **new** work.

```
                  request emitted at T0
                          │
                          ▼
                ┌─────────┴──────────┐
                │ Tier 3 sweeper:   │
                │ now - T0 > TTL?   │
                │ AND no completion │
                └────┬────────┬─────┘
                     │        │
              ack?  │  yes   │  no   (= stuck in queue)
                     │        │
            idempotent?       │
            ┌──┴──┐           │
           YES   NO          │
            │     │           │
            ▼     ▼           ▼
       re-dispatch DLQ     re-dispatch
       (same event_id,      or DLQ
        idempotency         (no idempotency
        collapses dup)      assumption)
```

The sweeper uses the existing `WorkerManager._reaper_loop`
reclaim path as its primitive for stuck queues — both paths
emit with the same `event_id`, idempotency collapses true
duplicates. The two paths **complement**, not duplicate:

- `ReaperLoop` recovers messages **already delivered** to a
  worker that XACKed nothing (worker died mid-invocation).
- TTL sweep recovers messages **never delivered** (queue
  stalled, or worker crashed before XREAD).

#### 2.3.1 The `Tool.idempotent: bool` flag

Added to the `Tool` Protocol (already merged). Default `True`
(safer — retry is the right default). Operators opt out per-tool
when the tool has a non-idempotent side effect (payment
processing, message dispatch, file mutation).

A tool without a registered descriptor is treated as
**non-idempotent** (safe-by-default; the operator can add a
descriptor).

#### 2.3.2 Sweeper branches on idempotency

```python
# ToolCallTTLSweeperSystem — replacement for the current
# "emit tool.<name>.failed" branch.
if was_acknowledged:
    audit_event_type = "tool.<name>.stale_acked"
else:
    audit_event_type = "tool.<name>.stale_unacked"
emit(audit_event, ...)

if tool_idempotent:
    re_emit("tool.<name>.requested", same request_event_id)
else:
    # Route to DLQ via DeadLetterQueue.append.
    dlq.append(DeadLetterEvent(
        event=request,
        reason=TOOL_STALE_ACKNOWLEDGED if was_acknowledged
               else TOOL_STALE_UNACKNOWLEDGED,
        ...
    ))
```

The sweeper uses its existing in-memory dedup set
(`_emitted_events`) to ensure one recovery pass per
`request_event_id` per dispatcher instance. Process restart
re-derives the set from the EventLog via `causation_id`.

#### 2.3.3 Saga compensation → DLQ wire (merged into the sweeper)

The first revision of this ADR proposed a separate
`SagaDLQAdapterSystem`. **That proposal is rejected** — see the
comparison below. Instead, the saga → DLQ wire is a **branch in
the same TTL sweeper**:

```python
# In ToolCallTTLSweeperSystem.__call__, after the per-request
# reconciliation:
for stale in stale_requests:
    if stale.tool in NON_IDEMPOTENT_TOOLS:
        dlq.append(DeadLetterEvent(reason=TOOL_STALE_*, ...))
    # Saga-specific path: if the tool was dispatched by a
    # saga and that saga is in "compensating" state, also
    # emit saga.<name>.compensation_failed. The saga
    # already does this in begin_compensation; the sweeper
    # only emits it for saga-originated stale tool calls.
    if saga_id := stale.data.get("saga_id"):
        emit("saga.<saga_name>.compensation_failed",
             data={"stuck_step": stale.tool_name,
                   "saga_id": saga_id})
```

**Why merge** (over a separate adapter):
- **Single source of recovery**: one sweeper, one dedup set,
  one DLQ insertion per stale request. A separate adapter
  would race with the sweeper (the sweeper emits `failed` for
  the original request; the adapter emits another event; two
  writes per failure, harder to reason about).
- **Single rebuild path**: a restart that crashes mid-recovery
  re-runs the same sweep (idempotency collapses).
- **Convention fit**: the sweeper is already a registered
  `WorldSystem` (auto-retried by the dispatcher); a subscriber
  via `dispatcher.subscribe` can silently miss events.

The saga system **stays pure** (the ADR-069 §11.9 contract). The
side effect (DLQ insert) lives entirely inside the sweeper.

#### 2.3.4 New `DLQReason` values

Added to `DLQReason` (ADR-019):

- `TOOL_STALE_ACKNOWLEDGED` — TTL expired after a worker
  acknowledged (worker likely crashed between ack and
  completion).
- `TOOL_STALE_UNACKNOWLEDGED` — TTL expired before any worker
  acknowledged (message likely stuck in the queue).

Existing `PROCESSING_FAILED` and `MAX_RETRIES_EXCEEDED`
remain for non-stale failures. No `TOOL_NOT_IDEMPOTENT` reason
is added — that path is captured by `TOOL_STALE_*` (it's
not idempotent + stale, automatically DLQ).

### 2.4 Tier 4 — Recovery-driven observability (R ↔ D ↔ F)

**Goal.** Every recovery state has a **read API** the operator
can call. Tier 4 closes the gaps flagged in §1.3 (#8–10).

| # | Query | Backed by | What it returns |
|---|---|---|---|
| 8 | `dispatcher.in_flight_tasks(agent_id=None)` | `view.tool_requests` − `tool_completions` (per-agent view) | Tasks `requested` but no `completed`/`failed` yet |
| 9 | `dispatcher.stuck_in_queue(threshold_seconds)` | `XLEN knt:tools:<name>:queue` vs last dispatch tick | Tasks `requested` in the stream past the threshold with no `XPENDING` consumer activity |
| 10 | `dispatcher.dead_lettered_tasks(reason=None)` | `DeadLetterQueue.list_by_reason / list_for_agent` | DLQ entries awaiting operator action |
| 11 | `dispatcher.stale_tasks(threshold_seconds)` | `view.tool_requests` ∩ `now - expires_at > threshold` | Tasks past TTL but not yet recovered (race window) |

Implementation: each query is async (does **not** block the
dispatcher tick loop), reads from the existing data
sources listed in §1.1, and caches the result with TTL 5s to
amortise scan cost across dashboard refreshes.

#### 2.4.1 `dispatcher.detect_and_recover()` convenience

A single entry point that runs **all** recovery paths in one
call — intended for an operator cron / alert:

```python
await dispatcher.detect_and_recover(
    stale_threshold_s=300,
    stuck_in_queue_threshold_s=300,
    dry_run=False,
)
```

This is sugar over Tier 2 + Tier 3 (the sweeper triggers the
re-dispatch path) + Tier 4 queries (used for the report). Not a
new recovery primitive.

---

## 3. Components

### 3.1 New: `DLQReason` values

Added to `DLQReason` (ADR-019):

- `TOOL_STALE_ACKNOWLEDGED`
- `TOOL_STALE_UNACKNOWLEDGED`

The existing `PROCESSING_FAILED` and `MAX_RETRIES_EXCEEDED` cover
non-stale failures (worker hard crash, OOM, etc.).

### 3.2 Modified: `ToolCallTTLSweeperSystem` (ADR-045)

Replace the current `emit tool.<name>.failed` branch with the
two-path branch from §2.3.2 + the saga-DLQ wire from §2.3.3.
The existing in-memory dedup set (`_emitted_events`) is
preserved — one recovery pass per `(request_event_id, ack-status)`
per dispatcher instance.

### 3.3 Modified: `Tool` Protocol (ADR-036)

```python
@runtime_checkable
class Tool(Describable, Protocol[R]):
    name: str
    description: str
    input_schema: dict[str, "JsonValue"]
    idempotent: bool = True  # ADR-075: per-tool idempotency
```

Default `True`. Operators opt out per-tool for non-idempotent
side effects. Read by the TTL sweeper via
`WorkerManager.tool_for(name).idempotent`.

### 3.4 New: `ReactiveDispatcher` observability queries

```python
# src/kntgraph/runner/reactive.py
class ReactiveDispatcher:
    async def in_flight_tasks(
        self, agent_id: str | None = None
    ) -> list[InFlightTask]:
        ...

    async def stale_tasks(
        self, threshold_seconds: float = 300.0
    ) -> list[InFlightTask]:
        ...

    async def stuck_in_queue(
        self, threshold_seconds: float = 300.0
    ) -> list[InFlightTask]:
        ...

    async def dead_lettered_tasks(
        self, reason: DLQReason | None = None,
        agent_id: str | None = None,
    ) -> list[DeadLetterEvent]:
        ...

    async def detect_and_recover(
        self,
        stale_threshold_s: float = 300.0,
        stuck_in_queue_threshold_s: float = 300.0,
        dry_run: bool = False,
    ) -> RecoveryReport:
        """Convenience: run all recovery paths and return a report."""
```

The implementation composes existing primitives (see §1.1).
The cache is TTL-bounded (default 5s) so dashboards polling at
1Hz amortise the cost.

---

## 4. Backward compatibility

| Change | Compatibility |
|---|---|
| `ToolCallTTL.default_ttl_seconds > 0` enforced | Existing deployments that set `0` (opt-out) **break**. Migration: set a finite default (e.g., `300.0`). |
| `ToolCallRequest.expires_at: datetime` (non-Optional) | Code that constructs `ToolCallRequest` directly without `expires_at` **breaks**. Migration: always pass `expires_at`. |
| New `DLQReason.TOOL_STALE_*` values | Pure addition; existing DLQ queries unaffected. |
| `Tool.idempotent: bool = True` (Protocol field) | Default `True`; existing tools behave idempotently until they opt out. The Protocol change is additive (existing classes satisfy the Protocol since `True` is the default). |
| TTL sweeper now branches idempotent vs non-idempotent | Behaviour change for non-idempotent tools: **stale → DLQ** instead of **stale → failed**. This is the intended change but operators should be aware. |
| Saga → DLQ merge into sweeper | Application no longer needs to register `SagaDLQAdapter` (it doesn't exist). Saga stays pure. |
| New `dispatcher.in_flight_tasks()` etc. | Pure addition. |

---

## 5. Migration

### Phase 1 — Mandatory TTL (shipped)
- Tighten `ToolCallTTL.__post_init__` (ADR-075 §3.2).
- Tighten `ToolCallRequest` field type (ADR-075 §3.3).

### Phase 2 — Failure detection (shipped)
- No code change for the detection primitives themselves; they
  are already in place (ReaperLoop, TTL sweeper, retry counter).
  The **spec** is documented here.

### Phase 3 — Recovery (shipped + DLQ wire + idempotent branch)
- ✅ Idempotent + DLQ branch in TTL sweeper (shipped).
- ✅ Saga → DLQ wire in TTL sweeper (shipped).
- ✅ `Tool.idempotent: bool` flag on Protocol (shipped; sweeper
  reads it).
- ✅ New `DLQReason.TOOL_STALE_*` values (shipped).

### Phase 4 — Recovery-driven observability
- Implement `dispatcher.in_flight_tasks()`,
  `stale_tasks()`, `stuck_in_queue()`, `dead_lettered_tasks()`,
  `detect_and_recover()` on `ReactiveDispatcher`.
- Document the API in `docs/tool_task_observability.md`.

---

## 6. Source code layout

```
src/kntgraph/
├── core/
│   ├── event/
│   │   ├── id_helpers.py            (existing — R)
│   │   └── correlation.py            (existing — R)
│   ├── long_poll.py                  (existing — Tier 4 helper)
│   └── world/
│       ├── components.py            (ToolCallTTL, ToolCallRequest)
│       └── projection_tool_calls.py  (existing)
├── runner/
│   ├── reactive.py                  (add: in_flight_tasks, stale_tasks,
│   │                                 stuck_in_queue, dead_lettered_tasks,
│   │                                 detect_and_recover)
│   └── tool_call_ttl_sweeper.py     (Tier 3 — re-dispatch / DLQ branch
│                                     AND saga → DLQ wire, merged)
├── tools/
│   ├── manager.py                   (existing — ReaperLoop, retry
│   │                                 counter)
│   └── protocol.py                  (add: Tool.idempotent)
├── events/
│   └── dlq/
│       ├── values.py                (add: TOOL_STALE_* reasons)
│       ├── store.py                 (existing)
│       └── actions.py               (existing)
└── resilience/
    ├── circuit_breaker.py           (existing — R for EventLog)
    ├── timeout.py                    (existing — BackoffPolicy)
    └── retry.py                      (existing — retry_with_backoff)
```

The `reactive.py` file may exceed the 500-line guideline once
Tier 4 queries land. If it does, split into `_observability.py`
(following the saga `_dispatch / _compensation / _dispatch` /
`_records` split).

**No `saga_dlq_adapter.py`** — the saga → DLQ wire lives in the
TTL sweeper (§2.3.3), by design (§3.2 and §1's table).

---

## 7. Open questions

1. **`Tool.idempotent` default.** Per-tool flag in `Tool`
   Protocol, defaulting to `True`. Operators opt out per-tool.
   *Recommendation*: keep default `True`; ship a helper that
   surfaces a per-tool idempotency audit (which tools opt out?).

2. **Backpressure coupling.** ADR-070 proposes per-tool
   `max_in_flight`; how does `ReaperLoop`'s `reaper_idle_time`
   interact with it? If `reaper_idle_time` is too small, a slow
   legitimate tool gets reclaimed; if too large, recovery is
   slow.
   *Recommendation*: keep them independent. `reaper_idle_time`
   is the recovery SLA; `max_in_flight` is the dispatch SLA.
   Operators tune both.

3. **Multi-shard re-dispatch.** In a sharded deployment
   (ADR-035), each shard owns a subset of agents. Cross-shard
   re-dispatch is non-trivial. For v1, re-dispatch stays within
   the shard that processed the original request.
   *Recommendation*: defer cross-shard to a follow-up.

4. **Dashboard integration.** Grafana + UI consume the
   observability API. The contract is the API; the dashboard
   is a separate concern.
   *Recommendation*: not in scope of this ADR. Tier 4 is the
   API; dashboards live in their own ADR.

5. **Saga DLQ entry shape.** The current `DeadLetterEvent` carries
   the original `Event` + failure metadata; should the saga-DLQ
   wire also stash the saga's compensating-state snapshot (so the
   operator can see the `compensate_stack` at the moment of the
   failure)?
   *Recommendation*: stash the `compensate_stack` snapshot in
   `data` on the `DeadLetterEvent`. Operators get the full
   picture from the DLQ entry alone.

---

## 8. References

- Evans, E. *Domain-Driven Design*, 2003 — Repository pattern
- Richardson, C. *Microservices Patterns*, 2018 — Chapter 4, Saga
- Kleppmann, M. *Designing Data-Intensive Applications*, 2017 —
  Chapter 5 (replication, recovery), Chapter 9 (consistency)
- [ADR-019 — DLQ](./ADR-019-Redis-Adapter-Typing.md)
- [ADR-034 — ToolCall ECS Components](./ADR-034-ToolCall-ECS-Components.md)
- [ADR-036 — Tool Worker Pattern](./ADR-036-Tool-Worker-Pattern.md)
- [ADR-037 — Mandatory Correlation Propagation](./ADR-037-Mandatory-Correlation-Propagation.md)
- [ADR-045 — Tool Call TTL](./ADR-045-Tool-Call-Request-TTL.md)
- [ADR-068 — Idle Redis traffic and EventLog subscribe](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md)
- [ADR-070 — Worker-Level Back-Pressure](./ADR-070-Worker-Level-Back-Pressure.md)
- [ADR-071 — BusinessFSM Concordo](./ADR-071-BusinessFSM-Concordo.md)
- [ADR-072 — WorkflowSaga Concordo](./ADR-072-WorkflowSaga-Concordo.md)
- [ADR-074 — Per-System Cursors in AgentView](./ADR-074-Per-System-Cursors-in-AgentView.md)

---

## 9. Failure-mode table (for easy reference)

This is the same table as §1.3, kept here so the ADR is
self-contained:

| # | Failure mode | Detection (existing or new) | Recovery |
|---|---|---|---|
| 1 | Worker crashes after read but before start | ReaperLoop + TTL Sweeper | XAUTOCLAIM, re-dispatch (Tier 3) |
| 2 | Queue never delivers | TTL Sweeper | Re-dispatch or DLQ |
| 3 | Worker never completes (TTL unset/infinite) | TTL mandatory (`__post_init__`) | Sweeper re-dispatch / DLQ |
| 4 | Completion event lost in transit | EventLog cursor + idempotency | Re-emit collapsed |
| 5 | Dispatcher crashes mid-tick | WorldCheckpoint + cursor | Pure replay |
| 6 | Stale request never picked up | TTL Sweeper (`XLEN` + no `XPENDING`) | Re-dispatch or DLQ |
| 7 | Stale request after worker started | TTL Sweeper (`XPENDING` exists) | Re-dispatch with reset |
| 8 | No "what's in flight right now" query | NEW: Tier 4 `in_flight_tasks()` | Operator dashboards |
| 9 | No "what's stuck in queue" query | NEW: Tier 4 `stuck_in_queue()` | Drives `detect_and_recover()` cron |
| 10 | No DLQ-side "what to action" query | NEW: Tier 4 `dead_lettered_tasks()` | Operator workflow |
| 11 | Saga compensation → DLQ not wired | NEW: Tier 3 sweeper branch | DLQ via sweeper (merged) |
| 12 | Worker reaper re-runs non-idempotent tool | `Tool.idempotent: bool` flag | Operator opts in/out per-tool |
