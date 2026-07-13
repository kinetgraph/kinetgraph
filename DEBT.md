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

# 2.2  api/intent_router/routes.py  (19 errors)
#
#   Two distinct issues:
#
#   A. ``PrincipalDep | None`` is being called in 8 places
#      because the type annotation is wrong: the dependency
#      is non-optional at runtime (FastAPI raises 401 on
#      missing auth header). The static checker sees the
#      ``None`` branch because the stub helper returns
#      ``None`` when no header is present.
#
#   B. ``ValidatorInput`` is being passed where ``Principal``
#      / ``type[HealthResponse]`` is expected. The routes
#      use a re-export of the dependency that lost its
#      concrete return type.
#
#   Action: split the auth dependency in two — one
#           ``Optional[Principal]`` (for routes that
#           tolerate anonymous) and one ``Principal``
#           (for the strict routes). Update the
#           ``response_model`` parameters to use the
#           concrete ``type[X]`` aliases already exported
#           from ``api/schemas.py``.
#
#   Acceptable: add ``# type: ignore[call-arg, arg-type]``
#               on the 19 lines. Cheap, but masks a real
#               security issue (optional auth on routes that
#               should be strict).
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

# 2.5  infra/redis/_memory/{_continuity,_profile}.py  (4+4 errors)
#
#   The ``PipelineLike`` Protocol is missing
#   ``hset`` / ``expire`` / ``delete`` / ``items``.
#   These are used at lines 83-94 (``_continuity``) and
#   87-98 (``_profile``) when the cache write is batched
#   into a pipeline. The Protocol in
#   ``infra/redis/_client.py`` is too narrow.
#
#   Action: extend ``PipelineLike`` with the four methods
#           (defer to the real ``redis.asyncio.client.Pipeline``
#           interface), or use the concrete ``Pipeline`` type
#           from the redis client.
#
#   Effort: 1 hour (typing only).
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

# 2.9  infra/redis/{_auth,_world_checkpoint}/_redis.py  (1+1 errors)
#
#   Both pass ``bytes`` to ``client.set`` which expects
#   ``str``. The pattern is identical: HSET-style
#   writes that use bytes but the type stub assumes
#   ``decode_responses=True`` (str-only).
#
#   Action: cast the value to ``str`` (or bytes) explicitly
#           at the call site, with a comment.
#
#   Effort: 10 minutes.
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
# 4.1  Stale ``# type: ignore`` comments
#
#   Pyright 1.1.411 reports 11 ``# type: ignore`` comments
#   as unnecessary. Distribution:
#
#     knowledge/extraction/__init__.py                6
#     events/dlq/actions.py                           1
#     infra/graph/_lite_pool.py                       1
#     agents/memory/solutions/__init__.py             1
#     agents/memory/solutions/_fingerprints.py         1
#     tools/worker.py                                 1
#
#   Action: delete them. Verify pyright still passes
#           after each deletion.
#
#   Effort: 15 minutes.
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
#   6.1  File-level error counts (pyright strict, 2026-07-13):
#
#         20  knowledge/extraction/argument/_gliner_finder.py
#         19  api/intent_router/routes.py
#          6  knowledge/extraction/__init__.py
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
#          1  agents/memory/solutions/__init__.py
#          1  agents/memory/solutions/_extractor.py
#          1  agents/memory/solutions/_fingerprints.py
#          1  agents/memory/solutions/_promoter.py
#          1  agents/roles/semantic_router.py
#          1  agents/tools/arg_validation.py
#          1  agents/tools/pii/_tool.py
#          1  infra/graph/_lite_pool.py
#          1  infra/redis/_auth/_redis.py
#          1  infra/redis/_dlq/_redis.py
#          1  infra/redis/_world_checkpoint/_redis.py
#          1  knowledge/embedding/_ollama.py
#          1  knowledge/extraction/argument/_extractor.py
#          1  memory/continuity/cache_codec.py
#          1  resilience/bulkhead.py
#          1  resilience/retry.py
#          1  tools/manager.py
#          1  tools/schema.py
#          1  tools/system.py
#          1  tools/worker.py
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
#   6.4  Gate snapshot:
#
#         ruff lint               0 errors
#         ruff format             421 / 421 formatted
#         bandit                  3 LOW (intentional, B110)
#         radon CC                avg 2.53 (A), 0 rank D+
#         pytest tests/unit       1457 passed, 1 skipped
#         pytest tests/agents     294 passed
#         pyright                 111 errors / 1274 warnings
#
# ---------------------------------------------------------------------------
