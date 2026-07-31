<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-049: Zero Token Architecture support — read-side Solution lookup + shipped deterministic role system

**Status:** Proposed
**Date:** July 30, 2026
**Version:** 0.1.0
**Authors:** kntgraph architecture team
**Related to:** [ADR-008](./ADR-008-Caching-Transport.md) (the original `CachingLLMTransport` removed in v0.9.0), [ADR-010](./ADR-010-Memory-Business-Tier.md) (Solution tier — write-side), [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md) (pure `WorldSystem`), [ADR-036](./ADR-036-Tool-Worker-Pattern.md) (`@tool_worker`)

> **Event type form (canonical).** All tool events in this
> ADR follow the canonical ADR-036 form
> `tool.<name>.requested` / `tool.<name>.completed` /
> `tool.<name>.failed`, where `<name>` is the registered
> tool name. The legacy bare form is not used.

## 1. Context

### 1.1 Zero Token Architecture (ZTA)

[ZTA](https://zerotokenarchitecture.com) (Kelsey Hightower, PlatformCon 2026) is a design philosophy:

> *Use AI for thinking. Use software for repetition.*

The four principles:

  1. **Infer once, export the logic** — let AI discover a
     pattern; convert repeated solutions into normal
     software (functions, rules, templates, configurations).
  2. **Treat AI as a consultant, not a permanent
     contractor** — AI solves the problem once; the
     solution then runs as code.
  3. **Run the solution without inference** — exported
     solutions are cheap, fast, predictable, testable,
     and debuggable.
  4. **Hybrid: stable → software, uncertain → AI** —
     stable paths become code; variable paths stay on
     AI.

Anti-patterns ZTA combats: agentic loops in repetitive
tasks; LLM in critical request paths; AI hiding bad
infrastructure.

### 1.2 Where kntgraph already supports ZTA

The framework ships with the **mechanical substrate**
ZTA needs:

  - **Pure ECS** (`World = fold(events)`) — replay
    produces the same world without inference
    ([ADR-018](./ADR-018-WorldIncremental-WorldSystem.md)).
  - **Event Sourcing** with dedup (Redis Streams
    `dedup_keys`) — `tool.<name>.completed` events live
    in the log forever; replays do not re-call tools.
  - **Idempotency** (`idempotency_key` on every
    `Tool`/`@tool_worker` invocation; `event_id` is the
    key) — duplicate dispatches are deduplicated
    upstream of the worker.
  - **`Tool` Protocol** (generic, no LLM required) —
    `LiteLLMToolWorker` is *one* `@tool_worker` among
    many; tools like `WeatherTool`, `OpenMeteoApi`,
    `PiiRedactionTool` (regex level), and the shipped
    `EchoTool` are deterministic and run with the same
    infra as the LLM worker.
  - **Solution Promotion** ([ADR-010](./ADR-010-Memory-Business-Tier.md))
    — `SolutionExtractorSystem` (pure) finds repeated
    tool calls; `SolutionProjector` writes them to
    FalkorDB; `SolutionPromoterSystem` bumps the
    `confidence` cross-agent.
  - **`WorkerManager`** (`ProcessPoolExecutor`) runs
    deterministic and LLM tools with the same
    resilience infra (retry, circuit breaker, bulkhead,
    DLQ).

### 1.3 The two gaps this ADR closes

**Gap 1 — Read-side of Solutions.** The Solution tier
is **write-only by default**: `SolutionPromoter` writes
to FalkorDB, but no shipped `WorldSystem` consults
Solutions *before* emitting `tool.<name>.requested`.
The "stable → software" loop needs an explicit
read-side to be operational in a shipped example. The
application is responsible for it today.

**Gap 2 — Role systems always emit LLM requests.** The
four shipped role systems (`ChatRoleSystem`,
`PlannerRoleSystem`, `SummarizerRoleSystem`,
`PersonalizedRoleSystem`) all emit
`tool.chat_llm.requested` as their only output path.
The shipped `MockChatLlmTool` / `MockChatLlmWorker`
references in `examples/05b` / `05c` are commented out.
Operators who want ZTA write their own system.

**Removed in v0.9.0 (related, not addressed here):**
the `CachingLLMTransport` decorator from
[ADR-008](./ADR-008-Caching-Transport.md) was removed
as part of the `LiteLLMTool` cleanup. Two references
to the dead module remain:

  - `src/kntgraph/tools/llm_transport.py:172` (docstring
    referencing `agents.tools.cache.CachingLLMTransport`)
  - `tests/unit/test_llm_client_deleted.py:53-54`
    (test docstring referencing the same)

These are out of scope for this ADR; cleaning them up
is a 5-minute follow-up.

## 2. Decision

This ADR delivers **two framework primitives** that
turn the write-side Solution tier (ADR-010) into a
closed ZTA loop, plus **one shipped example** that
demonstrates the loop end-to-end.

### 2.1 `SolutionLookupSystem` — the read-side

A new pure `WorldSystem` in
`src/kntgraph/agents/memory/solutions/_lookup.py`:

  - **Trigger**: a new `tool.<name>.requested` event lands
    on the view (component key `"tool_requests"`).
  - **Action**: consult the Solution tier via the
    existing `find_solutions_by_problem(...)` graph query
    (FalkorDB). The query is keyed on the request's
    `(tool_name, params_fingerprint)` — both available
    on the `ToolCallRequest` dataclass already.
  - **Decision**:
      - If a `Solution` exists with `confidence ≥
        min_confidence` AND `latency_ms` budget allows
        AND the `tool_name` is in the per-tool `allowlist`
        (configuration, not hard-coded), emit
        `tool.<name>.completed` with the cached payload
        from the Solution node. **No LLM call.**
      - Otherwise, do nothing (let the default LLM path
        run via `LiteLLMToolWorker`).
  - **Metrics**: emit structlog events
    `solution.cache_hit` / `solution.cache_miss` /
    `solution.cache_bypass_low_confidence` /
    `solution.cache_bypass_not_in_allowlist` so the
    operator can observe the ZTA score in production.

```python
class SolutionLookupSystem(ToolAwareSystem):
    """Read-side of the Solution tier (ADR-010 / ADR-049).

    On every new ``tool.<name>.requested`` event, query
    the FalkorDB Solutions graph for a matching
    (tool_name, params_fingerprint) pair. If a Solution
    exists with confidence above ``min_confidence`` and
    the tool is in ``allowlist``, emit a synthetic
    ``tool.<name>.completed`` event with the cached
    payload — bypassing the LLM.

    Pure: the only I/O is the FalkorDB query (via the
    injected `solution_store` Protocol). The system
    does not write to Redis or the graph.

    Implements ZTA "infer once, export the logic" —
    a tool call that has been used cross-agent with
    high confidence becomes a normal software path.
    """

    def __init__(
        self,
        *,
        solution_store: SolutionStoreLike,
        min_confidence: int = 3,
        allowlist: frozenset[str] | None = None,
    ) -> None: ...

    def __call__(self, world: World) -> list[Event]: ...
```

#### 2.1.1 Configuration

`min_confidence` (default `3`): the minimum number of
cross-agent uses required for auto-application. Below
this, the operator wants human review (per ADR-010 §3.2
`review_threshold` = 1; ADR-049 raises the default to 3
because ZTA-auto-apply has a higher bar than
auto-promote).

`allowlist` (default `None`): the per-tool allowlist. If
`None`, no tool is auto-applied (operator must
explicitly opt-in). This is the safe default: ZTA is
opt-in per tool, not opt-out.

#### 2.1.2 `SolutionStoreLike` Protocol

The system depends on a Protocol, not the concrete
FalkorDB adapter, for testability:

```python
class SolutionStoreLike(Protocol):
    """Subset of the Solution tier used by
    SolutionLookupSystem.

    Same Protocol pattern as ``APIKeyStorage`` in
    ``infra/redis/_auth`` (ADR-019). Concrete
    implementations: ``FalkorDBSolutionStore``
    (production), ``InMemorySolutionStore`` (tests).
    """

    async def find_match(
        self,
        *,
        tool_name: str,
        params_fingerprint: str,
        min_confidence: int,
    ) -> Solution | None: ...
```

The existing `SolutionProjector.find_solutions_by_problem`
already implements the graph query; we expose a thin
`find_match` method on it that wraps the Cypher with
the right parameters. The `InMemorySolutionStore` is a
test double backed by a `dict`.

#### 2.1.3 Concurrency

The system emits the `tool.<name>.completed` event via
the standard reactive loop. There is a subtle
concurrency concern: the `LiteLLMToolWorker` may also
start processing the same request (because the original
`tool.<name>.requested` event is in the log and the
worker is dispatched on it). The framework's existing
**idempotency on `event_id`** is the mechanism that
prevents double-execution: the synthetic `completed`
event has the **same `causation_id`** (= the
`request_event_id`) as the LLM's eventual `completed`;
the second write to the EventLog is deduplicated via
`dedup_keys`. The LLM's `completed` either lands first
(solution wins via the cache lookup ordering; the LLM's
result is *also* written but dedup-skipped on a second
attempt — wait, actually the dedup key is
`event_id`-based, not `causation_id`-based).

**Resolution**: the synthetic `completed` event uses a
**deterministic `event_id`** derived from the request
`event_id` (e.g. `f"{request_event_id}-solution"`).
The LLM's `completed` event uses a different `event_id`
(the natural UUID the worker generates). Both events
end up in the log; consumers reading by
`causation_id` will see **two completions**. This is
acceptable because:

  - The downstream `SolutionExtractorSystem` (already
    shipped) is idempotent on `(tool_name, params)` —
    double-counts are not an issue at the promotion
    level.
  - Operators can suppress the LLM path entirely by
    excluding the tool from the `WorkerManager`
    `tool_whitelist` for the agent in question.

**Decision**: do NOT couple the system to the
WorkerManager dispatch; both paths run independently.
The ZTA path is an **optimization**, not a lock.

### 2.2 `RuleBasedChatSystem` — the no-LLM chat path

A new ECS-shaped system in
`src/kntgraph/agents/role_systems/_rule_based.py`:

```python
class RuleBasedChatSystem(_BaseRoleSystem):
    """No-LLM chat path: deterministic responses from
    a per-tenant rule table (ZTA "stable → software").

    Falls back to the canonical LLM path (emits
    ``tool.chat_llm.requested`` like ``ChatRoleSystem``)
    when no rule matches. Operators can register rules
    via ``register_rule(tenant_id, persona_pattern,
    message_pattern, response)``.

    The rule table is in-memory + Redis-cached (per
    tenant). In the canonical use case, the rules are
    populated by a batch job that compiles prior LLM
    outputs (ZTA "infer once, export the logic").
    """

    REQUEST_EVENT_TYPE = EVENT_TYPE_USER_INTENT
    GENERATED_EVENT_TYPE = EVENT_TYPE_CHAT_REPLY_GENERATED
    OUTPUT_MODEL = ChatReply
```

The system reads `view.components[REQUEST_EVENT_TYPE]`,
looks up the rule by `(tenant_id, persona, message)`
tuple, and emits `chat.reply.generated` directly without
the `tool.chat_llm.requested` step.

When no rule matches, the system does **nothing** — the
canonical `ChatRoleSystem` (registered after
`RuleBasedChatSystem` in the dispatcher list) handles
the fallback. This composability is the **canonical
expression of ZTA principle 4 (hybrid)**.

#### 2.2.1 Rule table API

```python
@dataclass(frozen=True)
class ChatRule:
    tenant_id: str
    persona_pattern: str  # glob-style, e.g. "support-*"
    message_pattern: str  # substring match, e.g. "refund"
    response: str
    priority: int = 0  # higher wins on tie

class RuleBasedChatSystem(_BaseRoleSystem):
    def register_rule(self, rule: ChatRule) -> None: ...
    def unregister_rule(self, rule: ChatRule) -> None: ...
    def rules_for_tenant(self, tenant_id: str) -> list[ChatRule]: ...
```

In production, rules are typically loaded at boot from
a YAML/JSON file or a Redis hash; we ship a tiny
`register_from_yaml(path)` helper for ergonomics.

### 2.3 Example `09b_solution_lookup_zta.py`

A new runnable example that wires the read-side
end-to-end:

```
example/09b_solution_lookup_zta/
├── README.md              # explains the ZTA loop
├── bootstrap.py           # spins up EventLog + WorkerManager
├── register_solutions.py  # pre-populates 5 Solutions
├── system.py              # SolutionLookupSystem + ChatRoleSystem
└── run.py                 # 5 turns; first 3 hit Solutions (no LLM)
                           # last 2 miss (LLM called)
```

The example requires no LLM API key for the first 3
turns — the operator can see the cache hit / cache miss
metrics in the logs.

### 2.4 `docs/zta.md`

A short doc mapping each ZTA principle to the kntgraph
components that satisfy it. Honest about the gap
(`CachingLLMTransport` removed in v0.9.0; the
`SolutionLookupSystem` from this ADR partially fills it).

## 3. Consequences

### 3.1 Pros

  - The Solution tier becomes a **closed loop**:
    write-side promotes; read-side applies.
    Applications get a shipped example.
  - The role systems get a **deterministic alternative**
    (ZTA principle 4 — hybrid). Operators can compose
    rule-based + LLM-based chat in the same dispatcher.
  - The framework's positioning as "ZTA-friendly"
    becomes **defensible** rather than aspirational.
  - Metrics (`solution.cache_hit` / `cache_miss`) give
    operators a way to observe the ZTA score in
    production.

### 3.2 Cons

  - Two new shipped systems add surface area. Mitigated
    by the Protocol-based design (the underlying
    `SolutionStoreLike` can be implemented in-memory for
    tests).
  - `SolutionLookupSystem` reads FalkorDB on every new
    tool request. Operators with high-volume agents
    must size the connection pool / cache the graph.
    Mitigated by the `allowlist` (only tools in the
    allowlist are looked up).
  - `RuleBasedChatSystem` is a *new pattern* for operators.
    Mitigated by the explicit "fallback to LLM" design —
    operators don't need to migrate; they layer.

### 3.3 Compatibility

  - **No breaking changes.** All additions are
    opt-in. `ReactiveDispatcher` with no
    `SolutionLookupSystem` behaves exactly as before.
  - `LiteLLMToolWorker` is untouched.
  - Existing `ChatRoleSystem` etc. unchanged.

## 4. Alternatives considered

### 4.1 Reintroduce `CachingLLMTransport` instead

We considered restoring the v0.3-era caching
transport ([ADR-008](./ADR-008-Caching-Transport.md))
with a `(idempotency_key, model, response_format)` key.
This would address the "infer once" principle more
directly for the LLM call layer.

**Rejected** for two reasons:

  1. The cache is **transport-only**; it does not
     capture *business context* (the
     `solution_problem_fingerprint`). The Solution tier
     (ADR-010) does that, but the transport-level cache
     is unaware of it. The two caches would coexist and
     be hard to reason about.
  2. The transport-level cache has a narrower key
     surface; it can't capture the cross-agent
     `confidence` metric or the
     `params_fingerprint` / `tool_name` composite
     that the Solution tier uses. The Solution tier is
     semantically richer.

This ADR keeps the door open for ADR-008 restoration
as a **follow-up** (a third layer of caching), but does
not bundle it.

### 4.2 Bake Solution lookup into `ChatRoleSystem`

We considered extending `ChatRoleSystem` with a
`lookup_first=True` flag. The lookup logic would
short-circuit the LLM emit when a Solution matched.

**Rejected**: role systems are pure and side-effect free
(ADR-018). Solution lookup is I/O (FalkorDB query).
Mixing concerns makes `ChatRoleSystem` impure and harder
to test. The composable `SolutionLookupSystem` is the
correct separation: it's a *separate* system registered
*before* `ChatRoleSystem` in the dispatcher list.

### 4.3 Build a "compile solution into code" exporter

We considered an export step that converts FalkorDB
Solutions into Python source code (the Hightower-style
"export the logic"). This would be the purest form of
ZTA.

**Rejected** for v0.11.0 scope: it would require a code
generator, a deployment story for the generated code, and
a fallback for when the generated code fails at runtime
(ZTA §6). The Solutions-as-graph approach (ADR-010 + this
ADR) is the **minimum viable ZTA** and is enough to
demonstrate the principle.

## 5. Acceptance checklist

  - [ ] `SolutionLookupSystem` shipped with `find_match`
        Protocol + `InMemorySolutionStore` + FalkorDB
        adapter.
  - [ ] `RuleBasedChatSystem` shipped with
        `register_rule` / `register_from_yaml`.
  - [ ] Example `09b_solution_lookup_zta` demonstrates the
        end-to-end ZTA loop with cache hit / miss metrics.
  - [ ] `docs/zta.md` documents the principle-to-component
        mapping honestly (incl. the
        `CachingLLMTransport` removal gap).
  - [ ] Test coverage:
        - `tests/unit/agents/memory/solutions/test_lookup.py`
          (cache hit, miss, low confidence, not in
          allowlist, concurrency race)
        - `tests/unit/agents/role_systems/test_rule_based.py`
          (rule match, no match → fallback, priority)
        - `tests/integration/examples/test_09b_solution_lookup_zta.py`
  - [ ] README updated: ZTA badge, link to
        `docs/zta.md`.
  - [ ] CI green: 9/9 gates; pyright 0 errors.

## 6. Follow-ups (out of scope)

  - **ADR-008 revival** (transport-level cache) — keep
    the door open, do not bundle in this ADR.
  - **`compile solution into code`** exporter — separate
    ADR when there's a concrete use case (e.g. an
    operator wants to deploy an agent as a static binary
    for offline use).
  - **Metrics endpoint** (`cache_hit_rate`) for the
    visibility dashboard (ADR-048).
  - **FalkorDB-backed `SolutionStoreLike`** —
    production-grade Solution store (graph query for
    similarity + metadata). The shipped
    `RedisSolutionStore` covers the canonical
    `(tool_name, params_fingerprint)` lookup; the
    FalkorDB adapter would add cross-tool similarity
    and the documented "cross-agent use count" that
    drives `confidence`. **Status (v0.10.0)**:
    the Redis adapter ships (`RedisSolutionStore` in
    `src/kntgraph/infra/redis/_memory/_solution.py`,
    factory `create_solution_storage`); the FalkorDB
    adapter is still pending and unblocks when the
    first operator asks for cross-tool similarity.

## 7. References

  - [Zero Token Architecture](https://zerotokenarchitecture.com/)
    (Kelsey Hightower, PlatformCon 2026)
  - [ADR-008](./ADR-008-Caching-Transport.md) — the
    original caching transport (removed in v0.9.0)
  - [ADR-010](./ADR-010-Memory-Business-Tier.md) —
    Solution tier (write-side)
  - [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md) —
    pure `WorldSystem`
  - [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md) —
    `Protocol + Adapter` pattern (`SolutionStoreLike`
    follows the same template as `APIKeyStorage`)
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) —
    `@tool_worker` (the generic tool surface)
  - `src/kntgraph/agents/memory/solution_extractor.py`
    — the existing `SolutionExtractorSystem` (write-side
    pattern this ADR mirrors)
  - `src/kntgraph/agents/knowledge/solution_projector.py`
    — the existing graph adapter (extended with
    `find_match`)