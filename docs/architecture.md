<!--
SPDX-FileCopyrightText: 2026 kinetgraph
SPDX-License-Identifier: Apache-2.0
-->

# Architecture

`kntgraph` is a pure, event-sourced agent
framework. Its design rests on **three pillars**
that compose:

1. **Pure ECS** — agents are a deterministic
   function of events (`World.fold`).
2. **Event Sourcing** — the source of truth is a
   Redis Stream; the `World` is a derived
   projection.
3. **Resilience** — every I/O is bounded and
   retried; failures land in a Dead Letter Queue.

The same design supports both the **core
framework** (ECS, event sourcing, resilience,
security, API gateway) and the **`agents`
sub-module** (LLM/cache/PII adapters, roles,
semantic routing, solution tier).

## High-level diagram

```
                          ┌─────────────────┐
                          │   Application   │
                          │   (Role, Tool)  │
                          └────────┬────────┘
                                   │ emit events
                                   ▼
┌──────────────────────────────────────────────────────┐
│                    EventLog (Redis Streams)            │
└────────┬─────────────────────────────────────┬────────┘
         │ read                                  │ append
         ▼                                       ▲
┌────────────────────┐                  ┌─────────────────┐
│  ReactiveDispatcher│  ── World ──▶   │  WorldSystem(s)  │
│  + WorldCheckpoint │                  │  (your code)    │
└────────┬───────────┘                  └─────────────────┘
         │
         ▼
┌────────────────────┐
│  World (in memory) │
│  - agent views     │
│  - tool calls      │
│  - solution nodes  │
└────────────────────┘
```

## The three pillars

### 1. Pure ECS

The `World` is the **fold** of all events for
all agents. It is a deterministic function:

```python
world = World.fold(events)
```

`World.fold` is:
- **Pure** — given the same events, it
  produces the same `World`.
- **Incremental** — `world.with_event(e)`
  returns a new `World` in O(1) per event.
- **Inspectable** — `world.agents[agent_id]`
  is an `AgentView` with components
  (operational phase, domain phase, tool
  calls, solution candidates, …).

There is no in-place mutation of agent state
that bypasses the event log. Every change is
expressed as an `Event` and folded.

See [ECS](ecs.md) for the details.

### 2. Event Sourcing

The **single source of truth** is the `EventLog`
(Redis Streams). The `World` is a derived
projection:

```python
log = EventLog(RedisEventLogAdapter(redis))
events = await log.read(agent_id)
world = World.fold(events, projection=project_tool_calls)
```

Properties:
- **Replay-safe** — re-applying the same events
  produces the same `World`. The EventLog
  deduplicates via `event_id`.
- **Crash-safe** — the `ReactiveDispatcher`
  commits a checkpoint *after* the batch's
  events are durably appended; a crash between
  append and save replays the same events on
  restart.
- **At-most-once side effects** — the `Tool`
  protocol's `idempotency_key` is forwarded
  from the framework into the tool call. A
  tool that honors it (most LLM providers do)
  dedupes the side effect.

See [Event Sourcing](event_sourcing.md) for the
details.

### 3. Resilience

Every I/O call in the framework is wrapped in
the resilience layer:

- **Circuit breaker** — when a downstream is
  failing, stop sending it requests for a cool-down
  period.
- **Retry** — exponential backoff + jitter.
- **Bulkhead** — limit concurrent calls to a
  single downstream.
- **Timeout** — bound every call.
- **Fallback** — when all else fails, a
  secondary model or a cached response.

When a `WorldSystem` raises during dispatch, the
exception is caught and the event lands in the
**Dead Letter Queue** for manual or automatic
replay. The dispatcher never crashes on a
single bad event.

See [Resilience](resilience.md) and
[Dead Letter Queue](dead_letter_queue.md) for
the details.

## Dual lifecycle

Every event has an `event_class` that puts it in
one of two orthogonal lifecycles:

| `event_class`  | Purpose                                         |
| -------------- | ----------------------------------------------- |
| `lifecycle`    | Operational / framework state (spawn, ack, fail) |
| `domain`       | Application state (intent, document, decision) |

The `AgentView` separates the two:

```python
view = world.agents["a-1"]
view.operational_phase  # from lifecycle events
view.domain_phase       # from domain events
```

The same `World` carries both views because
both classes of events are folded into the same
ECS storage; the projection just exposes them
separately.

See [ADR-003: Ciclo Dual](../ADRs/ADR-003-Ciclo-Dual.md)
for the rationale.

## Idempotency

The framework guarantees:
- **EventLog append is idempotent** — the same
  `event_id` is deduplicated via `dedup_keys`.
- **World fold is idempotent** — applying the
  same event twice produces the same `World`.
- **Tool calls carry `idempotency_key`** — a
  tool that honors it can dedupe the side
  effect.

This is what makes at-least-once delivery
(via the `ReactiveDispatcher`) safe to use with
external systems that don't have their own
idempotency (e.g. payment APIs).

## The `agents` sub-module

The `agents` namespace ships the **vertical
adapters** — the things that depend on a
specific LLM provider, a specific cache
backend, a specific PII detection model:

```python
from kntgraph.agents.tools import LiteLLMTool
from kntgraph.agents.tools.cache import CachingLLMTransport
from kntgraph.agents.tools.pii import PiiRedactionTool
from kntgraph.agents.roles import RoleComponent, IntentResolutionSystem
```

Users who only need the core ECS / event-sourcing
layer can install `kntgraph` and ignore the
`agents` namespace. The core has no LLM, PII, or
provider-specific deps.

## The Solution tier (ADR-010)

The flagship feature on top of the framework.
When a tool call succeeds for a given
`(problem, params)` pair across multiple
agents, the framework promotes it into a
reusable **Solution** node in FalkorDB. A
future agent facing a similar problem can
look up the solution before invoking the tool,
saving latency and cost.

The tier has three roles:
- `SolutionExtractorSystem` — observes
  `tool.*.requested` / `.completed` pairs,
  builds `SolutionCandidate`s.
- `SolutionPromoterSystem` — deduplicates
  candidates across agents; bumps confidence
  when a pattern repeats.
- `SolutionReviewPublisher` — surfaces
  high-confidence candidates to a human
  review queue (the "man in the loop").

See [Solution tier (ADR-010)](consolidation.md)
for the details.

## Where to read next

- [ECS](ecs.md) — `World`, `AgentView`,
  `WorldSystem`.
- [Event Sourcing](event_sourcing.md) —
  `EventLog`, `World.fold`, replay.
- [Tools](tools.md) — the `Tool` Protocol,
  `ToolInvoker`, idempotency.
- [Resilience](resilience.md) — circuit
  breaker, retry, bulkhead.
- [Solution tier](consolidation.md) —
  tool-call re-use (ADR-010).
- [Security](security/) — event signing,
  RBAC, ACL.
- [ADRs](../ADRs/) — the full list of
  architecture decisions.
