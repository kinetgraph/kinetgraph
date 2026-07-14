# Changelog

All notable changes to Kinetgraph will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- **Pure ECS Role Architecture (ADR-039):**
  - Introduced `RoleComponent` as a pure, immutable data component to store agent personas, instructions, and permitted tool inventories.
  - Introduced `IntentComponent` to model in-flight user intent requests inside the ECS World projection.
  - Implemented `IntentResolutionSystem` as a pure `WorldSystem` to process pending intents, perform Zero-Trust security checks (`ToolACL`), and check semantic capability permissions.
  - Added comprehensive unit tests in [test_resolution.py](file:///home/adriano/Projects/kinetgraph/kinetgraph/tests/agents/unit/roles/test_resolution.py) validating security constraints, semantic capabilities, and fail-fast scenarios.
- **Messaging Ingestion Proposal (ADR-040):**
  - Proposed `--use-intent-messaging` CLI option for asynchronous message-based ingestion.
  - Documented three ingestion models (HTTP-only, Messaging-only, Hybrid) and detailed how a background consumer can ingest intents concurrently to the `EventLog`.
- **Derived component preservation (ADR-044 + 05b shim):** the
  default domain projection's `_apply_event` now preserves
  a closed set of **derived component keys** (string keys
  `tool_requests` / `tool_completions` and class keys
  `SessionComponent` / `ProfileComponent` /
  `ContinuityComponent`) across a domain fold. The previous
  rule replaced the entire `components` dict on every
  domain event, which clobbered the tool-call overlay slots
  AND the memory components installed by the hydration
  projection (ADR-042 §6.1) on the next domain event. The
  fix is opt-in by key: a domain event's own payload still
  replaces the component keyed by `event.event_type` (the
  existing last-event-wins contract, pinned by
  `test_domain_replaces_components` in
  `tests/unit/test_world.py`); unrelated derived components
  survive. This unblocks the example 05b hydration shim
  end-to-end.
- **Example 05b shim closed (DEBT §2.18):** the projection
  shim in `examples/05b_session_chat_ecs.py` is now
  end-to-end correct. The `SessionChatSystem` reads
  `SessionComponent` from the hydrated view, emits a
  `tool.chat_llm.requested` event on a new user intent,
  and emits two `tool.session_recorder.requested` events
  (append_user + append_assistant) when the chat_llm
  completion lands. 8 unit tests in
  `tests/agents/unit/test_example_05b_shim.py` cover the
  shim installation, the hydration contract (SessionComponent
  is installed on the view), the tool-call overlay accumulation
  contract (request persists across ticks), and the full
  chat round-trip (request → completion → recorder).
- **`@tool_worker` forward-reference resolution (ADR-043 follow-up):**
  the `@tool_worker` decorator's Pydantic schema extraction
  now resolves forward-reference string annotations via
  `importlib.import_module(cls.__module__)` instead of the
  (non-existent) `cls.__globals__`. Without this, classes
  using `from __future__ import annotations` with a
  Pydantic model parameter produced an empty schema
  (`{"title": "Payload"}` instead of `{"$ref": "#/$defs/..."}`).
  Regression test: `test_tool_worker_with_pydantic_model`
  in `tests/unit/tools/test_worker.py`.
- **Role → ECS migration (ADR-039 + ADR-043 + ADR-044 follow-up):**
  new module `src/kntgraph/agents/role_systems/` provides
  the event-driven `WorldSystem` counterparts to the
  legacy `ChatRole` / `PlannerRole` / `SummarizerRole` /
  `PersonalizedRole`:

    - `ChatRoleSystem` — reads `SessionComponent` from
      the `AgentView`, emits `tool.chat_llm.requested`
      with the role's `SYSTEM_PROMPT` and the formatted
      transcript, parses the LLM's response into a
      `ChatReply` and emits `chat.reply.generated`.
    - `PlannerRoleSystem` — reacts to `plan.request`
      events, emits `plan.generated` with a typed
      `Plan` payload.
    - `SummarizerRoleSystem` — reacts to
      `summary.request` events, emits
      `summary.generated` with a typed `Summary`.
    - `PersonalizedRoleSystem` — reacts to
      `personalized.request` events, emits
      `personalized.reply.generated` with the raw text.

  The systems REUSE the legacy role's `SYSTEM_PROMPT`
  and input-formatting helpers so the prompt engineering
  lives in one place; the migration is a thin port from
  the synchronous `await role.reply()` to the
  event-driven `system(world)` cycle. The dispatcher's
  event loop is NOT blocked while the LLM runs. 9 unit
  tests in
  `tests/agents/unit/roles/test_role_systems.py` cover
  the request/completion cycle for all four roles.
  Reference example:
  `examples/05c_session_chat_ecs_roles.py` (the
  canonical migration of `ChatRole` end-to-end,
  including a `SessionRecorderRoleBridge` that persists
  the turn via the `session_recorder` tool).
- **Tool-call request TTL (ADR-045):** the
  `ToolCallRequest` component has a new
  `expires_at: Optional[datetime]` field (computed at
  materialisation time as
  `requested_at + ttl_seconds`). A new
  `ToolCallTTL` dataclass in
  `core/world/components.py` carries the per-tool
  TTL config (default 5 minutes; per-tool override
  via `per_tool_ttls`). The
  `overlay_tool_calls` projection now threads the
  `ToolCallTTL` and SETS `expires_at` on each new
  request (the overlay remains pure — it does NOT
  enforce the TTL). A new
  `ToolCallTTLSweeperSystem` (in
  `runner/tool_call_ttl_sweeper.py`) is a
  `WorldSystem` that walks the `tool_requests` slot
  once per tick and EMITS `tool.<name>.failed` for
  every stale request (the dedup is in-memory via
  `_emitted_failures`). The
  `ReactiveDispatcher` auto-registers the sweeper
  when the operator passes a `tool_ttls=ToolCallTTL()`
  config (opt-in; default is no TTL enforcement, for
  back-compat with the legacy behaviour). 9 unit
  tests in
  `tests/unit/runner/test_tool_call_ttl_sweeper.py`
  cover the request/completion cycle, dedup,
  multi-agent, empty world, and the legacy bare
  `tool.requested` form. ADR-045 was revised after
  the original draft (inline TTL eviction in the
  overlay) was rejected: the sweeper system
  separates concerns (the overlay stays pure; the
  sweeper handles the I/O) and the failure event is
  observable by downstream systems.

### Changed
- **Traceability Enforcement (ADR-037 / ADR-039):**
  - Enabled explicit `CorrelationContext` propagation in `IntentResolutionSystem` across all success (`tool.<name>.requested`) and failure (`intent.validation_failed`) event paths to guarantee end-to-end auditability.
- **CLI Bounded Context Template:**
  - Updated `knt new context` templates to automatically wire `ToolRegistry` and `IntentResolutionSystem` into the generated dispatcher files.
- **Documentation Updates:**
  - Marked [ADR-006 (Tool-Role Separation)](file:///home/adriano/Projects/kinetgraph/kinetgraph/ADRs/ADR-006-Tool-Role-Separation.md) as **Superseded by ADR-039** to replace tool wrappers with the pure data component model.

### Fixed
- **Tool-call overlay canonical form:** The `overlay_tool_calls`
  projection and the `_has_tool_events` helper now recognise the
  canonical `tool.<name>.<suffix>` form (ADR-036) in addition
  to the legacy bare `tool.<suffix>` form. 3 regression tests
  in `tests/unit/runner/test_reactive_tool_projection.py`.
- **Tool-call overlay multi-tick slot loss (ADR-044):** the
  `overlay_tool_calls` projection now **accumulates** requests
  and completions across ticks rather than rebuilding the
  slot from the current batch only. A request emitted in
  tick N remains visible in the `tool_requests` slot in
  tick N+K; it is **evicted** only when a matching
  `tool_completions` entry lands in a subsequent tick
  (Option B, completion-driven eviction). The
  `_apply_event` helper now preserves the `tool_requests` /
  `tool_completions` slots when the incoming event is a
  tool event (so the `World.with_event` chain between
  ticks no longer drops the slot before the overlay runs).
  The `SolutionExtractorSystem` was updated to iterate
  `completions` (source of truth for "finished") and
  look up the request from the (possibly evicted) slot.
  2 multi-tick acceptance tests in
  `tests/unit/runner/test_reactive_tool_projection.py`
  (request persists across batches, unrelated completion
  doesn't evict it). 1 fix-test in
  `tests/unit/core/test_projection_tool_calls.py`
  (request + completion in the same batch: request is
  preserved for the system to react to).
- **ADR-044 (Tool-call Overlay Accumulation):** full ADR
  with the option analysis (rebuild vs. accumulate vs.
  TTL), the chosen approach (Option B, completion-driven
  eviction), the `_apply_event` preservation rule, the
  multi-tick acceptance tests, and the follow-up
  ADR-045 (TTL-based eviction for orphaned requests).

## [0.8.0] — 2026-07-14

### Added
- **Memory components (ADR-042):** `SessionComponent`,
  `ProfileComponent`, `ContinuityComponent` in
  `src/kntgraph/core/components/memory.py`. Frozen dataclasses
  installed on the `AgentView` by the hydration projection.
- **Memory hydration projection (ADR-042 §6.1):**
  `src/kntgraph/core/world/projection_memory.py::project_memory`.
  Pure fold of `session.*` / `profile.*` / `continuity.*` events
  into the three components. Multi-tick safe (preserves the
  base component when the current batch has no memory events).
- **Example 05b (`examples/05b_session_chat_ecs.py`):** WIP
  reference implementation of the ADR-042 §6.1 hydration
  pipeline. Runs a reactive system that reads
  `SessionComponent` from the `AgentView` (no Redis I/O in
  the system). The chat round-trip is the canonical pattern;
  the example does not yet persist a full multi-turn chat
  end-to-end (see DEBT.md §2.18 for the open work).
- **`LiteLLMToolWorker` (ADR-043):** New
  `@tool_worker(name="chat_llm")` implementation of the LLM
  bridge. Runs in the `WorkerManager`'s `ProcessPoolExecutor`;
  the dispatcher event loop is not blocked while the LLM
  responds. Returns a JSON-serialisable dict with `text` /
  `model` / `usage` / `finish_reason` / `cost_usd` / `latency_ms`.
  7 unit tests in `tests/agents/unit/tools/test_litellm_worker.py`.
- **ADR-042 (Memory Model Exposure):** Full ADR (sections
  §1-9) covering the Session/Profile/Continuity components,
  the hydration pipeline, the 3-layer tool-calling enforcement,
  the sequence diagram (T0-T2+), and the acceptance checklist.
- **ADR-043 (LiteLLM worker migration):** Migration plan for
  the LLM tool from the legacy `Tool` Protocol to the
  `@tool_worker` pattern. Deprecates `LiteLLMTool` (removal
  target v0.9.0) and `ToolInvoker` (removal target v1.0.0).

### Changed
- **Canonical `tool.<name>.<suffix>` form (ADR-036):** ADRs
  034, 036, 037, 039, 042, 043 all updated. The legacy bare
  `tool.requested` / `tool.completed` / `tool.failed` form is
  still recognised by the projection (back-compat with old
  EventLogs) but is documented as deprecated in the wire
  contract.
- **Deprecation warnings:** `LiteLLMTool` and `ToolInvoker`
  emit `DeprecationWarning` on import (one-shot). Class-level
  `__deprecated__ = True` marker. Removal targets: v0.9.0 and
  v1.0.0 respectively.

### Deprecated
- `LiteLLMTool` (legacy `Tool` Protocol). Use
  `LiteLLMToolWorker` instead. Removal target: v0.9.0.
- `ToolInvoker` (legacy orchestrator). Use `@tool_worker`
  orchestrated by `WorkerManager`. Removal target: v1.0.0.

### Known issues
- Example 05b (`examples/05b_session_chat_ecs.py`) is
  WIP: the hydration shim
  (`_install_projection_shim`) does not yet produce a
  full multi-turn chat end-to-end (the chat system
  never emits a `request_tool` event in the example).
  The projection path (ADR-042 + ADR-044) is
  production-ready; the example's shim is the only
  blocker. See DEBT.md §2.18.
- The `Role` classes (`ChatRole`, `PlannerRole`, etc.)
  still call the LLM synchronously; the migration to
  emit `tool.chat_llm.requested` is the work of ADR-044
  follow-up (the example 05b's `SessionChatSystem` is
  the reference implementation; the roles can be ported
  to emit a `request_tool` event in place of the
  synchronous `_invoke`).
- TTL-based eviction (ADR-045, planned): the current
  completion-driven eviction leaves orphaned requests
  (e.g. after a worker crash) in the slot forever.
  The follow-up ADR proposes a TTL bound on
  `tool_requests` entries (default 5 minutes,
  configurable per tool) so the slot cannot grow
  unbounded.

### Deprecated
- **`kntgraph.agents.roles` package (ADR-041):**
  - The `ChatRole`, `PlannerRole`, `SummarizerRole`, `PersonalizedRole`, and `SemanticRoutingRole` classes are deprecated. They have been superseded by the pure-ECS architecture from [ADR-039](file:///home/adriano/Projects/kinetgraph/kinetgraph/ADRs/ADR-039-Role-rethinking-and-intentions-routing.md) (`RoleComponent` + `IntentResolutionSystem`).
  - Importing `kntgraph.agents.roles` emits a `DeprecationWarning` since v0.8.0. The package will be removed in v1.0.0 (target: 2026 Q4).
  - The new components (`RoleComponent`, `IntentComponent`, `IntentResolutionSystem`) remain importable from the same package through v0.9 to ease the migration.
  - See [ADR-041](file:///home/adriano/Projects/kinetgraph/kinetgraph/ADRs/ADR-041-agents-roles-deprecation.md) for the migration guide and removal schedule.
