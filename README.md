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

The `agents` sub-module ships concrete LLM, cache, and PII adapters on top of
the framework:

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
| **Solution tier** | Successful tool calls are promoted to reusable Solution nodes in FalkorDB, with per-tenant allow-list and human-in-the-loop review. |

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

## Quality gates

The badges below mirror the gates in [`scripts/ci.py`](scripts/ci.py). Values
are generated by `scripts/quality_report.py` on every CI run and pinned in
[`docs/quality.md`](docs/quality.md).

<div align="center">

### Code quality

[![cc](https://img.shields.io/badge/CC-A%20%282.55%29-brightgreen?style=for-the-badge&logo=radar&logoColor=white)](https://radon.readthedocs.io/)
[![mi](https://img.shields.io/badge/MI-250_A_0_B_0_C-brightgreen?style=for-the-badge&logo=heartbeat&logoColor=white)](https://radon.readthedocs.io/)
[![pyright](https://img.shields.io/badge/pyright-0%20errors-brightgreen?style=for-the-badge&logo=microsoft&logoColor=white)](https://microsoft.github.io/pyright/)
![Version](https://img.shields.io/badge/version-0.14.1-blue)
[![pypi](https://img.shields.io/badge/pypi-0.14.1-blue?style=for-the-badge&logo=pypi&logoColor=white)](https://pypi.org/project/kntgraph/)

### Tests

[![coverage](https://img.shields.io/badge/coverage-88%25-brightgreen?style=for-the-badge&logo=codecov&logoColor=white)](https://coverage.readthedocs.io/)
[![tests](https://img.shields.io/badge/tests-2305%20passed-brightgreen?style=for-the-badge&logo=pytest&logoColor=white)](https://docs.pytest.org/)

### Security

[![security](https://img.shields.io/badge/security-bandit-brightgreen?style=for-the-badge&logo=shield&logoColor=white)](https://bandit.readthedocs.io/)
[![audit](https://img.shields.io/badge/audit-pip--audit-blueviolet?style=for-the-badge&logo=dependabot&logoColor=white)](https://pypi.org/project/pip-audit/)

</div>

Pyright: 0 errors above the baseline (870 warnings tracked separately;
see [`DEBT.md`](DEBT.md) §4.2 for the warning budget).

## Project status

| Version | Highlights |
| --- | --- |
| **0.14.1** *(current)* | Reliability fixes on top of the Three-Gate Model cycle. |
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
| 250 | 214 (2,305 tests collected) | 62 | 27 pages |
<!-- STATS END -->
