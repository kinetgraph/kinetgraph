<!--
SPDX-FileCopyrightText: 2026 kinetgraph
SPDX-License-Identifier: Apache-2.0
-->

# Zero Token Architecture (ZTA)

kntgraph supports the [Zero Token Architecture](https://zerotokenarchitecture.com/)
patterns out of the box. The shape is: every chat path
on top of the framework is a **`WorldSystem`** — a pure
function `__call__(world) -> list[Event]` — that can
either short-circuit an event with a deterministic
response (software) or emit a tool request that an
LLM-backed worker fulfils (AI).

This document maps the four ZTA principles to the
kntgraph components that implement them, and points at
the canonical examples.

## 1. What ZTA is

ZTA is a set of architectural patterns for building
chat / agent systems where **stable logic is software**
and **uncertain logic is delegated to AI**. The four
principles (from [zerotokenarchitecture.com](https://zerotokenarchitecture.com/)):

  1. **Schema-first contracts** — every state
     transition is a typed schema (request + response).
  2. **Deterministic software handlers** — known cases
     are handled by pure code, no LLM in the loop.
  3. **Caching / reuse of previously solved cases** —
     when a known question surfaces again, reuse the
     answer instead of re-running the model.
  4. **Hybrid: stable → software, uncertain → AI** —
     the system stacks deterministic handlers BEFORE
     the LLM call so the model only sees what code
     could not handle.

## 2. Where kntgraph already satisfies ZTA

| ZTA principle                                    | kntgraph component                                                          |
| ------------------------------------------------ | --------------------------------------------------------------------------- |
| 1. Schema-first contracts                        | `Event` (event-sourced, typed data), `ChatReply` / `Plan` / `Summary` (Pydantic) |
| 2. Deterministic software handlers               | `RuleBasedChatSystem`, `SessionRecorderRoleBridge`, `ContinuityManager`     |
| 3. Caching / reuse of previously solved cases     | `SolutionLookupSystem` + `InMemorySolutionStore` / `RedisSolutionStore`, `ToolCallDeduplicator`, `CachingLLMTransport` (legacy) |
| 4. Hybrid: stable → software, uncertain → AI     | The `ReactiveDispatcher` runs systems in order; `RuleBasedChatSystem` short-circuits before `ChatRoleSystem` |

The framework was already "ZTA-shaped" in v0.8.0 —
event sourcing gives the schema-first contracts, the
dispatcher gives the system stack, and the solution
tier (ADR-010) gives the cache. v0.10.0 adds the
two missing pieces that the ZTA whitepaper
emphasises (ADR-049):

  - `RuleBasedChatSystem` (item 2 — explicit
    deterministic handler stackable before the LLM).
  - `SolutionLookupSystem` (item 3 — read-side cache
    with explicit `solution.matched` events).

## 3. The hybrid dispatcher (principle 4)

The canonical expression of ZTA principle 4 is the
**system stack** in the `ReactiveDispatcher`:

```python
from kntgraph.agents.role_systems import (
    ChatRoleSystem, RuleBasedChatSystem,
)
from kntgraph.agents.memory.solution_lookup import (
    InMemorySolutionStore, SolutionLookupSystem,
)
from kntgraph.runner.reactive import ReactiveDispatcher

dispatcher = ReactiveDispatcher(
    log=log,
    systems=[
        # T1: deterministic rule table (zero tokens).
        RuleBasedChatSystem(rules=[...]),
        # T2: read-side cache (zero tokens).
        SolutionLookupSystem(store=InMemorySolutionStore()),
        # T3: LLM fallback (variable tokens).
        ChatRoleSystem(persona="..."),
    ],
)
```

A `user.intent` event lands:

  - **Rule hits** → `chat.reply.generated` emitted by
    the rule system; T2 and T3 are no-ops (the
    `request_event_id` was consumed).
  - **Rule misses, cache hit** → `solution.matched`
    emitted by the lookup system.
  - **Both miss** → `ChatRoleSystem` emits
    `tool.chat_llm.requested`; the worker runs the LLM.

The wire format of `chat.reply.generated` is identical
across all three paths (`output` is a `ChatReply.model_dump()`
in every case), so downstream consumers
(`SessionRecorderRoleBridge`, the projection, the
session recorder) cannot tell whether the reply came
from rules, cache, or LLM.

## 4. The rule table (principle 2)

`RuleBasedChatSystem` is a `WorldSystem` that
short-circuits `user.intent` events with deterministic
replies. A rule is a 5-tuple:

```python
from kntgraph.agents.role_systems import ChatRule, RuleBasedChatSystem

rules = [
    ChatRule(
        tenant_id="*",                  # "*" = any tenant
        persona_pattern="*",            # fnmatch glob
        message_pattern="hours",        # substring (case insensitive)
        response="Mon-Fri, 9-18 UTC.",
        priority=10,
    ),
    ChatRule(
        tenant_id="tenant-A",
        persona_pattern="support-*",
        message_pattern="refund",
        response="Please contact billing@tenant-A.example.",
        priority=20,
    ),
]

system = RuleBasedChatSystem(rules=rules)
```

Rules can also be loaded from a YAML file:

```python
system = RuleBasedChatSystem()
system.register_from_yaml("path/to/zta_rules.yaml")
```

The shipped YAML schema is at
[`examples/_data/zta_rules.yaml`](../examples/_data/zta_rules.yaml).
Unknown keys are ignored (forward-compat).

### When to use it

  - **FAQ replies** that never change.
  - **Compliance boundaries** where the answer MUST be
    a specific string (legal disclaimers, regulatory
    text, brand-mandated wording).
  - **Latency-sensitive paths** — rules short-circuit
    before the LLM worker is even invoked (saves a
    `WorkerManager` round-trip).

### When NOT to use it

  - **Open-ended questions** — the rule table does
    not have a notion of "embedding similarity"; use
    the solution lookup system instead.
  - **Multi-turn** — the rule system emits a reply per
    request, but does not consult session history. If
    the answer depends on prior turns, register the
    rule system AFTER a context-aware system.

## 5. The solution lookup system (principle 3)

`SolutionLookupSystem` reads a previously extracted
answer from a store and synthesises a
`tool.<name>.completed` event when a known
`(tool_name, params_fingerprint)` pair surfaces again.
The store is typically fed by `SolutionExtractor`
(ADR-010) on the write side.

The store protocol (`SolutionStoreLike`) is pluggable;
ship your own adapter for Redis / Postgres / FalkorDB
/ etc. The framework ships two:

  - `InMemorySolutionStore` (tests + the
    `09b_solution_lookup_zta.py` example).
  - `RedisSolutionStore` (production, ships in
    v0.10.0). One Redis Hash per tool
    (`knt:solution:<tool_name>`); field =
    `params_fingerprint`; value = JSON
    `CachedSolution`. Built-in TTL knob
    (`Settings.solution_ttl_seconds`).

### In-memory example

```python
from kntgraph.agents.memory.solution_lookup import (
    InMemorySolutionStore, SolutionLookupSystem,
)

store = InMemorySolutionStore()
store.add(
    CachedSolution(
        tool_name="knowledge_lookup",
        params_fingerprint="...",
        confidence=5,
        result={"answer": "Click Settings → Export. The file is sent by email."},
    )
)
system = SolutionLookupSystem(
    solution_store=store,
    allowlist=frozenset({"knowledge_lookup"}),
    min_confidence=3,
)
```

### Redis-backed example

```python
import redis.asyncio as aioredis
from kntgraph.infra.redis import create_solution_storage

client = aioredis.from_url("redis://:redispassword@localhost:6379")
store = create_solution_storage(client=client)  # Settings-driven TTL
store = RedisSolutionStore(client=client, ttl_seconds=3600)

# Populate (write side: the Solution promoter's job).
await store.put(CachedSolution(...))

# Read side: the lookup system.
system = SolutionLookupSystem(solution_store=store, ...)
```

The `09c_solution_lookup_zta_redis.py` example is
the end-to-end reference.

### Lookup stats

The system exposes a `stats` attribute (`hits`,
`misses`, `bypass_low_confidence`,
`bypass_not_in_allowlist`) for observability:

```python
print(system.stats.cache_hit, system.stats.cache_miss)
```

### Fail-open semantics

Both stores degrade to a miss on a Redis-side
failure (`find_match` returns `None`); the LLM
fallback takes over. The wire format is identical
between stores, so the lookup system is store-agnostic.

## 6. Example

End-to-end walkthroughs:

  - [`examples/09b_solution_lookup_zta.py`](../examples/09b_solution_lookup_zta.py)
    — the in-memory reference. Drives a few
    `user.intent` and `tool.<name>.requested` events
    through the dispatcher and prints the resulting
    `chat.reply.generated` and `tool.<name>.completed`
    events so the wire format is visible without an
    LLM in the loop.
  - [`examples/09c_solution_lookup_zta_redis.py`](../examples/09c_solution_lookup_zta_redis.py)
    — the Redis-backed variant. Seeds the
    `RedisSolutionStore` with two Solutions and walks
    the same dispatcher path; surfaces both the
    `tool.<name>.completed` events AND the operator-
    side cache audit (`iter_keys` / `read_all`).

Run them with:

```bash
KNT_REDIS_FAKE=1 uv run python examples/09b_solution_lookup_zta.py
uv run python examples/09c_solution_lookup_zta_redis.py
```

## 7. When to add another ZTA component

The whitepaper calls out additional patterns
(memory-write gating, fallback hierarchies,
explanation traces). kntgraph's coverage of those:

  - **Memory-write gating** — `SolutionExtractor`'s
    PII gate (ADR-010, `KNT_PII_LEVEL`).
  - **Fallback hierarchies** — the dispatcher's
    system list is the fallback chain; reorder to
    change the policy.
  - **Explanation traces** — every event has
    `causation_id` + `correlation` (ADR-037).

If a new pattern is needed, write an ADR (the
template lives at `ADRs/ADR-000-template.md`) and
follow the same recipe: ship the new system as a
`WorldSystem` so it composes with the dispatcher.

## See also

  - [ADR-049](../ADRs/ADR-049-Zero-Token-Architecture.md) —
    the decision record that landed these components.
  - [Architecture](architecture.md) — the dispatcher +
    systems model.
  - [Event Sourcing](event_sourcing.md) — how the
    `EventLog` and `World.fold` underpin principle 1.
  - [Solution tier (ADR-010)](consolidation.md) —
    the write-side counterpart to the lookup system.
