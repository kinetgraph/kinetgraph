<!--
SPDX-FileCopyrightText: 2026 kinetgraph
SPDX-License-Identifier: Apache-2.0
-->

# API Reference

This is a high-level map of the public API. For
the full per-class reference, see the
auto-generated API docs (TODO) or read the
docstrings directly in
[`src/kntgraph/`](src/kntgraph/) and
[`src/kntgraph/agents/`](src/kntgraph/agents/).

The split between the core framework and the
`agents` sub-module mirrors the split between
"infrastructure" and "vertical adapters". Users
who only need ECS / event sourcing can ignore
the `agents` namespace.

## Core framework (`kntgraph`)

### `core` — events and the World

- [`kntgraph.core.event`](src/kntgraph/core/event/)
  — the `Event` dataclass, `Event.create`,
  `Event.domain_from`, `CorrelationContext`.
- [`kntgraph.core.world`](src/kntgraph/core/world/)
  — `World`, `World.fold`, `World.with_event`,
  `AgentView`, `WorldStorage`.
- [`kntgraph.core.system`](src/kntgraph/core/system.py)
  — `WorldSystem` Protocol.
- [`kntgraph.core.result`](src/kntgraph/core/result/)
  — `Result[T, E]`, `Ok`, `Err`, `ToolError`,
  `BusinessError`.

### `stream` — event sourcing

- [`kntgraph.stream.event_log`](src/kntgraph/stream/event_log/)
  — `EventLog`, `RedisEventLogAdapter`,
  append/read, idempotency dedupe.

### `runner` — the dispatch loop

- [`kntgraph.runner.reactive`](src/kntgraph/runner/reactive.py)
  — `ReactiveDispatcher`, the main event loop.
- [`kntgraph.runner.runner`](src/kntgraph/runner/runner.py)
  — `Runner`, the polling-driven variant.
- [`kntgraph.runner.reactive_tool_projection`](src/kntgraph/runner/reactive_tool_projection.py)
  — tool-call projection helpers (ADR-036 §2.3).

### `events` — Dead Letter Queue

- [`kntgraph.events.dlq`](src/kntgraph/events/dlq/)
  — `DeadLetterEvent`, `DLQReason`, `DLQStore`,
  the replay path.

### `memory` — short-term memory tiers

- [`kntgraph.memory`](src/kntgraph/memory/)
  — `SessionManager`, `ProfileManager`,
  `ContinuityManager`, the Redis-backed
  short-term tiers (ADR-014).

### `tools` — the Tool Protocol

- [`kntgraph.tools.protocol`](src/kntgraph/tools/protocol.py)
  — `Tool` Protocol, `Describable`, `Callable`.
- [`kntgraph.tools.registry`](src/kntgraph/tools/registry.py)
  — `ToolRegistry`, the global registry.
- [`kntgraph.tools.invoker`](src/kntgraph/tools/invoker.py)
  — `ToolInvoker`, the dispatcher that turns
  `tool.*.requested` events into calls.
- [`kntgraph.tools.system`](src/kntgraph/tools/system.py)
  — `ToolAwareSystem` mixin.
- [`kntgraph.tools.router`](src/kntgraph/tools/router.py)
  — `ToolRouter`, the global tool-queue router
  (ADR-036 §2.5).
- [`kntgraph.tools.worker`](src/kntgraph/tools/worker.py)
  — `@tool_worker` decorator, the WorkerManager
  protocol.
- [`kntgraph.tools.schema`](src/kntgraph/tools/schema.py)
  — `FieldSpec`, `walk_schema`,
  `compute_schema_version`.

### `knowledge` — graph + embeddings + extraction

- [`kntgraph.knowledge.extraction`](src/kntgraph/knowledge/extraction/)
  — `EntityExtractor`, `IntentClassifier`,
  `ArgExtraction` (ADR-013 §2.2).
- [`kntgraph.knowledge.falkordb`](src/kntgraph/knowledge/falkordb/)
  — FalkorDB adapter (`GraphPool`,
  `LiteFalkorDBClient`).
- [`kntgraph.knowledge.graph`](src/kntgraph/knowledge/graph/)
  — graph schema for the `(:Document)` and
  `(:Entity)` sub-graphs.
- [`kntgraph.knowledge.graphrag`](src/kntgraph/knowledge/graphrag/)
  — `GraphRAGRetriever`, hybrid retrieval.
- [`kntgraph.knowledge.embedding`](src/kntgraph/knowledge/embedding/)
  — `EmbeddingClient` (Hash, Ollama).

### `infra` — adapters and config

- [`kntgraph.infra.config`](src/kntgraph/infra/config/)
  — `Settings` (the Pydantic v2 settings
  class), `BaseSettings`, helpers.
- [`kntgraph.infra.redis`](src/kntgraph/infra/redis/)
  — `RedisLike` Protocol, factory functions,
  world-checkpoint store.
- [`kntgraph.infra.hashing`](src/kntgraph/infra/hashing.py)
  — `short_hash` (SHA-256 truncated to 16 hex
  chars).
- [`kntgraph.infra.world_checkpoint`](src/kntgraph/infra/world_checkpoint.py)
  — `IncrementalWorldStore`,
  `WorldCheckpoint`.

### `resilience` — fault tolerance

- [`kntgraph.resilience.circuit_breaker`](src/kntgraph/resilience/circuit_breaker.py)
- [`kntgraph.resilience.retry`](src/kntgraph/resilience/retry.py)
- [`kntgraph.resilience.bulkhead`](src/kntgraph/resilience/bulkhead.py)
- [`kntgraph.resilience.timeout`](src/kntgraph/resilience/timeout.py)
- [`kntgraph.resilience.fallback`](src/kntgraph/resilience/fallback.py)
- [`kntgraph.resilience.rate_limit`](src/kntgraph/resilience/rate_limit.py)
- [`kntgraph.resilience.edge`](src/kntgraph/resilience/edge.py)

### `security` — auth and signing

- [`kntgraph.security.signing`](src/kntgraph/security/signing/)
  — Ed25519 + RFC 8785 event signing (ADR-016).
- [`kntgraph.security.principal`](src/kntgraph/security/principal.py)
  — `Principal`, the authenticated subject.
- [`kntgraph.security.keys`](src/kntgraph/security/keys/)
  — `Key` and the key registry.
- [`kntgraph.api._auth`](src/kntgraph/api/_auth/)
  — `APIKeyVerifier`, `bind_principal_dependency`.

### `api` — the optional HTTP gateway

- [`kntgraph.api.intent_router`](src/kntgraph/api/intent_router/)
  — `app_factory`, the FastAPI gateway that
  turns external HTTP calls into
  `tool.*.requested` events (ADR-012).

## Agents sub-module (`kntgraph.agents`)

### `roles` — agent personas

- [`kntgraph.agents.roles.semantic_router`](src/kntgraph/agents/roles/semantic_router.py)
  — `SemanticRoutingRole` (ADR-013 M1).
- [`kntgraph.agents.roles.summarizer`](src/kntgraph/agents/roles/summarizer.py)
  — `SummarizerRole`.
- [`kntgraph.agents.roles.planner`](src/kntgraph/agents/roles/planner.py)
  — `PlannerRole`.
- [`kntgraph.agents.roles.personalized`](src/kntgraph/agents/roles/personalized.py)
  — `PersonalizedRole`.

### `tools` — vertical tool adapters

- [`kntgraph.agents.tools.llm`](src/kntgraph/agents/tools/llm.py)
  — `LiteLLMTool`, the unified LLM caller.
- [`kntgraph.agents.tools.cache`](src/kntgraph/agents/tools/cache/)
  — `CachingLLMTransport`, `InMemoryCacheStorage`,
  `RedisCacheAdapter`.
- [`kntgraph.agents.tools.pii`](src/kntgraph/agents/tools/pii/)
  — `PiiRedactionTool` (3 levels: regex, NER,
  audit batch).
- [`kntgraph.agents.tools.invoker`](src/kntgraph/agents/tools/invoker/)
  — `ToolInvoker` re-exports.
- [`kntgraph.agents.tools.arg_validation`](src/kntgraph/agents/tools/arg_validation.py)
  — `validate_args`, the schema validator.

### `memory` — vertical memory tiers

- [`kntgraph.agents.memory.solutions`](src/kntgraph/agents/memory/solutions/)
  — `SolutionExtractorSystem`,
  `SolutionPromoterSystem`, the Solution tier
  (ADR-010, ADR-034).

### `config` — agents-level config

- [`kntgraph.agents.config`](src/kntgraph/agents/config.py)
  — `RateLimiter`, `CostBudget` (per-tool
  knobs).

## Common patterns

### Building an `Event`

```python
from uuid import uuid4
from kntgraph.core.event import Event, CorrelationContext

ctx = CorrelationContext.new(correlation_id=uuid4())
event = Event.create(
    event_type="user.intent",
    agent_id="a-1",
    event_class="domain",
    data={"intent": "summarize"},
    correlation=ctx,
)
```

### Folding a `World`

```python
from kntgraph.core.world import World

world = World.fold([e1, e2, e3], tick=3)
print(world.agents["a-1"].operational_phase)
print(world.agents["a-1"].domain_phase)
```

### Calling a `Tool`

```python
from kntgraph.agents.tools import LiteLLMTool

tool = LiteLLMTool(default_model="gpt-4o-mini")
result = await tool.invoke(
    idempotency_key="k1",
    system="You are a helpful assistant.",
    user="What is the capital of France?",
)
if result.is_ok():
    print(result.unwrap().text)
```

### Using the `Result` type

```python
from kntgraph.core.result import Ok, Err, ToolError

result: Result[int, ToolError] = Ok(42)
if result.is_ok():
    print(result.unwrap())
else:
    print(result.err_value())
```

## Environment variables

The canonical schema is
[`Settings`](src/kntgraph/infra/config/__init__.py).
Highlights:

| Env var                    | Default                        | Purpose                          |
| -------------------------- | ------------------------------ | -------------------------------- |
| `FMH_REDIS_URL`            | `redis://localhost:6379`       | EventLog + checkpoint backing    |
| `FMH_FALKORDB_HOST`        | `localhost`                    | Graph DB host                    |
| `FMH_FALKORDB_PORT`        | `16379`                        | Graph DB port                    |
| `FMH_FALKORDB_PASSWORD`    | (empty)                        | Graph DB password (prod-required) |
| `FMH_STREAM_MAXLEN`        | `100_000`                      | Per-tenant Redis Stream MAXLEN   |
| `FMH_GLOBAL_STREAM_MAXLEN` | `1_000_000`                    | Global Redis Stream MAXLEN       |
| `FMH_TICK_INTERVAL`        | `1.0`                          | ReactiveDispatcher poll interval (s) |
| `FMH_LLM_DEFAULT_MODEL`    | (settings-dependent)           | LLM model (provider/model format) |
| `FMH_LLM_TIMEOUT`          | `30.0`                         | LLM call timeout (s)              |
| `FMH_LLM_MAX_COST_USD_PER_REQUEST` | `1.0`                  | Per-request cost cap (USD)       |
| `FMH_CIRCUIT_BREAKER_THRESHOLD` | `5`                          | Failures before opening breaker  |
| `FMH_RETRY_MAX_ATTEMPTS`   | `3`                            | Retry attempts on transient failure |
| `FMH_ENV`                  | `dev`                          | Set to `prod` to enable stricter validation |

## Versioning

kntgraph follows [Semantic Versioning](https://semver.org/).
Breaking changes are documented in the
[CHANGELOG](CHANGELOG.md) (TODO when the first
release ships).

## See also

- [README](README.md) — project overview.
- [Getting Started](GETTING_STARTED.md) — mental
  model and first agent.
- [Quick Start](docs/quickstart.md) — install and
  run.
- [Architecture](docs/architecture.md) — the
  three pillars.
- [ADRs](ADRs/) — the full list of architecture
  decisions.
