<!--
SPDX-FileCopyrightText: 2026 kinetgraph
SPDX-License-Identifier: Apache-2.0
-->

# Quick Start

Get up and running with `kntgraph` in 5 minutes.
This guide installs the core framework, brings
up a Redis container for the EventLog, and
shows a complete "hello world" example.

## 1. Install

### Prerequisites

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) — the
  Python project manager.
- Docker (for Redis) — or a local Redis install.

### Install `uv`

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### Install `kntgraph`

```bash
uv add kntgraph
```

Or, for the full development setup with tests
and dev tooling:

```bash
git clone https://github.com/kinetgraph/kntgraph.git
cd kntgraph
uv sync --all-extras
```

### Install Redis

```bash
docker run -d -p 6379:6379 --name kntgraph-redis redis:latest

# Verify
docker ps | grep kntgraph-redis
```

## 2. Hello world

A minimal agent that produces two events and
folds them into a `World`:

```python
import asyncio
from kntgraph.core.event import Event
from kntgraph.core.world import World


async def main() -> None:
    e1 = Event.create(
        event_type="agent.spawned",
        agent_id="a-1",
        event_class="lifecycle",
    )
    e2 = Event.create(
        event_type="document.received",
        agent_id="a-1",
        event_class="domain",
        data={"doc_id": "NF-001"},
    )
    world = World.fold([e1, e2], tick=2)
    print(world.agents["a-1"].operational_phase)  # "spawned"
    print(world.agents["a-1"].domain_phase)       # "document.received"


asyncio.run(main())
```

Run with:

```bash
python hello.py
```

You should see:

```
spawned
document.received
```

## 3. The `ReactiveDispatcher`

The "hello world" is pure: no I/O, no Redis. For
a full system, the `ReactiveDispatcher` reads
events from the `EventLog`, folds them into a
per-agent `World`, and calls every registered
`WorldSystem`:

```python
import asyncio
import redis.asyncio as aioredis
from kntgraph.core.event import Event
from kntgraph.infra.config import fresh_settings
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog, RedisEventLogAdapter


async def main() -> None:
    settings = fresh_settings()
    redis = aioredis.from_url(settings.redis_url)
    log = EventLog(RedisEventLogAdapter(redis))
    dispatcher = ReactiveDispatcher(
        log=log,
        systems=[],  # no systems yet
    )
    await dispatcher.dispatch_once()


asyncio.run(main())
```

The dispatcher:
1. Reads the per-agent `WorldCheckpoint` from
   Redis.
2. Polls the `EventLog` for new events.
3. Folds the new events into the `World`.
4. Calls every `WorldSystem` with the post-fold
   `World`.
5. Appends the resulting events to the
   `EventLog`.
6. Saves a new checkpoint *after* the batch
   is durably committed.

## 4. The `agents` sub-module

For LLM-backed agents, install the `agents`
sub-module extras:

```bash
uv add "kntgraph[llm,falkordb,ollama]"
```

Then a simple LLM call:

```python
import asyncio
from kntgraph.agents.tools import LiteLLMTool


async def main() -> None:
    tool = LiteLLMTool(default_model="gpt-4o-mini")
    result = await tool.invoke(
        idempotency_key="k1",
        system="You are a helpful assistant.",
        user="What is the capital of France?",
    )
    print(result.unwrap().text)  # "Paris"


asyncio.run(main())
```

## 5. Next steps

- [Architecture overview](architecture.md) —
  the three pillars and how the pieces fit.
- [Event Sourcing](event_sourcing.md) — the
  `EventLog` and the `World.fold` algorithm.
- [Tools](tools.md) — the `Tool` Protocol and
  the `ToolInvoker`.
- [Semantic routing](routing.md) — GLiNER2-based
  intent + argument extraction.
- [Solution tier](consolidation.md) — tool-call
  re-use (ADR-010).

## 6. Configuration

All settings are loaded from env vars with the
`FMH_` prefix. The canonical schema is
`kntgraph.infra.config.Settings`:

```python
from kntgraph.infra.config import settings

print(settings.redis_url)        # redis://localhost:6379
print(settings.falkordb_port)    # 16379
print(settings.stream_maxlen)    # 100_000
```

See [Configuration](../README.md#configuration)
in the README for the full env-var table.

## 7. Running the tests

```bash
# Unit (fast, no Redis required)
uv run --package kntgraph pytest kntgraph/tests/unit/

# Integration (requires Redis on localhost:6379)
uv run --package kntgraph pytest kntgraph/tests/integration/
```

## Troubleshooting

- **`ImportError: falkordblite is not installed`**
  → run `uv sync --all-extras` to install the
  dev extras.
- **`redis.exceptions.ConnectionError`** →
  make sure the Redis container is up
  (`docker ps | grep kntgraph-redis`).
- **`pydantic.ValidationError` on `Settings`** →
  check your env vars; the framework validates
  types at import time.

If something else is wrong, file a
[bug report](https://github.com/kinetgraph/kntgraph/issues/new?template=bug_report.md).
