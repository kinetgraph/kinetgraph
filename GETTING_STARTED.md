<!--
SPDX-FileCopyrightText: 2026 kinetgraph
SPDX-License-Identifier: Apache-2.0
-->

# Getting Started with kntgraph

`kntgraph` is a pure, event-sourced framework for
building autonomous agents. This guide walks
you through the mental model and your first
end-to-end agent in about 10 minutes.

If you just want to install and run something
quickly, jump to the [Quick Start](docs/quickstart.md).
This guide explains **how the framework thinks** —
useful before you write your first non-trivial
agent.

## Mental model

`kntgraph` is built on three pillars:

1. **Pure ECS** — the state of every agent is a
   pure function of its events. There is no
   mutable agent object; the `World` is recomputed
   (incrementally) from the event stream.
2. **Event Sourcing** — the source of truth is a
   Redis Stream. The `World` is a derived
   projection; you can throw it away and rebuild
   it from the events.
3. **Resilience** — every I/O call has a timeout,
   a circuit breaker, a retry, and a bulkhead.
   Failures land in a Dead Letter Queue.

If you've used Redux, event sourcing in any
language, or Erlang/OTP's process model, the
mental model will feel familiar.

## The four concepts

| Concept       | One-liner                                                              |
| ------------- | ---------------------------------------------------------------------- |
| `Event`       | "Something that happened" — append-only, immutable, has a UUID.        |
| `EventLog`    | An append-only store of events, backed by Redis Streams.               |
| `World`       | A derived projection of all events for all agents, recomputed on demand. |
| `WorldSystem` | A function `World → list[Event]`. The agent's logic.                  |

That's it. Everything else (the `Tool` Protocol,
the `Solution` tier, the semantic router, …) is
built on top of these four.

## How a tick works

The `ReactiveDispatcher` is the main loop. Every
tick (default `1.0` seconds), for every tracked
agent, it does:

1. **Load checkpoint** from Redis
   (`WorldCheckpoint` — the last `(World,
   last_stream_id)` pair the dispatcher
   committed).
2. **Read new events** from the EventLog
   (`xrange(last_stream_id, "+")`).
3. **Fold** the new events into the World
   incrementally (`World.with_event(e)` × N).
4. **Call systems** with the post-fold World:
   `events = [s(world) for s in self._systems]`.
5. **Append the resulting events** to the
   EventLog.
6. **Save a new checkpoint** *after* the append
   is durable.

This is what makes the framework
**crash-safe**: a crash between steps 5 and 6
replays the same events on restart, the
`EventLog` deduplicates by `event_id`, and the
`World` ends up identical.

## Your first agent

Let's build an agent that processes a
`document.received` event and emits a
`document.classified` event with a category.

### Step 1: the event

Every `Event` has a stable `event_id` (UUID) and
a `correlation` (the chain of causation IDs that
led to it). The framework provides builders:

```python
from kntgraph.core.event import Event, CorrelationContext

ctx = CorrelationContext.new()
event = Event.create(
    event_type="document.received",
    agent_id="a-1",
    event_class="domain",
    data={"doc_id": "NF-001", "amount": 100.0},
    correlation=ctx,
)
```

### Step 2: the system

A `WorldSystem` is a function that takes a
`World` and returns a list of events. Here's a
trivial classifier:

```python
from kntgraph.core.system import WorldSystem
from kntgraph.core.event import Event, CorrelationContext
from kntgraph.core.world import World


def classify_doc(world: World) -> list[Event]:
    events = []
    for agent_id, view in world.views.items():
        for event_type, _ in view.components.items():
            if event_type != "document.received":
                continue
            # In a real system, you'd inspect the
            # event payload via the view. Here we
            # always classify as "standard".
            events.append(
                Event.create(
                    event_type="document.classified",
                    agent_id=agent_id,
                    event_class="domain",
                    data={"category": "standard"},
                    correlation=CorrelationContext.new(),
                )
            )
    return events


system = WorldSystem(classify_doc)
```

### Step 3: the dispatcher

Wire the system to the `ReactiveDispatcher`:

```python
import asyncio
import redis.asyncio as aioredis
from kntgraph.infra.config import fresh_settings
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog, RedisEventLogAdapter


async def main() -> None:
    settings = fresh_settings()
    redis = aioredis.from_url(settings.redis_url)
    log = EventLog(RedisEventLogAdapter(redis))
    dispatcher = ReactiveDispatcher(
        log=log,
        systems=[system],
    )
    dispatcher.track_agent("a-1")
    await dispatcher.dispatch_once()


asyncio.run(main())
```

Run with `FMH_REDIS_URL=redis://localhost:6379/0
python my_agent.py`. The dispatcher will:
1. Bootstrap (read the existing agents in
   Redis).
2. Find no new events for `a-1`.
3. Do nothing.

To see the agent *do* something, append an event
to the stream first:

```python
await log.append(event)
await dispatcher.dispatch_once()
```

Now the dispatcher sees the new event, folds it
into the `World`, calls your `classify_doc`
system, appends the `document.classified` event,
and saves a checkpoint.

### Step 4: idempotency

Real systems need to handle at-least-once
delivery safely. The framework gives you
`idempotency_key` on `Event` and `Tool.invoke`:

```python
result = await tool.invoke(
    idempotency_key=str(request.event_id),
    **merged_args,
)
```

The `Tool` Protocol forwards this to whatever
downstream you're calling. LiteLLM, for example,
dedupes calls with the same `idempotency_key`
(when the upstream provider supports it). For
tools that don't, the framework's
`ToolInvoker` is the safe place to add
deduplication.

## What to read next

- [Quick Start](docs/quickstart.md) — install
  and run the hello world.
- [Architecture](docs/architecture.md) — the
  full three-pillar overview.
- [ECS](docs/ecs.md) — `World`, `AgentView`,
  `WorldSystem`.
- [Event Sourcing](docs/event_sourcing.md) —
  `EventLog` and `World.fold`.
- [Tools](docs/tools.md) — the `Tool` Protocol
  and `ToolInvoker`.
- [Solution tier](docs/consolidation.md) —
  tool-call re-use (ADR-010).
- [ADRs](ADRs/) — the full list of architecture
  decisions.

## When to use `kntgraph`

Good fit:
- You need **replayable, audit-friendly**
  agent state (regulatory, debugging,
  postmortem).
- You're building **multi-agent** systems with
  shared state and per-agent isolation.
- You want **deterministic** testing — fold a
  fixed event list, assert on the resulting
  World.
- You need **at-least-once** delivery with safe
  retries (Redis Streams give you this for free).

Not a good fit:
- Single-process, single-agent prototypes
  where the framework's ceremony is overkill.
- Sub-millisecond latency requirements (the
  Redis hop is in the hot path).
- Pure request/response APIs (use FastAPI or
  a similar framework; the `agents` sub-module
  integrates with these via the HTTP gateway).
