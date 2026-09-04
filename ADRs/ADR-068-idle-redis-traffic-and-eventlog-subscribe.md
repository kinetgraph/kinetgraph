<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-068: Idle Redis traffic — the EventLog subscribe primitive and the incremental read paths

- **Status:** Proposed
- **Date:** 2026-09-04
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-002](./ADR-002-Replay-Puro.md) — pure replay; EventLog as source of truth
  - [ADR-005](./ADR-005-Checkpoints-Idempotency.md) — idempotency contract
  - [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md) — WorldIncremental + WorldSystem (the incremental dispatcher)
  - [ADR-019](./ADR-019-Redis-Adapter-Typing.md) — typed Redis adapters; `RedisLike` Protocol
  - [ADR-035](./ADR-035-sharding-and-dispatcher-coordination-for-horizontal-scaling.md) — dispatcher sharding
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — Tool Worker Pattern (the healthy blocking consumer)
  - [ADR-042](./ADR-042-Agents-Memory-Model-usage.md) — memory hydration projection
  - [ADR-045](./ADR-045-Tool-Call-Request-TTL.md) — tool-call TTL (reason idle ticks must still run systems)
  - [ADR-057](./ADR-057-durabilidade-dos-dados.md) — data durability; per-class retention
  - [ADR-065](./ADR-065-http-intake-event-driven-review.md) — long-poll → SSE migration at the HTTP edge

> **Scope.** This ADR addresses the **Redis-side traffic
> cost** of the framework's read paths. The diagnosis,
> confirmed by a code-level audit (2026-09-04): every
> subscriber of the EventLog polls — the
> `ReactiveDispatcher` at 4 Hz per agent, the `Runner` at
> 1 Hz with a full-history re-fold, the `Consolidator`/
> `CacheWarmer` pair with a full-stream re-fold and cache
> rewrite per memory agent per tick, and the SSE endpoint
> at 10 Hz **per connected client**. Under zero event
> emission, the traffic is proportional to
> `agents × poll-rate × payload` — not to `events`. The
> ADR proposes a **push-first read model** built on one
> new primitive (`EventLog.subscribe`) plus five
> incremental-read / dirty-write changes that remove the
> payload and round-trip waste independent of the push
> model.

## 1. Context and Problem

### 1.1 The traffic model of a polling framework

Every read path in the framework is a **deadline-driven
poll loop**:

```
while running:
    read(state from Redis)          # round-trip 1
    if nothing new: sleep(interval) # idle cost
    else: process; write state back # round-trip 2
```

This is the correct *semantics* — the EventLog is the
source of truth (ADR-002) and every consumer must observe
it — but it is the wrong *transport* when the interval is
short and the payload is large. With no events flowing,
each loop still pays the full read cost every tick:

| Loop | File / line | Interval | Per-tick Redis ops | Payload |
|---|---|---|---|---|
| `ReactiveDispatcher._loop` | `runner/reactive.py:131` (`poll_interval=0.25`) | 0.25 s | `GET` checkpoint + `XRANGE (cursor` (+ `SET` checkpoint, see §1.3) | pickled World (KB–hundreds of KB per agent) |
| `ReactiveDispatcher` rediscovery | `runner/reactive.py:137` (`5.0s`) | 5 s | `SCAN knt:agents:*:events` | key list |
| `Runner._loop` | `runner/runner.py:78` (`tick_interval=1.0`) | 1.0 s | `SCAN` + `XRANGE` **full history** per agent + `World.fold` | **entire EventLog, every second** |
| `Consolidator` → `CacheWarmer` | `memory/consolidation.py:298`, `memory/cache_warmer.py:182` (0.25 s pump) | 0.25 s | per memory agent: `XRANGE` full-history fold + `DEL`+`HSET`+`EXPIRE` | entire memory-agent stream + full cache rewrite |
| SSE endpoint | `api/intent_router/routes.py:488` | 0.1 s **per client** | `XRANGE (cursor` | parsed `Event` list per poll |
| Long-poll status (deprecated) | `api/intent_router/routes.py:633` | 0.1 s | `log.read(agent_id)` = `XRANGE` **full stream** + decode **all** events | worst payload of all |
| `WorkerManager._consume_loop` | `tools/manager.py:381` | blocking (`block=1000`) | `XREADGROUP count=1 block=1000` | ~0 when idle — **the healthy baseline** |

The `WorkerManager` row is the reference point: a
`XREADGROUP ... BLOCK 1000` holds the connection open and
returns nothing until a message arrives. Idle cost ≈ 0.
That is the transport this ADR wants for the other rows.

### 1.2 Quantified idle baseline

One pod, ten agents, no events emitted:

- `ReactiveDispatcher`: 10 agents × 4 ticks/s ×
  (`GET` + `XRANGE`) ≈ **80 round-trips/s**, two of which
  carry a pickled World each. With a 50 KB checkpoint,
  that is ~400 KB/s of `GET` payload alone.
- `Runner`: 1 fold/s × (`SCAN` + per-agent full `XRANGE`).
  With 10 agents × 1 000 events, that is ~10 000 event
  decodes/s **at zero traffic** — the cost grows O(N)
  with log depth, forever.
- `Consolidator`/`CacheWarmer`: every tick re-enqueues a
  refresh for every memory agent in the World
  (`consolidation.py:309–319` walks `world.agents`
  unconditionally); each refresh folds the full
  memory-agent stream and rewrites the whole cache hash.
- SSE: 10 clients × 10 polls/s = 100 `XRANGE`/s that all
  return empty.
- Long-poll (if any legacy client remains): each poll
  decodes the **entire** agent stream every 100 ms during
  its 5 s window — up to 50 full-stream scans per request.

Aggregate: an idle 10-agent pod sustains
**hundreds of Redis round-trips per second**, several with
large payloads. The call-rate is flat in `events`; it is
driven entirely by the poll cadences above.

### 1.3 Why the loops exist (constraints to preserve)

The audit found each loop defensible *semantically*; the
problem is the transport, not the contract:

- **Idle ticks run the systems on purpose** (ADR-045):
  the `ToolCallTTLSweeperSystem` must evict orphan tool
  requests on ticks where the log is empty, and systems
  with queued `_pending_results` (ADR-049) must be pumped.
  `runner/reactive.py:344–356` encodes this. Any
  wake-up redesign must preserve "systems still run when
  there is nothing new" — the fix is to make idle ticks
  *cheap*, not to remove them.
- **Checkpoint save on idle ticks**: when
  `tool_ttls` is set or a system emitted events,
  `run_systems_and_persist` (`runner/_systems_runner.py:97`)
  saves the checkpoint even with `new_event_count == 0`.
  Correct for cursor advancement, but it re-`SET`s an
  unchanged pickled World every 0.25 s per agent.
- **The SSE poll-and-yield is documented as a stopgap**
  (`routes.py:429–442`): "the EventLog does not yet expose
  a `subscribe(agent_id)` primitive … When the volume
  justifies it, the internals of this endpoint swap to a
  real Pub/Sub channel without changing the public
  contract." The volume question is now answered: this
  ADR.
- **Idempotency must hold under push**: ADR-005 dedupe and
  the ADR-018 checkpoint-after-append ordering are the
  crash-safety net. A push model that can drop
  notifications is acceptable **only** because a periodic
  fallback poll guarantees convergence; the notification
  is a latency optimisation, never a correctness
  dependency.

### 1.4 The missing primitive

`RedisLike` (`infra/redis/_client.py:78–194`) declares
`xreadgroup` but **not `xread`**. There is no
`EventLog.subscribe(agent_id)`. Every EventLog consumer
therefore invents its own polling cadence, and the poll
interval becomes the framework's de-facto latency/traffic
knob — hardcoded, per consumer, and invisible to
`Settings` (§2.8).

## 2. Decision

Adopt a **push-first, poll-as-fallback read model**:

1. **New primitive** — `EventLog.subscribe(agent_id)` /
   `subscribe_many(...)` backed by blocking `XREAD`
   (§3.1). The notification is a *hint*; the reader still
   reads via the cursor path, so dropped hints are
   invisible.
2. **All idle-heavy loops migrate** to event-driven wake-up
   with a slow fallback poll (§3.2, §3.6).
3. **The loops that cannot block** (`Runner`,
   `Consolidator`/`CacheWarmer`) migrate to
   **incremental reads with a durable cursor**, so the
   per-tick work is O(new events), not O(log) (§3.3, §3.4).
4. **Writes become dirty-only** — the dispatcher stops
   re-saving unchanged checkpoints (§3.5).
5. **The hot path is pipelined** and **all cadences move to
   `Settings`** (§3.7, §3.8).

The poll fallback interval becomes the only knob that
bounds worst-case latency and traffic; its default is
5 s (was 0.1–0.25 s effective).

## 3. Proposed changes

### 3.1 P1 — `EventLog.subscribe`: the keystone primitive

**New surface** (in `stream/event_log/store.py` + the
storage Protocol in `infra/redis/_event_log/_adapter.py`):

```python
# EventLog — notification is a hint; the read path is unchanged.
async def subscribe(
    self,
    agent_id: str,
    *,
    cursor: str | None = None,
    block_ms: int = 30_000,
) -> AsyncIterator[list[Event]]:
    """Yield batches of new events strictly after ``cursor``,
    blocking up to ``block_ms`` per read. Reconnect-safe:
    yields nothing on timeout and lets the caller re-loop."""
```

**Implementation choice — blocking `XREAD`, not Pub/Sub:**

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| Blocking `XREAD` on the agent stream | No new keys; cursor semantics are native (`$` + `Last-Event-ID`); byte-exact gap replay; survives broker restart with cursor | One long-lived connection per subscriber (bounded by pool `max_connections`) | **Chosen** |
| `PUBLISH`/`SUBSCRIBE` fan-out channel | One connection serves many agents | Fire-and-forget: a notification lost between poll and publish stalls the consumer until the fallback poll; subscriber must then re-read via cursor anyway; new channel wire format | Rejected as primary; kept as a possible fan-out optimisation later |
| Keyspace notifications (`notify-keyspace-events`) | Zero app code | Coarse (key-level, not entry-level); global config; delivery unreliable | Rejected |

`XREAD` needs one method added to `RedisLike`
(`_client.py`) — the Protocol is already the audited
single point for this (ADR-019):

```python
async def xread(
    self, streams: dict[str, str], count: int | None = None,
    block: int | None = None,
) -> list: ...
```

**Fallback poll is mandatory and cheap**: a consumer that
receives no notification for `fallback_poll_interval_s`
(default 5 s) re-reads from its durable cursor. This is
what makes dropped/misrouted hints harmless: the EventLog
read path (`read_after_cursor`, ADR-019 Iteration 5) stays
the single source of truth.

### 3.2 P2 — `ReactiveDispatcher`: wake on event, idle ticks become cheap

Today `_dispatch_for_agent` (`runner/reactive.py:384–445`)
always does `GET` checkpoint + `XRANGE`, even when
nothing changed since the last tick. After P1:

- **Wake path**: the dispatcher's per-agent loop blocks in
  `subscribe(agent_id, cursor=ckpt.last_stream_id)`; on
  yield it runs the existing cycle. Idle cost = one held
  connection, zero round-trips.
- **Fallback path**: a timer fires the same cycle every
  5 s (also covers the ADR-045 idle-tick requirement —
  the TTL sweeper still runs, now at 1/20th the
  frequency and zero Redis I/O when there is nothing to
  evict).
- **Rediscovery** (`list_agents` SCAN) stays on its 5 s
  cadence initially; ADR-035 shard coordination is the
  follow-up owner if that needs to become push-driven.

**Multi-agent fan-in**: `subscribe_many` (one `XREAD`
over N agent streams, `block`) avoids N connections for
the dispatcher's fan-out case. **This is a hard
requirement of Phase 2, not an optional follow-up** (see
the connection-pressure analysis in §6): a real downstream
consumer (soldi-backoffice, `docs/ADRs/adr-010` in their
repo) tracks 100+ per-tenant agents behind a
`max_connections=30` pool with a documented
pool-exhaustion history — one-connection-per-agent
subscribing exhausts that pool on day one. The
per-subscriber connection cost and the pool cap
(`redis_max_connections=50`) must be surfaced in the
operator docs either way.

### 3.3 P3 — `Runner`: incremental fold, retire the per-tick full replay

`Runner.tick_once` (`runner/runner.py:100–136`) folds the
World via `fold_world(log)` (`stream/projection.py:39–62`)
→ `iter_all` → `SCAN` + full-history `XRANGE` per agent,
**every tick**. The correct model already exists in the
codebase: ADR-018's incremental fold with a durable
cursor.

- **Change**: give `Runner` the same
  checkpoint/cursor treatment the `ReactiveDispatcher`
  has — `World` + `last_stream_id` persisted between
  ticks; per tick read only `(cursor`, fold O(M), run
  systems.
- **Alternative rejected**: deleting `Runner` and
  routing cyclic systems through the
  `ReactiveDispatcher`. Tempting (one loop, one code
  path) but cyclic systems and reactive systems have
  different contracts (ADR-018 §"Tick model"); forcing
  them together breaks the pure-cyclic semantics
  documented in `memory/consolidation.py`. Keep both
  loops; make both incremental.
- **Second-order win**: `Consolidator.as_cyclic_system`
  runs inside `Runner`'s tick; with an incremental fold
  the World contains only live agents, which also stops
  the Consolidator from re-enqueuing stale memory agents
  forever (§3.4).

### 3.4 P4 — `Consolidator` / `CacheWarmer`: incremental refresh, no full rewrite

Today `refresh_cache` (`memory/base.py:211–228`) is
**fold the full stream + `DEL` + `HSET` + `EXPIRE`**
(`infra/redis/_memory/_profile.py:76–98`,
`_continuity.py:94`). Called for every memory agent on
every tick, regardless of delta.

- **Store the fold cursor in the cache payload itself**
  (one new hash field, e.g. `fold_cursor`, written
  atomically in the same pipeline as the state fields).
- **Refresh reads the delta**: `XRANGE (cursor` → apply →
  `HSET` only the mutated fields + `EXPIRE` (drop the
  `DEL`; the write-through is already idempotent per
  ADR-005 reasoning — the cache is derived data, ADR-014).
- **The `CacheRefreshRequest` gains an `events_since`
  short-circuit**: the Consolidator knows the World's
  view of the agent; if no memory-namespace event for
  that identity arrived since the last published request,
  do not enqueue (the request bus is in-memory and
  lossy-safe by design — `cache_warmer.py:16–22` — so a
  skipped request only means the next real event
  refreshes; correctness stays with the read-through
  miss path).

### 3.5 P5 — Dirty-only checkpoint save

`run_systems_and_persist` always saves
(`runner/_systems_runner.py:88–97`), and the idle path in
`_dispatch_for_agent` runs it whenever
`_should_run_systems_on_idle_tick()` is true — with
`tool_ttls` set, that is **every 0.25 s per agent**, re-`SET`ing
an unchanged pickled World.

- **Save only when the fold advanced the cursor or a
  system emitted an event.** The cursor-advancement save
  for fully-filtered batches (`runner/reactive.py:435–440`)
  is already minimal and stays.
- **Shrink the payload**: the checkpoint is a pickle of
  the full World (tick, storage, views,
  `last_stream_id`). Two independent reductions:
  1. Compress (`zlib`/`lz4`) — mechanical, wire-format
     compatible because `IncrementalWorldStore` owns
     encode/decode (`infra/world_checkpoint.py:101–133`).
  2. Follow-up (tracked, not blocking): split
     `last_stream_id` into its own small key so the
     *wake-up* path never needs the World payload —
     `GET cursor` (bytes) decides "anything new?", and
     the World payload is fetched only when there is
     work. This converts the biggest per-tick payload
     into a ~20-byte read.

### 3.6 P6 — SSE endpoint: blocking read behind the same primitive

`register_sse_events` (`routes.py:480–556`) polls at 100 ms
per client. Behind P1:

- The generator awaits
  `log.subscribe(agent_id, cursor=from_, block_ms=15_000)`
  and yields as batches arrive; the 15 s `:heartbeat`
  comment is emitted on timeout, exactly matching today's
  idle-keepalive semantics.
- `Last-Event-ID` reconnect keeps working — the cursor is
  the stream id, unchanged (ADR-065 §3.1).
- The public HTTP contract does not change; only the
  generator internals swap, as the module docstring
  already promised.
- The deprecated long-poll endpoint inherits the fix
  automatically if it is reimplemented over
  `subscribe(...)`; otherwise it should be
  re-pointed to a cursor read (never full-stream) for its
  one remaining minor cycle.

### 3.7 P7 — Pipeline the residual hot path

Where polling legitimately remains (fallback ticks,
first-dispatch bootstrap), collapse round-trips:

- `GET checkpoint` + `XRANGE (cursor` in one
  `pipeline(transaction=False)` — the two reads are
  independent; `transaction=False` avoids the
  MULTI/EXEC overhead (`RedisLike.pipeline` already
  exposes the flag, `_client.py:191`).
- `XACK` + next `XREADGROUP` in the WorkerManager is
  already sequential-safe; leave it.

### 3.8 P8 — Cadences belong in `Settings`

Hardcoded cadences found in the audit:

| Knob | Where | Default | Proposed env var |
|---|---|---|---|
| Dispatcher poll interval | `runner/reactive.py:131` | 0.25 s | `KNT_REACTIVE_POLL_INTERVAL` |
| Dispatcher rediscovery | `runner/reactive.py:137` | 5 s | `KNT_REACTIVE_REDISCOVERY_SECONDS` |
| Warmer pump interval | `memory/cache_warmer.py:182` | 0.25 s | `KNT_WARMER_PUMP_INTERVAL` |
| SSE poll interval | `core/long_poll.py:60` | 0.1 s | superseded by `block_ms` |
| Fallback poll (new) | §3.1 | 5 s | `KNT_FALLBACK_POLL_INTERVAL` |

`RunnerSettingsMixin` (`infra/config/_runner.py`) already
pins `KNT_TICK_INTERVAL`; the new knobs join the same
flat `KNT_` namespace.

## 4. Alternatives considered

| Alternative | Assessment |
|---|---|
| **Keyspace notifications** (`notify-keyspace-events`) | No application-level ordering, no per-entry cursor, global server config; rejected (§3.1 table) |
| **Pub/Sub fan-out channel** (`PUBLISH knt:notify:{agent_id}`) from a dispatcher-side hook | Works, but adds a second wire format and a delivery hole exactly where the EventLog already has one authoritative ordering; blocking `XREAD` needs zero new writes. Keep as a future fan-in optimisation if connection count becomes a problem |
| **RedisGears / server-side triggers** | Not in the deployment matrix (Redis 7 / FalkorDB stack); rejected |
| **Compress checkpoints only, keep polling** | Cuts bytes but not round-trips; the call-rate (§1.2) is the dominant symptom at scale. Worth doing (P5) but not sufficient alone |
| **Move all loops onto `XREADGROUP` consumer groups** | Semantically heavy for stateless observers; consumer groups add PEL/ack bookkeeping the EventLog readers do not need. The Worker path (ADR-036) keeps `XREADGROUP`; read-side stays `XREAD` |
| **Longer poll intervals only** (e.g. 0.25 → 2 s) | One-line mitigation, buys an order of magnitude, ships immediately as P8 defaults; does not change the O(agents × rate × payload) shape |

## 5. Phased implementation plan

Order chosen so each phase ships value independently and
P1 lands before anything depends on it.

| Phase | Contents | Depends on | Est. blast radius |
|---|---|---|---|
| **0 (mitigation, one minor)** | P8 env knobs at conservative defaults + `count>1` in `XREADGROUP` + P5 compression | — | ~6 files, no behaviour change |
| **1 (primitive)** | P1: `xread` in `RedisLike`; `EventLog.subscribe`/`subscribe_many`; unit tests with `KNT_REDIS_FAKE` blocking semantics | — | `infra/redis/_client.py`, `_event_log/`, `stream/event_log/` |
| **2 (dispatcher)** | P2 wake-up + fallback (**incl. `subscribe_many` fan-in — hard requirement, §6 connection pressure**); P5 dirty-only save + cursor-key split | P1 | `runner/reactive.py`, `_checkpoint_io.py`, `_systems_runner.py` |
| **3 (runner + memory)** | P3 incremental `Runner`; P4 cursor-in-cache refresh | P1 (for wake-up optional), none for the cursor work | `runner/runner.py`, `memory/base.py`, `infra/redis/_memory/` |
| **4 (gateway)** | P6 SSE over `subscribe`; re-point deprecated long-poll | P1 | `api/intent_router/routes.py`, `core/long_poll.py` |

Each phase keeps the existing public contracts: the
`EventLog` read API, the SSE wire format, the
`WorldCheckpoint` durability ordering (append-before-save,
ADR-018), and the `Result`/typed-error contracts
(`kntgraph-typed-errors` discipline) are untouched.

## 6. Risks and invariants

- **Notification loss.** Hints may be lost (connection
  drop, failover). Invariant: every consumer keeps a
  durable cursor + fallback poll; the hint only affects
  latency. The idempotency net (ADR-005) already closes
  the replay window.
- **Connection pressure.** Blocking reads hold pooled
  connections. `max_connections=50` (default,
  `infra/config/_redis.py:28`) bounds it, but the
  binding constraint is downstream: a real consumer
  (soldi-backoffice) tracks 100+ per-tenant agents behind
  a `max_connections=30` pool with a documented
  exhaustion history (their ADR-009). For that
  deployment shape, one-connection-per-agent subscribing
  is a day-one outage — therefore `subscribe_many` is a
  **hard Phase 2 requirement** (§3.2), and the fan-in
  path must be the only supported configuration for
  agents > pool margin. If even fan-in saturates the
  pool, the Pub/Sub fan-out alternative is the release
  valve. Operator docs must state the per-subscriber
  connection cost.
- **Idle-tick semantics (ADR-045).** The TTL sweeper and
  `_pending_results` systems must still run without new
  events. P2 keeps an explicit fallback tick; it must
  never be removed, only re-cadenced.
- **`KNT_REDIS_FAKE` test path.** The fake must grow a
  blocking-faithful `xread` (or the tests drive
  `subscribe` via injected batches); the CI gate
  (`scripts/ci.py`) runs unit tests on fakeredis, so this
  is a hard prerequisite of Phase 1.
- **Multi-pod dispatchers (ADR-035).** Waking every pod on
  every event multiplies wake-ups. The sharding ADR owns
  stream→pod affinity; this ADR only requires that
  wake-up be per-agent-granular so the sharder can route
  subscriptions.

## 7. Open questions

1. **`subscribe` cursor semantics** — expose `"$"`-style
   "only new" vs explicit cursor at the `EventLog` level,
   or keep the API cursor-only and let callers bootstrap?
   (Leaning: cursor-only, `None` ⇒ read-all, mirroring
   `read_after_cursor`.)
2. **`Runner` vs `ReactiveDispatcher` convergence** — P3
   removes the *cost* difference; do the two loops stay
   separate long-term, or does a follow-up ADR unify them
   under one scheduler? Deferred until both are
   incremental.
3. **SSE backpressure** — carried from ADR-065 §7.3; P6
   makes the Redis side blocking, which makes the
   per-connection buffer cap the only new policy needed.
4. **Measurements** — accept `INFO commandstats` deltas
   (call-rate per command, before/after per phase) as the
   acceptance metric, or require an end-to-end network
   capture? Proposed: commandstats + a one-off
   `redis-cli MONITOR` sample per phase, recorded in this
   ADR's status notes.

## 8. Decision

Adopt the push-first read model: one new
`EventLog.subscribe` primitive over blocking `XREAD`
(P1), consumers woken on arrival with a 5 s durable
fallback poll (P2, P6), incremental cursor-based reads for
the loops that cannot block (P3, P4), dirty-only
checkpoint writes (P5), and Settings-owned cadences (P8).

**Recommended next steps:**

1. Land Phase 0 (knobs + `count>1` + compression) in the
   next minor — immediate traffic relief, no design
   risk.
2. Open the Phase 1 implementation PR for
   `RedisLike.xread` + `EventLog.subscribe`; it is the
   only prerequisite for everything else and unblocks
   the DEBT-published "planned Redis Pub/Sub channel"
   note in `routes.py`.
3. Record the `commandstats` baseline **before** Phase 1
   lands so the acceptance data (§7.4) is comparable.