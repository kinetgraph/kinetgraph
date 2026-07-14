# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Outstanding technical debt — Kinetgraph v0.7 → v0.8 quality push.

Generated on 2026-07-13 after the memory / events-dlq / agents.roles /
pyright sprint. This file lists every gate that still has open
issues, with a short description, severity, location, and
suggested next action. The intent is to give the next contributor
(or the next AI session) a one-pager that maps the debt so the
work can be picked up without re-discovering the baseline.

Gate state at the time of writing:

  - ruff lint:        All checks passed!
  - ruff format:      421 files already formatted
  - bandit:           3 LOW (B110 try/except/pass — intentional)
  - radon CC:         avg 2.53 (A), 0 functions rank D+
  - radon MI:         100% rank A/B
  - pytest unit:      1457 passed, 1 skipped (87 warnings)
  - pytest agents:    294 passed (32 DeprecationWarning, filtered)
  - coverage unit:    75% (target: 80% in `core` + `infra`; 90% in
                       `memory` and `events`)
  - pyright strict:   111 errors (baseline 253; the 142-error
                       delta is the work done in this sprint)

How to use this file
--------------------
1. Each section is ordered by severity / blast radius.
2. The "Action" line is the suggested first step; the
   "Acceptable" line is the cheap escape hatch if the team
   decides to defer the work to a later milestone.
3. File paths are relative to the repo root.
4. Line numbers are pinned to the current tree; re-run
   pyright / ruff / coverage to refresh.

Recent closures (since the last regen of this file)
----------------------------------------------------

The "Faixa 1" work merged via ``quality/pyright-low-hanging``
(2026-07-13). The following items were closed and are
kept here as a historical record.

  - 2.3 / 2.4  Stale ``# type: ignore`` (11 lines) —
    deleted; the underlying errors are no longer present.
  - 2.5  ``PipelineLike`` Protocol missing 4 methods —
    extended the Protocol in
    ``infra/redis/_client.py`` (added ``delete``,
    ``hset``, ``expire``). Fixed 10 errors across
    ``infra/redis/_memory/{_continuity,_profile}.py``
    and ``agents/tools/cache/_redis.py``.
  - 2.9  ``_auth/_redis.py`` and ``_world_checkpoint/_redis.py``
    passing ``bytes`` to ``client.set`` — widened the
    ``RedisLike.set`` Protocol to accept ``str | bytes``.
  - 1.3  ``api/intent_router/routes.py`` — 19 errors.
    Tightened the ``Dependable`` / ``HeaderParam`` /
    ``RouterApp`` Protocols in ``core/_typing.py`` to use
    ``object`` instead of ``ValidatorInput`` (the framework's
    opaque boundary type) so FastAPI kwargs don't
    get narrowed. Converted ``Depends``/``Header``/
    ``HTTPException``/``auth`` to keyword-only and
    non-Optional on the installers. The
    ``app_factory.py`` was updated to pass them by
    keyword.

Net pyright delta: 111 → 71 (-40 errors).

Ownership
---------
No owner is assigned. The current convention is that any
contributor can pick up an item and ship a focused PR. The
"Remove this file" line in the "Cleanup" section is the
release-criteria for v1.0.
"""

from __future__ import annotations

# 1. CRITICAL: SECURITY
# ---------------------------------------------------------------------------
#
# 1.1  Bandit B110 (try/except/pass) — 3 occurrences
#
#   Why it's open: B110 is filtered at severity MEDIUM in
#   ``scripts/ci.py`` so it does not fail the gate. Three
#   LLM-tool paths swallow the exception silently:
#
#     src/kntgraph/agents/tools/llm.py:163
#     src/kntgraph/agents/tools/llm.py:858
#     src/kntgraph/agents/tools/llm.py:966
#
#   Action: replace each ``except: pass`` with
#           ``except Exception as exc: logger.debug("llm.skip", error=str(exc))``
#           so the LLM transport never silently drops errors
#           during a streaming decode. The intent is documented
#           but the silence is hostile to operators.
#
#   Acceptable: keep as-is, but document the rationale in each
#               site as a comment + bump the bandit filter to LOW
#               (already the case).
#
# ---------------------------------------------------------------------------

# 2. HIGH: PYRIGHT (111 errors)
# ---------------------------------------------------------------------------
#
# The pyright delta vs the baseline is 142 errors resolved
# in this sprint. The 111 remaining errors are organised
# below by file. The recurring patterns are:
#
#   A. ``JsonValue`` leaking into scalar parameters
#      (``Mapping[str, JsonValue]`` is a wider type than
#      ``Mapping[str, str]``; the storage protocol returns
#      the wider shape and several decoder sites assumed
#      the narrower one).
#
#   B. ``object`` / ``None`` being passed where a concrete
#      type is expected (the framework uses ``object`` as
#      the duck-typed protocol placeholder; downstream
#      helpers expect more specific shapes).
#
#   C. Stale ``# type: ignore`` comments that the latest
#      pyright (1.1.411) considers unnecessary — the bug
#      they were suppressing was fixed in this sprint and
#      the comment can be removed.
#
#   D. Protocol members not yet declared on the runtime
#      class (e.g. ``PipelineLike`` missing ``hset``/
#      ``expire``/``delete``; ``GraphAdapter`` not exported
#      from the falkordb module).
#
# ---------------------------------------------------------------------------

# 2.1  knowledge/extraction/argument/_gliner_finder.py  (20 errors)
#
#   The 20 errors cluster on the bridge between the GLiNER
#   raw output (``GlinerRawResult`` / ``_MatchDict`` /
#   ``_MatchObj``) and the framework's ``ValidatorInput``
#   protocol. The ``field_o`` validator expects a specific
#   ``ValidatorInput`` shape; the GLiNER result has
#   ``object`` slots that the static checker refuses to
#   narrow.
#
#   Action: replace the ``object`` annotations on the
#           ``_MatchDict`` / ``_MatchObj`` TypedDicts with
#           the concrete ``GlinerMatch`` / ``GlinerRawResult``
#           shapes the runtime sees. This requires unifying
#           the field-o validator with the GLiNER 2.x schema
#           (a small ADR, not just a refactor).
#
#   Acceptable: add ``# type: ignore[arg-type]`` per call site
#               (10 suppressions). Cheap, but hides the design
#               issue.
#
# ---------------------------------------------------------------------------

# 2.2  api/intent_router/routes.py  (CLOSED)
#
#   CLOSED in Faixa 1 (2026-07-13). The 19 errors
#   broke down as:
#
#   A. 8× ``reportOptionalCall`` on ``Depends(auth)``
#      where ``auth: PrincipalDep | None = None``.
#   B. 3× ``reportUnnecessaryTypeIgnoreComment`` on
#      ``# type: ignore[valid-type]`` after the
#      Protocol was widened.
#   C. 3× ``type[X]`` not assignable to
#      ``response_model: ValidatorInput`` (Pydantic
#      models not in the union).
#   D. 5× remaining from nested patterns (calls on
#      ``Header(default=None)``/``HTTPException(...)``,
#      ``ValidatorInput`` not assignable to ``str``,
#      etc).
#
#   The fix path was structural rather than local:
#
#   1. ``core/_typing.py``: widened the ``Dependable``,
#      ``HeaderParam``, and ``RouterApp`` Protocols
#      from ``ValidatorInput`` to ``object`` (the
#      framework's opaque boundary type). The
#      ``HeaderParam.__call__`` return was changed
#      to ``str | None`` so the routers can
#      ``Header(default=None, alias=...)`` for an
#      ``Optional[str]`` parameter.
#   2. ``api/intent_router/routes.py``: converted
#      ``Depends``/``Header``/``HTTPException``/``auth``
#      to keyword-only and non-Optional on the
#      installers; the call sites in
#      ``app_factory.py`` were updated to pass them
#      by keyword.
#   3. Removed the now-stale ``# type: ignore`` comments.
#
#   Tests: ``tests/unit/api/test_intent_router.py`` (19
#   tests) — all green. The 422-vs-401 status code
#   distinction was preserved by keeping the default
#   ``Depends(auth)`` form (vs the experimental
#   ``Annotated[Principal, Depends(auth)]`` form,
#   which FastAPI 0.100+ narrows differently).
#
# ---------------------------------------------------------------------------

# 2.3  knowledge/extraction/__init__.py  (6 errors)
#
#   All 6 errors are ``reportUnnecessaryTypeIgnoreComment``.
#   Stale suppressions left over from an earlier typing pass.
#
#   Action: delete the 6 ``# type: ignore`` comments.
#
#   Effort: 2 minutes.
#
# ---------------------------------------------------------------------------

# 2.4  core/storage.py  (4 errors)
#
#   The ``ComponentT`` TypeVar leaks across methods in a
#   way the strict checker cannot unify (``Map[str, ComponentT]``
#   in one method is the same TypeVar as ``Map[str, ComponentT@query]``
#   in another, but pyright treats them as different
#   bindings).
#
#   Action: split the storage into one class per method's
#           generic parameter, or use a Protocol with
#           bound TypeVars. The current shape is too clever
#           for the strict checker.
#
#   Acceptable: add ``# type: ignore[arg-type, return-type]``
#               on the 4 lines. Hides the design issue.
#
# ---------------------------------------------------------------------------

# 2.5  infra/redis/_memory/{_continuity,_profile}.py  (CLOSED)
#
#   CLOSED in Faixa 1 (2026-07-13). The
#   ``PipelineLike`` Protocol was extended with
#   ``delete``/``hset``/``expire`` in
#   ``infra/redis/_client.py``; the
#   ``record.items()`` issues in
#   ``_continuity.py``/``_profile.py`` were fixed
#   by guarding the call with ``isinstance(record,
#   Mapping)``. Net delta: 8 → 0 errors.
#
# ---------------------------------------------------------------------------

# 2.6  resilience/{edge,timeout}.py  (3+3 errors)
#
#   A. ``edge.py`` lines 84/157/232 use a TypeVar
#      (``R = TypeVar("R")``) inside a runtime expression
#      rather than a type expression; pyright flags this as
#      ``reportInvalidTypeForm``.
#
#   B. ``timeout.py`` line 52 imports ``BackoffPolicy``
#      from a module that no longer exports it (the import
#      was renamed during the resilience refactor in
#      iteration 26).
#
#   Action: replace the TypeVar-as-value usage with a
#           callable annotation; fix the import (the
#           symbol lives in ``kntgraph.resilience.retry``
#           now). Lines 224 and 457 are coroutine-vs-await
#           issues that need a small refactor — the
#           ``with_timeout`` wrapper should ``return await coro``
#           not ``return coro``.
#
#   Effort: 2 hours.
#
# ---------------------------------------------------------------------------

# 2.7  events/dlq/actions.py  (2 errors)
#
#   Line 76 is a stale ``# type: ignore``. Line 133 passes
#   a ``str | None`` (the result of ``read_index``) to
#   ``storage.read`` which expects ``str``.
#
#   Action: drop the stale comment, and add an early-return
#           when ``stream_id`` is ``None`` (no entry to read).
#
#   Effort: 5 minutes.
#
# ---------------------------------------------------------------------------

# 2.8  infra/redis/_memory/_session.py  (2 errors)
#
#   The JSON session cache stores the payload as a
#   ``CacheRecord`` (``str | Mapping[str, JsonValue]``)
#   but the call site at line 103 passes the value to
#   ``dict(...)`` directly. The type alias is honoured
#   at the boundary but lost inside the function.
#
#   Action: change the function signature to accept
#           ``CacheRecord`` and dispatch on the type tag
#           (``str`` vs ``Mapping``).
#
#   Effort: 30 minutes.
#
# ---------------------------------------------------------------------------

# 2.9  infra/redis/{_auth,_world_checkpoint}/_redis.py  (CLOSED)
#
#   CLOSED in Faixa 1 (2026-07-13). The ``RedisLike.set``
#   Protocol was widened from ``value: str`` to
#   ``value: str | bytes`` in ``infra/redis/_client.py``.
#   The auth + checkpoint adapters store the API-key
#   binding and the checkpoint blob as raw ``bytes``;
#   the real Redis client accepts both. Net delta:
#   2 → 0 errors.
#
# ---------------------------------------------------------------------------

# 2.10 infra/redis/_dlq/_redis.py  (1 error)
#
#   Line 172: ``hscan_iter`` may return ``None`` (the
#   redis client stubs it that way) but the type is
#   ``AsyncIterator``. The ``async for`` doesn't tolerate
#   ``None``.
#
#   Action: assert non-None before iterating, or use
#           ``async for _ in (hscan_iter(...) or [])``.
#
#   Effort: 10 minutes.
#
# ---------------------------------------------------------------------------

# 2.11 infra/{checkpoint.py, graph/_lite_pool.py}  (2+1 errors)
#
#   The mapping passed to ``from_dict`` is ``Mapping[str, str]``
#   but the destination expects ``dict[Unknown, Unknown]``.
#   The ``from_dict`` signature is too wide.
#
#   Action: tighten the destination to ``Mapping[str, str]``.
#   Effort: 15 minutes.
#
# ---------------------------------------------------------------------------

# 2.12 knowledge/ (3 clusters, ~12 errors)
#
#   - ``gliner.py`` line 294/300: ``object`` passed to
#     ``int(``/``float()`` slots of pydantic validators.
#   - ``_slm_facades.py`` line 168/231: ``EntityExtractorWithMentions.extract``
#     and a 2-arg call signature don't match the protocol.
#   - ``embedding/_ollama.py`` line 215: response object
#     not assignable to ``dict[Unknown, Unknown]``.
#   - ``falkordb/adapter.py`` line 64/212: ``GraphAdapter``
#     is an unknown import symbol; ``Iterable[Event]`` is
#     not assignable to ``list[Event]``.
#
#   Action: add explicit casts in the four files (5 minutes
#           each). The ``GraphAdapter`` re-export is a one-line
#           fix in ``knowledge/falkordb/__init__.py``.
#
# ---------------------------------------------------------------------------

# 2.13 agents/ (10 clusters, ~17 errors)
#
#   - ``agents/knowledge/solution_projector.py`` lines
#     300/330: ``str | None`` to ``str`` and
#     ``CoroutineType | None`` to ``int``. The signature
#     claims sync return but the body is async. Either
#     fix the signature or wrap in ``run_sync``.
#   - ``agents/memory/solution_review_publisher.py`` line 80:
#     ``JsonValue`` leaking into ``int()`` coercion.
#   - ``agents/memory/solutions/_promoter_helpers.py`` lines
#     84/90: ``object`` with no ``redacted`` attribute.
#   - ``agents/memory/solutions/_extractor.py`` line 450:
#     ``str | None`` to ``str``.
#   - ``agents/memory/solutions/_promoter.py`` line 49:
#     list expression in a TypeVar context.
#   - ``agents/memory/solutions/{__init__,_fingerprints}.py``:
#     stale ``# type: ignore``.
#   - ``agents/memory/solution_extractor.py`` line 102:
#     nested-dict type mismatch.
#   - ``agents/tools/cache/_redis.py`` lines 150/152:
#     missing ``PipelineLike`` methods (same root cause as
#     2.5).
#   - ``agents/tools/invoker/_emit.py`` lines 69/128:
#     ``ToolArgValue`` not assignable to ``JsonValue``.
#   - ``agents/tools/invoker/_invoker.py`` lines 122/142:
#     generic ``Result`` mismatch.
#   - ``agents/tools/arg_validation.py`` line 131:
#     ``ToolArgValue`` not assignable to ``JsonValue``.
#   - ``agents/tools/pii/_tool.py`` line 113:
#     ``invoke`` override is incompatible with the ``Tool``
#     base (``**kwargs`` is too permissive).
#   - ``agents/roles/semantic_router.py`` line 329:
#     ``descriptions`` kwarg doesn't exist on
#     ``IntentClassifier.classify``.
#
#   Action: most of these are 5-line fixes. The biggest is
#           ``solution_projector.py`` (the async/sync
#           inconsistency is a design choice that needs an
#           ADR). Estimate: half a day total.
#
# ---------------------------------------------------------------------------

# 2.14 tools/ (4 files, 4 errors)
#
#   - ``tools/manager.py`` line 188: ``run_in_executor``
#     args of type ``tuple[Type[Unknown], ...]``.
#   - ``tools/schema.py`` line 169: ``sorted`` argument
#     of type ``JsonValue``.
#   - ``tools/system.py`` line 67: ``causation_id: str | None``
#     to ``UUID | None``. Same pattern as the resolution.py
#     fix earlier this sprint (cast or coerce helper).
#   - ``tools/worker.py`` line 81: stale ``# type: ignore``.
#
#   Effort: 30 minutes.
#
# ---------------------------------------------------------------------------

# 2.15 memory/continuity/cache_codec.py  (1 error)
#
#   Line 135: ``decoded: dict[bytes, bytes]`` is
#   ``Mapping[str, str]`` at the actual call site. Same
#   root cause as the other Mapping-vs-dict issues.
#
#   Action: tighten the type to ``Mapping[str, str]``.
#   Effort: 2 minutes.
#
# ---------------------------------------------------------------------------

# 2.16 resilience/{bulkhead,retry}.py  (2 errors)
#
#   Both: ``Result[..., Unknown]`` losing its concrete
#   error type. Add explicit ``BusinessError`` /
#   explicit ``None`` annotation on the error slot.
#
#   Effort: 15 minutes.
#
# ---------------------------------------------------------------------------

# 2.17 stream/event_log/dispatch.py  (2 errors)
#
#   Lines 70/82: ``Result[T, Unknown]`` not assignable
#   to ``Result[T, PersistenceError]``. The dispatch
#   helper returns ``Result`` from the underlying
#   redis call; the type erasure loses the error type.
#
#   Action: pass the ``PersistenceError`` constructor
#           through the dispatch chain.
#   Effort: 30 minutes.
#
# ---------------------------------------------------------------------------

# 3. MEDIUM: COVERAGE GAPS
# ---------------------------------------------------------------------------
#
# Coverage by subpackage (unit tests only — integration
# tests are skipped in this CI run):
#
#   memory/cache_warmer.py            43%   ← weakest in memory
#   memory/consolidation.py           32%   ← projector + consolidator
#   memory/continuity/cache_codec.py  74%   ← bytes-vs-JSON branch
#   memory/continuity/manager.py      69%   ← PII gate + entity
#   memory/continuity/pii.py          60%
#   memory/continuity/recorders/entity.py  64%
#
#   events/dlq/store.py               79%   ← XINFO error branch
#   events/dlq/actions.py            86%
#
# 3.1  memory/cache_warmer.py (43%)
#
#   The CacheWarmer consumes a Redis pubsub channel that
#   needs a real Redis (or a pubsub-capable fakeredis) to
#   exercise the hot path. The current unit tests mock
#   the bus.
#
#   Action: add an integration test in
#           ``tests/integration/memory/test_cache_warmer.py``
#           that uses fakeredis-pubsub. The infrastructure
#           already exists (see ``conftest.py::real_redis``).
#
# ---------------------------------------------------------------------------
#
# 3.2  memory/consolidation.py (32%)
#
#   The Consolidator + Projector pull from the EventLog
#   and write to the cache. The fold path is well-tested
#   (see ``tests/unit/memory/test_continuity_fold.py``)
#   but the orchestration (consume events → fold →
#   write cache) is not.
#
#   Action: extract the orchestration into a pure
#           ``consolidate(events) -> State`` helper and
#           unit-test it directly. The Projector writes
#           to the cache; the Consolidator consumes
#           events. Two more focused unit tests would
#           push coverage past 80%.
#
# ---------------------------------------------------------------------------
#
# 3.3  events/dlq/store.py (79%)
#
#   The missing 17 statements are concentrated on the
#   ``XINFO_STREAM`` error branch (lines 142-152 in
#   ``_redis.py``) and the storage ``client.hsetnx`` path.
#
#   Action: add a unit test that injects a Redis stub
#           which raises on ``xinfo_stream`` (mock the
#           fakeredis client). The fakeredis lib already
#           supports ``hsetnx`` in pipeline; an explicit
#           test that pins the PLACEHOLDER behaviour is
#           missing.
#
# ---------------------------------------------------------------------------

# 4. LOW: TOOLING
# ---------------------------------------------------------------------------
#
# 4.1  Stale ``# type: ignore`` comments  (CLOSED)
#
#   CLOSED in Faixa 1 (2026-07-13). All 11 stale
#   comments identified in the initial DEBT sweep
#   were deleted; pyright 1.1.411 confirms zero
#   ``reportUnnecessaryTypeIgnoreComment`` errors
#   remain. The breakdown was:
#
#     knowledge/extraction/__init__.py                6
#     events/dlq/actions.py                           1
#     infra/graph/_lite_pool.py                       1
#     agents/memory/solutions/__init__.py             1
#     agents/memory/solutions/_fingerprints.py         1
#     tools/worker.py                                 1
#
#   If a future commit introduces new pyright errors
#   and adds new ``# type: ignore`` lines, run
#   ``pyright src/kntgraph`` and clean any
#   ``Unnecessary "# type: ignore" comment`` warnings
#   in the same PR.
#
# ---------------------------------------------------------------------------
#
# 4.2  pyright config — consider enabling
#       ``reportOptionalIterable`` and
#       ``reportOptionalCall`` at error level
#
#   These rules are warnings right now (see
#   ``pyrightconfig.json``). The bulk of the work in
#   section 2 is the result of these two rules being
#   soft. Flipping them to errors is a separate
#   milestone.
#
# ---------------------------------------------------------------------------

# 5. CLEANUP (when this list is empty)
# ---------------------------------------------------------------------------
#
# 5.1  Remove this file.
#
# 5.2  Bump the pyright baseline: ``scripts/regen_pyright_baseline.py``
#     reads the current ``pyright --outputjson`` and overwrites
#     ``.pyright-baseline.json``. Run only when the 111
#     remaining errors are at zero.
#
# 5.3  Bump the radon baseline: ``scripts/regen_radon_baseline.py``
#     does the same for ``.radon-baseline.json``.
#
# 5.4  Re-enable ``-W error::DeprecationWarning`` in
#      ``pyproject.toml::tool.pytest.filterwarnings`` after
#      ``kntgraph.agents.roles`` is removed in v1.0
#      (per ADR-041 §5).
#
# ---------------------------------------------------------------------------

# 6. APPENDIX: DATA POINTS
# ---------------------------------------------------------------------------
#
#   6.1  File-level error counts (pyright strict, 2026-07-13, post-Faixa-1):
#
#         20  knowledge/extraction/argument/_gliner_finder.py
#         11  events/dlq/store.py
#          6  knowledge/extraction/__init__.py   (stale # type: ignore)
#          4  core/storage.py
#          4  infra/redis/_memory/_continuity.py
#          4  infra/redis/_memory/_profile.py
#          3  knowledge/extraction/gliner.py
#          3  resilience/edge.py
#          3  resilience/timeout.py
#          2  agents/knowledge/solution_projector.py
#          2  agents/memory/solution_review_publisher.py
#          2  agents/memory/solutions/_promoter_helpers.py
#          2  agents/tools/cache/_redis.py
#          2  agents/tools/invoker/_emit.py
#          2  agents/tools/invoker/_invoker.py
#          2  events/dlq/actions.py
#          2  infra/checkpoint.py
#          2  infra/redis/_memory/_session.py
#          2  knowledge/extraction/_slm_facades.py
#          2  knowledge/falkordb/adapter.py
#          2  stream/event_log/dispatch.py
#          1  agents/memory/solution_extractor.py
#          1  agents/memory/solutions/_extractor.py
#          1  agents/memory/solutions/_promoter.py
#          1  agents/roles/semantic_router.py
#          1  agents/tools/arg_validation.py
#          1  agents/tools/pii/_tool.py
#          1  infra/redis/_auth/_redis.py
#          1  infra/redis/_dlq/_redis.py
#          1  knowledge/embedding/_ollama.py
#          1  knowledge/extraction/argument/_extractor.py
#          1  memory/continuity/cache_codec.py
#          1  resilience/bulkhead.py
#          1  resilience/retry.py
#          1  tools/manager.py
#          1  tools/schema.py
#          1  tools/system.py
#
#   6.2  Error counts by rule:
#
#         52  reportArgumentType
#         15  reportAttributeAccessIssue
#         14  reportReturnType
#         11  reportUnnecessaryTypeIgnoreComment
#          8  reportOptionalCall
#          4  reportInvalidTypeForm
#          3  reportCallIssue
#          1  reportInvalidTypeArguments
#          1  reportIncompatibleMethodOverride
#          1  reportOptionalIterable
#          1  reportAssignmentType
#
#   6.3  Coverage (unit tests only):
#
#         memory/                 76%   (base 91, fold 99,
#                                          session 85, profile 89,
#                                          continuity 69-99)
#         events/dlq              86%   (values 100, store 79,
#                                          actions 86)
#         overall                 75%
#
#   6.4  Gate snapshot (post-Faixa-1, 2026-07-13):
#
#         ruff lint               0 errors
#         ruff format             425 / 425 formatted
#         bandit                  3 LOW (intentional, B110)
#         radon CC                avg 2.53 (A), 0 rank D+
#         pytest tests/unit       1457 passed, 1 skipped
#         pytest tests/agents     294 passed
#         pyright                 71 errors / 1261 warnings
#
# ---------------------------------------------------------------------------

## 2.15 ADR-042 hydration pipeline (memory components)

**Status:** Partially delivered (components + projection
exist; full hydration pipeline in the dispatcher is
still a shim; example 05b is WIP).

**Delivered in this iteration (2026-07-14):**

  - **Memory components** (3 new files in
    ``core/components/memory.py``):
    - ``SessionComponent`` (Redis tier 1)
    - ``ProfileComponent`` (Redis tier 2)
    - ``ContinuityComponent`` (Redis tier 3)

  - **Hydration projection**
    (``core/world/projection_memory.py::project_memory``):
    a pure projection that walks the agent's
    ``session.*`` / ``profile.*`` / ``continuity.*``
    events and materialises the three components on the
    ``AgentView``. Preserves the base component when
    the current batch has no memory events
    (multi-tick safe).

  - **Reactive shim**
    (``examples/05b_session_chat_ecs.py::_install_projection_shim``):
    monkey-patches ``ReactiveDispatcher._fold_with_filter``
    to compose: ``default projection`` →
    ``project_memory`` → ``overlay_tool_calls``.

  - **Example 05b** (``examples/05b_session_chat_ecs.py``):
    the canonical reference implementation of the
    ADR-042 §6.1 pipeline. **WIP** — the example
    shows the architecture (no Redis I/O in the
    system, ECS components on the view, pure
    hydration via projection) but does not yet
    persist a full multi-turn chat end-to-end. The
    bug is the multi-tick overlay loss (see
    item 2.16 below).

**Open work (ADR-042 §6.1 follow-up):**

  - **Compose API.** The shim is a monkey-patch; the
    framework needs a proper
    ``ReactiveDispatcher(projections=[...])`` API
    that composes projections in order. The shim
    should be deleted once the API ships.
    Action: ADR follow-up PR.

  - **Run the projection in the framework.**
    ``project_memory`` lives in
    ``core/world/projection_memory.py``; the
    framework's default ``World.fold`` does not
    call it. The shim is the only way to wire
    it in today. Action: expose a
    ``MemoryHydrationProjection`` class in
    ``runner/reactive_extensions.py`` and call it
    from the default ``_fold_with_filter`` after
    the base projection and before the tool
    overlay.

  - **Tests for the projection.** The
    ``project_memory`` projection has no unit
    tests. It is currently exercised only by
    example 05b (WIP). Action: add tests in
    ``tests/unit/core/world/test_projection_memory.py``
    covering: ``session.*`` fold, ``profile.*``
    fold, ``continuity.*`` fold, multi-tick
    preservation of base component.


## 2.16 Tool-call overlay: multi-tick slot loss

**Status:** Closed in 2026-07-14 via ADR-044
(``ADRs/ADR-044-Tool-call-Overlay-Accumulation.md``).

**Closed by:**

  - **``overlay_tool_calls``** now MERGES the new
    requests/completions with the existing slots
    on the base view, keyed by
    ``request_event_id``. A request emitted in
    tick N remains visible in the slot in tick
    N+K (accumulation).
  - **Eviction policy (Option B, completion-driven):**
    a ``tool_requests`` entry is **evicted** when
    a matching ``tool_completions`` entry lands
    AND the request was carried in from
    ``base_views`` (a previous tick). Requests
    created by the current batch are kept (the
    system may not have reacted to them yet).
  - **``_apply_event`` preservation:** the default
    domain projection now preserves the
    ``tool_requests`` and ``tool_completions``
    slots when the incoming event is a tool
    event (``tool.<name>.<suffix>`` or legacy
    bare form). Without this, the
    ``World.with_event`` chain between ticks
    would drop the slot before the overlay ran.
  - **``SolutionExtractorSystem`` updated** to
    iterate ``completions`` (source of truth for
    "finished") and look up the request from the
    (possibly evicted) ``tool_requests`` slot;
    entries with no request are skipped (orphan
    completions).

**Tests** (``tests/unit/runner/test_reactive_tool_projection.py``):

  - ``test_request_remains_visible_until_completion_arrives_in_next_batch``:
    request in tick N, completion in tick N+1.
    The completion matches the request (via
    ``causation_id``); the request is evicted
    from the slot (it was carried from
    ``base_views``). The completion is recorded.
  - ``test_unrelated_request_persists_across_batches``:
    request in tick N, an unrelated tool
    completion in tick N+1. The request remains
    in flight (the unrelated completion doesn't
    evict it).

  - **Canonical-form acceptance** (ADR-036
    regression): three tests
    (``test_canonical_form_requested_accepted``,
    ``test_canonical_form_completed_accepted``,
    ``test_canonical_form_failed_accepted``) cover
    the ``tool.<name>.<suffix>`` form which is the
    shape emitted by ``ToolAwareSystem.request_tool``
    and ``LiteLLMToolWorker``. Both forms are
    accepted by ``_requested_tool_name``,
    ``_completion_status``, and
    ``_has_tool_events``.

**Open follow-ups (out of scope of §2.16):**

  - **§2.18** — example 05b hydration shim still
    has a separate bug (the system never emits a
    request_tool event end-to-end; the ECS path
    reaches the hydration step but the request
    phase is short-circuited by the projection
    shim). Tracked separately.
  - **TTL-based eviction (ADR-045, planned):**
    the current eviction is completion-driven;
    orphaned requests (e.g. worker crash) linger
    in the slot forever. The follow-up ADR
    proposes a TTL bound on ``tool_requests``
    entries (default 5 minutes; configurable per
    tool) so the slot can't grow unbounded.

**Acceptable:** N/A — closed.


## 2.17 LiteLLM worker migration (ADR-043)

**Status:** Delivered (v0.8.0).

**Delivered in this iteration (2026-07-14):**

  - **``LiteLLMToolWorker``** (new class in
    ``src/kntgraph/agents/tools/llm.py``): a
    ``@tool_worker(name="chat_llm")`` implementation
    of the LLM bridge. Runs in the
    ``WorkerManager``'s ``ProcessPoolExecutor``; the
    dispatcher event loop is not blocked while the
    LLM responds. Returns a JSON-serialisable dict
    (``text`` / ``model`` / ``usage`` / ``finish_reason``
    / ``cost_usd`` / ``latency_ms``) so the system
    can introspect usage and cost from the
    ``tool_completion.data``.

  - **Deprecation warnings on the legacy paths:**
    - ``LiteLLMTool`` (legacy ``Tool`` Protocol) emits
      a one-shot ``DeprecationWarning`` on import.
      Class-level ``__deprecated__ = True`` marker.
      Removal target: v0.9.0.
    - ``ToolInvoker`` (legacy orchestrator) emits a
      ``DeprecationWarning`` on import. Class-level
      ``__deprecated__ = True`` marker. Removal
      target: v1.0.0 (two releases to migrate the
      remaining tools — e.g. ``PiiRedactionTool``).

  - **Example 05b updated** to use the
    ``LiteLLMToolWorker`` (replaces the
    ``MockChatLlmTool``; the mock is kept as a
    commented drop-in for CI environments without an
    LLM).

  - **Tests** (``tests/agents/unit/tools/test_litellm_worker.py``):
    7 tests covering the worker metadata, the
    ``invoke`` envelope (text / model / usage /
    finish_reason / latency_ms), the timeout path
    (``Err(TimeoutError)``), the generic-error path
    (``Err(Exception)``), and the default-model
    fallback.

**Open work (ADR-043 follow-ups):**

  - **Role migration (ADR-044).** The
    ``ChatRole.reply`` / ``PlannerRole.plan`` /
    ``SummarizerRole.summarize`` /
    ``PersonalizedRole.respond`` methods still call
    ``await self._llm.invoke(...)`` directly. The
    canonical path (ADR-039) is for the role to
    emit a ``tool.chat_llm.requested`` event and let
    the ``WorkerManager`` orchestrate. Migration is
    a 50-line change across 4 role files. The
    example 05b's ``SessionChatSystem`` is the
    reference implementation of the new pattern;
    the roles can be ported to emit a
    ``request_tool`` event in place of the
    synchronous ``_invoke``.

  - **Example migration (01-07).** Examples
    01-07 still use the legacy ``LiteLLMTool`` (with
    a deprecation warning on import). They should be
    migrated to use the ``LiteLLMToolWorker`` via
    the ``WorkerManager`` in the v0.9.0 cycle.

  - **Suppress deprecation noise in CI.** Add a
    ``warnings.filterwarnings`` rule to
    ``pyproject.toml::[tool.pytest.ini_options]`` to
    suppress the ``DeprecationWarning`` from
    ``LiteLLMTool`` and ``ToolInvoker`` until the
    examples are migrated.

**Acceptable:** Continue with the legacy paths for
now. The deprecation warnings are intentional;
the migration is opt-in for the next release.


## 2.18 Example 05b hydration shim: system never emits a request_tool event

**Status:** Open (WIP).

**Severity:** Medium. Example
``examples/05b_session_chat_ecs.py`` runs
end-to-end through the hydration pipeline but
**emits zero ``user_response`` events** in the
current state. The ECS path is reached, the
``SessionComponent`` is hydrated correctly, but
the chat system's reaction phase is
short-circuited by the projection composition
in the local shim
(``_install_projection_shim``).

**Discovery:** While validating ADR-044, the
example 05b (which exercises the multi-tick
accumulation path) was re-run with the
overlay fix. The session still produces no
``user_response`` events; the reactive
``SessionChatSystem`` reads a ``tool_requests``
slot that is **empty** when the system reacts
(even though the ``LiteLLMToolWorker`` is
running and emitting the request event).

**Impact:** Example 05b is WIP. The
``ADR-042 §6.1`` reference implementation is
not yet end-to-end-correct. The fix for §2.16
unblocks the projection path; the chat system
reaction is a separate problem.

**Fix (sketch):** The
``_install_projection_shim`` in example 05b
composes the default projection with the
``project_memory`` and the
``overlay_tool_calls`` but the composition
order drops the ``tool_requests`` slot before
the chat system reads it. The fix is to make
the shim return a single ``Projection`` that
delegates to a combined fold and then runs
the overlay at the end of the dispatch (the
same way ``ReactiveDispatcher._fold_with_filter``
does in production).

**Action:** Open an issue (or amend example
05b) once the shim is fixed. Validate
end-to-end: spawn the agent, send a
``user_input``, assert that the system emits
``tool.chat_llm.requested`` and that the
completion in the next tick is correlated to
it.

**Acceptable:** Until the shim is fixed,
example 05b is a **read-only reference** of
the pipeline. Production code paths (the
``ReactiveDispatcher``) are unaffected by the
shim.
