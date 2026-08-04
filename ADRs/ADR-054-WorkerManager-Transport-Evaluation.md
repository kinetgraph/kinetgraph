<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-054: WorkerManager transport evaluation — keep `ProcessPoolExecutor` + Redis Streams; do not migrate to a different executor

**Status:** Accepted
**Date:** 2026-08-04
**Version:** 0.1.0
**Authors:** kntgraph architecture team
**Related to:** [ADR-036](./ADR-036-Tool-Worker-Pattern.md) (the WorkerManager foundation), [ADR-034](./ADR-034-ToolCall-ECS-Components.md) (ToolCallRequest/ToolCallCompletion components), [ADR-037](./ADR-037-Mandatory-Correlation-Propagation.md), [ADR-043](./ADR-043-LiteLLM-Worker-Migration.md) (LiteLLMToolWorker), [ADR-049](./ADR-049-Zero-Token-Architecture.md) (ZTA — deterministic workers)

> **Operator's-eye-view.** This ADR is a **decision not to
> migrate**. The `WorkerManager` keeps `ProcessPoolExecutor`
> as the function executor and Redis Streams as the per-tool
> queue. The realistic local alternatives
> (`ThreadPoolExecutor`, pure `asyncio`, external
> subprocess JSON-lines workers) are recorded with the
> criteria that would justify a revisit. The intent is to
> short-circuit the same evaluation cycle next time someone
> proposes changing the executor or the transport.

## 1. Context

### 1.1 The current design (ADR-036)

The `WorkerManager` (`src/kntgraph/tools/manager.py`) is
**not** a process pool. It is a Redis Streams consumer
with a process pool glued on as the function executor.
Three layers, from the dispatcher's edge to the worker's
runtime:

1. **`ToolRouter`** (`tools/router.py`, ~65 LOC) —
   Full-payload fan-out: copies
   `tool.<name>.requested` from the agent's EventLog to
   the global tool stream `knt:tools:<name>:queue` via
   `xadd`.
2. **`WorkerManager`** (`tools/manager.py`, ~300 LOC) —
   per-tool loop that does `xreadgroup` on the consumer
   group `fmh_tool_workers`, parses the event payload,
   delegates execution to `loop.run_in_executor(self._pool,
   _invoke_tool_sync, ...)` (this is the
   `ProcessPoolExecutor`), and writes
   `tool.<name>.completed` / `tool.<name>.failed` back to
   the EventLog via `self._event_log.append(...)`.
   A parallel reaper does `xautoclaim` for stuck
   messages; `xpending_range` decides DLQ after
   `__tool_worker_retries__`.
3. **`@tool_worker` decorator** (`tools/worker.py`) —
   marks a class as a worker; exposes
   `__tool_worker_max_concurrency__` (per-tool process
   budget) and `__tool_worker_retries__` (DLQ threshold).

The `ProcessPoolExecutor` is **the executor of function
calls within the WorkerManager process**. It is not the
queue. The queue is Redis. The worker's process is reused
across many `invoke()` calls via the pool — see the
inline comment in
`agents/tools/llm.py:618-621`:

> *The `ProcessPoolExecutor` worker runs this
> `__init__` once per process (the same process is
> reused across many `invoke` calls via the pool).*

### 1.2 What triggered this evaluation

A proposal was raised to replace `ProcessPoolExecutor`
with a different executor + transport. The motivation was
the well-known friction of Python's
`ProcessPoolExecutor` on Linux (fork + TLS, GIL-free
worker processes, pickling overhead, dead-process
detection being manual).

The evaluation asked: does any local alternative solve
problems that the current combination does not, at a cost
that justifies the change? Cross-process or cross-host
transports (network brokers, polyglot workers, multi-host
topologies) are out of scope: the project does not state
those as requirements, and adopting any of them would
also require replacing Redis as the EventLog backing
store — a much larger change than the WorkerManager alone.

## 2. Decision

**Keep the current design.** The `WorkerManager` continues
to use `ProcessPoolExecutor` as the function executor and
Redis Streams as the per-tool queue. The realistic local
alternatives are listed in §3.2 with the criteria that
would justify a future revisit.

## 3. Rationale

### 3.1 What the current design gives us

The combination of Redis Streams + `ProcessPoolExecutor`
covers the four failure modes the design has to handle:

- **Worker crashes mid-call.** Detected via
  `xpending_range` + delivery count exceeding
  `__tool_worker_retries__`; the Reaper loop's
  `xautoclaim` re-enqueues to a live consumer. DLQ
  re-emits a `tool.<name>.failed` event so the agent's
  state machine does not block forever.
- **Worker hangs on slow I/O.** Same path as crashes:
  the consumer never `xack`s; the Reaper claims the
  pending entry after `reaper_idle_time` and reprocesses.
- **Worker process dies between `xreadgroup` and
  `xack`.** Redis PEL survives worker process death; the
  Reaper picks the message up on the next tick.
- **Multiple workers for the same tool.** Native via the
  consumer group; the WorkerManager's `__init__` and
  `start()` are designed to be run as N replicas against
  the same group.

The event flow is end-to-end the same Python `Event`
type: the request event lands in the agent's EventLog,
is fan-out-copied to the tool queue, is consumed by
the WorkerManager, is executed in a worker process,
and the completion event is appended back to the
EventLog via `self._event_log.append(...)`. The
causation_id is `idempotency_key = str(request_event.event_id)`
— the same join key every other event in the system
uses. No serialisation across an external wire; no
parsing back into Python objects on the worker side.

### 3.2 Real local alternatives (not network transports)

If the motivation to revisit `ProcessPoolExecutor` is
**Python-specific friction** (fork/TLS, pickle, dead-process
detection), the realistic options are local, not network
transports. Listed by effort and blast radius:

#### 3.2.1 `ThreadPoolExecutor` (smallest change)

Swap `ProcessPoolExecutor` for `ThreadPoolExecutor` in
`WorkerManager.start()`. Same executor surface
(`loop.run_in_executor(self._pool, ...)`), no transport
change. Trade-off:

- **Wins:** No fork. No pickle. `_invoke_tool_sync`
  becomes `_invoke_tool_async` — the `asyncio.run()` dance
  inside the worker process disappears.
- **Costs:** GIL contention if any tool is CPU-bound.
  None of the shipped workers are CPU-bound
  (`LiteLLMToolWorker` is I/O; `PiiRedactionTool` is
  regex; the example `WeatherTool` is HTTP). For the
  current worker set, the GIL is irrelevant.
- **Revisit when:** A `@tool_worker` is added whose
  body is CPU-bound (e.g. a local embedding model with
  no I/O) and saturates a thread. At that point a
  per-tool executor choice (`Process | Thread`) becomes
  the right knob.

#### 3.2.2 Pure `asyncio` (no executor at all)

Drop `run_in_executor` entirely. Each tool's `invoke()`
is already `async def`; the Manager can `await` it
directly inside its existing `async def _process_message`
loop. The "process boundary" disappears; the
`ProcessPoolExecutor` becomes obsolete.

- **Wins:** Removes the executor layer entirely. Tools
  become first-class awaitables in the Manager's task
  graph; cancellation, timeouts, and structured
  concurrency are inherited from `asyncio` for free.
  The `_invoke_tool_sync` wrapper goes away.
- **Costs:** CPU-bound tools would block the dispatcher's
  tick. **Unworkable** for the current LiteLLM workload
  *only* because the LLM transport is I/O — if any future
  worker is CPU-bound, that worker needs its own
  process. The right design is per-worker executor
  choice, not a global switch.
- **Revisit when:** The tool catalog is fully I/O-bound
  and stays that way. The threshold is "no tool runs
  for >1s on the CPU without yielding". When that holds,
  this is the smallest possible design.

#### 3.2.3 External subprocess + JSON-lines (most invasive)

Each worker is a separate OS process
(`python -m kntgraph.tools.worker_runtime <tool_name>`),
controlled by the Manager via `asyncio.subprocess.PIPE`.
The wire format is JSON-lines on stdin/stdout. The
Manager spawns / kills workers, monitors exit codes,
and restarts on crash.

- **Wins:** No fork. No pickle. No GIL. The worker's
  process can run on a different host (over SSH or any
  other transport that gives two pipes). Worker
  crashes are surfaced via the subprocess exit code,
  not through `ProcessPoolExecutor`'s opaque future
  state.
- **Costs:** Reimplements what Redis Streams already
  gives (queue, replay, ack, DLQ) on top of pipes
  unless the JSON-lines layer is just a transport for
  *another* Redis stream. The "different host" case
  is real but rare; the "same host" case is dominated
  by §3.2.1.
- **Revisit when:** Cross-host workers become a real
  requirement (multi-region deployments, language
  heterogeneity) **and** the team rules out the current
  Redis-only design for some reason — typically "we
  already run Redis, we don't want another broker
  process". In that case JSON-lines is the
  lower-friction option over a bespoke broker.

### 3.3 Acceptance criteria for reopening this decision

The choice of executor + transport is reopened only when
one of the following becomes a stated, ADR-documented
requirement:

- A worker whose body is CPU-bound for >1s at a time
  (§3.2.2 — pure asyncio breaks down).
- A multi-host worker topology (workers on a host
  different from the dispatcher). ADR material must
  include the latency budget, the failure modes (broker
  split-brain, worker partition), and the reason Redis
  Streams is ruled out.
- A polyglot worker (e.g. a Go-based embedding service).
  ADR material must include the cross-language payload
  contract and the correlation/causation propagation
  requirements.
- The Redis dependency is removed from the runtime stack.
  ADR material must include the migration path for
  `EventLog`, `CheckpointStorage`, `SessionStorage`,
  `ContinuityStorage`, and the DLQ adapters — a much
  larger change than the `WorkerManager` alone.

Until one of those lands, §3.2.1 (`ThreadPoolExecutor`)
and §3.2.2 (pure `asyncio`) are the right places to
spend effort if `ProcessPoolExecutor` ever becomes a
real bottleneck.

## 4. Consequences

### 4.1 Pros

- **No code change.** The `WorkerManager` source is
  unchanged. The `tools/manager.py` line that imports
  `ProcessPoolExecutor` (line 14) stays; the
  `self._pool: ProcessPoolExecutor | None = None`
  declaration (line 77) stays; the
  `ProcessPoolExecutor(max_workers=max_workers)`
  instantiation in `start()` (line 102) stays.
- **Short-circuits future evaluations.** Anyone
  proposing "anything but ProcessPool" next year has
  a recorded decision to point at, with the criteria
  that would justify revisiting it. This is the ADR's
  primary purpose.
- **Preserves the EventLog integration.** The Manager
  writes `tool.<name>.completed` / `tool.<name>.failed`
  as ordinary events via `self._event_log.append(...)`
  (lines 218, 228). The causation_id is
  `idempotency_key = str(request_event.event_id)` —
  the same shape every other event in the system
  uses. Any cross-process transport would serialise
  and re-deserialise the payload across the wire; the
  current design keeps the Event type intact end-to-end.

### 4.2 Cons

- **No new capabilities unlocked.** Cross-host workers
  and polyglot workers remain unsupported. If either
  becomes a requirement, this ADR's acceptance criteria
  (see §3.3) must be met before reopening.
- **`ProcessPoolExecutor` quirks are still there.**
  The Linux fork + openssl + thread-local-state
  interaction is unchanged. The team's known workaround
  (fork-before-import of `cryptography` / openssl
  consumers in the worker process) is unchanged.
- **The "why didn't we use X?" question will recur.**
  ADRs are read by humans; the rationale in §3 may be
  skimmed. The short-circuit value of this ADR is real
  but only as durable as the project's memory of
  writing it.

## 5. Migration

### 5.1 Done in this PR

None. The decision is *not to change code*.

### 5.2 Update to ADR-036 §4.2

ADR-036's §4.2 "Pendente (follow-up PRs)" lists five
follow-up items, none of which is about transport
evaluation. This ADR adds a pointer so the next person
who reads ADR-036 and asks "what about replacing the
executor?" finds ADR-054 immediately. The pointer lands
as item 6 in ADR-036 §4.2 (no re-numbering of existing
items):

> 6. **Executor + transport evaluation recorded in
>    ADR-054** — the local alternatives
>    (`ThreadPoolExecutor`, pure `asyncio`, subprocess
>    JSON-lines) were evaluated and the current
>    `ProcessPoolExecutor` + Redis Streams design
>    confirmed; the acceptance criteria for reopening
>    are in ADR-054 §3.3. **Quem:** nobody
>    (decision not to change). **Prazo:** N/A.
>    Reopen only when one of the ADR-054 §3.3
>    criteria is met.

### 5.3 Not done (deliberately)

- **No code change in `tools/manager.py`.** The
  `ProcessPoolExecutor` import stays.
- **No new dependency.** The local alternatives in
  §3.2 require no new packages
  (`ThreadPoolExecutor` is `concurrent.futures`;
  `asyncio.subprocess` is stdlib). If the decision is
  ever reversed in favour of a network transport, that
  PR adds the dependency and the broker process.
- **No removal of any existing ADRs.** ADR-036 stays
  the authoritative design record; this ADR is the
  supplement.

## 6. Related decisions

- **ADR-036 §2.5** is the original "Full Payload
  Fan-Out + Confiabilidade (Redis)" decision. This
  ADR does not contradict it; it answers the question
  "should that decision have been something other than
  `ProcessPoolExecutor` + Redis Streams?".
- **ADR-043** migrates `LiteLLMTool` →
  `LiteLLMToolWorker` and notes that the worker "runs
  in the `WorkerManager`'s `ProcessPoolExecutor`". The
  `ProcessPoolExecutor` reference is now backed by an
  ADR saying it stays.
- **ADR-049 §1.2** lists `WorkerManager (ProcessPoolExecutor)`
  as part of the "mechanical substrate" ZTA needs.
  This ADR confirms the substrate's executor choice.
- **AGENTS.md §11.3** (branch policy) means this ADR
  ships in the human-reviewed commit on
  `feat/governance-rules`, like the rest of the
  reliability-gate and verticals-gate work in this
  branch.

## 7. References

- `src/kntgraph/tools/manager.py` — the WorkerManager
  whose executor is the subject of this ADR.
- `src/kntgraph/tools/router.py` — the ToolRouter fan-out
  that produces the input the WorkerManager consumes.
- `src/kntgraph/tools/worker.py` — the `@tool_worker`
  decorator.
- `src/kntgraph/agents/tools/llm.py` — the LiteLLM
  worker; the canonical `@tool_worker` reference
  implementation. Comment at lines 618-621 documents
  the per-process `__init__` invariant that the
  ProcessPool reuses across calls.
- ADR-036 §1, §2.2, §2.5 — the architectural foundation
  this ADR supplements.
- Python stdlib `concurrent.futures.ProcessPoolExecutor`
  — the executor this ADR confirms.
- Python stdlib `asyncio.subprocess` — the substrate
  for §3.2.3 if it is ever adopted.
