<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-070: Worker-Level Back-Pressure

- **Status:** Proposed
- **Date:** 2026-09-07
- **Author:** kinetgraph architecture team
- **Supersedes:** (none)
- **Related to:**
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — `@tool_worker` + `WorkerManager`
  - [ADR-066](./ADR-066-Single-Tool-Path.md) — single tool path / three-gate ACL
  - [ADR-068](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md) — wakeup stream (delay path)
  - [ADR-069 §11.17](./ADR-069-Agent-Concordo-Macro-Behaviors.md) — Pipeline Concordo removed; back-pressure deferred here

---

## 1. Context

ADR-069 §11.17 removed the `PipelineConcordo` because
its only piece of value (per-stage back-pressure)
did not justify the "one agent per item" model
that the Pipeline proposed. The legitimate
concern — protecting a slow tool (e.g. SEFAZ,
fiscal-document batch importer) from being
overwhelmed by a flood of requests — is real
and affects the same workloads the Pipeline was
meant to serve.

This ADR proposes that concern be addressed at
the **worker level** rather than as a Concordo.

## 2. Goal

Extend `@tool_worker` (or `WorkerManager`) with
a declarative `max_in_flight` knob. When the
counter saturates, dispatch is **delayed** rather
than rejected: the request stays in the EventLog,
and the dispatcher is woken when the gate clears
(via the wakeup stream introduced by ADR-068).

## 3. Non-goals

- Cross-tenant quota enforcement. That is a
  separate concern (ADR-066 gate 1) and is out
  of scope here.
- Per-step saga back-pressure. The saga timeout
  layer (ADR-069 §4.6) covers wall-clock
  enforcement; per-step in-flight throttling is a
  WorkerManager concern, not a saga concern.
- Replacing the existing tool-router fan-out
  (ADR-036). The back-pressure gate is a single
  filter applied BEFORE fan-out.

## 4. Proposed shape (sketch)

```python
@tool_worker(
    name="nfe_emitter",
    max_in_flight=5,           # new knob
)
class NfeEmitterToolWorker:
    async def invoke(self, request):
        ...
```

The `WorkerManager` keeps a per-tool counter in
Redis (key `knt:worker:<tool>:in_flight`). Before
fanning a `tool.<name>.requested` event to the
queue, the WorkerManager:

1. Reads the counter.
2. If `counter >= max_in_flight`, defers the
   dispatch: the event stays in the EventLog
   (no fan-out), and the dispatcher schedules a
   wakeup at `wakeup_interval_seconds` (ADR-068).
3. Otherwise, `INCR`s the counter, fans out,
   and the completion path `DECR`s.

The deferred events are idempotent — they live in
the EventLog and the dispatcher keeps polling —
so a restart does not lose them.

## 5. Why this beats a Pipeline Concordo

- **No agent cardinality cost.** The counter is
  a single Redis key per tool, not one per item.
- **No `AgentView` schema change.** The Pipeline
  required `view.last_event` for routing (a new
  field added in ADR-069 §11.16); the worker
  approach reads the existing
  `tool.<name>.requested` event directly.
- **Single primitive.** Back-pressure lives where
  it conceptually belongs (the worker that can
  be overwhelmed), not in a separate Concordo.
- **5% of the code.** ~50 LOC in
  `WorkerManager` + a Redis adapter; no new
  components, no new systems, no new CLI
  surface.

## 6. Open questions

- Where the `DECR` lives (worker process vs.
  dispatcher). The `DECR` must happen even when
  the worker crashes mid-tool; a TTL on the
  counter is the safest fallback.
- Whether `max_in_flight` should be per-tenant
  or global. The fiscal use case wants
  per-tenant fairness; the simpler version
  (global) is easier to ship first.
- How `max_in_flight` interacts with the TTL
  sweeper (ADR-045). A request that has been
  awaiting dispatch for longer than the tool's
  TTL should probably emit
  `tool.<name>.timed_out` rather than block the
  gate forever.

## 7. Status

This is a stub. The full proposal will be
drafted after ADR-069 is accepted; the design
above is enough to justify the link from
ADR-069 §11.17 and to coordinate the follow-up.
