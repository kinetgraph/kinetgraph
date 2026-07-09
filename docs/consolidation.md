<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Memory Consolidation

**Consolidation** is the path that takes state from the
**EventLog** (source of truth) to the projected **memory
tiers** (Redis cache, FalkorDB graph). It is what makes
the framework go from "immutable log" to "agent that
remembers".

This document covers:

1. The 3 tiers, the 2 consolidators, and the 3 sinks.
2. Why `Consolidator` and the post-tick Solutions loop
   run in **separate coroutines**.
3. The Solutions coroutine (ADR-010) in detail.
4. Configuration and per-tenant opt-in.
5. Errors and fallback.

> **Prerequisites**
>
> - Familiarity with [ADR-004](./ADR-004-Memory-Tools-Knowledge.md)
>   (memory tiers, F8).
> - Familiarity with [ADR-010](./ADR-010-Memory-Business-Tier.md)
>   (Solutions sub-graph, PII, man-in-the-loop).
> - Familiarity with [graphrag.md](./graphrag.md) (two
>   FalkorDB sub-graphs).

---

## 1. The 3 tiers, 2 consolidators, 3 sinks

```
                      ┌─────────────────────────────────┐
                      │       EventLog (Redis Streams)   │
                      │  (source of truth)               │
                      └────────────────┬─────────────────┘
                                       │
                               World.fold(events)
                                       │
                ┌──────────────────────┼──────────────────────┐
                │                      │                      │
                ▼                      ▼                      ▼
       ┌────────────────┐     ┌────────────────┐     ┌────────────────┐
       │  Consolidator  │     │  Consolidator  │     │  Post-tick     │
       │  (tick)        │     │  (tick)        │     │  Solutions     │
       │  cyclic, fast  │     │  cyclic, fast  │     │  coroutine     │
       └────────┬───────┘     └────────┬───────┘     └────────┬───────┘
                │                      │                      │
                ▼                      ▼                      ▼
       ┌────────────────┐     ┌────────────────┐     ┌────────────────┐
       │ CacheRefreshBus│     │ CacheRefreshBus│     │ SolutionPromo- │
       │  (session)     │     │  (profile)     │     │ tionBus        │
       └────────┬───────┘     └────────┬───────┘     └────────┬───────┘
                │                      │                      │
                ▼                      ▼                      ▼
       ┌────────────────┐     ┌────────────────┐     ┌────────────────┐
       │ CacheWarmer    │     │ CacheWarmer    │     │ SolutionPromot.│
       │ → Redis JSON   │     │ → Redis Hash   │     │ → FalkorDB     │
       │   (read-thru)  │     │   (read-thru)  │     │   (write-only) │
       └────────────────┘     └────────────────┘     └────────────────┘
```

**Three memory tiers**:

| Tier | Sink | Operation | Idempotency |
|------|------|-----------|-------------|
| **Session** | Redis JSON | read-through | deterministic event_id |
| **Profile** | Redis Hash | read-through | deterministic event_id |
| **Knowledge** | FalkorDB (two sub-graphs) | write-only (Solutions) / one-shot (Documents) | `MERGE` by natural key |

**Two consolidators**:

| Consolidator | Loop | Target latency | Blocks the tick? |
|--------------|------|----------------|------------------|
| `Consolidator` (existing) | cyclic, inside the `Runner` tick | sub-ms | yes (synchronous) |
| Solutions coroutine (ADR-010) | standalone coroutine, post-tick | 10s (configurable) | **no** |

**Three sinks** (CacheWarmer ×2 + SolutionPromoter).

---

## 2. Why split into two consolidators

The split is deliberate. Temptations to merge them into
"a single loop" were rejected for 4 reasons
(ADR-010 §2.4):

1. **Different cost** — Redis JSON/Hash writes are sub-ms.
   FalkorDB MERGE with embedding is 10-100ms. Consolidating
   in the tick doubles the critical-path latency.
2. **Different failure mode** — Redis is local, rarely
   fails. FalkorDB can be down. If it goes down, the tick
   must not stop.
3. **Different reconfiguration** — switching the
   `EmbeddingProvider` requires reprojecting FalkorDB.
   Switching the cache TTL is trivial.
4. **Different test** — `Consolidator` tests with a
   `redis_aioredis` mock. The Solutions coroutine tests
   with FalkorDB (skipped when unavailable).

In production, both run in parallel:

```python
# Tick loop (Consolidator)
runner = Runner(
    log, cyclic_systems=[consolidator.as_cyclic_system()]
)

# Background coroutine (Solutions)
promoter = SolutionPromoter(
    tenant_id="...",
    projector=projector,
    redactor=pii.redact,
)
bus = SolutionPromotionBus()
extractor = SolutionExtractor()

async def solutions_loop():
    while True:
        events = await log.read(extractor.target_agent_id)
        for c in extractor.extract_from_events(events):
            await bus.publish(c)
        await promoter.pump_once(bus)
        await asyncio.sleep(10.0)

await runner.start()             # ticks every N seconds
asyncio.create_task(solutions_loop())  # pump every 10s

# Shutdown
await runner.stop()
# cancel the background task
```

The Solutions coroutine and `Runner` are independent. The
`Runner` can be restarted without affecting the Solutions
loop, and vice versa.

---

## 3. `Consolidator` (existing, ADR-004)

(Summary for contrast with the new one. Full details in
`memory/consolidation.py` and ADR-004 §2.2.)

```
tick T:
    world = await fold_world(log)        # Pure
    for agent_id in world.agents:
        mem = parse_agent_id(agent_id)
        if mem is None: continue
        bus.publish(CacheRefreshRequest(mem.kind, mem.id1, mem.id2))
        # Hot path: no I/O beyond the in-memory deque

# In parallel (separate coroutine):
warmer = CacheWarmer(bus, sm, pm)
while True:
    await warmer.pump_once()    # I/O: Redis SET/HSET
    await asyncio.sleep(0.25)
```

`parse_agent_id` classifies each `agent_id` into a
`MemoryKind` (`"session"` or `"profile"`). Solutions
**are not agents** in the EventLog — they do not go
through `parse_agent_id` and do not use the
`CacheRefreshBus`.

---

## 4. The Solutions coroutine (ADR-010)

### 4.1 Main loop

```
# Standalone coroutine (not in the tick loop):
while running:
    await pump_once()         # pure extract + adapter write
    await asyncio.sleep(interval)
```

The actual pump body is `SolutionPromoter.pump_once(bus)`,
fed by `SolutionExtractor.extract_from_events(events)`
reading from the EventLog.

### 4.2 `pump_once` in detail

`SolutionPromoter.pump_once(bus)`:

1. Drains candidates from the bus.
2. For each candidate: runs `redactor` (PII gate).
3. Hands the redacted candidate to `projector.upsert`
   (FalkorDB MERGE).
4. Returns `PromoteStats` with `promoted`, `pii_blocked`,
   `failed` counters.

The promoter is fail-closed: a PII failure or a projector
I/O failure aborts the candidate and the loop moves on.

`SolutionExtractor.extract_from_events(events)` (pure):

1. Reads each `tool.*.completed` and `tool.*.failed`.
2. Reconstructs `(Problem, Action, Outcome)` from `data`.
3. Optionally bumps confidence cross-agent (pure).
4. Returns the list of `SolutionCandidate`s.

### 4.3 Promotion gate

Two rules:

- **Confidence < threshold** -> review queue.
- **Tool in the approval list** -> review queue (even with
  high confidence).
- Otherwise -> auto-promote.

```python
def _gate(self, candidates):
    auto, review = [], []
    approval = self._approval_list_for_tenant()
    threshold = self._review_threshold
    for c in candidates:
        if c.tool_name in approval or c.confidence < threshold:
            review.append(c)
        else:
            auto.append(c)
    return auto, review
```

`approval_list` = `fmh:tenant:{cnpj}:approval_list` (Redis
set). `threshold` = `FMH_SOLUTIONS_REVIEW_THRESHOLD` (env).

### 4.4 Review queue

Redis Stream `fmh:solutions:review`. Each entry is a
serialized `SolutionCandidate`. TTL
`FMH_SOLUTIONS_REVIEW_TTL_S` (default 7 days). After the
TTL, the event becomes `stale_review` in the DLQ.

The application plugs in its own HTTP review endpoint
that drains the stream. Approval -> emits
`solution.promoted` on the EventLog; `SolutionPromoter`
consumes it and writes to FalkorDB. Rejection -> emits
`solution.rejected` on the EventLog + DLQ.

### 4.5 PII gate

`SolutionPromoter` calls `PiiRedactionTool.redact(candidate.data)`
before the MERGE. Levels:

- **1 (default)**: regex. CPF/CNPJ/email/phone/CEP/PIX
  key -> placeholders.
- **2 (opt-in)**: GLiNER2 with the tool's schema labels.
  Names/addresses detected semantically.
- **3 (opt-in)**: GLiNER2 v1.5 with the `pii` task. Batch
  audit; does not block promotion.

A redaction failure produces a `pii.check_failed` event on
the EventLog; the candidate is **not** written and goes
to the DLQ. Fail-closed.

### 4.6 Errors and recovery

| Scenario | Behavior |
|----------|----------|
| FalkorDB unavailable | `pump_once` logs `solution.promoter.upsert_failed`, returns 0. Tick loop unaffected. |
| Embedding provider fails | Same pattern. Candidate goes to internal retry queue; after N attempts, to DLQ. |
| PII tool fails | Fail-closed. Candidate does not promote. |
| `ToolRegistry` without a `(:Tool)` node | `SolutionPromoter.ensure_tool_nodes()` is called on boot, idempotent. |
| Restart mid-pump | The next `pump_once` re-processes. Idempotency via `MERGE` on FalkorDB and `event_id` on the EventLog. |

---

## 5. Configuration

| Variable | Default | Where it applies |
|----------|---------|------------------|
| `FMH_KNOWLEDGE_INTERVAL_S` | `10.0` | Solutions coroutine interval |
| `FMH_PII_LEVEL` | `1` | `PiiRedactionTool.level` |
| `FMH_SOLUTIONS_TOOL_ALLOWLIST` | (empty) | `SolutionExtractor` filters by tool |
| `FMH_SOLUTIONS_CONFIDENCE_BUMP_AGENTS` | `2` | `SolutionExtractor.bump_confidence` threshold |
| `FMH_SOLUTIONS_REVIEW_THRESHOLD` | `1` | Promotion gate review threshold |
| `FMH_SOLUTIONS_REVIEW_TTL_S` | `604800` | Redis Stream TTL |

Per-tenant opt-in: Redis hash
`fmh:tenant:{cnpj}:flags` with field
`knowledge_enabled: "1"`.

---

## 6. Full wiring (example)

```python
import asyncio
import redis.asyncio as aioredis
from kntgraph.stream.event_log import EventLog
from kntgraph.knowledge.embedding import (
    EmbeddingClient,
)
from kntgraph.infra.graph import GraphPool
from kntgraph.agents.memory.solutions import (
    SolutionExtractor,
    SolutionPromoter,
    SolutionPromotionBus,
)
from kntgraph.agents.tools.pii import PiiRedactionTool
from kntgraph.agents.knowledge.solution_projector import (
    SolutionProjector,
)
from kntgraph.memory.consolidation import (
    CacheRefreshBus, CacheWarmer, Consolidator,
    SessionManager, ProfileManager,
)
from kntgraph.tools.registry import ToolRegistry

async def main():
    redis = aioredis.from_url("redis://localhost:6379")
    log = EventLog(redis)
    fdb = GraphPool(host="localhost", port=16379)
    embedding = EmbeddingClient()
    registry = ToolRegistry()

    # --- Redis tier (cache) ---
    sm = SessionManager(event_log=log, redis_client=redis)
    pm = ProfileManager(event_log=log, redis_client=redis)
    cache_bus = CacheRefreshBus()
    consolidator = Consolidator(log, cache_bus, sm, pm)
    warmer = CacheWarmer(cache_bus, sm, pm)

    # --- FalkorDB tier (knowledge) ---
    pii = PiiRedactionTool(level=1)
    projector = SolutionProjector(
        client=fdb, embedding=embedding,
        tenant_id="12.345.678/0001-90",
    )
    promoter = SolutionPromoter(
        tenant_id="12.345.678/0001-90",
        projector=projector,
        redactor=pii.redact,
    )
    sol_bus = SolutionPromotionBus()
    extractor = SolutionExtractor()

    # --- Start everything ---
    asyncio.create_task(warmer.run_forever())

    async def solutions_loop():
        while True:
            events = await log.read(extractor.target_agent_id)
            for c in extractor.extract_from_events(events):
                await sol_bus.publish(c)
            await promoter.pump_once(sol_bus)
            await asyncio.sleep(10.0)

    asyncio.create_task(solutions_loop())
    # ... register Runner with consolidator.as_cyclic_system() ...
    try:
        await asyncio.sleep(3600)
    finally:
        await redis.aclose()

asyncio.run(main())
```

---

## 7. See also

- [ADR-004: Memory Tiers, Tools and Knowledge](../ADRs/ADR-004-Memory-Tools-Knowledge.md) — F8.1 Session/Profile
- [ADR-010: Memory Tier "business"](../ADRs/ADR-010-Memory-Business-Tier.md) — F8.3 Solutions sub-graph
- [graphrag.md](./graphrag.md) — retrieval on the two sub-graphs
- [embedding.md](./embedding.md) — pluggable `EmbeddingProvider`
- [architecture.md](./architecture.md) — framework overview
- [`Consolidator`](../../src/fmh_backend/memory/consolidation.py)
- [`SolutionExtractor`](../../src/fmh_agents/memory/solutions/_extractor.py) (Phase 2)
- [`SolutionPromoter`](../../src/fmh_agents/memory/solutions/_promoter.py) (Phase 2)
- [`PiiRedactionTool`](../../src/fmh_agents/tools/pii/_tool.py) (Phase 3)
