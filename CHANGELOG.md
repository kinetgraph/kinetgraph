<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Changelog

All notable changes to Kinetgraph will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- *(placeholder for the next release; copy entries from
  the previous `[Unreleased]` block as you land them.)*

## [0.11.0] — 2026-08-02

### Added
- **PyPI publishing via Trusted Publishing (ADR-052,
  Accepted):** the v0.11.0 release is the first
  distribution on PyPI at
  <https://pypi.org/project/kntgraph/>. The release
  workflow is split into two (`release.yml` cuts the
  tag + opens the GitHub Release; `publish.yml`
  builds the wheel from the tag and uploads to PyPI
  via `pypa/gh-action-pypi-publish` with an OIDC
  short-lived token). The `pypi` GitHub Environment
  is the human-in-the-loop gate; the publish
  workflow fails fast with a clear diagnostic if the
  tag is missing on the remote (`git ls-remote`
  pre-check). 17 contract tests in
  `tests/scripts/test_workflow_split.py` enforce the
  split (release.yml contains no PyPI action;
  publish.yml contains no tag-cut step; the
  committer identity is set before the
  `bump_version.py` tag step; the CHANGELOG commit
  lands before the tag so `git show vX.Y.Z:
  CHANGELOG.md` shows the dated section, not the
  previous release).
- **PEP 639 license metadata (+ `setuptools>=77`
  floor):** `pyproject.toml` now declares
  `license = "Apache-2.0"` (the modern
  `License-Expression` form, replacing the
  `License :: OSI Approved :: Apache Software
  License` classifier which setuptools ≥ 77
  rejects when the `license =` field is set). 11
  canonical PyPI classifiers are added (Operating
  System, Intended Audience, Programming Language,
  Topic, Typing). The `[build-system]::requires`
  floor is bumped from `setuptools>=61.0` to
  `setuptools>=77` to make the PEP 639 minimum
  explicit.
- **Operator runbook
  (`docs/pypi_publishing_runbook.md`):** the
  one-time setup (PyPI Trusted Publisher + GitHub
  `pypi` Environment) and the per-release
  `gh workflow run` flow are documented with the
  exact PyPI form fields and the exact `gh`
  commands. Failure-mode runbook (§4) covers the
  pre-check failure, the sanity-check mismatch,
  `invalid-publisher`, duplicate uploads, wheel
  build failures, and the "release without
  publish" orphan risk.
- **CLI Boilerplate Generation v2 (ADR-053):** the
  `knt` CLI's templates are now aligned with the
  framework's current contracts. The historical
  `agent.py.jinja` template referenced the
  never-implemented `CapabilityPolicy` class; the
  `event.py.jinja` template imported
  `correlation_middleware` from the historical
  `core.correlation` path; the `tool.py.jinja`
  template emitted the pre-ADR-047 return type
  `Result[dict, Exception]`. All these are fixed.
  New templates: `consumer.py.jinja` (3-responsibility
  Redis Streams consumer pattern) and
  `config.py.jinja` (Pydantic Settings subclass
  inheriting the framework's `KNT_` env prefix).
  The `dispatcher.py.jinja` template now threads
  `redis` and `tool_router` through the
  `ReactiveDispatcher` constructor and supports a
  `with_supervisor` flag for the two-layer
  (Reactive + Cyclic) wiring. The `main.py.jinja`
  template likewise threads the two arguments
  through the dispatcher factory placeholder.
  All templates replace the Jinja
  `{# #}` SPDX comment (which was stripped at render
  time, defeating the REUSE 3.3 lint gate) with
  Python `#` comments (the header survives the
  render).
- **`knt upgrade` regeneration workflow (ADR-053):**
  the new sub-command has three modes
  (`check` / `apply <path>` / `apply-all [--force]`)
  that re-render the boilerplate against the current
  templates. The `check` mode is content-based
  (renders the template against the discovered
  context and `difflib.unified_diff`s the result).
  The `apply-all` mode respects local edits
  (skips files with drift + local modifications
  unless `--force` is passed). The `--check`
  mode is the canonical CI gate for "did the
  operator's framework version drift the
  boilerplate".

### Fixed
- **`scripts/update_version_badge.py` regex bug
  (caught by v0.11.0):** the regex was
  `^\[!\[\s*Version\s*\]` (looking for `[![Version]`
  with TWO opening brackets). The actual badge is
  `![Version]...` (ONE opening bracket). The bug
  meant the existing badge was never matched, so the
  script silently **appended** a new badge instead of
  **replacing** the old one. Fixed to
  `^!\[Version\]` (the canonical markdown image
  alt-text escape).
- **`scripts/readme_stats.py::_version_badge()`
  followed the latest tag, not the dev
  `__version__`:** when the working tree is dirty
  (mid-edit), `setuptools_scm` infers
  `0.11.1.dev0+g<sha>` to signal "not the tagged
  release". The badge used to pick `0.10.1`/`0.11.1`
  (the inferred next minor) instead of the actual
  `0.11.0` (the latest tag). The badge now derives
  from `git describe --tags --abbrev=0` (the tag)
  with a fallback to `__version__` when no tag is
  reachable. Tests updated to assert the tag-derived
  version.
- **`release.yml` ordering: CHANGELOG commit lands
  before the tag (caught by v0.11.0):** the original
  ordering ran `bump_version.py` (which creates the
  annotated tag at HEAD) **before** the
  `git commit CHANGELOG.md` step, so the tag pointed
  at the commit **before** the new dated section. The
  `v0.11.0` tag was therefore created with a
  CHANGELOG that still showed `[0.10.0]` as the latest
  entry. The workflow now commits the CHANGELOG,
  sets the committer identity, and **then** creates
  the tag (so the tag points at the commit with the
  new section).
- **`release.yml` committer identity (caught by
  v0.11.0):** the GitHub Actions runner does not
  ship with `user.name`/`user.email` configured by
  default, so `bump_version.py` failed with
  `empty ident name not allowed` on the
  `git tag -a vX.Y.Z` invocation. The fix is a
  dedicated `Set git committer identity` step with
  `env: GIT_AUTHOR_*` / `GIT_COMMITTER_*` (belt-and-
  braces) and `git config user.name`/
  `user.email` in the `run:` block. The identity is
  set **before** the tag step (whichever ordering
  is chosen by the workflow).

### Removed (docs)
- **`FMH_*` env-var references** (the pre-rename
  prefix from the `fmh_backend` / `fmh_agents`
  packages, see ADR-036): all references in the
  README, `REFERENCE.md`, `SECURITY.md`,
  `GETTING_STARTED.md`, `examples/05b_session_chat_ecs.py`,
  `examples/knt-cli/weather_platform/.env.example`,
  and `tests/agents/unit/tools/test_litellm_worker.py`
  were replaced with `KNT_*` (the canonical
  prefix in `infra.config._base.py`). The
  `FMH_CRYPTO_ENABLED=1` mention in `SECURITY.md`
  is also gone (the env var never existed in
  `src/`; the cryptographic event signing is
  opt-in via `settings.event_signing_enabled`).

### Tool-Adapter Pattern — `HttpClientLike` Protocol (ADR-047, DEBT §2.24):** the
  framework now owns the I/O boundary for async HTTP clients
  (`HttpClientLike` / `HttpResponseLike` in
  `src/kntgraph/infra/http/_client.py`). The
  `HttpxHttpClientAdapter` wraps `httpx2.AsyncClient` with a
  lazy import; verticals inject the adapter via DI. The
  framework-level Protocol catalogue (ADR-047 §2.2.4) now
  lists `LLMTransport` / `EmbeddingProvider` / `RedisLike` /
  `HttpClientLike` as the four I/O boundaries a ToolWorker
  can reuse.
- **CLI test suite collect-time skip (DEBT §2.25):** the
  `tests/unit/cli/` directory now ships a `conftest.py` that
  uses `collect_ignore_glob` to skip the directory at
  collect time when the optional `typer` dependency is
  missing. The pattern is the standard Python community
  fix for optional-dependency test directories. The
  `scripts/ci.py::_run_step` step now tolerates pytest
  exit code 5 ("no tests ran") on the `tests` step with a
  guard that the output mentions "no tests ran", so the
  CI gate passes in both the default (no `[cli]` extra) and
  the `uv sync --extra cli` configurations.
- **CC gate detects new blocks (DEBT §2.26):** the
  `gate_complexity` in `scripts/ci.py` previously detected
  only "block grew in CC" regressions. Blocks added by a
  refactor that landed above CC=10 silently passed the
  gate (10 new offenders were hidden from CI). The gate
  now flags `CC new offender: <key> = <N>` when a block has
  no baseline entry and CC > 10, with a hint that the
  operator must refactor or update the baseline before
  merging.

### Changed
- **Tool-Adapter Pattern — Workers refactored to typed errors
  (ADR-047, DEBT §2.24):** the `invoke` signatures of every
  existing `@tool_worker` in the codebase were tightened from
  `Result[dict, Exception]` to `Result[dict, ToolError]`
  (AGENTS.md §6.1). The original exception is preserved as
  `__cause__` on the `ToolError` for diagnostics.
  Affected Workers:
  - `LiteLLMToolWorker` (`src/kntgraph/agents/tools/llm.py`).
  - `OpenMeteoApi` (`examples/knt-cli/weather_platform/.../tools/open_meteo_api.py`).
    The Worker was also refactored to receive the new
    `HttpClientLike` via DI; the `httpx` import is no longer
    in the Worker's module path.
  - `SessionRecorderTool` (in `examples/05b_session_chat_ecs.py`
    and `examples/05c_session_chat_ecs_roles.py`).
  - `WeatherTool` (`examples/19_tool_worker_pattern.py`).
  - The `knt new tool` CLI template
    (`src/kntgraph/cli/templates/tool.py.jinja`).
- **ADR-047 §3.1 / §3.2 / §5 / §6.4 aligned with the canonical
  code:** the `LLMTransport` Protocol returns the LiteLLM-style
  dict (not a discriminated envelope), the `LLMResponse`
  dataclass is the LLM-side envelope the `LiteLLMToolWorker`
  returns to the `WorkerManager`, and the `AdapterResponse`
  base class proposal from §6.4 is deferred to ADR-049. The
  ADR **status** remains `Draft` (the §6.1 `StreamsWorker` /
  §6.2 cancellation follow-ups are still open; "Accepted"
  is gated on ADR-049).
- **Cyclomatic complexity — 10 offenders refactored to CC ≤ 10
  (DEBT §2.26):** all 10 functions over CC=10 in the previous
  baseline were broken into per-event-type dispatch tables
  (or single-responsibility helpers) and dropped to A/B
  rank. The radon baseline (`.radon-baseline.json`) was
  regenerated. The 10 refactors:

  | File | Function | CC before | CC after |
  | --- | --- | --- | --- |
  | `memory/profile.py` | `_fold_profile_events` | 18 | 4 |
  | `agents/role_systems/__init__.py` | `_BaseRoleSystem.__call__` | 16 | 8 |
  | `core/world/projection_memory.py` | `project_memory` | 13 | 4 |
  | `core/world/projection_memory.py` | `_fold_session` | 13 | 4 |
  | `core/world/projection_memory.py` | `_fold_profile` | 13 | 4 |
  | `core/world/projection_memory.py` | `_fold_continuity` | 13 | 4 |
  | `memory/session.py` | `_fold_session_events` | 11 | 4 |
  | `agents/tools/arg_validation.py` | `validate_args` | 11 | 4 |
  | `agents/tools/llm.py` | `LiteLLMTransportAdapter` | 11 | 5 |
  | `core/world/projection_tool_calls.py` | `overlay_tool_calls` | 11 | 4 |

  The shared pattern is the **dispatch table**:
  ``_HANDLERS: dict[str, Callable[[Event, dict], None]]``
  with one small handler per event type; the fold itself
  is a linear ``for`` loop. Net effect: ``avg 2.56 → 2.49``
  CC, ``237 → 237`` A-rank files (MI), ``1263 → 1309`` CC
  blocks (more, smaller).

### Removed
- **Raw Redis client constructors (F5 cleanup / Breaking):**
  - Removed backward-compatibility wrappers from `EventLog`, `IncrementalWorldStore`, `SessionManager`, `ProfileManager`, and `ContinuityManager` constructors. They now strictly require their Protocol-compliant storage adapters (`EventLogStorage`, `WorldCheckpointStorage`, `ShortMemoryStorage`) instead of accepting raw Redis clients directly. Updated all test files and call sites accordingly.

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
- **Tier 1 cleanup (post-ADR-045):**

    - `tests/conftest.py` trimmed: removed
      `MockRedis` / `MockPubSub` / `MockPipeline` and
      the 7 derived fixtures (`fake_redis`,
      `fake_redis_pipeline`, `redis_with_pubsub`,
      etc.) that were left over from the pre-ADR-042
      era. ~220 lines deleted. Verified: the kept
      fixtures (`reset_correlation_context`,
      `reset_settings_cache`) cover the only
      autouse requirements of the suite.

    - `src/kntgraph/agents/tools/llm_transport.py`
      removed: the file was a back-compat shim that
      re-exported from
      `src/kntgraph/tools/llm_transport.py` (the
      canonical location). No external callers in the
      repository or in `examples/`. The package
      `__init__.py` now imports from the canonical
      module directly. Docstring in
      `agents/tools/llm.py` updated to point to the
      canonical path.

    - 16 unit tests added in
      `tests/unit/core/test_projection_memory.py`
      covering the `project_memory` projection
      (closes DEBT §2.15 item 3): single-tick and
      multi-tick fold for `SessionComponent` /
      `ProfileComponent` / `ContinuityComponent`,
      multi-agent projection, and base-component
      preservation across ticks. The new tests
      uncovered two latent bugs which were fixed
      in the same change: `project_memory` now
      accepts `base_views=None` (default: empty
      dict — was required), and `_fold_profile` /
      `_fold_continuity` now reuse the base
      component when the incoming batch has no
      event of the corresponding type (matching the
      `_fold_session` behaviour; previously the base
      component was discarded on every tick).

    - `docs/quickstart.md` §4 updated: the first
      user-facing code example now uses
      `LiteLLMToolWorker` + `WorkerManager.invoke(
      "chat_llm", ...)` (the new canonical pattern
      from ADR-043) instead of the deprecated
      `LiteLLMTool` direct call. A note about the
      deprecation and the migration target (v0.9.0)
      is included.

    - CHANGELOG `[0.8.0] ### Known issues` cleared:
      the three stale items (Example 05b WIP,
      synchronous Role LLM calls, TTL-based
      eviction) have all been delivered (DEBT
      §2.18, §2.20, §2.21) and now live in
      `[Unreleased]`. The section is kept as a
      pointer, not a TODO list.
- **Slot GC for the TTL sweeper (ADR-045 follow-up;
  DEBT §2.21):** the `ReactiveDispatcher` now
  closes the memory leak in the tool-call slot. The
  legacy code path had two structural issues that
  prevented the orphan request from being evicted
  in the same tick the sweeper detected it:

    - The `dispatch_once` short-circuit
      (`if not new_events: return 0`) skipped the
      systems pipeline on ticks where the EventLog
      had no new events. The TTL sweeper emits its
      `tool.<name>.failed` events in those very
      ticks (the orphan request was emitted several
      ticks earlier; the current tick has no
      activity on the log).
    - The first overlay pass
      (`_fold_with_filter`) runs BEFORE the systems
      and never sees the `failed` events. The
      completion-driven eviction rule in
      `overlay_tool_calls` only fires when the
      matching completion lands in a next tick's
      batch.

  The fix:

    - **Systems run on every tick.** The
      `dispatch_once` short-circuit is replaced
      with a no-op fold + full systems pipeline;
      the cursor advances only when the EventLog
      has new events.
    - **Post-systems re-fold
      (`_fold_with_systems`).** The
      `overlay_tool_calls` projection is re-applied
      with the system-emitted events as input. The
      `tool.<name>.failed` event joins the slot as
      a completion, and the completion-driven
      eviction rule removes the orphan request in
      the same tick.
    - **Re-fold is opt-in.** `_fold_with_systems`
      short-circuits when `system_events` has no
      `tool.*` event, so a non-tool batch pays zero
      for the second pass (ADR-044 §2.4 "no
      allocation for non-tool batches"
      optimisation preserved).

  6 new unit tests in
  `tests/unit/runner/test_reactive_dispatcher_ttl_gc.py`
  cover: orphan eviction in the same tick, fresh
  request preserved, opt-out path (no sweeper = no
  GC), cheap non-tool batches, no GC when systems
  emit nothing, and router fan-out of the
  TTL-failure event. 1811 unit tests pass (+6 vs
  the 1805 baseline).
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
- **Examples 01-07 cleanup (ADR-043 + ADR-039
  follow-up; DEBT §2.17 + §2.20):**

    - `examples/01_llm_basic.py` migrated to
      `LiteLLMToolWorker`: one worker instance +
      `await worker.invoke(system=..., user=...,
      idempotency_key=...)` (the new canonical
      pattern). The call signature is the same; the
      return envelope is now a JSON-serialisable
      `dict` (the same shape the `WorkerManager`
      consumes in the production path; the example
      calls the worker directly without the
      `WorkerManager` infrastructure because the
      example is a one-shot script).
    - `examples/02_llm_with_rate_limit.py` removed:
      the `LiteLLMToolWorker` does not own a
      `rate_limiter` / `cost_budget` (those were
      `LiteLLMTool` Tool-class concerns; the worker
      is a stateless callable that runs in a
      process pool).
    - `examples/03_role_usage.py` removed: the
      concept of a `Role` as a synchronous wrapper
      around `LiteLLMTool` was superseded by the
      ECS path (ADR-039 + ADR-044):
      `ChatRoleSystem` / `PlannerRoleSystem` /
      `SummarizerRoleSystem` /
      `PersonalizedRoleSystem` in
      `src/kntgraph/agents/role_systems/`.
    - `examples/04_reactive_system_with_llm.py`
      removed: the canonical reactive + LLM
      example is `examples/05b_session_chat_ecs.py`
      and `examples/05c_session_chat_ecs_roles.py`.
    - `examples/05_session_chat.py` removed: the
      legacy session chat pattern was the basis
      of the 05b shim (DEBT §2.18 closed). The
      canonical session chat example is 05b/05c.
    - `examples/06_profile_preferences.py` removed:
      the legacy `PersonalizedRole` was ported to
      `PersonalizedRoleSystem` (DEBT §2.20); the
      canonical example is 05c.
    - `examples/07_caching_transport.py` removed:
      the `CachingLLMTransport` decorator is still
      supported (unchanged in
      `agents/tools/cache.py`) but the example
      is no longer a `LiteLLMTool` example; a
      custom-transport snippet in the docs is
      a better place for that pattern.

  2 new unit tests in
  `tests/unit/examples/test_example_01_migration.py`
  cover the source-level migration (no
  `LiteLLMTool` import) and the runtime contract
  (transport called once; `idempotency_key`
  matches the example's stable prefix). 1813 tests
  pass (+2 vs the 1811 baseline).

- **Deprecation removal: `LiteLLMTool`,
  `ToolInvoker`, `kntgraph.agents.roles` (v0.9.0
  breaking change).**

    The deprecated Tool path was removed in
    v0.9.0:

      - **`LiteLLMTool`** (the legacy
        ``Tool``-Protocol wrapper around LiteLLM)
        was REMOVED. The canonical path is
        ``LiteLLMToolWorker``
        (``@tool_worker(name="chat_llm")``,
        ADR-043). The worker runs in the
        ``WorkerManager``'s
        ``ProcessPoolExecutor`; the dispatcher's
        event loop is no longer blocked by the
        LLM call.
      - **`ToolInvoker`** (the legacy
        ``EventLog``-driven orchestrator) was
        REMOVED. The canonical orchestration
        path is the ``WorkerManager`` consuming
        ``tool.<name>.requested`` events from
        a Redis stream; the ``@tool_worker``
        decorator handles the worker-class
        registration, schema extraction, and
        cross-tick correlation via
        ``causation_id`` (= the
        ``request_event_id``).
      - **`kntgraph.agents.roles`** package
        (containing ``ChatRole`` / ``PlannerRole``
        / ``SummarizerRole`` / ``PersonalizedRole``
        / ``SemanticRoutingRole`` /
        ``IntentResolutionSystem`` /
        ``RoleComponent`` / ``IntentComponent``)
        was REMOVED. The canonical
        ECS-shaped replacements live in
        ``src/kntgraph/agents/role_systems/``:
        ``ChatRoleSystem`` / ``PlannerRoleSystem``
        / ``SummarizerRoleSystem`` /
        ``PersonalizedRoleSystem``.
      - **`agents/tools/cache/`** package
        (containing ``CachingLLMTransport`` and
        Redis/in-memory cache adapters) was
        REMOVED. The caching transport was a
        ``LiteLLMTool``-specific decorator; a
        future iteration can add a similar
        transport-agnostic cache adapter if
        needed.
      - **`agents/tools/llm_transport.py`**
        shim was REMOVED. The canonical path
        is ``kntgraph.tools.llm_transport``.

    **Prompt extraction**: the ``SYSTEM_PROMPT``
    constants, the Pydantic output schemas
    (``ChatReply`` / ``Plan`` / ``Summary``), the
    ``format_chat_history`` helper, and the
    ``build_personalized_system_prompt`` helper
    were extracted from the legacy roles into
    ``src/kntgraph/agents/role_systems/_prompts.py``
    so the prompt engineering lives in one place
    and the role systems have a single source of
    truth.

    **CLI template** (``src/kntgraph/cli/templates/dispatcher.py.jinja``)
    was updated: the legacy
    ``IntentResolutionSystem(registry)`` reference
    in the generated ``build_<context>_dispatcher``
    was replaced with an empty ``systems = []``
    placeholder (per-role ``WorldSystem``
    instances are wired by the context's agents).

    **Examples removed** (3):
    ``examples/11_tool_invoker.py``
    (ToolInvoker end-to-end demo),
    ``examples/12_semantic_routing.py``
    (semantic routing + arg extraction demo,
    used the legacy ``ToolInvoker`` +
    ``SemanticRoutingRole``),
    ``examples/13_multi_agent.py`` (multi-agent
    approval flow, used the legacy
    ``IntentResolutionSystem``).

    **Tests removed** (16 files, ~6k lines):
    ``tests/agents/unit/roles/`` (9 files:
    ``test_base``, ``test_chat``,
    ``test_deprecation``, ``test_parsing``,
    ``test_personalized``, ``test_planner``,
    ``test_resolution``, ``test_semantic_router``,
    ``test_summarizer``),
    ``tests/agents/unit/tools/test_llm.py``,
    ``tests/agents/unit/tools/test_llm_settings.py``,
    ``tests/agents/unit/tools/test_cache.py``,
    ``tests/agents/unit/tools/test_redis_cache_adapter.py``,
    ``tests/agents/unit/tools/test_invoker_helpers.py``,
    ``tests/unit/tools/test_invoker*.py`` (3),
    ``tests/integration/tools/test_invoker.py``,
    ``tests/integration/tools/test_litellm_transport.py``.

    **1566 tests pass** (vs the 1813 baseline
    = 247 fewer; net change is the 247 legacy
    tests removed minus the 0 new tests; the
    role_systems and ttl_sweeper tests survive
    and run green).

- **Build cleanup + AGENTS.md scaffold (DEBT
  §2.22).**

    - **`build/` artifact removed.** The 2 MB
      `build/` directory (a stale
      `python -m build` artifact) was deleted
      from the repo. `build/` is already in
      `.gitignore` (line 11); the directory
      was not tracked by git, but the on-disk
      presence was noise. Future builds will
      land in the same path; the gitignore
      entry keeps them out of tracking.
    - **`scratch_replace_redis_url.py`** and
      **`scratch_run_all.py`** removed from
      git tracking (`git rm --cached`; the
      on-disk files remain). Two one-off
      debug helpers that were historically
      versioned but are not part of the
      production code. New scratch scripts
      should live in `scripts/` (or
      `/tmp/opencode/`) so the `__init__.py`
      layout and the gate's test discovery
      stay clean.
    - **`AGENTS.md` created** (at the repo
      root). The conventions document
      referenced by the test docstrings
      (`AGENTS.md §1`, `§2`, `§6`, `§7`,
      `§9`, etc) was missing — the
      conventions lived implicitly in
      `CONTRIBUTING.md` and the tests'
      docstrings, but the single source of
      truth file did not exist. The new
      `AGENTS.md` is the canonical reference:
      type discipline (`Any` / `object`
      exceptions), no-compat-shims (removal-
      target contract), 500-line file
      guideline, typed errors (`Result[T, E]`
      + typed `*Error`), behaviour tests, the
      single CI gate (the 9-step
      `scripts/ci.py`), prose language
      (English), branch policy (AI agents do
      not push or create branches), and the
      env vars + local-services reference.

- **REUSE 3.3 license compliance cleanup (DEBT
  §2.23).**

    - **`reuse` gate added to `scripts/ci.py`.**
      `step_reuse()` was defined in
      `scripts/ci.py` but missing from the
      `ALL_STEPS` dict; the gate was
      effectively a no-op before this
      cleanup. The dict now registers
      `"reuse": step_reuse()` between
      `complexity` and `pyright`; the
      `--only reuse` flag now works in
      isolation for local iteration. The
      `AGENTS.md` and `CONTRIBUTING.md`
      documentation was updated to reflect
      the 9-step gate (the `CONTRIBUTING.md`
      table was out of date — it listed 8
      steps; the new table has 9 with
      `reuse` between `complexity` and
      `pyright`).
    - **Invalid SPDX expression** in
      `scripts/quality_report.py` fixed: the
      `render_markdown` function embedded a
      markdown template string that REUSE
      parsed as an invalid license
      expression (the literal "SPDX-License-
      Identifier: Apache-2.0" with the
      trailing Python comma). Fixed by
      wrapping the template's SPDX header in
      `REUSE-IgnoreStart` / `REUSE-IgnoreEnd`
      comments.
    - **Missing SPDX headers** added to 55
      files: `CHANGELOG.md`, 8 ADRs
      (ADR-038 through ADR-045), 3 docs,
      3 `dev-servers/` files (2
      docker-compose YAML + 1 redis.conf),
      9 `examples/` files (the 2 missing
      examples 18/20 plus 7
      `knt-cli/weather_platform` files
      including a `pyproject.toml`,
      `.env.example`, and `uv.lock`), 6
      `src/kntgraph/cli/` files, 9
      `cli/templates/` Jinja files (using
      `{# ... #}` Jinja comments), 1
      `scripts/export_kntgraph.py`, 1
      `tests/agents/unit/conftest.py`, 7
      `tests/unit/cli/test_*.py`,
      `.gitignore`, the top-level `uv.lock`,
      and the 2 `scratch_*.py` debug
      helpers.
    - **Verification**: 521 / 522 files
      compliant (was 466 / 522); the
      `scripts/ci.py --only reuse` gate
      now passes; the full suite (1566
      tests) is unchanged; `ruff check`
      and `ruff format --check` are clean.

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


## [0.10.0] — 2026-07-30

### Removed (Breaking)
- **`_legacy_principal` fallback in `RedisAPIKeyVerifier`
  (ADR-017 §7.3).** The pre-ADR-017 plain-string
  binding format (e.g. `b"tenant-A.agent-1"` stored
  under `knt:api:keys:<sha256>`) is no longer accepted.
  Such bindings are now rejected as
  `AuthError(kind="malformed", message=...)` with a
  remediation hint pointing to
  `scripts/migrate_principals.py --apply`.

  **Migration**: operators with pre-ADR-017 plain-string
  bindings MUST run the migration script before
  upgrading to 0.10.0:

      # Dry-run (prints what would change; safe)
      python scripts/migrate_principals.py

      # Apply (writes the JSON Principal form to Redis)
      python scripts/migrate_principals.py --apply

  The script delegates to `Principal.from_agent_id`
  for the single-tenant derivation (no duplicated
  heuristic). Idempotent; running twice is a no-op.

  **Affected code**:
  - `src/kntgraph/api/_auth/_helpers.py`: `_legacy_principal`
    removed (only `_digest` / `_decode` remain).
  - `src/kntgraph/api/_auth/_verifier.py`: fallback
    replaced by `Err(AuthError("malformed", ...))`.
  - `src/kntgraph/api/auth/__init__.py`: re-export
    removed; `__all__` trimmed.
  - `scripts/migrate_principals.py`: refactored to
    use `Principal.from_agent_id` as the single
    source of truth for tenant derivation.
  - `tests/unit/security/test_rbac.py`: legacy
    helper tests deleted; new
    `test_redis_verifier_rejects_legacy_string`
    documents the new (reject) behaviour.

  **KNT_AUTH_MODE flag**: was never implemented (the
  flag is aspirational per ADR-017 §2.4 / §7.1; no
  operator could depend on it). The wire-format
  detection in `_verifier.py:155-180` is the only
  path that distinguishes JSON from legacy. Closing
  this removal also retires that pretense: from 0.10.0
  onward, "legacy" is unambiguously the wire-format
  fallback (now removed).

### Added
- **Zero Token Architecture support (ADR-049).**
  Two new ECS-shaped systems compose with the
  `ReactiveDispatcher` to short-circuit LLM calls
  when deterministic answers are available.
  - `RuleBasedChatSystem`
    (`src/kntgraph/agents/role_systems/_rule_based.py`):
    matches per-tenant rules (`tenant_id`,
    `persona_pattern`, `message_pattern`, `response`,
    `priority`) against `user.intent` events and emits
    `chat.reply.generated` directly. Wire format is
    identical to `ChatRoleSystem` (the LLM path), so
    downstream consumers cannot tell the difference.
    Rules can be loaded from YAML via
    `register_from_yaml` (sample at
    `examples/_data/zta_rules.yaml`).
  - `SolutionLookupSystem`
    (`src/kntgraph/agents/memory/solution_lookup.py`):
    read-side cache for the Solution tier (ADR-010).
    Walks the world's `tool_requests` slot; on a
    `(tool_name, params_fingerprint)` hit with
    `confidence >= min_confidence` and a tool in the
    allowlist, synthesises a `tool.<name>.completed`
    event with the cached payload. Pluggable via the
    `SolutionStoreLike` Protocol — two shipped
    implementations: `InMemorySolutionStore` (tests
    + example `09b`) and `RedisSolutionStore`
    (production, this release).
  - `RedisSolutionStore`
    (`src/kntgraph/infra/redis/_memory/_solution.py`):
    Hash-backed Redis adapter for the Solution cache.
    One Hash per tool (`knt:solution:<tool_name>`);
    field = `params_fingerprint`; value = JSON
    `CachedSolution`. Built-in TTL knob
    (`Settings.solution_ttl_seconds`). Fail-open on
    Redis errors (the read side degrades to a miss so
    the LLM fallback takes over). Factory:
    `create_solution_storage(...)`. Reaches
    `settings.solution_ttl_seconds` (default `None` =
    no TTL; Solutions are explicitly invalidated by
    the operator).
- **ReactiveDispatcher drain fix (ADR-049 §2.1)**: the
  dispatcher now invokes every registered system on
  every tick when a system has unconsumed
  ``_pending_results`` (the lookup system's contract
  is "the next ``__call__`` returns the queued
  completions from the previous tick's
  ``run_pending_lookups``"). Without this fix the
  synthetic completion never landed in the EventLog
  on idle ticks. Regression test:
  `tests/unit/runner/test_reactive_dispatcher_drain.py`.
- **Role systems refactor**: `_BaseRoleSystem` +
  `_emit_chat_completion` extracted to
  `src/kntgraph/agents/role_systems/_base.py` so the
  rule-based and LLM paths can share the request /
  completion wire format without circular imports.
- **`docs/zta.md`** maps the four ZTA principles to
  kntgraph components and walks the hybrid dispatcher
  pattern.
- **`examples/09b_solution_lookup_zta.py`** is the
  end-to-end reference (in-memory store): rule-based
  hit, two solution cache hits, and one miss. No LLM
  is called.
- **`examples/09c_solution_lookup_zta_redis.py`** is
  the Redis-backed variant. Seeds the
  `RedisSolutionStore` with two Solutions and walks
  the same dispatcher path; surfaces the
  `tool.<name>.completed` events AND the operator-
  side cache audit (`iter_keys` / `read_all`).

### Changed
- **CLI command consistency (ADR-050).** Three
  mechanical improvements to the `knt` CLI:
  - `init` is now a sub-Typer with a `project` sub-
    command (`knt init project <name>`). The
    pre-ADR-050 flat form (`knt init <name>`) is
    removed in this cycle (no deprecation shim —
    see ADR-050 "Deprecation note"). The full
    surface is uniform: every top-level command is
    a namespace (`init project`, `new <artifact>`,
    `keys generate`).
  - `--routing-mode` is a Typer `Enum` (`external` /
    `autonomous` / `collaborate`). The imperative
    `valid_modes` set + hand-written error message
    are gone; Typer auto-lists the valid choices in
    `--help` and rejects unknown values.
  - `_templates.render_template(name, ctx)` helper
    in `src/kntgraph/cli/_templates.py`. Replaces
    7 sites where `Environment(loader=...)` was
    instantiated per command. I/O stays at the
    call site (the helper is pure).

### Removed (Breaking)
- **`knt init <name>` flat form.** The pre-ADR-050
  command shape is removed. Use
  `knt init project <name>` instead. The
  `README.md` and `docs/cli_guide.md` examples are
  updated. Typer prints `No such command 'foo'`
  when the old form is used. The change is the
  deliberate outcome of ADR-050 §"Deprecation note"

### Added (ADR-051, PR 1 + PR 2)
- **Release versioning via git tags + `setuptools_scm`**
  (ADR-051). The project's version is now the
  **git tag** (``vX.Y.Z``, PEP 440). The
  `pyproject.toml::version` field is removed; the
  ``[project]`` table has ``dynamic = ["version"]``
  and the ``[tool.setuptools_scm]`` table writes
  ``src/kntgraph/_version.py`` at install time.
  ``kntgraph.__version__`` is exposed from the
  generated file (with a ``"0.0.0+unknown"``
  fallback for source installs without
  ``setuptools_scm``).
- **CI step `check_version` (10th gate).** Fails
  the build when the installed version is older
  than the latest tag. Run ``uv sync`` to refresh
  after a new tag.
- **CI step `bump_dry_run` (11th gate).** Asserts
  the bump-version logic is sane by computing
  (but not creating) the next major version.
- **``scripts/bump_version.py``**. Reads the
  current tag, computes the next version per
  ``--level {major,minor,patch}``, and creates
  the tag locally with ``git tag -a vX.Y.Z -m
  "Release vX.Y.Z"``. Idempotent (refuses to
  recreate an existing tag). ``--dry-run`` mode
  prints the next version without touching the
  git history.
- **Retroactive tags** for ``v0.7.0``, ``v0.8.0``,
  ``v0.10.0`` so ``git log v0.8.0..v0.10.0`` and
  ``uv sync`` work as expected before the next
  release. The ``0.9.0`` release was never
  documented in ``CHANGELOG.md`` and has no tag.
- **``CONTRIBUTING.md::Release checklist``** is
  the canonical 6-step ritual for cutting a
  release. PyPI publishing remains out of scope
  (ADR-052).
- **``.github/workflows/release.yml`** automates
  the release: ``workflow_dispatch`` with a
  ``level`` input (major / minor / patch); the
  workflow runs ``bump_version.py`` (dry-run
  first as a guard), ``changelog_release.py``
  (moves `[Unreleased]` to a dated section),
  commits, pushes the tag, and opens the GitHub
  Release with the dated section as the body.
  Manual trigger only (no auto-on-PR); the
  operator retains control of the release
  cadence.
- **``scripts/update_version_badge.py``** keeps
  the README version badge in sync with
  ``kntgraph.__version__`` (derived from the
  git tag). The badge is a shields.io image
  with the format
  ``![Version](https://img.shields.io/badge/version-X.Y-Z-blue)``;
  the ``+g<sha>`` and ``.devN`` suffixes that
  ``setuptools_scm`` adds when the working tree
  is past the latest tag are stripped so the
  badge always shows a clean semver triple.

### Fixed
- **``scripts/quality_report.py`` reads the
  version from ``kntgraph.__version__``** (the
  ADR-051 source of truth), not from
  ``pyproject.toml::version`` (which was removed
  in ADR-051 PR 1). The new ``get_version()``
  helper reads the live import; the new
  ``_format_version_for_report()`` strips the
  ``.devN+g<sha>`` suffix so the
  ``docs/quality.md`` header shows a clean
  ``X.Y.Z``. 4 new tests cover the contract.

### Added (ADR-052, draft)
- **ADR-052: PyPI publishing via Trusted
  Publishing (PEP 740).** Drafted (Status:
  Proposed). The ADR covers: package name
  (``kntgraph``, currently unregistered on
  PyPI); first release (no retroactive
  publishing — the next release is the first
  PyPI release); workflow split (the original
  draft had a single ``release.yml`` with a
  ``publish: yes|no`` input; the implementation
  decision was to split into two workflows
  because PyPI's Trusted Publisher binding is
  per-workflow and the two responsibilities have
  different blast radii and re-rodability).
  - ``.github/workflows/release.yml`` cuts the
    tag and opens the GitHub Release
    (unchanged from ADR-051 PR 4).
  - ``.github/workflows/publish.yml`` is the
    new workflow: ``workflow_dispatch`` with a
    ``tag`` input; the operator passes an
    existing tag (``v0.11.0``); the workflow
    builds the wheel from the checked-out tag,
    sanity-checks ``__version__`` against the
    tag, runs ``uv build --wheel``, and uploads
    via ``pypa/gh-action-pypi-publish``. The
    ``pypi`` GitHub Environment (with
    "required reviewers") is the human gate.
  - 13 contract tests in
    ``tests/scripts/test_workflow_split.py``
    enforce the split (``release.yml`` contains
    no PyPI action; ``publish.yml`` contains no
    tag-cut step; the two workflows are
    disjoint).
  (the shim was cut from scope because the
  Typer/sub-Typer interaction does not allow a
  flat command and a sub-Typer to share the same
  name).

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
- None at release. The three items previously listed
  here (Example 05b shim, synchronous Role LLM calls,
  TTL-based eviction) have all been resolved in the
  `[Unreleased]` section above: 05b shim
  (DEBT §2.18 closed), Role → ECS migration
  (DEBT §2.20 closed; `role_systems` module), and
  TTL-based eviction (DEBT §2.21 closed;
  `ToolCallTTLSweeperSystem`).

### Deprecated
- **`kntgraph.agents.roles` package (ADR-041):**
  - The `ChatRole`, `PlannerRole`, `SummarizerRole`, `PersonalizedRole`, and `SemanticRoutingRole` classes are deprecated. They have been superseded by the pure-ECS architecture from [ADR-039](file:///home/adriano/Projects/kinetgraph/kinetgraph/ADRs/ADR-039-Role-rethinking-and-intentions-routing.md) (`RoleComponent` + `IntentResolutionSystem`).
  - Importing `kntgraph.agents.roles` emits a `DeprecationWarning` since v0.8.0. The package will be removed in v1.0.0 (target: 2026 Q4).
  - The new components (`RoleComponent`, `IntentComponent`, `IntentResolutionSystem`) remain importable from the same package through v0.9 to ease the migration.
  - See [ADR-041](file:///home/adriano/Projects/kinetgraph/kinetgraph/ADRs/ADR-041-agents-roles-deprecation.md) for the migration guide and removal schedule.
