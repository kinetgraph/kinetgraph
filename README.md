<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# kntgraph

A pure, event-sourced agent framework over Redis Streams.

[![lint](https://img.shields.io/badge/lint-ruff-4c1?style=flat-square)](https://docs.astral.sh/ruff/)
[![format](https://img.shields.io/badge/format-ruff-4c1?style=flat-square)](https://docs.astral.sh/ruff/)
[![type-check](https://img.shields.io/badge/type--check-pyright-blue?style=flat-square)](https://microsoft.github.io/pyright/)
[![tests](https://img.shields.io/badge/tests-pytest-0a9?style=flat-square)](https://docs.pytest.org/)
[![security](https://img.shields.io/badge/security-bandit-yellow?style=flat-square)](https://bandit.readthedocs.io/)
[![audit](https://img.shields.io/badge/audit-pip--audit-blueviolet?style=flat-square)](https://pypi.org/project/pip-audit/)

The badges above mirror the gates in [`scripts/ci.py`](scripts/ci.py) — the
single source of truth for the project's quality bar. Keep them green.

kntgraph is the renamed and unified successor of
two internal packages (formerly `fmh_backend` and
`fmh_agents`). It provides the core abstractions
needed to build autonomous, replayable agents:

- **Pure ECS** — `World` is a deterministic
  function of events.
- **Event Sourcing** — Redis Streams is the single
  source of truth.
- **Idempotency** — replay produces the same
  World; `idempotency_key` on `Tool` makes
  at-least-once delivery into at-most-once
  side effects.
- **Dual lifecycle** — operational (framework) and
  domain (application) lifecycles are orthogonal;
  the same `World` carries both views.
- **Resilience** — circuit breaker, retry,
  bulkhead, timeout, fallback, and a Dead Letter
  Queue for failed events.
- **Durable checkpoints** — the
  `ReactiveDispatcher` commits a Redis checkpoint
  *after* the batch's emitted events are
  durably appended to the EventLog, so a crash
  between append and save replays the same events
  on restart (idempotency window).
- **Solution tier** (ADR-010) — tool-call
  re-use: when a tool call succeeds for a given
  `(problem, params)` pair across multiple
  agents, the framework promotes the call into a
  reusable Solution node in FalkorDB, with
  per-tenant allow-list and man-in-the-loop
  review.
- **Semantic routing** (ADR-013) — opt-in GLiNER2
  intent classification and argument extraction
  in the `agents` sub-module.

## Install

Currently, `kntgraph` is not published to PyPI. You can install it directly from GitHub:

```bash
uv add git+https://github.com/kinetgraph/kinetgraph.git
```

Optional extras (install only what you need):

```bash
uv add "kntgraph[cli]@git+https://github.com/kinetgraph/kinetgraph.git"      # CLI Boilerplate Generator (ADR-038)
uv add "kntgraph[falkordb]@git+https://github.com/kinetgraph/kinetgraph.git" # graph projection + Cypher
uv add "kntgraph[ollama]@git+https://github.com/kinetgraph/kinetgraph.git"   # local LLM / embeddings
uv add "kntgraph[gliner]@git+https://github.com/kinetgraph/kinetgraph.git"   # NER-based PII redaction
uv add "kntgraph[api]@git+https://github.com/kinetgraph/kinetgraph.git"      # HTTP gateway (FastAPI)
uv add "kntgraph[crypto]@git+https://github.com/kinetgraph/kinetgraph.git"   # Ed25519 event signing
uv add "kntgraph[llm]@git+https://github.com/kinetgraph/kinetgraph.git"      # LiteLLM adapter
uv add "kntgraph[all-runtime]@git+https://github.com/kinetgraph/kinetgraph.git" # everything above
```

## Hello world

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

The `agents` sub-module ships concrete LLM, cache,
and PII adapters on top of the framework:

```python
from kntgraph.agents.tools import LiteLLMTool

tool = LiteLLMTool(default_model="gpt-4o-mini")
result = await tool.invoke(
    idempotency_key="k1",
    system="You are a helpful assistant.",
    user="What is the capital of France?",
)
```

## Run the tests

```bash
# Unit (fast, no Redis required)
uv run --package kntgraph pytest kntgraph/tests/unit/

# Integration (requires Redis on localhost:6379)
uv run --package kntgraph pytest kntgraph/tests/integration/
```

## CLI Boilerplate Generator

Kinetgraph provides a first-party CLI (`knt`) to scaffold complete, ADR-compliant Modular Monoliths and Contexts.

Install the framework with the `[cli]` extra and initialize a new project:

```bash
# 1. Install Kinetgraph with the CLI extra globally or in your venv
uv pip install "kntgraph[cli]@git+https://github.com/kinetgraph/kinetgraph.git"

# 2. Scaffold a new application with the HTTP Gateway included
knt init my_platform --use-intent-http

# 3. Enter the project and scaffold domain contexts
cd my_platform
knt new context weather
knt new system weather.WeatherRouter
knt new tool weather.OpenMeteoApi
```

For a comprehensive walkthrough on building an application from scratch using the CLI, refer to the [CLI Guide](docs/cli_guide.md).

## Architecture

```
kntgraph/
├── src/kntgraph/
│   ├── core/        # Pure: ECS, Event, World, System
│   ├── stream/      # Redis Streams (EventLog, fold)
│   ├── runner/      # Side effects (Runner, ReactiveDispatcher)
│   ├── events/      # Dead Letter Queue
│   ├── resilience/  # Circuit breaker, retry, bulkhead, etc.
│   ├── infra/       # Config, Redis pool, hashing
│   ├── tools/       # Tool Protocol, registry, worker
│   ├── api/         # Optional HTTP gateway
│   ├── security/    # Ed25519 signing, principal, ACL
│   └── agents/      # LLM/cache/PII adapters, roles
│       ├── roles/   # SemanticRoutingRole, etc.
│       ├── tools/   # LiteLLMTool, PiiRedactionTool
│       └── memory/  # Solution extractor/promoter
├── tests/
│   ├── unit/        # No external dependencies
│   ├── integration/ # Real Redis required
│   └── agents/      # agents sub-module tests
├── ADRs/            # Architecture Decision Records
├── docs/            # Public documentation
└── examples/        # Runnable end-to-end examples
```

## Configuration

All settings live under the `FMH_` env-var prefix
and are loaded via Pydantic v2 `BaseSettings`. The
canonical schema is `Settings` in
`kntgraph.infra.config`. Highlights:

| Env var                       | Default                          |
| ----------------------------- | -------------------------------- |
| `FMH_REDIS_URL`               | `redis://localhost:6379`         |
| `FMH_FALKORDB_HOST`           | `localhost`                      |
| `FMH_FALKORDB_PORT`           | `16379`                          |
| `FMH_STREAM_MAXLEN`           | `100_000`                        |
| `FMH_TICK_INTERVAL`           | `1.0` (seconds)                  |
| `FMH_ENV`                     | `dev` (set to `prod` in deploy)  |

## Documentation

- [Getting Started](GETTING_STARTED.md) —
  mental model and your first agent.
- [Quick Start](docs/quickstart.md) — 5-minute
  install and "hello world".
- [Architecture](docs/architecture.md) — the
  three pillars (ECS, event sourcing,
  resilience) and how the pieces fit together.
- [API Reference](REFERENCE.md) — the public
  API map, env-var table, and common patterns.
- [docs/](docs/README.md) — full index of the
  docs.
- [ADRs](ADRs/) — Architecture Decision Records.

## Project status

- `0.7.0` — public release under the
  `kntgraph` package name. Backwards-incompatible
  with the old `fmh_backend` / `fmh_agents`
  imports; the source is structurally the same
  (same modules, same tests).
- `0.6.x` — internal releases under the
  `fmh_*` package names (no longer distributed).

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for
development setup, the gate that runs in CI, and
the pull request workflow. Bug reports and
security disclosures follow
[SECURITY.md](SECURITY.md).


<!-- STATS START -->
<!-- This block is regenerated by scripts/readme_stats.py. Do not edit by hand. -->
## Project metrics

- **Source modules**: 241 (37,125 LOC)
- **Test modules**: 161 (36,090 LOC, 1,692 tests collected)
- **ADRs**: 37
- **Docs** (`docs/`): 20 pages
<!-- STATS END -->
