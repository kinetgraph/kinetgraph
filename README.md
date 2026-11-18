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

An agent is **declared as data**: one frozen dataclass names its persona, its
capability policy, its state machine and its workflows. The framework derives
the installable runtime from that declaration — no hand-wiring, no duplicated
policy, and a dispatcher that knows nothing about agents (ADR-088).

## When to use

- You need agents whose decisions are **auditable and replayable** from first principles.
- You want to **model an agent as data** — persona, tool policy, state machine and workflows declared once, with the runtime derived from the declaration.
- You want to run **LLM calls in process-pool workers** so the event loop never blocks.
- You need **idempotent tool execution** across retries and dispatcher restarts.
- You want a **single Redis Stream** as the source of truth, with no separate state DB.
- You are building **multi-tenant** systems where each agent has its own isolated event log.
- You want the **scaffolder in one command** — `curl … | sh` — with no Python environment to set up first.

## Install

### The `knt` CLI — one command

```bash
curl -LsSf https://raw.githubusercontent.com/kinetgraph/kinetgraph/main/scripts/install.sh | sh
```

A POSIX shell installer, in the style of `uv`'s own. It is
**user-local**: it never touches the system Python and never
needs root. With `uv` on the PATH it delegates to
`uv tool install`; without it, it builds its own virtual
environment under `~/.local/share/knt/venv` and uses `pip`.
Re-running it upgrades in place.

The package is pure Python, so there is **one artifact for
every platform** — no OS or CPU detection, no binary download.

```bash
knt --version              # what got installed
knt init project my_platform
```

> Prefer to read what you run? `curl -O` the script and read it
> first — it is a couple of hundred lines of shell that prints
> what it does.
>
> Windows: `pip install "kntgraph[cli]"` and use `knt` from the
> shell. The installer script is POSIX only.

### The library

```bash
uv add kntgraph
```

Python **3.12** or **3.13** — the two runtimes the CI gate covers.
The floor is 3.12; newer interpreters pass the gate too.

Optional extras (install only what you need):

```bash
uv add "kntgraph[cli]"          # knt scaffold CLI
uv add "kntgraph[falkordb]"     # graph projection + Cypher (FalkorDB)
uv add "kntgraph[ollama]"       # local LLM / embeddings
uv add "kntgraph[gliner]"       # GLiNER2 NER — intent routing and PII redaction
uv add "kntgraph[api]"          # HTTP gateway (FastAPI)
uv add "kntgraph[crypto]"       # Ed25519 event signing
uv add "kntgraph[llm]"          # LiteLLM adapter
uv add "kntgraph[metrics]"      # Prometheus metrics sink (ADR-075 Tier 4)
uv add "kntgraph[all-runtime]"  # everything above
```

> To install the unreleased `main` between tagged releases:
> `uv add "kntgraph @ git+https://github.com/kinetgraph/kinetgraph.git"`.
> Tagged releases on PyPI are the canonical, supported path.

## Model an agent

An agent is **declared as data**, not wired by hand. One frozen
dataclass carries everything the agent *is* — its persona, its
capability policy, its state machine and its workflows — and the
framework **derives** the runtime from that declaration
([ADR-088](ADRs/ADR-088-Declarative-Agent-Definition-over-Concordos.md)).

```python
from dataclasses import dataclass

from kntgraph.agents import AgentDefinition, RoleSpec
from kntgraph.concordos.base import Specification, StepContext
from kntgraph.concordos.fsm import (
    BusinessFSMConcordo,
    FSMConfig,
    FSMTransition,
)
from kntgraph.concordos.saga import (
    SagaConfig,
    SagaStepConfig,
    WorkflowSagaConcordo,
)
from kntgraph.core.world import DomainComponent


# 1. The agent's business object — a frozen ECS component.
@dataclass(frozen=True, slots=True)
class OrderDomainComponent(DomainComponent):
    """The order's business state.

    ``status`` drives the state machine; ``amount`` drives the
    business rules.
    """

    status: str = "draft"
    amount: float = 0.0


# 2. A business rule — a pure predicate over the carrier.
@dataclass(frozen=True, slots=True)
class AmountWithinLimit(Specification):
    """True when the order amount is within the accepted range."""

    min_amount: float
    max_amount: float

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        order = ctx.get_component(OrderDomainComponent)
        if order is None:
            return False
        return self.min_amount <= order.amount <= self.max_amount


# 3. The reactive cycle — zero-token governance.
ORDER_FSM = FSMConfig(
    component_type=OrderDomainComponent,
    state_field="status",
    transitions={
        "draft": {"order.submitted": FSMTransition(to="validating")},
        "validating": {
            "order.approved": FSMTransition(
                to="approved",
                guard=AmountWithinLimit(1.0, 100_000.0),
            ),
        },
    },
    terminal=frozenset({"cancelled"}),
)


# 4. The deliberative cycle — tool orchestration with compensation.
ORDER_SAGA = SagaConfig(
    name="OrderFulfilment",
    steps=(
        SagaStepConfig(
            name="reserve",
            tool_name="reserve_inventory",
            compensate_tool="release_inventory",
        ),
        SagaStepConfig(
            name="charge",
            tool_name="charge_payment",
            compensate_tool="refund_payment",
        ),
    ),
)


# 5. The declaration — the only object the developer writes.
ORDER_AGENT = AgentDefinition(
    name="order-fulfilment",
    persona=RoleSpec(
        persona="fulfilment",
        allowed_tools=("reserve_inventory", "charge_payment"),
    ),
    role_event_type="role.assigned",
    fsm=BusinessFSMConcordo(config=ORDER_FSM),
    sagas=(WorkflowSagaConcordo(config=ORDER_SAGA),),
    domain_types=(OrderDomainComponent,),
)
```

### The runtime is derived

Nothing above is registered by hand. The declaration derives the
systems and projections the dispatcher runs:

```python
bundle = ORDER_AGENT.bundle()
# AgentBundle(
#     name="agent:order-fulfilment",
#     systems=(FSMSystem, SagaSystem, SagaTimeoutSystem),
#     projections=(FSMProjection, SagaProjection, RoleProjection),
# )
```

The bundle repeats **no** field of the declaration, so the two can
never disagree. Installing it takes the runtime credential the host
owns — a `Principal` is never stored in a model:

```python
from kntgraph.security.principal import Principal, PrincipalLevel

principal = Principal(
    agent_id="order-fulfilment",
    level=PrincipalLevel.agent,
    tenant_id="acme",
    key_id="k-1",
)

ORDER_AGENT.install(dispatcher, principal=principal)
```

Several agents install through the catalog path, which takes the
derived bundles:

```python
from kntgraph.concordos import ConcordoCatalog

catalog = ConcordoCatalog(*(d.bundle() for d in definitions))
catalog.install_all(dispatcher)
```

### The two cycles

An agent runs two cycles, and they share the same rules.

| Cycle | Concordo | What it does | LLM |
| --- | --- | --- | --- |
| **Reactive** | `BusinessFSMConcordo` | Reacts to domain events and guards transitions. A rejected transition emits `fsm.transition_rejected` — no model is called, no token is spent. | none |
| **Deliberative** | `WorkflowSagaConcordo` | Walks its steps, dispatches tools, and compensates in LIFO order when a step fails. | only through a tool |

Both cycles evaluate their rules against the **same carrier**,
built in exactly one place (`build_step_context`). A rule written
as an FSM guard is reused unchanged as a Saga skip condition: it
reads the agent's domain component by its concrete class, the three
memory tiers (session, continuity, profile) and the dispatcher's
tick clock.

### Tools are capabilities

A tool is one I/O capability, declared with `@tool_worker` and run
in a worker pool, off the event loop:

```python
from kntgraph.core.result import Ok, Result, ToolError
from kntgraph.tools.worker import tool_worker


@tool_worker(name="reserve_inventory")
class ReserveInventory:
    """Reserves stock for an order."""

    async def invoke(
        self, order_id: str, *, idempotency_key: str
    ) -> Result[dict, ToolError]:
        return Ok({"order_id": order_id, "reserved": True})
```

The `idempotency_key` makes the call **at-most-once** across
retries and dispatcher restarts. The tool is registered on the
worker manager, which executes it in a process pool and writes the
completion back to the event log:

```python
worker_manager.register(ReserveInventory)
```

The tool's name appears in exactly one place — the agent's
`RoleSpec.allowed_tools` — which the framework projects into
`RoleComponent.allowed_tools`, the policy gate 2 of the three-gate
model reads.

The `agents` sub-module ships concrete LLM, cache and PII adapters
on top of the framework:

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

### Under the hood

The declaration derives systems; a system observes the `World` and
emits events; the events are folded back into state. Four
primitives carry that:

| Primitive | One-liner |
| --- | --- |
| `Event` | "Something that happened" — append-only, signed, idempotent. The EventLog is the source of truth. |
| `EventLog` | The append-only store of events, backed by a Redis Stream. |
| `World` | A pure fold of the event log into `AgentView`s with typed ECS components. Re-fold from event 0 to reproduce any past state exactly. |
| `WorldSystem` | A pure function `(World) -> list[Event]`. Emits events; never touches I/O. |

Run the whole model end to end — both cycles, the guard, the skip
condition and the derived runtime — in
[`examples/30_declarative_agent_modelling.py`](examples/30_declarative_agent_modelling.py).

## What the framework provides

| Capability | How it works |
| --- | --- |
| **Declarative agent model (ADR-088)** | `AgentDefinition` declares the persona, the capability policy, the FSM and the sagas; the framework derives the installable runtime (`AgentBundle`) and projects `RoleComponent` from the role event stream. The dispatcher never learns the concept of an agent. |
| **Replayable state** | `World` is a pure fold over the EventLog. Re-fold from event 0 to reproduce any past state exactly. |
| **Portable business rules** | A `Specification` is a pure predicate over the carrier. The carrier is built in one place (`build_step_context`), so the same rule serves as an FSM guard and as a Saga skip condition. |
| **At-most-once tools** | `idempotency_key` on every tool call deduplicates side effects across retries and restarts. |
| **Non-blocking LLM** | Workers run in a `ProcessPoolExecutor`; the async event loop is never blocked by an LLM call. |
| **Three-gate authorisation** | Role persona (gate 2) → per-tool ACL in `WorkerManager` (gate 1) → worker-level check (gate 3). |
| **Resilience primitives** | Circuit breaker, retry, bulkhead, timeout, fallback, and a Dead Letter Queue — all composable. |
| **Durable checkpoints** | `ReactiveDispatcher` commits a Redis checkpoint *after* emitted events are durably appended, so a crash replays the same batch on restart. |
| **Domain memory** | Fold domain events into frozen ECS `@dataclass` components attached to the `World` entity (no volatile sliding window required). |
| **Zero-Token Architecture** | `RuleBasedChatSystem` short-circuits deterministic intents; `SolutionLookupSystem` synthesises cached completions before calling the LLM. |
| **Semantic routing** | Opt-in GLiNER2 intent classification and argument extraction in the `agents` sub-module. |
| **Solution tier** | Successful tool calls are promoted to reusable Solution nodes in FalkorDB, with per-tenant allow-list and human-in-the-loop review. |
| **Service-scoped Redis keys (ADR-076)** | `KNT_REDIS_KEY_PREFIX` namespaces every Redis key under a configurable prefix so two services sharing one Redis do not cross-talk. `scripts/migrate_redis_keys.py` adopts the prefix in existing deployments via atomic `SCAN` + `RENAME`. |
| **Prometheus metrics sink (ADR-075 Tier 4)** | Opt-in `MetricsSink` Protocol on the dispatcher; `prometheus-client` adapter ships a `Counter`/`Histogram` surface for tool calls, saga transitions, and DLQ ingestion. |

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

# Build the bounded context
cd my_platform
knt new context sales
knt new component sales.Invoice
knt new tool sales.ProcessPayment

# Declare the agent's two execution cycles
knt concordo add fsm sales.InvoiceFSM --component Invoice
knt concordo new saga sales.InvoiceFlow --steps validate:check,emit:send

# Declare the agent — the runtime is derived from the declaration
knt new agent sales.CheckoutAgent

# The work no event triggers — a sweep on the Runner's timer
knt new sweep sales.CompactionSweep

# Check for framework drift in boilerplate
knt upgrade check
```

The agent declaration and the context's dispatcher are **derived**
from the context's artifacts: the tool allow-list is the context's
tools, the domain types are its components, the two execution
cycles are its concordos, and its sweeps are the cyclic work the
`Runner` runs. The CLI keeps them in sync as you add artifacts —
and never overwrites a file you edited by hand.

One dispatcher hosts every system of the context: the systems are
pure, so they need no isolation from each other. The framework's
second loop — the `Runner`, for the work no event triggers — is
scaffolded explicitly (`knt new sweep`), never inferred.

See the [CLI Guide](docs/cli_guide.md) for a full walkthrough.

## Architecture

```
kntgraph/
├── src/kntgraph/
│   ├── core/        # Pure: ECS, Event, World, System
│   ├── concordos/   # Specification, StepContext, FSM/Saga Concordos
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
│   └── agents/      # Declarative agent model + LLM/PII adapters
│       ├── _definition.py # RoleSpec, AgentDefinition
│       ├── _bundle.py     # AgentBundle, derive_bundle
│       ├── _projection.py # RoleProjection
│       ├── role_systems/  # ChatRoleSystem, PlannerRoleSystem, etc.
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
| `KNT_REDIS_CLUSTER` | `false` (set to `true` for Redis Cluster / Valkey cluster mode; the URL must not select a database) |
| `KNT_FALKORDB_HOST` | `localhost` |
| `KNT_FALKORDB_PORT` | `16379` |
| `KNT_STREAM_MAXLEN` | `100_000` |
| `KNT_TICK_INTERVAL` | `1.0` (seconds) |
| `KNT_REDIS_KEY_PREFIX` | `""` (empty — pre-076 wire format; set to e.g. `acme-billing:` when two services share one Redis) |
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
- [Declarative Agent Modelling](docs/declarative_agent_modelling.md) — the four moves: declare, derive, install, write the rules (ADR-088).
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

[![cc](https://img.shields.io/badge/CC-A%20%282.64%29-brightgreen?style=for-the-badge&logo=radar&logoColor=white)](https://radon.readthedocs.io/)
[![mi](https://img.shields.io/badge/MI-314_A_1_B_0_C-brightgreen?style=for-the-badge&logo=heartbeat&logoColor=white)](https://radon.readthedocs.io/)
[![pyright](https://img.shields.io/badge/pyright-0%20errors-brightgreen?style=for-the-badge&logo=microsoft&logoColor=white)](https://microsoft.github.io/pyright/)
[![pypi](https://img.shields.io/badge/pypi-0.20.0-blue?style=for-the-badge&logo=pypi&logoColor=white)](https://pypi.org/project/kntgraph/)
[![release](https://img.shields.io/badge/release-v0.20.0-blue?style=for-the-badge&logo=github&logoColor=white)](https://github.com/kinetgraph/kntgraph/releases/tag/v0.20.0)

### Tests

[![coverage](https://img.shields.io/badge/coverage-89%25-brightgreen?style=for-the-badge&logo=codecov&logoColor=white)](https://coverage.readthedocs.io/)
[![tests](https://img.shields.io/badge/tests-3349%20passed-brightgreen?style=for-the-badge&logo=pytest&logoColor=white)](https://docs.pytest.org/)

### Security

[![security](https://img.shields.io/badge/security-bandit-brightgreen?style=for-the-badge&logo=shield&logoColor=white)](https://bandit.readthedocs.io/)
[![audit](https://img.shields.io/badge/audit-pip--audit-blueviolet?style=for-the-badge&logo=dependabot&logoColor=white)](https://pypi.org/project/pip-audit/)

</div>

Pyright: 0 errors above the baseline (warnings tracked separately;
see [`DEBT.md`](DEBT.md) §4.2 for the warning budget).

## Project status

| Version | Highlights |
| --- | --- |
| **0.20.0** *(current)* | **The CLI adopts the declarative agent model.** `knt new agent` emits one `AgentDefinition` — persona, capability policy, state machine, workflows, domain types — derived from the bounded context: the tool allow-list is the context's tools, the domain types are its components, the two execution cycles are its Concordos. The context's dispatcher installs the declaration instead of aggregating systems by hand. `knt concordo` is context-scoped, and `knt new sweep` makes the framework's second loop (the `Runner`) explicit instead of inferred from a filename. **A `curl | sh` installer** — `scripts/install.sh` — installs the CLI user-locally, delegating to `uv tool install` when uv is present. **Breaking:** the `knt concordo` target now carries the context, and the generated Concordos moved into it. |
| 0.19.1 | **Fixed:** the CLI works with the `[cli]` extra alone. The `keys` command imported `cryptography` at the top of its module; `cryptography` belongs to the `[crypto]` extra, so the import broke **every** `knt` command with a `ModuleNotFoundError` on a CLI-only install — including the ones that never touch a key. The import is deferred into the command now. |

| 0.19.0 | **ADR-088** — the declarative agent model: `RoleSpec`, `AgentDefinition`, `AgentBundle` and `RoleProjection` in the `agents` vertical. The developer declares an agent once — persona, capability policy, FSM, sagas, domain types — and the framework derives the installable runtime from it. `build_step_context` becomes the single construction path for the carrier every `Specification` evaluates against, so a rule written for an FSM guard is reused unchanged as a Saga skip condition; `StepContext.session` and `StepContext.get_component(MyComponent)` land with it (closes DEBT §2.42). **ADR-089** — the architecture fitness harness: `scripts/design_gate.py` and the `kntgraph.testing` contract verifiers become an executable gate. Zero breaking changes — every new field is optional with a backwards-compatible default. |
| 0.18.1 | **ADR-089** — the architecture fitness harness lands: the standards move from prose into `scripts/design_gate.py` and the `kntgraph.testing` contract verifiers, wired into the CI gate as the `design` step. **ADR-088** — the declarative agent definition is designed (the implementation ships in 0.19.0). |

| 0.17.1 | **ADR-085** — the reaper reclaims a tool on **its own** deadline: `@tool_worker(timeout_seconds=)` declares the budget, `register()` publishes it, and the router writes it into the request, so a tool slower than the global 300 s fallback is no longer reclaimed *while it still runs* (the same request used to execute twice, in parallel). A non-idempotent tool is never re-executed. **ADR-087** — the TTL sweeper's failure event carries a **stable** identity (the `swept_at` timestamp left the hash, not the payload), so one expired request produces exactly one failure event instead of one per tick. **ADR-083** — the tool-queue dedup, with its premise corrected. Zero breaking changes — the new kwargs are optional with backwards-compatible defaults. |
| 0.17.0 | **ADR-084** — one Redis connection pool per process, owned by the caller: `RedisPool.aclose()` now closes the pool it built, and the `create_*` factories require `client=`. **ADR-078** — the `WorkerManager` no longer creates an internal `ProcessPoolExecutor`; the execution strategy is a per-tool decision (`executor_factory=`). **ADR-082** — Redis transport defense-in-depth. **ADR-077** — centralised Redis exception translation. |
| 0.16.0 | **ADR-076** — service-scoped Redis keys via `KNT_REDIS_KEY_PREFIX`. New `infra/redis/_prefix.py` (`namespaced` + `validate_prefix`); every Redis adapter (EventLog, DLQ, memory tiers, solution store) accepts `key_prefix` and composes via `_k()` helper. `SCAN_PATTERN` constant becomes `scan_pattern(prefix)` function. `events/dlq/values.py` re-exports the canonical DLQ keys from `infra/redis/_dlq` (single source of truth). New one-shot `scripts/migrate_redis_keys.py` adopts the prefix in existing deployments via atomic `SCAN` + `RENAME` (preserves TTL). **ADR-075 Tier 4** — opt-in `MetricsSink` Protocol + `PrometheusMetricsSink` (`prometheus-client` extra). **ADR-074** — per-system cursor on `AgentView` so FSM/Saga systems no-op on re-folds they already processed. **ADR-069** — Concordos: `Specification` ABC + `Composable` (`and_`/`or_`/`not_`) + `BusinessFSMConcordo` + `WorkflowSagaConcordo` + bundle format loader (`load_bundle_yaml/json/dict`). **ADR-070** — `max_in_flight` back-pressure knob on `@tool_worker`. Worker split, `Runner._fold_incremental` (O(δ) fold), `tests/stress/` flake excluded under `KNT_REDIS_FAKE=1`. Zero breaking changes — all new fields/parameters are optional with backwards-compatible defaults. |
| 0.15.3 | ADR-037 correlation propagation (audit-trail integrity): dispatcher now threads the trigger event's correlation into the per-tick scope via `correlation_middleware.continue_from(entry)`. New `BaseShortTermMemory.invalidate_cache(*key_parts)` + `ShortMemoryStorage.delete_fold_cursor(key)` for cold-rebuild management. |
| 0.15.2 | Default TTL on event_id idempotency index (`IDEMPOTENCY_TTL_DEFAULT = 86_400`) — closes a window where a single `XADD` race could block all later retries on the same `event_id` indefinitely. |
| 0.15.1 | ReactiveDispatcher push-first spin-loop fix (ADR-068 §3.2): `_wake_once()` now sleeps `_interval` instead of looping when `subscribe()` is not yet ready, bounding unseeded/idle Redis queries to the `poll_interval` cadence. |
| 0.15.0 | ADR-068 phases 0–1–4: idle-traffic mitigation (P0/P8: KNT_-env knobs, `count>1` `XREADGROUP`, checkpoint zlib compression), `EventLog.subscribe` blocking-XREAD primitive (P1), parallel-key incremental cache refresh (P4 — `<key>:fold_cursor`, payload stays legacy-compatible). `Runner._fold_incremental` for O(delta) fold. Pyright baseline cleared (5 → 0). `quality_report.py` flake fixed. Zero breaking changes — Protocol/superclass widened, kwargs optional, wire format only adds optional fields. |
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
| 315 (59,788 LOC) | 301 (81,172 LOC, 3,295 tests collected) | 86 | 44 pages |

<!-- STATS END -->
