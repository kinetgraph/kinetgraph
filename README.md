<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# kinetgraph

A pure, event-sourced ECS framework for building autonomous agents over Redis Streams.

The framework models agent state as a deterministic fold over an immutable event
log. Side effects (LLM calls, HTTP requests, tool execution) run outside the fold,
in isolated workers. The result is an agent whose entire history is replayable,
whose state is a pure function of events, and whose tool calls are at-most-once
even under at-least-once delivery.

## When to use

- You need agents whose decisions are **auditable and replayable** from first principles.
- You want to run **LLM calls in process-pool workers** so the event loop never blocks.
- You need **idempotent tool execution** across retries and dispatcher restarts.
- You want a **single Redis Stream** as the source of truth, with no separate state DB.
- You are building **multi-tenant** systems where each agent has its own isolated event log.

## Install

```bash
uv add kntgraph
```

Optional extras (install only what you need):

```bash
uv add "kntgraph[cli]"          # knt scaffold CLI
uv add "kntgraph[falkordb]"     # graph projection + Cypher (FalkorDB)
uv add "kntgraph[ollama]"       # local LLM / embeddings
uv add "kntgraph[gliner]"       # GLiNER2 NER — intent routing and PII redaction
uv add "kntgraph[api]"          # HTTP gateway (FastAPI)
uv add "kntgraph[crypto]"       # Ed25519 event signing
uv add "kntgraph[llm]"          # LiteLLM adapter
uv add "kntgraph[all-runtime]"  # everything above
```

> To install the unreleased `main` between tagged releases:
> `uv add "kntgraph @ git+https://github.com/kinetgraph/kinetgraph.git"`.
> Tagged releases on PyPI are the canonical, supported path.

## Hello world

A agent runs as a **pure fold** over its event log; the
side effects (LLM calls, HTTP requests, tool execution) live
in **systems** that emit events and in **tools** that
process them. The framework gives you three primitives
that compose:

- `Event` — append-only, signed, idempotent. The
  EventLog is the source of truth.
- `World` — a pure fold of events into `AgentView`s with
  typed ECS components. Re-fold from event 0 to reproduce
  any past state exactly.
- `System` — a pure function `(World) -> list[Event]`.
  Emits events; never touches I/O.
- `Tool` — an async function decorated with `@tool_worker`,
  executed by a `WorkerManager` in a process pool. The
  dispatcher fans `tool.requested` events out to the
  worker pool, and the worker writes `tool.<name>.completed`
  back to the EventLog.

```python
import asyncio
from kntgraph.core.event import Event
from kntgraph.core.result import Ok, Result, ToolError
from kntgraph.core.world import World
from kntgraph.tools.system import ToolAwareSystem
from kntgraph.tools.worker import tool_worker


# 1. A tool — runs in a worker pool, off the event loop.
@tool_worker(name="weather_api")
class WeatherTool:
    async def invoke(self, city: str, *, idempotency_key: str) -> Result[dict, ToolError]:
        return Ok({"city": city, "temp_c": 22, "condition": "sunny"})


# 2. A system — pure, observes the World, requests a tool.
class WeatherSystem(ToolAwareSystem):
    async def __call__(self, world):
        view = world.agents.get("weather-bot")
        if view is None or view.last_event_id is None:
            return []
        # Emit tool.requested only when a new city arrives.
        if "weather.requested" in view.components:
            return []
        return [Event.domain_from(
            agent_id="weather-bot",
            event_type="weather.requested",
            data={"city": "Rio"},
            correlation=world.agents["weather-bot"].correlation_id,
        )]


# 3. A pure fold — replayable, deterministic.
events = [
    Event.create("agent.spawned", agent_id="weather-bot", event_class="lifecycle"),
    Event.create("weather.requested", agent_id="weather-bot", event_class="domain",
                 data={"city": "Rio"}),
]
world = World.fold(events, tick=2)
print(world.agents["weather-bot"].components["weather.requested"])
# {"city": "Rio"}
```

Run this with a real `ReactiveDispatcher` and `WorkerManager`
to see the system request the tool, the worker execute it,
and the fold surface the completion — see
[`examples/19_tool_worker_pattern.py`](examples/19_tool_worker_pattern.py)
for the full picture.

The `agents` sub-module ships concrete LLM, cache, and PII
adapters on top of the framework:

```python
from kntgraph.agents.tools import LiteLLMToolWorker

worker = LiteLLMToolWorker()
result = await worker.invoke(
    system="You are a helpful assistant.",
    user="What is the capital of France?",
    idempotency_key="k1",
)
# ``result`` is a ``Result[dict, ToolError]``; the dict
# envelope carries ``text`` / ``model`` / ``usage`` /
# ``finish_reason`` / ``cost_usd`` / ``latency_ms``.
```

## What the framework provides

| Capability | How it works |
| --- | --- |
| **Replayable state** | `World` is a pure fold over the EventLog. Re-fold from event 0 to reproduce any past state exactly. |
| **At-most-once tools** | `idempotency_key` on every tool call deduplicates side effects across retries and restarts. |
| **Non-blocking LLM** | Workers run in a `ProcessPoolExecutor`; the async event loop is never blocked by an LLM call. |
| **Three-gate authorisation** | Role persona (gate 2) → per-tool ACL in `WorkerManager` (gate 1) → worker-level check (gate 3). |
| **Resilience primitives** | Circuit breaker, retry, bulkhead, timeout, fallback, and a Dead Letter Queue — all composable. |
| **Durable checkpoints** | `ReactiveDispatcher` commits a Redis checkpoint *after* emitted events are durably appended, so a crash replays the same batch on restart. |
| **Domain memory** | Fold domain events into frozen ECS `@dataclass` components attached to the `World` entity (no volatile sliding window required). |
| **Zero-Token Architecture** | `RuleBasedChatSystem` short-circuits deterministic intents; `SolutionLookupSystem` synthesises cached completions before calling the LLM. |
| **Semantic routing** | Opt-in GLiNER2 intent classification and argument extraction in the `agents` sub-module. |
| **Solution tier** | Successful tool calls are promoted to reusable Solution nodes in Redis Hashes + HNSW vector index (ADR-062), orchestrated via `KnowledgeConsolidationSaga` (ADR-069) with LGPD PII gate. |

## CLI scaffold

`knt` is the first-party CLI for scaffolding ADR-compliant projects and contexts:

```bash
# Install with the [cli] extra
uv add "kntgraph[cli]"

# Scaffold a new application
knt init project my_platform --use-intent-http

# Or choose a routing mode explicitly
knt init project my_platform --routing-mode external
# external   — routes intents from outside the agent boundary
# autonomous — agent resolves intents internally
# collaborate — multiple agents coordinate on a shared intent

# Add domain contexts and systems
cd my_platform
knt new context weather
knt new system weather.WeatherRouter
knt new tool weather.OpenMeteoApi

# Check for framework drift in boilerplate
knt upgrade check
```

See the [CLI Guide](docs/cli_guide.md) for a full walkthrough.

## Architecture

```
kntgraph/
├── src/kntgraph/
│   ├── core/        # Pure: ECS, Event, World, System
│   ├── stream/      # Redis Streams (EventLog, fold)
│   ├── runner/      # Side effects (Runner, ReactiveDispatcher,
│   │                #   WorldProjection, MemoryHydrationProjection,
│   │                #   ToolCallTTLSweeperSystem)
│   ├── events/      # Dead Letter Queue
│   ├── resilience/  # Circuit breaker, retry, bulkhead, etc.
│   ├── infra/       # Config, Redis pool, hashing
│   ├── tools/       # Tool Protocol, WorkerManager, worker, ACL
│   ├── api/         # Optional HTTP gateway
│   ├── security/    # Ed25519 signing, principal, ACL, PrincipalLevel
│   ├── memory/      # Session, Profile, Continuity managers
│   ├── knowledge/   # Embedding, FalkorDB graph, GraphRAG, GLiNER2
│   ├── testing/     # Public test utilities (fakes, stubs)
│   ├── cli/         # knt CLI — scaffold generator
│   └── agents/      # LLM/PII adapters, role_systems
│       ├── role_systems/ # ChatRoleSystem, PlannerRoleSystem, etc.
│       ├── tools/   # LiteLLMToolWorker, PiiRedactionTool
│       └── memory/  # Solution extractor/promoter
├── tests/
│   ├── unit/        # No external dependencies
│   ├── integration/ # Real Redis required
│   ├── agents/      # agents sub-module tests
│   ├── stress/      # 5 agents × 3 tools × 5 s concurrent load
│   └── scripts/     # CI contract tests (workflow split, etc.)
├── ADRs/            # Architecture Decision Records
├── docs/            # Public documentation
└── examples/        # Runnable end-to-end examples
```

## Configuration

All settings live under the `KNT_` env-var prefix and are loaded via
Pydantic v2 `BaseSettings`. The canonical schema is `Settings` in
`kntgraph.infra.config`.

| Env var | Default |
| --- | --- |
| `KNT_REDIS_URL` | `redis://localhost:6379` |
| `KNT_FALKORDB_HOST` | `localhost` |
| `KNT_FALKORDB_PORT` | `16379` |
| `KNT_STREAM_MAXLEN` | `100_000` |
| `KNT_TICK_INTERVAL` | `1.0` (seconds) |
| `KNT_ENV` | `dev` (set to `prod` in deploy) |

## Run the tests

```bash
# Unit (fast, no Redis required)
uv run pytest tests/unit/

# Integration (requires Redis on localhost:6379)
uv run pytest tests/integration/

# Agents sub-module tests
uv run pytest tests/agents/

# Stress suite (requires Redis on localhost:6379)
uv run pytest tests/stress/

# CI contract tests
uv run pytest tests/scripts/
```

## Documentation

- [Getting Started](GETTING_STARTED.md) — mental model and your first agent.
- [Quick Start](docs/quickstart.md) — 5-minute install and "hello world".
- [Architecture](docs/architecture.md) — the three pillars (ECS, event sourcing, resilience) and how the pieces fit together.
- [Zero Token Architecture](docs/zta.md) — software handlers before LLM, read-side cache, hybrid dispatcher stack.
- [API Reference](REFERENCE.md) — the public API map, env-var table, and common patterns.
- [CLI Guide](docs/cli_guide.md) — scaffolding projects, contexts, systems, tools, and agents.
- [docs/](docs/README.md) — full index of all docs.
- [ADRs/](ADRs/) — Architecture Decision Records.

### Active Architecture Decision Records (In Progress)

- **[ADR-062](ADRs/ADR-062-GraphRAG-redis-graphblas.md)** — **GraphRAG in Redis with PyGraphBLAS & WorkflowSagaConcordo**: Complete FalkorDB decommissioning, native Redis vector search (HNSW `< 2ms`), PyGraphBLAS C sparse matrix analytics, and single Redis infrastructure.
- **[ADR-069](ADRs/ADR-069-Agent-Concordo-Macro-Behaviors.md)** — **Agent Concordo Macro Behaviors**: Pure BusinessFSM & WorkflowSaga orchestration patterns.
- **[ADR-070](ADRs/ADR-070-Entity-Relation-Extraction-Concordo-Pipeline.md)** — **Entity & Relation Extraction Concordo Pipeline**: Decoupled extraction saga with Human-in-the-Loop (HITL) confidence gating (`< 0.80`) to prevent ontology drift and specialized `@tool_worker` tools.

## Quality gates

The badges below mirror the gates in [`scripts/ci.py`](scripts/ci.py). Values
are generated by `scripts/quality_report.py` on every CI run and pinned in
[`docs/quality.md`](docs/quality.md).

<div align="center">

### Code quality

[![cc](https://img.shields.io/badge/CC-A%20%282.59%29-brightgreen?style=for-the-badge&logo=radar&logoColor=white)](https://radon.readthedocs.io/)
[![mi](https://img.shields.io/badge/MI-251_A_0_B_0_C-brightgreen?style=for-the-badge&logo=heartbeat&logoColor=white)](https://radon.readthedocs.io/)
[![pyright](https://img.shields.io/badge/pyright-0%20errors-brightgreen?style=for-the-badge&logo=microsoft&logoColor=white)](https://microsoft.github.io/pyright/)
![Version](https://img.shields.io/badge/version-0.15.0-blue)
[![pypi](https://img.shields.io/badge/pypi-0.15.0-blue?style=for-the-badge&logo=pypi&logoColor=white)](https://pypi.org/project/kntgraph/)

### Tests

[![coverage](https://img.shields.io/badge/coverage-90%25-brightgreen?style=for-the-badge&logo=codecov&logoColor=white)](https://coverage.readthedocs.io/)
[![tests](https://img.shields.io/badge/tests-2369%20passed-brightgreen?style=for-the-badge&logo=pytest&logoColor=white)](https://docs.pytest.org/)

### Security

[![security](https://img.shields.io/badge/security-bandit-brightgreen?style=for-the-badge&logo=shield&logoColor=white)](https://bandit.readthedocs.io/)
[![audit](https://img.shields.io/badge/audit-pip--audit-blueviolet?style=for-the-badge&logo=dependabot&logoColor=white)](https://pypi.org/project/pip-audit/)

</div>

Pyright: 0 errors above the baseline (warnings tracked separately;
see [`DEBT.md`](DEBT.md) §4.2 for the warning budget).

## Project status

| Version | Highlights |
| --- | --- |
| **0.15.0** *(current)* | ADR-068 phases 0–1–4: idle-traffic mitigation (P0/P8: KNT_-env knobs, `count>1` `XREADGROUP`, checkpoint zlib compression), `EventLog.subscribe` blocking-XREAD primitive (P1), parallel-key incremental cache refresh (P4 — `<key>:fold_cursor`, payload stays legacy-compatible). `Runner._fold_incremental` for O(delta) fold. Pyright baseline cleared (5 → 0). `quality_report.py` flake fixed. Zero breaking changes — Protocol/superclass widened, kwargs optional, wire format only adds optional fields. |
| 0.14.2 | Fixed ownership rule for derived components (ADR-067) and implicit materialisation of profile/continuity states. |
| 0.14.1 | Reliability fixes on top of the Three-Gate Model cycle. |
| 0.14.0 | Three-Gate authorisation (`RoleComponent` + `WorkerManager` ACL + worker-level). `PrincipalLevel` replaces the legacy `Role` enum. Pluggable `WorldProjection` on `ReactiveDispatcher`. `ToolRegistry` deprecated in favour of `WorkerManager`. Fixes `CorrelationContext` binding inside dispatcher ticks. |
| 0.13.0 | Domain Memory via ECS Components (`ADR-059`). Data durability strategy and disaster recovery (`ADR-057`, `ADR-058`). |
| 0.12.1 | Reliability fixes, worker invocation module. |
| 0.11.0 | First PyPI release (`pip install kntgraph`). Two-workflow publish flow with Trusted Publishing (PEP 740). CLI Boilerplate Generation v2 with `knt upgrade`. |
| 0.10.0 | Zero Token Architecture (`RuleBasedChatSystem`, `SolutionLookupSystem`). Removes legacy `_legacy_principal` fallback (breaking — run `scripts/migrate_principals.py` before upgrading). |
| 0.9.0 | Drops deprecated `LiteLLMTool` / `ToolInvoker` / `kntgraph.agents.roles`. ECS role systems (`ChatRoleSystem`, `PlannerRoleSystem`, etc.). |
| 0.7.0 | Public release under the `kntgraph` package name. |

Full changelog: [`CHANGELOG.md`](CHANGELOG.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, the CI gate, and
the pull request workflow. Bug reports and security disclosures follow
[SECURITY.md](SECURITY.md).





<!-- STATS START -->
<!-- This block is regenerated by scripts/readme_stats.py. Do not edit by hand. -->
## Project metrics

| Source modules | Test modules | ADRs | Docs |
| --- | --- | --- | --- |
| 251 (43,125 LOC) | 218 (52,772 LOC, 2,372 tests collected) | 64 | 28 pages |
<!-- STATS END -->
