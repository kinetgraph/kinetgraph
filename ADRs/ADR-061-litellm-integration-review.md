<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-061: LiteLLM integration — review against current framework patterns

- **Status:** Proposed
- **Date:** 2026-08-25
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-007](./ADR-007-LiteLLM-Adapter.md) — original LiteLLM Tool (Protocol path)
  - [ADR-030](./ADR-030-LLMTransport-as-Callable.md) — `LLMTransport` as `Callable[LLMRequest, dict]`
  - [ADR-043](./ADR-043-LiteLLM-Worker-Migration.md) — `LiteLLMToolWorker` (WorkerManager path)
  - [ADR-047](./ADR-047-Tool-Adapter-Pattern.md) — Tool-Adapter pattern; three sub-Workers split
  - [ADR-049](./ADR-049-Zero-Token-Architecture.md) — ZTA and `SolutionLookupSystem`
  - [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) — Domain Memory via ECS Components
  - [ADR-060](./ADR-060-fmh-office-v2-pillars.md) — three-gate ACL model + `RoleComponent`

> **Scope.** This ADR evaluates the **current** LiteLLM
> integration against the framework's current patterns.
> It does not propose a redesign; it documents what
> works, what is debt, and what the candidates for
> follow-up are. The "follow-up candidates" section
> ( §6) is the input for the next-minor scope
> discussion in `ROADMAP.md`.

## 1. Context

The LiteLLM integration has been touched by **three
ADRs** between v0.7 and v0.11:

| ADR | Decision | Year |
|---|---|---|
| 007 | LiteLLM as the unified LLM provider; `LiteLLMTool` (Tool Protocol, sync) | 2026-06 |
| 030 | `LLMTransport` Protocol as `Callable[LLMRequest, dict]`; `LLMRequest` value object | 2026-07 |
| 043 | Migration to `LiteLLMToolWorker` (`@tool_worker(name="chat_llm")`); runs in `WorkerManager` `ProcessPoolExecutor` | 2026-07 |

Each ADR landed cleanly and the integration is **in
production use today** (examples 01, 05b, 05c). The
intent of this ADR is not to "redo" the work, but to
**audit the result against the framework's current
patterns** so that any leftover debt is visible before
the `v1.0` freeze (`ROADMAP.md`).

The audit evaluates six axes:

1. The `LLMTransport` Protocol — does it fit the
   current `tools/` shape?
2. The `llm.py` file size and responsibility split.
3. Legacy shims (`_RateLimitLike`, `_AuthLike`,
   `_StreamDone`, `_StreamTimeout`).
4. The three-gate ACL model (ADR-060 §3.0) applied
   to the LLM call path.
5. Cost budget / rate limit / timeout — where they
   live, who owns them.
6. The cross-process contract (`WorkerManager` +
   `ProcessPoolExecutor`).

## 2. Audit 1 — `LLMTransport` Protocol fit

The `LLMTransport` Protocol lives in
`src/kntgraph/tools/llm_transport.py` (200 LOC). It
declares:

```python
@runtime_checkable
class LLMTransport(Protocol):
    async def __call__(self, request: LLMRequest) -> dict: ...
```

Plus the four value objects: `LLMRequest`,
`LLMResponse`, `LLMUsage`, `LLMChunk`.

**What works (current):**

- The Protocol is `@runtime_checkable`, so factories
  can use `isinstance(obj, LLMTransport)` defensively
  (same pattern as `RedisLike`, per ADR-019).
- The Protocol is structural — any `Callable[LLMRequest, dict]`
  qualifies (per ADR-030). The docstring spells out
  that the transport is "shape-only"; the response
  contract is by convention (LiteLLM-style).
- The value objects are `@dataclass(frozen=True,
  slots=True)` (per AGENTS.md §1 type discipline).

**What is debt:**

1. **The Protocol is "shape-only" but the concrete
   transport returns a LiteLLM dict.** The docstring
   admits this — "LiteLLM-style by convention, but the
   contract is shape-only". The `LLMResponse.raw`
   field carries the raw dict, so callers that want
   the structured shape call the helpers in
   `llm.py`. This is the same shape-discipline gap
   the framework closed for Redis (`RedisLike` returns
   typed values) and FalkorDB (`GraphAdapter.query`
   returns `GraphQueryResult`). The LLM is the only
   I/O boundary that returns a `dict` instead of a
   typed envelope.
2. **`LLMUsage` duplicates fields the `LLMResponse`
   already carries.** `LLMResponse.usage: LLMUsage`
   and `LLMResponse` has `prompt_tokens` /
   `completion_tokens` via `usage`. The
   `LLMResponse` envelope in `LiteLLMToolWorker.invoke`
   (line 716-729) builds a new dict for `usage` from
   the `LLMUsage` — three identical fields expressed
   twice. The transport → Worker boundary should hand
   over `LLMResponse` directly; the Worker envelope
   shape is a separate concern.
3. **`response_format: Optional[dict]` is untyped.** A
   `dict` in `LLMRequest.response_format` is the same
   gap the framework closed for tool input schemas
   (`Tool.input_schema: dict` → typed in ADR-047).
   Today the schema is opaque until the response
   arrives; a typed `ResponseFormat` (Pydantic model)
   would surface the contract at the call site.

## 3. Audit 2 — `llm.py` size and responsibility split

`src/kntgraph/agents/tools/llm.py` is **732 LOC** and
houses:

| Symbol | LOC | Concern |
|---|---|---|
| `_StreamDone` / `_StreamTimeout` (sentinels) | ~30 | Internal: stream termination |
| `_compute_cost_usd(response)` | ~30 | Helper: cost extraction |
| `LiteLLMTransportAdapter` | ~110 | Adapter: `litellm.acompletion` |
| `_RateLimitLike`, `_AuthLike` (legacy aliases) | ~20 | **Shim**: typed exceptions |
| `LLMError`, `LLMRateLimitError`, `LLMAuthError` | ~50 | Typed exceptions |
| `_TerminalToolError` | ~20 | Marker for non-retryable |
| `_safe_dict`, `_parse_message`, `_parse_usage`, `_to_llm_response`, `_convert_to_raw_dict` | ~120 | Helpers: response shape coercion |
| `LiteLLMToolWorker` (the Worker) | ~200 | **The Worker** |
| `LLMConfig` import + worker init | ~30 | Wiring |

**Comparison with the rest of the framework:**

| Other Tool worker | LOC | Pattern |
|---|---|---|
| `LiteLLMToolWorker` | 732 | One file, four concerns |
| `SolutionPipeline` (target per ADR-060 §6.5.2) | ~250 (planned) | One class, four methods |
| `PiiRedactionTool` | 180 | One class |
| `ice_offload_tick` (ADR-057 §4.11.4) | ~70 | One tool, 4 steps inline |
| `GenericToolWorker` (ADR-036) | ~50 | Pure router |

`llm.py` is **4× the size of the next-largest Tool**,
and the split between concerns is **not file-aligned**
— sentinels, helpers, adapter, typed exceptions, and
worker all live in the same module. ADR-047
established the convention that a ToolWorker is one
class per file. ADR-060 §6.5.3 re-applies this to the
role systems (one class per file).

**Recommendation:** split `llm.py` into:

```text
src/kntgraph/agents/tools/llm/
  __init__.py         # canonical re-exports
  adapter.py          # LiteLLMTransportAdapter (~110 LOC)
  worker.py           # LiteLLMToolWorker (~200 LOC)
  exceptions.py       # LLMError, LLMRateLimitError, LLMAuthError (~70 LOC)
  _response.py        # _safe_dict, _parse_message, _parse_usage, _to_llm_response, _convert_to_raw_dict, _compute_cost_usd (~150 LOC)
  _streams.py         # _StreamDone, _StreamTimeout (~30 LOC)
```

This mirrors the `memory/solutions/` structure
(`_values.py`, `_bus.py`, `_extractor.py`,
`_promoter.py`, `_fingerprints.py` — see ADR-060
§6.5.1).

## 4. Audit 3 — Legacy shims

Three categories of shim live in `llm.py`:

### 4.1 `_RateLimitLike` and `_AuthLike` (lines 288-303)

These are **deprecated base classes** kept for
backward compatibility with the test fake. The docstring
explicitly says:

> *"Kept as an alias for the test fake
> (`_FakeRateLimitError`), which subclasses this for
> backwards compatibility. New code should raise/catch
> `LLMRateLimitError` directly."*

The fake lives in `tests/agents/unit/_fake_transport.py`
which subclasses both shims AND the new typed exceptions.
The shims are a one-line `pass` per class; their only
caller is the fake. They cost 16 LOC and one mental
import.

**Recommendation:** remove the shims and update the
fake to subclass `LLMRateLimitError` / `LLMAuthError`
directly. ~1 line of fake change; 16 LOC removed.

### 4.2 `_StreamDone` and `_StreamTimeout` (lines 104-133)

These are singletons used by `LiteLLMTool.astream`
(which is currently **not invoked anywhere** in the
codebase — see §6.5 below). They exist for a path
that has no caller.

**Recommendation:** move to `_streams.py` per §3, OR
delete if `astream` is confirmed unused.

### 4.3 `drop_unsupported_params: bool` (LLMRequest)

`LLMRequest.drop_unsupported_params: bool = True`
defaults to True and is forwarded to
`litellm.drop_params = request.drop_unsupported_params`
in the adapter. The flag **mutates the litellm
global** (`litellm.drop_params = ...`) — this is
**not thread-safe across processes** (the
`WorkerManager` runs each worker in a separate
process, so each worker has its own globals; but a
test that imports the adapter twice mutates the
global twice). ADR-019 closed this for
`litellm.telemetry` (similar globals concern) with a
process-local wrapper.

**Recommendation:** per-call `litellm.drop_params`
override (passing `drop_params=` in `acompletion(...)`
kwargs) instead of mutating the global. ADR-030's
`LLMRequest` already has the slot.

## 5. Audit 4 — Three-gate ACL (ADR-060 §3.0) applied to LLM

The three-gate model applies to **any** tool call:

| Gate | Owner | LLM call today |
|---|---|---|
| 1 — RBAC of request | `ToolACL.check(principal)` | `tools/llm_transport` is **not** registered in any `ToolACL`. The `chat_llm` tool is callable by anyone. |
| 2 — Persona of agent | `target_tool in role.allowed_tools` | `chat_llm` is **not** in any `RoleComponent.allowed_tools` by default. The `ChatRoleSystem` emits `tool.chat_llm.requested` unconditionally. |
| 3 — Handoff ACL | `RoleComponent.handoff_targets` | N/A — handoff is between agents, not for LLM calls |

**Two gaps:**

1. **`chat_llm` is an unauthenticated tool.** A
   `PrincipalLevel=agent` user (per ADR-017) can
   cause the dispatcher to emit a
   `tool.chat_llm.requested` event directly, bypassing
   any role check. Today this is **not exploitable**
   because the only emitter of `tool.chat_llm.requested`
   is the `_BaseRoleSystem` (ADR-039 §3), which gates
   on `SessionComponent`. But the **contract** says
   any `WorldSystem` can emit, and a future system
   could emit one without the persona check.

2. **`ChatRoleSystem` does not declare its emitted
   tool in the persona.** A `RoleComponent` carries
   `allowed_tools`; the `chat_llm` tool name is hard-coded
   in `_BaseRoleSystem.TOOL_NAME = "chat_llm"`. A role
   persona that wants to deny LLM access (e.g. a
   deterministic-only role) has **no way** to express
   that; it would emit `tool.chat_llm.requested`
   anyway.

**Concrete fix:**

- Register `chat_llm` in `default_acl()` with the same
  ACL as `tools.echo` (`{Role.agent, Role.service,
  Role.admin}` per ADR-017 §4) — or per-tenant via
  the `ToolACL` registry.
- Allow `RoleComponent.allowed_tools` to be checked
  on the **emitted** tool, not just the target
  tool. `ChatRoleSystem.__call__` should read the
  role's `allowed_tools`; if `"chat_llm"` is missing,
  emit `intent.validation_failed` instead of
  `tool.chat_llm.requested`.

This is the **same Double-Lock** ADR-039 §3 already
documents for the target tool; the LLM path needs the
same lock for its **own** tool name.

## 6. Audit 5 — Cost budget / rate limit / timeout

Three concerns today:

### 6.1 Cost budget

`LLMConfig.from_env()` (in `agents/config/llm.py`)
reads cost budget from environment. The actual cost
is computed via `_compute_cost_usd(response)` which
calls `litellm.completion_cost(completion_response=response)`.
The cost is **computed per call** but **never
checked against the budget** in `LiteLLMToolWorker.invoke`.
The Worker returns `cost_usd` in the result envelope;
**nothing rejects the call** based on cumulative cost.

ADR-007 §2.5 promised "Rate limit e Cost budget"
enforcement. The code has the **envelope** but not the
**gate**.

**Recommendation:** `LiteLLMToolWorker.__init__` reads
`CostBudget` from settings; `invoke` checks
`current_spend + estimated_cost <= limit` before
calling the transport; returns `Err(ToolError("budget_exceeded"))`
otherwise. Per-tenant overrides via
`KNT_LLM_BUDGET__PER_TENANT_USD__<tenant_id>`.

### 6.2 Rate limit

ADR-007 §2.4 documents the **fallback chain**: when
a provider returns 429, the Worker tries the next
model in `fallback_models`. The code path:
`LiteLLMTransportAdapter.__call__` raises
`LLMRateLimitError` on `litellm.RateLimitError`; the
**legacy `LiteLLMTool`** (Tool path, now removed in
v0.11 per ADR-043) was the catcher. The current
`LiteLLMToolWorker` **does not catch** `LLMRateLimitError`
— it surfaces the exception as `Err(ToolError("llm_transport_error"))`.

**This is a regression** introduced by the ADR-043
migration. The fallback chain is **dead code** in
the Worker path.

**Recommendation:** the Worker catches
`LLMRateLimitError`, retries against the next model
in `fallback_models`, with the same circuit-breaker
logic the legacy Tool had. The ADR-043 migration
dropped this by accident; this ADR flags it as
**high-priority debt** because a rate-limit spike
currently manifests as a hard failure for the user.

### 6.3 Timeout

`LiteLLMToolWorker.invoke` wraps the transport call
in `asyncio.wait_for(transport(request), timeout=self._timeout_s)`.
The timeout is read from `LLMConfig.timeout_s`. The
default is **not visible** in the file (per ADR-007
§2.5 it should be `60s`; verify in `agents/config/llm.py`).
A timeout returns `Err(ToolError("llm_timeout after
{s}s"))`. **The Worker does not retry on timeout**
(the fallback chain above also covers timeout).

**Recommendation:** include timeout in the retry loop
(`LLMRateLimitError`, `asyncio.TimeoutError` both
trigger fallback; `LLMAuthError` does not). Document
the timeout default in `LLMConfig` docstring.

## 7. Audit 6 — Cross-process contract (`WorkerManager`)

`LiteLLMToolWorker.invoke` runs in the
`WorkerManager`'s `ProcessPoolExecutor`. The contract
is:

- **Pickling**: the Worker class is picklable
  (`@tool_worker(...)` ensures this; tests cover it).
- **`__init__` runs once per worker process** (the
  pool reuses the process). This means `_transport`
  is **lazily initialised** per process. Good.
- **The result is JSON-serialisable**: the Worker
  returns `Ok({...})` whose payload is `dict[str,
  Any]` (line 716-729). All values are scalars or
  simple containers. Good.

**One subtle issue:** the Worker's result envelope
**does not include** the `LLMRequest` or
`LLMResponse` typed objects — it serialises to
`dict` because the result crosses the process
boundary. This is **correct** (Python objects don't
pickle cleanly across `ProcessPoolExecutor`), but it
means the **dispatcher's downstream code** (e.g.
`ChatRoleSystem._parse_completion` in
`role_systems/_base.py` line 104) re-parses the dict
instead of receiving a typed `LLMResponse`. This is
a cross-process impedance mismatch; the fix is not
worth it (the result envelope is small and the parse
is cheap), but it is the **reason** the Worker
returns `dict` and not `LLMResponse`.

**No recommendation** here — this is a known
constraint, not debt.

## 8. Summary — what is recommended

| # | Item | Type | Priority |
|---|---|---|---|
| 1 | `LLMResponse` returned across transport → Worker (typed envelope) | Refactor | medium |
| 2 | `ResponseFormat` typed (replaces `Optional[dict]`) | Refactor | medium |
| 3 | Split `llm.py` into 5 files (`adapter`, `worker`, `exceptions`, `_response`, `_streams`) | Refactor | low (cosmetic) |
| 4 | Remove `_RateLimitLike`, `_AuthLike` shims (4 lines) | Cleanup | low |
| 5 | Use per-call `drop_params=` instead of mutating `litellm.drop_params` global | Fix | medium (thread-safety) |
| 6 | Register `chat_llm` in `default_acl()` | Security | high |
| 7 | `RoleComponent.allowed_tools` must include `"chat_llm"` for `ChatRoleSystem` to fire | Security | high |
| 8 | Cost budget **gate** in `LiteLLMToolWorker.invoke` (the envelope exists; the gate is missing) | Feature | high |
| 9 | Fallback chain re-implemented in `LiteLLMToolWorker.invoke` (regression from ADR-043) | Fix | **critical** |
| 10 | Timeout triggers fallback (same loop) | Feature | medium |

Items 9 and 6/7 are the most impactful; 9 is a
regression that should land in **v0.14** (the next
minor) regardless of the other items.

## 9. Proposed scope split (per minor)

Following `ROADMAP.md`:

### v0.14.0 (alongside agents v2)

- Item 4 (remove shims)
- Item 5 (per-call `drop_params`)
- Item 6 (register `chat_llm` in `default_acl()`)
- Item 7 (`ChatRoleSystem` gates on `allowed_tools`)
- Item 9 (fallback chain in Worker — **critical**)

### v0.15.0 (deprecation wave)

- Item 3 (split `llm.py`) — file-level, no API change

### v0.16.0 (PrincipalLevel canonical)

- Item 1 (`LLMResponse` typed envelope in transport → Worker)

### v1.0.0 (cleanup)

- Item 2 (`ResponseFormat` typed)
- Item 8 (cost budget gate — feature)

This split keeps each minor coherent (no
half-migrated integrations) and matches the
deprecation policy (AGENTS.md §7).

## 10. Open questions

1. **Fallback chain scope.** ADR-007 §2.4 documents
   fallback as per-request (rate limit → next model).
   ADR-049 added `SolutionLookupSystem` as a
   pre-LLM-cache. Should fallback chain be **before**
   or **after** the cache lookup? Today the cache
   short-circuits before the LLM emits; the fallback
   chain is a fallback-on-rate-limit **after** the
   cache miss. Tracked.
2. **Cost budget granularity.** Per-tenant
   (`KNT_LLM_BUDGET__PER_TENANT_USD__<tenant_id>`)
   is one option; per-`RoleComponent` is another.
   Today the persona is in scope (ADR-060 §3.0 gate
   2); cost belongs somewhere similar. Tracked.
3. **`astream` path.** `_StreamDone` / `_StreamTimeout`
   exist but `astream` is unused. Is streaming an
   ADR-047 follow-up (partial-completion
   `StreamsWorker`)? Tracked as ADR-049 follow-up.
4. **`drop_params` thread-safety in tests.** A test
   that constructs two `LiteLLMToolWorker` instances
   in the same process mutates `litellm.drop_params`
   twice. This is a real (low-impact) bug. Tracked.

## 11. Zero Token Architecture review

The §2–§7 audits focused on the LLM I/O boundary
(`LiteLLMToolWorker` + `LLMTransport`). Zero Token
Architecture (ADR-049) covers **three more surfaces**:

1. The Solution **write-side** (`SolutionExtractorSystem`,
   `SolutionReviewPublisherSystem`,
   `SolutionPromoterSystem`, `SolutionProjector`).
2. The Solution **read-side** (`SolutionLookupSystem`).
3. The **rule-based deterministic path**
   (`RuleBasedChatSystem`).

This section audits the ZTA surfaces under the same
lens. It is **not** a separate ADR — ZTA is a layer
of the LLM integration (the whole point of ZTA is
"do less LLM"), so the audit belongs here.

### 11.1 ZTA principle-by-principle audit

**Principle 1 — "Infer once, export the logic."**
The Solution write-side pipeline does this: an LLM
call is observed by `SolutionExtractorSystem`, the
result is projected to FalkorDB by `SolutionProjector`,
and `SolutionPromoterSystem` bumps confidence
cross-agent. **Status: current.** The pipeline is
the canonical "exported logic" — the LLM is consulted
once per pattern, the result runs as code thereafter.

**Principle 2 — "Treat AI as a consultant, not a
permanent contractor."** The read-side
(`SolutionLookupSystem`) consults the store **before**
emitting `tool.<name>.requested`; on a hit, the
synthetic completion bypasses the LLM. **Status:
current.** Example `09b_solution_lookup_zta` (ADR-049
§2.3) demonstrates the closed loop end-to-end.

**Principle 3 — "Run the solution without inference."**
Three concrete artefacts run without inference:

- `RuleBasedChatSystem` for deterministic chat
  responses (rule table in memory).
- `SolutionLookupSystem` for cached tool completions
  (FalkorDB lookup).
- Pure `Tool` Protocol tools (`PiiRedactionTool`
  level 1, `EchoTool`, etc.) that were never LLM-backed.

**Status: current.** Three layers of "no-inference"
are shipped and stackable (per the dispatcher list).

**Principle 4 — "Hybrid: stable → software, uncertain
→ AI."** This is the **only** principle that has
visible debt in the shipped code. The composition
today is:

```python
dispatcher = ReactiveDispatcher(
    systems=[
        RuleBasedChatSystem(rules=[...]),  # ADR-049
        ChatRoleSystem(persona="..."),      # ADR-039
    ],
    ...
)
```

The `RuleBasedChatSystem` short-circuits when a rule
matches; on miss it returns `[]`, and the next system
(`ChatRoleSystem`) emits `tool.chat_llm.requested`.
This is the correct stack. **However**, two issues
undermine it:

1. **`_persona_for_view` is a stub.** In
   `src/kntgraph/agents/role_systems/_rule_based.py:322-336`,
   the `_persona_for_view` method returns `""` — the
   docstring admits this:
   > *"The persona is implementation-specific; the rule
   > registration API accepts a glob, so the operator
   > can register a default `'*'` rule that catches
   > every persona."*

   This means a rule registered with
   `persona_pattern: "support-*"` **never matches** for
   agents that have a real persona in their
   `RoleComponent`. The only rules that fire are the
   `"*"` catch-all. **Bug latente** desde v0.9
   (ADR-049 §2.2).

2. **`SolutionLookupSystem` writes its own
   cross-process state.** The system holds a
   `seen_request_event_ids: set[str]` (line 275 per
   the original review) that is **not shared across
   pods**. When the `WorkerManager` runs multiple
   pods (ADR-035 horizontal scaling), each pod has
   its own set; the same `ToolCallRequest` may
   trigger a `find_match` lookup on every pod. This
   is not a correctness bug (FalkorDB is the source
   of truth) but it is a **wasted-FalkorDB-call bug**
   that scales linearly with the pod count. ADR-049
   did not anticipate horizontal scaling.

### 11.2 File-level audit — `solution_lookup.py` (505 LOC)

`src/kntgraph/agents/memory/solution_lookup.py` is
**505 LOC** for one `ToolAwareSystem` class plus
helpers. The breakdown:

| Symbol | LOC | Concern |
|---|---|---|
| `InMemorySolutionStore` + `CachedSolution` | ~190 | Adapter (in-memory; tests use this) |
| `LookupStats` + helpers | ~50 | Telemetry |
| `SolutionLookupSystem.__init__` | ~40 | Construction |
| `SolutionLookupSystem.__call__` | ~120 | Main logic |
| `_params_fingerprint_from_request` | ~25 | Helper |
| Other helpers + tests | ~80 | |

For comparison, a typical `WorldSystem` in the
framework is 80–150 LOC. `SolutionLookupSystem` at
505 LOC is **3–6× the size** of the median system.
Per ADR-060 §6.5.2 the system is destined for
**consolidation into `SolutionPipeline`** — the read
side becomes `SolutionPipeline.read_for_overlay()`
and the in-memory store moves to
`agents/memory/solutions/_store.py`. The ADR-061
audit confirms that ADR-060 §6.5.2 is the right
direction.

### 11.3 File-level audit — `_rule_based.py` (342 LOC)

`src/kntgraph/agents/role_systems/_rule_based.py` is
**342 LOC** for one `ChatRule` dataclass plus
`RuleBasedChatSystem`. The breakdown:

| Symbol | LOC | Concern |
|---|---|---|
| `ChatRule` dataclass | ~15 | Data |
| `register_rule` / `unregister_rule` / `rules_for_tenant` | ~30 | API |
| `register_from_yaml` | ~40 | Loader |
| `_match_rule` | ~30 | Logic |
| `__call__` + `_handle_view` + `_persona_for_view` | ~120 | Main |
| Imports + docstrings | ~107 | |

The system is in **good shape** (342 LOC is large for
a system but reasonable for the surface area it covers:
tenant scoping + YAML loader + persona matching).
Per ADR-060 §6.5.3 it moves to
`agents/memory/role_systems/rule_based.py` as a
one-file-per-system.

### 11.4 Cross-cutting ZTA concerns

Three concerns span both the LLM audit and the ZTA
audit:

**a) `chat_llm` is an unauthenticated tool (§5 of this
ADR).** Both `SolutionLookupSystem` (which bypasses the
LLM on hit) and `RuleBasedChatSystem` (which bypasses
the LLM on match) **emit `chat.reply.generated`
directly**. They never invoke `tool.chat_llm.requested`
on the bypass path, so the audit's gate-1/gate-2 gaps
do not apply to the ZTA bypass paths. **This is good
news**: the ZTA paths are already safe under the
three-gate model because they don't cross the LLM
boundary. The audit's §5 recommendations apply only
to the LLM-emission paths, not the ZTA bypass paths.

**b) `SolutionLookupSystem` emits synthetic
`tool.<name>.completed` events.** Today the synthetic
emission carries the cached payload as `data`. It
does **not** go through the `IntentResolutionSystem`
gate. This is a **subtle gate-2 bypass**: an
`IntentComponent` from an unauthorised persona could
trigger a `ToolCallRequest`; the `SolutionLookupSystem`
matches and emits `tool.<name>.completed` without
checking `RoleComponent.allowed_tools`. The
recommendation is the same as §5: gate the synthetic
emission on the persona's `allowed_tools`.

**c) `SolutionLookupSystem` reads the FalkorDB on
**every** `ToolCallRequest` (per ADR-049 §1.2).**
This is the read-side cost. With the per-class
retention introduced by ADR-057 §4.11.6, the read
path can be optimised: only `event_class="tool"`
events trigger `find_match`, and the lookup key
includes the `event_class` so a single `ToolCallRequest`
does not double-look-up. This is a follow-up that
the `SolutionPipeline` consolidation (ADR-060 §6.5.2)
makes natural.

### 11.5 Summary — ZTA audit recommendations

| # | Item | Type | Priority |
|---|---|---|---|
| 11 | `_persona_for_view` stub returns `""` — **never matches** non-`"*"` persona globs | Bug fix | **critical** |
| 12 | `SolutionLookupSystem` writes `seen_request_event_ids` in-process; not shared across pods | Fix | medium |
| 13 | Synthetic `tool.<name>.completed` emission bypasses `RoleComponent.allowed_tools` | Security | high |
| 14 | `SolutionLookupSystem` 505 LOC → consolidate into `SolutionPipeline` (ADR-060 §6.5.2) | Refactor | medium (follows ADR-060 v0.14) |
| 15 | `_rule_based.py` 342 LOC → move to `agents/memory/role_systems/rule_based.py` | Refactor | low (follows ADR-060 v0.14) |
| 16 | `RedisSolutionStore` fail-open coverage + `solution_lookup.py:44` stale docstring (says "FalkorDB"; production is Redis Hash) | Docs + tests | medium |

Item 11 is **the highest-impact single fix** in this
whole audit. It is a latent bug since v0.9.0
(ADR-049 §2.2) that turns `RuleBasedChatSystem` from
"deterministic chat fallback" into "catch-all only".
Operators who register a rule with a non-`*`
`persona_pattern` see **zero hits** even when the
rule should fire.

### 11.6 Storage decision — Redis Hash today, FalkorDB as future evolution

The audit surfaces a question that was implicit in
ADR-049 and never made explicit: **which storage
backs the read-side of the ZTA loop?** The answer
matters because it determines latency, cross-pod
correctness, and operational cost.

#### 11.6.1 Three candidates

| Storage | Cardinality | Cross-pod | p99 latency | Operational cost |
|---|---|---|---|---|
| **Redis Hash** (today) | O(1) per field | yes (single shared Redis) | ~1 ms | already in critical path |
| **FalkorDB** (write-side only today; future evolution for graph-shaped reads) | O(N) per match | yes | ~10–50 ms | second stateful service |
| **In-memory dict** (tests + 09b example) | O(1) | **no** (single-process) | <1 µs | zero |

The read-side has three non-negotiable properties:

1. **High cardinality** — Solutions are keyed by
   `(tool_name, params_fingerprint)`. A single tool
   can produce thousands of distinct Solutions.
2. **Cross-pod correctness** — multiple dispatchers
   run (ADR-035 horizontal scaling); a
   `ToolCallRequest` may land on any pod. The lookup
   must return the same Solution regardless of which
   pod answers.
3. **Low latency** — the lookup is on the critical
   path (it bypasses the LLM). p99 must be < 5 ms.

#### 11.6.2 The decision

**Redis Hash is the read-side store today. FalkorDB
stays the write-side store today. FalkorDB on the
read-side is left as future evolution — it returns
to the table only when a real use case for graph-
shaped read queries appears.**

The `RedisSolutionStore` in
`src/kntgraph/infra/redis/_memory/_solution.py` already
implements the read-side. Wire layout:

```text
key:    knt:solution:<tool_name>      (Redis Hash)
field:  <params_fingerprint>          (hex digest)
value:  JSON CachedSolution {tool_name, params_fingerprint, confidence, result, source_completion_event_id}
TTL:    per-entry EXPIRE in put pipeline
```

Operations:

- `HGET knt:solution:<tool_name> <params_fingerprint>`
  → O(1), p99 <1 ms (Redis Hash field lookup).
- `put(...)` issues a transactional pipeline of
  `DELETE` + `HSET` + `EXPIRE` so the TTL is always
  applied atomically with the value.
- `find_match` is **fail-open**: a Redis error or
  corrupt payload returns `None` and logs the
  failure. The dispatcher's LLM fallback handles the
  miss (this is the read-side being a **best-effort
  accelerator**, not a correctness gate).

#### 11.6.3 FalkorDB on the read-side — future evolution, not today

FalkorDB is the right store for the **write-side**
because the knowledge graph carries **structure that
the Redis Hash cannot represent**:

- `(Problem)-[:SOLVED_BY]->(Action)` with confidence
  and validated_count.
- `Problem.embedding` for similarity search across
  Solutions (different params fingerprints may map
  to similar Problems).
- `(:Solution)-[:TAGGED]->(:Tag)` for cross-tool
  clustering.
- Audit trail: "which Solutions were promoted by
  which agent in which tenant".

A future read-side might benefit from graph-shaped
queries (e.g. "all Solutions that match
`Problem.embedding` within 0.05 cosine for a given
tenant"). When that use case appears, the path is:

1. Add a new adapter `GraphAwareSolutionStore`
   implementing the same `SolutionStoreLike`
   contract (or a wider contract — TBD when the
   query shape is concrete).
2. Plug it via the `SolutionPipeline.read_for_overlay()`
   constructor (ADR-060 §6.5.2) — same seam, no API
   change for existing callers.
3. Open an ADR (e.g. *ADR-064 "FalkorDB read-side
   adapter"*) at that point. The use case drives the
   design; speculation about the adapter today
   would lock us into a contract we don't yet need.

Forcing FalkorDB into the read-side **now** would
couple two unrelated concerns: the **lookup** (O(1)
per `(tool, fingerprint)`) and the **knowledge
traversal** (graph-shaped queries). The lookup does
not need graph shape; the traversal does not need
O(1) single-key lookup. Each storage in its lane.

The docstring at
`src/kntgraph/agents/memory/solution_lookup.py:44`
**says "FalkorDB"** as the production backend; this
is stale documentation from ADR-049 v1 — the
docstring was written when FalkorDB was the
proposed backend; the shipped implementation landed
on Redis Hash. Item 16 in §11.5 fixes this and adds
the fail-open coverage.

#### 11.6.4 Per-class retention interaction

ADR-057 §4.11.6 introduces per-`event_class` Redis
retention. The `RedisSolutionStore.put` already
applies a per-entry TTL (via `EXPIRE`); the
per-class retention policy applies to the **EventLog**
(where the cache key was first observed), not to the
**Solution Hash itself**. The two are independent:

- **EventLog retention** (`KNT_RETENTION_EVENT_CLASS_*`):
  how long the originating `tool.<name>.completed`
  event stays in the Redis Stream. Affects audit
  replay.
- **Solution Hash TTL** (`RedisSolutionStore.ttl_seconds`):
  how long a *cached Solution* stays in the lookup
  Hash. Affects ZTA bypass.

A deployment that wants "Solutions never expire"
sets `RedisSolutionStore.ttl_seconds=None`. A
deployment that wants "Solutions expire after 30 d"
sets `KNT_SOLUTION_STORE__TTL_SECONDS=2592000`.
This is orthogonal to the EventLog retention — the
two policies do not interact.

#### 11.6.5 Implication for the `SolutionPipeline` consolidation

The `SolutionPipeline.read_for_overlay()` method
(ADR-060 §6.5.2) takes a `SolutionStoreLike` as a
constructor arg. The Redis choice does not bind the
Pipeline to a backend; the `InMemorySolutionStore`
stays as the test fixture, and the `RedisSolutionStore`
is the default for production. The `Protocol` is
**open to future adapters** (a future
`GraphAwareSolutionStore` or other) — no code change
is required to add one, but no code is written today
because no use case requires it.

## 12. Consolidated scope (LiteLLM + ZTA)

§8 listed LiteLLM items 1–10. §11 lists ZTA items
11–16. The merged scope split per `ROADMAP.md`:

### v0.14.0 (next minor)

| Item | Source | Priority |
|---|---|---|
| 4 — remove shims (`_RateLimitLike`, `_AuthLike`) | §4.1 | low |
| 5 — per-call `drop_params=` | §4.3 | medium |
| 6 — `chat_llm` in `default_acl()` | §5 | high |
| 7 — `ChatRoleSystem` gates on `RoleComponent.allowed_tools` | §5 | high |
| 9 — fallback chain in Worker (regression from ADR-043) | §6.2 | **critical** |
| 11 — fix `_persona_for_view` stub | §11.1 | **critical** |
| 13 — `SolutionLookupSystem` synthetic emission gates on persona | §11.4b | high |

### v0.15.0 (deprecation wave)

| Item | Source | Priority |
|---|---|---|
| 3 — split `llm.py` into 6 files | §3 | low (cosmetic) |
| 14 — consolidate `SolutionLookupSystem` into `SolutionPipeline` | §11.2 + ADR-060 §6.5.2 | medium |
| 15 — move `_rule_based.py` to `agents/memory/role_systems/` | §11.3 + ADR-060 §6.5.3 | low |

### v0.16.0 (`PrincipalLevel` canonical)

| Item | Source | Priority |
|---|---|---|
| 1 — `LLMResponse` typed envelope in transport → Worker | §2 | medium |

### v1.0.0 (cleanup)

| Item | Source | Priority |
|---|---|---|
| 2 — `ResponseFormat` typed (replaces `Optional[dict]`) | §2 | medium |
| 8 — cost budget gate | §6.1 | high |
| 12 — `SolutionLookupSystem` shared `seen_request_event_ids` across pods | §11.1 | medium |
| 16 — `RedisSolutionStore` fail-open coverage + `solution_lookup.py:44` stale docstring fix | §11.6 | medium |

**Items 9 and 11 are the most impactful for v0.14**;
both are critical regressions/latent bugs. Items 6/7/13
are security-relevant gaps that should not slip past
v0.14. Items 14/15 follow naturally from ADR-060's
`agents/` rewrite and land together with that work.

## 13. Decision

This ADR **does not** close the above with a single
decision; it documents the audit and proposes the
scope split. Each item is a candidate for the next
minor; the most critical (9 and 11) are regressions
and latent bugs that should land in **v0.14** (the
next minor) regardless of the other items.

**Recommended next steps:**

- Open **ADR-062** *"LiteLLM fallback chain in the
  Worker path"* — owns item 9 specifically.
- Open **ADR-063** *"`RuleBasedChatSystem._persona_for_view`
  fix"* — owns item 11 specifically (or fold the fix
  into the agents/v2 rewrite, ADR-060 §6.5.3).
- Update `ROADMAP.md` v0.14 section to include items
  6, 7, 11, 13 alongside the agents/v2 work.

ADR-061 is the audit that prioritises these;
ADR-062 and ADR-063 are the actionable units.
decision; it documents the audit and proposes the
scope split. Each item is a candidate for the next
minor; the most critical (9) is the fallback chain
regression in `LiteLLMToolWorker.invoke`.

**Recommended next step:** open a follow-up ADR
(ADR-062) titled *"LiteLLM fallback chain in the
Worker path"* that owns item 9 specifically. ADR-062
is the actionable unit; ADR-061 is the audit that
prioritises it.