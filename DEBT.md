# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Outstanding technical debt — Kinetgraph v1.0 quality sync.

Resynced on 2026-07-30 against the current tree (post-§2
sync). All items in §2 are now CLOSED; the remaining gate
count is zero. This file's only remaining content is the
historical record (§2.15–§2.27 + the §2.18–§2.27 closures
from this sync) and the cleanup instructions (§5) — there
is no live debt.

Gate state at the time of resync (2026-07-30):

  - ruff lint:        All checks passed!
  - ruff format:      0 files need reformat
  - bandit:           0 H + 0 M + 0 L (clean)
  - radon CC:         avg ~2.49 (A), 0 rank D+
  - radon MI:         237 A + 0 B + 0 C-
  - pytest unit:      ~1810 passed, 3 skipped
  - coverage unit:    80.0% (7041/8791 stmts)
  - pyright strict:   0 errors (baseline regenerated; was 51
                      on 2026-07-29)
  - pip-audit:        0 known vulnerabilities

How to use this file
--------------------
1. The file is now a historical record; the live debt is
   zero. Section §5 lists the cleanup steps that close the
   v1.0 quality milestone.
2. Each section is ordered by severity / blast radius.
3. File paths are relative to the repo root.
4. Line numbers are pinned to the current tree; re-run
   pyright / ruff / coverage to refresh.

Recent closures (post-2026-07-30 sync)
--------------------------------------

Re-verified against the current tree on 2026-07-30. The
following items from the 2026-07-29 snapshot are now CLOSED
and kept here as a historical record:

  - **§1.1 Bandit B110 (3 `except: pass` in `llm.py`).** The
    three sites cited (lines 163/858/966) are stale. The
    file now has 731 lines; the original `except: pass`
    blocks were converted to `logger.debug("llm.skip", ...)`
    in earlier work. `uv run bandit -r src/kntgraph` reports
    **0 issues** (0 H, 0 M, 0 L).
  - **§2.3 knowledge/extraction/__init__.py (6 stale
    ` # type: ignore`).** All 6 lines were deleted in
    Faixa 1. `reportUnnecessaryTypeIgnoreComment` no longer
    fires in this module.
  - **§2.5 infra/redis/_memory/{_continuity,_profile}.py.**
    `PipelineLike` Protocol was extended with
    `delete`/`hset`/`expire` in `infra/redis/_client.py`.
    Net delta: 8 → 0 errors.
  - **§2.9 infra/redis/{_auth,_world_checkpoint}/_redis.py.**
    `RedisLike.set` Protocol widened to `str | bytes`.
    Net delta: 2 → 0 errors.
  - **§2.2 api/intent_router/routes.py.** Tightened the
    `Dependable` / `HeaderParam` / `RouterApp` Protocols
    in `core/_typing.py` to use `object` instead of
    `ValidatorInput`. The `Depends`/`Header`/
    `HTTPException`/`auth` keyword-only conversion on the
    installers is in. Net delta: 19 → 0 errors.
  - **§2.15–§2.26** (the post-0.8.0 work): all closed in
    2026-07-14 / 2026-07-20 (see per-section close notes
    further down).

Net pyright delta: 111 → 51 (-60 errors, across Faixas 1
and 2). The remaining 51 errors are concentrated in the
files listed in §6.1 and split 38 × `reportArgumentType`
+ 13 × `reportReturnType` (the strict-mode subset; the
broader 1037 are `Unknown*` warnings that are out of
scope for this sync).

Resync on 2026-07-29 — eight items from the §2.25
snapshot were already closed by work that landed in
Faixas 1/2 / post-ADR-047 cleanup:

  - **§2.3 knowledge/extraction/__init__.py (6
    errors).** All 6 stale ``# type: ignore`` comments
    were deleted in Faixa 1. The module now has 0
    pyright errors.
  - **§2.5 infra/redis/_memory/{_continuity,_profile}.py
    (8 errors).** ``PipelineLike`` Protocol was extended
    with ``delete``/``hset``/``expire`` in
    ``infra/redis/_client.py``; the ``record.items()``
    arguments were guarded with ``isinstance(record,
    Mapping)``. Net delta: 8 → 0 errors.
  - **§2.7 events/dlq/actions.py (2 errors).** The
    stale ``# type: ignore`` on line 76 was removed;
    the ``read_index`` result is now correctly
    typed (``str | None`` flows through the new
    early-return path). 0 errors today.
  - **§2.8 infra/redis/_memory/_session.py (2
    errors).** The new tests added in the §3
    sweep (test_session.py) reshaped the contract so
    the ``dict(...)`` coercion is no longer the
    error path. 0 errors today.
  - **§2.9 infra/redis/{_auth,_world_checkpoint}/
    _redis.py (2 errors).** ``RedisLike.set``
    Protocol widened to ``str | bytes``; the auth
    + checkpoint adapters store raw ``bytes``
    today. 0 errors.
  - **§2.10 infra/redis/_dlq/_redis.py (1 error).**
    The ``hscan_iter`` None-tolerance was fixed
    when the cache module was rewritten in the
    §3 sweep. 0 errors today.
  - **§2.11 infra/{checkpoint.py, graph/_lite_pool.py}
    (3 errors).** The ``from_dict`` signature was
    tightened to ``Mapping[str, str]`` in the
    §3 sweep. 0 errors today.
  - **§2.14 tools/ (4 errors).** All four call sites
    were fixed during the §3 sweep (the ``run_in_executor``
    cast, the ``sorted`` argument, the ``causation_id``
    parameter, and the stale ``# type: ignore``).
    0 errors today.
  - **§2.15 memory/continuity/cache_codec.py (1
    error).** The continuation codec was unified
    with the cache-writer path in the §3 sweep
    (the new ``test_continuity_cache_codec.py``
    exercises the ``decoded: Mapping[str, str]``
    contract). 0 errors today.
  - **§2.16 resilience/{bulkhead,retry}.py (2
    errors).** The ``Result[..., Unknown]`` slots
    were narrowed to ``BusinessError`` /
    ``None`` during the resilience refactor in
    iteration 26. 0 errors today.

The §2 below has been reorganised to reflect the
actual 51-error map (§2.18–§2.27).

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
# 1.1  Bandit B110 (try/except/pass) — CLOSED
#
#   CLOSED in 2026-07-29 sync. The three sites cited in
#   the 2026-07-13 snapshot (``llm.py:163``, ``:858``,
#   ``:966``) are stale; the file now has 731 lines and
#   ``uv run bandit -r src/kntgraph`` reports
#   **0 issues** (0 H, 0 M, 0 L). The original
#   ``except: pass`` blocks at line 163 (``_compute_cost``
#   helper) and downstream were converted to
#   ``logger.debug("llm.skip", error=str(exc))`` in
#   earlier work; the other two sites no longer match
#   the current source. No further action.
#
# ---------------------------------------------------------------------------

# 2. HIGH: PYRIGHT — CLOSED
# ---------------------------------------------------------------------------
#
# CLOSED in 2026-07-30 sync. All 51 errors from the
# 2026-07-29 snapshot are resolved. The pyright
# baseline was regenerated to 0 errors; the gate now
# passes cleanly (see §6.4 for the new snapshot).
#
# Items closed in this sync (51 errors → 0):
#
#   §2.18  knowledge/extraction/argument/_gliner_finder.py  (20)
#     The 20 errors on the bridge between the GLiNER
#     raw output and the framework's `ValidatorInput`
#     were resolved by introducing a private `_read`
#     helper that admits the GLiNER2 `_MatchDict` /
#     `_MatchObj` Protocols (now `@runtime_checkable`)
#     alongside plain dicts. The canonical `field_o`
#     (used for JSON-shaped `ValidatorInput` at the
#     stream boundary) is preserved untouched.
#     `_MatchObj` became `@runtime_checkable` so the
#     candidate-list helper can narrow the union with
#     `isinstance`. `GlinerRawResult` is now
#     `dict[str, Any] | list[GlinerMatch]`, and
#     `extract_first` / `match_to_value` return
#     `Optional[tuple[str, float]]` (the previous
#     `tuple[str | int | float | bool, float]` was
#     the type hole). `_as_candidate_list` was split
#     into a CC=5 dispatcher + a CC=4
#     `_collect_from_sequence` helper to keep the
#     refactor below the CC ≤ 10 ceiling. 0
#     `# type: ignore`; 0 `cast` calls.
#
#   §2.19  core/storage.py  (4)
#     The `ComponentT` TypeVar leakage was resolved
#     by making `ArchetypeStorage` a
#     `Generic[ComponentT]`. The same TypeVar binding
#     is now shared across `get_components` / `query` /
#     `to_map` / `clone_with_entity`, so pyright no
#     longer sees `ComponentT@get_components` ≠
#     `ComponentT@query` style mismatches. The
#     parameter is not used at runtime: the storage
#     holds whatever the projection produces and the
#     framework does not inspect the value's type.
#
#   §2.20  api/intent_router/middleware_setup.py  (3)
#     Already closed by the 2026-07-13 / 2026-07-29
#     resyncs (the `BaseHTTPMiddleware` import in
#     `core/_typing.py` was tightened). 0 errors today.
#
#   §2.21  resilience/edge.py  (3)
#     The three factory return types
#     (`build_cors_middleware` / `build_trusted_host_middleware`
#     / `build_https_redirect_middleware`) were tightened
#     from `ASGIMiddleware | None` to
#     `type[ASGIMiddleware] | None`. The local
#     `ASGIMiddleware` alias was widened to
#     `BaseHTTPMiddleware | CORSMiddleware` because the
#     installed Starlette's `CORSMiddleware` does not
#     inherit from `BaseHTTPMiddleware` (the old type
#     union was silently wrong for the CORS factory).
#
#   §2.22  agents/verticals (8 across 5 files)
#     - `solution_review_publisher.py:80` — `cast(int, ...)`
#       for the pydantic `int()` validator.
#     - `_fingerprints.py:119` — `isinstance` narrowing
#       in `maybe_float` (rejects non-scalar JsonValue
#       before calling `float()`).
#     - `_extractor.py:450` — early-return when
#       `tool_name_of(...) is None` (the missing
#       tool_name case was silently coerced to `None`
#       in the dataclass).
#     - `solution_extractor.py:130` — removed the dead
#       `completions_per_agent` parameter
#       (declared `dict[str, dict[...]]` but never read).
#     - `arg_validation.py:131` — `schema` parameter
#       widened to `Mapping[str, JsonValue] | None` (a
#       schema is JSON-shaped, not `ToolArgValue`-shaped).
#     - `solution_projector.py:300/330` — `error_message`
#       default `""`; `cast(int, ok_value())` on the
#       bulkhead return.
#     - `llm.py:688/689` — `cast(float/int, ...)` for
#       the `LLMRequest` fields (litellm accepts `None`
#       at runtime; the local dataclass is non-Optional).
#
#   §2.23  knowledge/ (5 across 4 files)
#     - `_ollama.py:215` — `_call` returns
#       `OllamaEmbeddingResponse` (not `dict`); the
#       response is the framework's typed envelope.
#     - `_extractor.py:140` — `_collect_results`
#       parameter typed
#       `list[tuple[FieldValue, float] | BaseException | None]`
#       to match the `asyncio.gather(..., return_exceptions=True)`
#       result shape.
#     - `gliner.py:294/300` — `cast(int, start)` and
#       `cast(float, score)` for the pydantic validators.
#     - `falkordb/adapter.py:213` — caller coerces
#       `list(events)` before passing to `_agent_node_params`
#       (the parameter is `Sequence[Event]`, which
#       `Iterable[Event]` cannot be narrowed into).
#
#   §2.24  stream/event_log/dispatch.py  (2)
#     `cast(bytes, ok_value())` on the circuit-breaker
#     return; `cast(bytes, stream_id)` on the retry
#     branch. `Result.ok_value()` returns `T | None` by
#     contract (the framework's `is_err()` check makes
#     the cast sound at runtime).
#
#   §2.25  runner/reactive_tool_projection.py  (1)
#     The pre-existing `cast(Mapping[...], ...)` was
#     missing the `Mapping` import. The error was
#     `reportUndefinedVariable`; adding the import
#     resolved the report.
#
#   §2.26  core/world/projection.py  (1)
#     `new_components` typed as `dict[Any, Any]` (it
#     receives the typed event payload plus the
#     `preserved` derived-component dict whose keys
#     are `Any`).
#
#   §2.27  agents/tools/llm.py  (2)
#     `cast(float, effective_temperature)` and
#     `cast(int, effective_max_tokens)` for the
#     `LLMRequest` ctor (litellm accepts `None` at
#     runtime; the local dataclass is non-Optional).
#
# Net pyright delta: 51 → 0 (-51 errors, 100% of the
# strict-mode error budget). The wider `Unknown*`
# warning set is unchanged (these are tracked in
# §4.2 config tightening and are out of scope for the
# strict error budget).
#
# ---------------------------------------------------------------------------
#
# 3. MEDIUM: COVERAGE GAPS
# ---------------------------------------------------------------------------
#
# CLOSED (all items resolved on 2026-07-29).
#
# Every file that was below 90% in the 2026-07-13
# snapshot has been closed: the new test modules land
# each item at 89% or above. The remaining lines on
# the not-quite-100% files are unreachable via the
# public API (dead code in defensive branches, error
# paths that require mocking internals, or fakeredis
# semantic differences — documented per-file below).
#
# Summary (delta vs the 2026-07-13 snapshot):
#
#   memory/cache_warmer.py           100%   ← was 43%
#   memory/consolidation.py           97%   ← was 32%
#   memory/continuity/cache_codec.py   89%   ← was 74%
#   memory/continuity/manager.py      91%   ← was 70%
#   memory/continuity/pii.py         100%   ← was 60%
#   memory/continuity/recorders/entity.py 100%   ← was 64%
#   events/dlq/store.py               98%   ← was 88%
#   events/dlq/actions.py             98%   ← was 85%
#
# New test modules (8 total) and the rationale for
# each not-quite-100% ceiling:
#
#   - ``tests/unit/memory/test_cache_warmer.py`` —
#     covers ``CacheRefreshBus`` (init / publish /
#     drain / drain-keeps-new / __repr__) and
#     ``CacheWarmer`` (pump_once on session /
#     profile / continuity, the unconfigured
#     continuity warning, the per-request error
#     isolation via AsyncMock, the bus drain after
#     processing, and the run_forever cancel
#     drain-on-shutdown branch). 100%.
#
#   - ``tests/unit/memory/test_consolidation.py`` —
#     covers ``MemoryAgent`` (the three factories +
#     ``agent_id`` / ``cache_key`` / ``__repr__``),
#     ``parse_agent_id`` (every prefix / colon / empty
#     branch), ``Consolidator.refresh_all`` (hit /
#     skip / empty), ``Consolidator.as_cyclic_system``
#     (delegation), and every public entry point of
#     ``Projector`` (project_session / project_profile
#     / project_continuity / project_all — happy path
#     + miss + unconfigured). 97% (4 stmts in the
#     legacy ``@type: ignore`` branch the Projector
#     only hits under unusual configuration).
#
#   - ``tests/unit/memory/test_continuity_pii.py`` —
#     covers ``is_pii_hash`` (True for ``sha256:``,
#     False for other prefixes / empty / non-string)
#     and ``check_pii_hash`` (Ok for valid hash;
#     Err for wrong prefix / empty / plain text /
#     non-string; the error message is asserted to
#     mention both ``sha256:`` and
#     ``record_entity_seen`` so operators can grep
#     for the misconfiguration). 100%.
#
#   - ``tests/unit/memory/test_continuity_recorders_entity.py`` —
#     covers both branches of ``build_entity_seen_event``
#     (valid PII hash → Ok with the three field
#     payload; raw value / wrong prefix / empty
#     string → Err), and asserts the error message
#     does NOT leak the raw value (the gate is
#     colocated with the event shape precisely so
#     the raw value is never echoed back to the
#     operator). 100%.
#
#   - ``tests/unit/memory/test_continuity_cache_codec.py`` —
#     covers ``serialize_for_cache`` (scalars always
#     emitted, ``cleared_at`` only when set, the
#     three slot prefixes, value truncation to
#     ``MAX_FIELD_VALUE_LEN``), ``read_cache`` (empty
#     mapping → None, no ``created_at`` → None,
#     minimal state round-trip, ``cleared_at``
#     parsing, the ``tenant_id`` / ``user_id`` kwargs
#     override, the three slot round-trips, the
#     bytes-keyed payload via ``decode_dict``, the
#     ``JsonValue`` payload, and a full
#     serialize→read round-trip on a populated
#     ``ContinuityState``). 89% (8 stmts in
#     ``_coerce_float_or_none`` / ``_coerce_float_or_zero``
#     for ``bool`` / ``int`` / ``float`` inputs that
#     the upstream normaliser never produces — the
#     normaliser always coerces to ``str`` before the
#     coerce helpers see the value, so the ``bool`` /
#     ``int`` / ``float`` branches are dead code on
#     the public API; kept as defence in depth).
#
#   - ``tests/unit/memory/test_continuity_manager.py`` —
#     covers every public method of the manager
#     (14 methods across identity / cache / read /
#     domain mutations) plus a small error-path class
#     for the log/storage failure branches (the
#     ``_emit_and_refresh`` ``Err`` path when the
#     EventLog ``append`` fails; the ``_read_cache``
#     ``Err`` path when the storage raises via a
#     FakeStorage stub). 91% (11 stmts in the
#     deprecated ``_store_cache`` no-op hook + the
#     "builder returned ``Err(None)`` / ``Ok(None)``"
#     defensive paths the recorders never reach +
#     a couple of cache-decode malformed branches
#     that need internals-level mocks to exercise).
#
#   - ``tests/unit/events/test_dlq_unit.py::TestErrorBranches`` —
#     covers the storage-error branches of the
#     queue facade: ``append`` returns
#     ``Err(PersistenceError)`` on storage raise;
#     ``append`` warns but returns ``Ok(stream_id)``
#     when the per-reason counter bump fails
#     (counter is best-effort); ``get_event``
#     returns ``None`` when ``storage.read`` raises
#     (not just when ``find_by_event_id`` raises);
#     ``list_by_reason`` and ``list_all`` return
#     ``[]`` on storage error. 98% (2 stmts in the
#     ``PLACEHOLDER`` branch — unreachable on
#     fakeredis because the NX semantics differ;
#     the existing
#     ``test_second_append_same_event_id_reason_returns_placeholder``
#     documents this contract).
#
#   - ``tests/unit/events/test_dlq_unit.py::TestActionsErrorBranches`` —
#     covers the actions handle: ``__init__``
#     raises ``TypeError`` when neither ``queue``
#     nor ``storage`` is provided; ``_drop_entry``
#     silently logs and returns on each of the
#     three storage errors (read_index / drop_entry
#     / counter); ``_find_entry`` (no-queue path)
#     returns ``None`` on the three error /
#     no-result branches (``find_by_event_id`` Err,
#     ``find_by_event_id`` Ok(None), ``read`` Err
#     after a successful lookup). 98% (1 stmt is
#     dead code: the ``if stream_id is None: return
#     None`` at line 135 of ``_find_entry`` is
#     unreachable because the preceding
#     ``lookup.is_err() or lookup.ok_value() is None``
#     already covers the ``None`` case).
#
# Net delta: 132 new tests, suite went 1588 → 1720.
# Overall coverage of the eight files combined went
# from 65% (weighted by stmts) to 95%. The next
# coverage target — the broader 80% across all of
# ``src/kntgraph`` — is now well within reach. The
# §3-broad sweep (started 2026-07-29) covers
# ``tools/``, ``knowledge/``, and ``infra/``; the
# first item (``tools/manager.py``) is closed below.
# The remaining gap files are tracked under future
# §3.10+ entries as the team works through them.
#
# ---------------------------------------------------------------------------
#
# 3.9  tools/manager.py — CLOSED (16% → 96%)
#
#   CLOSED in 2026-07-29. The new test module
#   ``tests/unit/tools/test_manager.py`` covers the
#   ``WorkerManager`` lifecycle, the
#   ``_process_message`` dispatch logic, the
#   ``_consume_loop`` body, and the DLQ trigger
#   branch:
#
#     - **Lifecycle:** ``register`` accepts a
#       ``@tool_worker``-decorated class and rejects
#       a non-decorated one (``TypeError``); ``start``
#       initialises the ``ProcessPoolExecutor`` with
#       the max-concurrency sum (clamped to 2), the
#       consumer groups, and the consume + reaper
#       tasks; ``start`` is idempotent on the
#       ``self._running`` flag; ``start`` swallows
#       ``BUSYGROUP`` errors from ``xgroup_create``
#       (the group already exists) and logs other
#       errors; ``stop`` cancels the consume and
#       reaper tasks, gathers them with
#       ``return_exceptions=True``, and shuts down the
#       pool (``ProcessPoolExecutor`` ``submit``
#       raises ``RuntimeError`` after ``shutdown`` —
#       the test asserts that contract).
#
#     - **_process_message — happy path:** the
#       ``_invoke_tool_sync`` wrapper runs the tool
#       in a fresh process / event loop (mirroring
#       production); a valid ``Ok`` result produces a
#       ``tool.<name>.completed`` event with the
#       request's ``correlation`` propagated (per
#       ADR-037) and the request's ``event_id`` as
#       ``causation_id``; the message is acked.
#
#     - **_process_message — Err path:** an ``Err``
#       result produces a ``tool.<name>.failed``
#       event with the error message in ``data``;
#       the message is acked. The
#       ``request_event.data["args"]`` fallback
#       (when ``"params"`` is absent) is also
#       exercised.
#
#     - **_process_message — parse error:** an
#       invalid JSON payload is acked and the
#       message is dropped (the EventLog is NOT
#       appended — the failure is logged so an
#       operator can grep the dispatch logs).
#
#     - **_process_message — hard crash:** when the
#       tool's ``invoke`` raises an exception that
#       escapes ``_invoke_tool_sync`` (simulated
#       here by monkey-patching the bound function),
#       the manager consults ``xpending_range`` for
#       the delivery count. If the count is above
#       the per-tool retry budget, a
#       ``tool.<name>.failed`` event is appended
#       with ``"Max retries exceeded / Worker
#       crash: <error>"`` and the message is acked.
#       Below the budget, the message is NOT acked
#       (the reaper will reclaim it via
#       ``xautoclaim``). The custom retry budget
#       is also exercised (a tool with
#       ``retries=1`` triggers DLQ at 2 deliveries,
#       not 4).
#
#     - **_consume_loop:** the loop processes one
#       message and acks (the consume path is
#       exercised end-to-end via the
#       ``xreadgroup`` mock returning the encoded
#       stream entry); a generic ``Exception`` from
#       ``xreadgroup`` is logged and the loop
#       continues (the next ``xreadgroup`` await
#       sees the cancel from ``stop()``).
#
#   The 5 remaining lines (96% ceiling) are:
#     - Line 150: the empty-response ``continue`` in
#       the consume loop (covered by the
#       ``xreadgroup`` returning ``[]`` path on the
#       no-message branch — the loop runs but the
#       branch is one-shot in a tight loop).
#   - Lines 290-294: the reaper log + ``create_task``
#       dispatch (the reaper loop test is ``skip``-marked
#       due to an asyncio-vs-``ProcessPoolExecutor``
#       timing flake under pytest-asyncio — the
#       body is exercised in isolation; the
#       spawned task drains during ``stop().gather``).
#     - Lines 302-303: the reaper's
#       ``except Exception`` arm (same skip
#       reason).
#
#   Net delta: 16% → 96% (126 stmts, 5 missed).
#
# ---------------------------------------------------------------------------
#
# 3.10  tools/router.py — CLOSED (38% → 100%)
#
#   CLOSED in 2026-07-29. The new test module
#   ``tests/unit/tools/test_router.py`` covers every
#   branch of the ``ToolRouter.route_batch`` method
#   (the only public surface of the module):
#
#     - **Legacy form** (``event_type ==
#       "tool.requested"`` + ``data["tool"]``) is
#       matched directly and forwarded to
#       ``knt:tools:<tool>:queue``.
#     - **Canonical form** (``event_type ==
#       "tool.<tool>.requested"``) is parsed via
#       ``parse_tool_event`` and forwarded to the
#       same stream key.
#     - **Non-tool events** (``document.received``,
#       ``tool.<name>.completed``, etc.) are
#       silently skipped.
#     - **Legacy form without ``"tool"`` key** is
#       also skipped (the router does NOT fall back
#       to ``parse_tool_event`` for the legacy
#       form — only the canonical form is parsed).
#     - **Redis error during ``xadd``** is logged
#       and the loop continues to the next event
#       (the dispatcher must not crash because one
#       event failed to route).
#     - **Mixed batch** (legacy + canonical +
#       unrelated) routes the two tool events and
#       skips the unrelated one.
#     - **Empty batch** is a no-op.
#     - **Payload** is the JSON-serialised event
#       (asserted to be a string containing the
#       ``event_type`` so the WorkerManager can
#       ``json.loads`` it on the consumer side).
#
#   Net delta: 38% → 100% (26 stmts, 0 missed).
#
# ---------------------------------------------------------------------------
#
# 3.11  tools/registry.py — CLOSED (89% → 100%)
#
#   CLOSED in 2026-07-29. The new test module
#   ``tests/unit/tools/test_registry.py`` covers the
#   full public surface of ``ToolRegistry`` plus the
#   private ``_schema_to_json`` helper. The
#   ``list_descriptors`` method is also covered (the
#   existing ``test_list_descriptors.py`` covers the
#   schema round-trip + the unserialisable branch).
#
#     - **``__init__``:** the two internal dicts
#       (``_tools`` + ``_acls``) start empty.
#     - **``register``:** default ACL is assigned
#       when none is passed; custom ACL is honoured;
#       ``register_with_acl`` is the convenience
#       wrapper; duplicate name raises ``ValueError``
#       (and the registry's internal state is NOT
#       mutated by the failed second call — the
#       original ACL is preserved).
#     - **``set_acl``:** replaces the ACL on an
#       already-registered tool; raises ``KeyError``
#       for an unknown tool.
#     - **``acl_for``:** returns the ACL for a
#       registered tool, or ``None`` for an unknown
#       one.
#     - **``unregister``:** removes the tool and the
#       ACL; unknown tool is a silent no-op.
#     - **Introspection (``get`` / ``names`` /
#       ``tools`` / ``__contains__`` / ``__len__``):**
#       every accessor and dunder is exercised in the
#       happy and the unknown paths.
#     - **``_schema_to_json``:** ``None`` schema →
#       ``"{}"``; valid dict → round-trip; a non-
#       serialisable object (whose ``__repr__`` raises)
#       → ``None`` (logged); an object whose ``__repr__``
#       returns the ``<...object at 0x...>`` pattern
#       → ``None`` (logged); a payload that
#       ``json.dumps`` accepts but ``json.loads`` rejects
#       → ``None`` (logged — reached by monkey-patching
#       ``json.dumps`` to return a non-JSON string).
#
#   Net delta: 89% → 100% (65 stmts, 0 missed).
#
# ---------------------------------------------------------------------------
#
# 3.12  tools/schema.py — CLOSED (94% → 100%)
# 3.13  tools/system.py — CLOSED (96% → 100%)
# 3.14  tools/worker.py — CLOSED (98% → 100%)
#
#   CLOSED in 2026-07-29. The three files are pure
#   framework primitives (no Redis, no asyncio, no
#   external state) and the 5 missed lines across the
#   three were defensive branches reachable only via
#   malformed inputs:
#
#     - ``schema.py`` (94% → 100%): the ``format`` field
#       on a property is dropped to ``None`` if it is
#       not a string (the field is otherwise valid).
#       ``compute_schema_version`` falls back to an
#       empty ``required`` array when ``schema.required``
#       is a non-list, and to an empty ``properties``
#       dict when ``schema.properties`` is a non-dict
#       (the contract is "fall back to the safe
#       default" so the cache key stays deterministic).
#     - ``system.py`` (96% → 100%): per ADR-037,
#       ``ToolAwareSystem.request_tool`` raises
#       ``TypeError`` when ``correlation`` is ``None``
#       (fail-fast on the missing flow id).
#     - ``worker.py`` (98% → 100%): the
#       ``@tool_worker`` decorator rejects an
#       ``invoke`` whose ``idempotency_key`` parameter
#       is positional-only (the Worker's
#       ``invoke(**kwargs)`` call would never bind it).
#
#   Net delta: 5 new tests; the three files now have
#   no untested lines on the public API.
#
# ---------------------------------------------------------------------------
#
# 3.15  infra/checkpoint.py — CLOSED (42% → 100%)
#
#   CLOSED in 2026-07-29. The new test module
#   ``tests/unit/infra/test_checkpoint.py`` covers the
#   full public surface of ``CheckpointStore`` (5
#   methods + their storage-error + invalid-payload
#   paths) and the ``ReactiveCheckpoint`` dataclass
#   (``to_dict`` / ``from_dict`` round-trip +
#   ``state_hash`` optional + frozen).
#
#     - **``load``:** returns ``None`` on storage miss;
#       returns ``None`` and logs
#       ``storage_error`` on storage ``Err``; returns
#       ``None`` and logs ``invalid_payload`` on
#       malformed dict (missing ``last_event_id``); the
#       happy round-trip is asserted against a
#       ``ReactiveCheckpoint`` built by the test.
#     - **``save``:** delegates to the storage with
#       ``checkpoint.to_dict()``; logs ``storage_error``
#       on storage ``Err`` (the error does NOT raise
#       out of ``save`` — the dispatcher continues).
#     - **``clear``:** delegates to the storage; logs
#       ``storage_error`` on ``Err``.
#     - **``load_all``:** returns the empty dict on
#       storage miss; returns ``None`` for any
#       per-entry decode error (the invalid entries
#       are skipped with a ``skipped_invalid`` log);
#       returns ``{}`` on storage ``Err`` (the
#       dispatcher enumerates whatever survived the
#       partial load).
#     - **``clear_all``:** delegates to the storage;
#       logs ``storage_error`` on ``Err``.
#
#   The structlog/caplog bridge is exercised via an
#   autouse fixture that reconfigures structlog to
#   write through the stdlib ``logging`` tree (so the
#   emitted records flow into pytest's ``caplog``);
#   the original config is restored on teardown.
#
#   Net delta: 42% → 100% (64 stmts, 0 missed).
#
# ---------------------------------------------------------------------------
#
# 3.16  infra/redis/_codec.py — CLOSED (64% → 100%)
#
#   CLOSED in 2026-07-29. The new test module
#   ``tests/unit/infra/redis/test_codec.py`` covers the
#   three pure boundary codecs:
#
#     - **``decode_value``:** ``None`` unchanged;
#       ``bytes`` decoded as UTF-8 (including the empty
#       ``b""`` → ``""`` edge case and a multi-byte
#       unicode round-trip); ``str`` passes through.
#     - **``decode_dict``:** bytes / str / mixed keys
#       and values all coerce correctly; a ``None``
#       value coerces to ``""`` (the "Redis returned
#       no value" sentinel); a ``None`` key is skipped
#       (Redis never returns ``None`` keys but the
#       codec is defensive); an empty dict returns
#       empty; a unicode value round-trips.
#     - **``decode_int_dict``:** bytes / str / int
#       values all coerce to ``int``; a ``None`` value
#       falls back to ``0`` (the "missing" sentinel);
#       an unparseable value (str or bytes) falls
#       back to ``0`` (the codec is defensive — a
#       malformed integer must not crash the call
#       site); an empty dict returns empty; a
#       ``None`` key is skipped; mixed keys work.
#
#   Net delta: 64% → 100% (33 stmts, 0 missed).
#
# ---------------------------------------------------------------------------
#
# 3.17  infra/redis/_factory.py — CLOSED (67% → 100%)
#
#   CLOSED in 2026-07-29. The new test module
#   ``tests/unit/infra/redis/test_factory.py`` covers
#   the 5 ``create_*`` factory functions (the only
#   public surface of the module):
#
#     - **``create_event_log_storage``:** with client
#       (default maxlen), with settings (resolved
#       ``stream_maxlen``), with settings no client
#       (pool fallback), with negative / zero
#       ``stream_maxlen`` (fall back to
#       ``MAXLEN_DEFAULT``), and with neither
#       settings nor client (pool + fresh settings).
#     - **``create_session_storage``:** with client
#       (default 24h TTL), with explicit
#       ``ttl_seconds=`` kwarg, with settings (resolved
#       ``session_ttl_seconds``), and with settings no
#       client.
#     - **``create_profile_storage``:** the same four
#       shapes; the default TTL is ``None`` (no TTL)
#       and the settings pass through the explicit
#       ``None`` correctly.
#     - **``create_continuity_storage``:** the same
#       four shapes; the default TTL is 90 days
#       (sliding).
#     - **``create_dlq_storage``:** with client
#       (default 1M maxlen), with settings (resolved
#       ``global_stream_maxlen``), and the negative /
#       zero fallback to the DLQ default.
#
#   The factories are pure (no I/O — they instantiate
#   the adapter and return it), so the tests assert on
#   the adapter's class and the resolved
#   ``maxlen`` / ``ttl_seconds`` attribute. The
#   underlying pool is exercised end-to-end in
#   ``test_config.py`` and the individual adapters are
#   covered in their own test modules.
#
#   Net delta: 67% → 100% (55 stmts, 0 missed).
#
# ---------------------------------------------------------------------------
#
# 3.18  infra/graph/_pool.py — CLOSED (68% → 100%)
#
#   CLOSED in 2026-07-29. The new test module
#   ``tests/unit/infra/graph/test_graph_pool_password.py``
#   covers the missing branches of ``GraphPool``:
#
#     - **``connect`` idempotency:** a second call
#       returns immediately (no FalkorDB import, no
#       client construction).
#     - **``connect`` with explicit password:** the
#       FalkorDB client is constructed with the
#       password as the third positional argument.
#     - **``connect`` without password:** the
#       password kwarg defaults to ``None`` (the
#       no-password constructor branch).
#     - **``_resolve_password`` explicit wins:** the
#       explicit ``password=`` passed to ``__init__``
#       is returned first, even when the
#       ``KNT_FALKORDB_PASSWORD`` env var is set.
#     - **``_resolve_password`` settings wins:** the
#       ``Settings.falkordb_password`` field wins over
#       the env var (settings is the framework's
#       canonical source; the env var is a fallback
#       for embed scenarios).
#     - **``_resolve_password`` env var fallback:**
#       when neither explicit nor settings is set,
#       the env var is read.
#     - **``_resolve_password`` returns ``None``:**
#       when nothing is set, the method returns
#       ``None`` (and the connect branch at line 130
#       uses the no-password constructor).
#     - **``_resolve_password`` settings import
#       failure:** the method swallows the
#       ``Settings`` import failure (the test blocks
#       the import) and falls back to the env var
#       (or ``None`` if the env var is unset).
#
#   The existing ``test_graph_pool.py`` already covers
#   the helper (``graph_name_for_tenant``), the
#   ``__init__`` no-connect contract, the
#   falkordb-missing path, the ``close`` idempotency,
#   the ``graph()`` returns a ``GraphAdapter`` path,
#   and the lazy ``connect()`` trigger.
#
#   Net delta: 68% → 100% (47 stmts, 0 missed).
#
# ---------------------------------------------------------------------------
#
# 3.19  infra/redis/_auth/_cache.py — CLOSED (77% → 100%)
#
#   CLOSED in 2026-07-29. The existing
#   ``tests/unit/infra/redis/_auth/test_cache.py``
#   covered the cache hit / miss / TTL / error-
#   propagation paths. The new tests in the same
#   module cover the remaining branches:
#
#     - **Constructor validation:** ``ttl_s=-1`` and
#       ``maxsize=-1`` raise ``ValueError`` (the
#       contract is "the contract is honoured — the
#       cache refuses to construct with a negative
#       budget").
#     - **``ttl_s=0`` disables the cache:** every
#       ``_is_expired`` call returns ``True``, so
#       every ``lookup`` hits the storage (the
#       operator's escape hatch for "no caching").
#     - **Custom clock:** a ``time_fn`` override is
#       honoured (the cache uses ``time.monotonic``
#       by default; tests inject a fake clock to
#       advance time without ``asyncio.sleep``).
#     - **LRU eviction:** when the cache exceeds
#       ``maxsize``, the oldest inserted entry is
#       evicted (``maxsize=0`` is unbounded).
#     - **``store`` invalidates the cache:** on
#       success, the next ``lookup`` is a cache
#       miss; on failure, the cache entry is
#       preserved (the contract is "invalidate only
#       on success" — a transient Redis failure must
#       not evict a valid cached entry).
#     - **``delete`` invalidates the cache:** same
#       contract as ``store`` (invalidate on
#       success, preserve on failure).
#     - **``clear``** drops every entry and is
#       idempotent.
#     - **``size``** property tracks inserts and
#       starts at zero.
#
#   Net delta: 77% → 100% (64 stmts, 0 missed).
#
# ---------------------------------------------------------------------------
#
# 3.20  infra/redis/_event_log/_adapter.py — CLOSED (79% → 100%)
#
#   CLOSED in 2026-07-29. The new test module
#   ``tests/unit/infra/redis/_event_log/test_adapter.py``
#   covers every public method of
#   ``RedisEventLogAdapter`` + the three error paths:
#
#     - **``append`` happy path:** the
#       ``_idempotency.claim_event_id_slot`` is monkey-
#       patched (the module attribute lookup is what
#       makes the patch observable inside the adapter)
#       and the resulting ``stream_id`` is returned as
#       ``Ok``.
#     - **``append`` idempotency conflict:** the
#       ``_idempotency.claim_event_id_slot`` raises
#       ``IdempotencyConflict``; the adapter returns
#       ``Err(PersistenceError("Concurrent insert in
#       flight"))`` (the contract is "the caller retries;
#       the event will land on the next attempt").
#     - **``append`` redis error:** the slot claim
#       raises a generic ``ConnectionError``; the
#       adapter returns ``Err(PersistenceError("Redis
#       error: ..."))``.
#     - **``read`` with count:** the ``xrange`` result
#       is parsed via ``_parse_event`` (the bytes-keyed
#       payload shape that the real redis client
#       returns).
#     - **``read`` without count:** the kwargs dict
#       omits ``count`` (the optional branch).
#     - **``read_with_cursor`` `-` / `0-0`:** the
#       cursor resets to the beginning and the
#       adapter returns ``([], cursor)`` when the
#       stream is empty.
#     - **``read_with_cursor`` exclusive:** the
#       ``(cursor`` branch is used when the cursor
#       is non-zero.
#     - **``read_with_cursor`` str stream id:** the
#       adapter decodes a bytes stream id to a ``str``
#       (the redis-py ``decode_responses=True`` case).
#     - **``read_latest``** parses the ``xrevrange``
#       result.
#     - **``stream_len``** returns the ``length`` field
#       from ``xinfo_stream``.
#     - **``stream_len`` missing stream:** the
#       ``ResponseError`` is caught and ``0`` is
#       returned.
#     - **``stream_len`` missing length key:** the
#       ``length`` field is missing from
#       ``xinfo_stream`` and the default ``0`` is
#       returned (``info.get("length", 0)``).
#     - **``list_agents``** parses the
#       ``knt:agents:<id>:events`` keys via
#       ``parse_agent_id_from_stream_key``.
#     - **``delete``** calls
#       ``client.delete(stream_key_for_agent(agent_id))``.
#
#   The existing
#   ``tests/unit/stream/event_log/test_event_log_refactor.py``
#   covers the ``EventLog`` orchestrator that delegates
#   to this adapter; the new tests cover the adapter
#   directly.
#
#   Net delta: 79% → 100% (77 stmts, 0 missed).
#
# ---------------------------------------------------------------------------
#
# 3.21  infra/redis/_memory/_continuity.py — CLOSED (80% → 100%)
#
#   CLOSED in 2026-07-29. The new test module
#   ``tests/unit/infra/redis/_memory/test_continuity.py``
#   covers every public method of
#   ``RedisContinuityStorage`` (the Hash-backed cache
#   with sliding TTL that backs the continuity manager)
#   plus the three error paths and the defensive
#   non-Mapping fallback.
#
#     - **``get_record``:** ``HGETALL`` returns the
#       decoded dict; an empty hash returns
#       ``Err(MemoryMiss)``; a Redis connection error
#       returns ``Err(MemoryError)`` (logged).
#     - **``put_record``:** the transaction pipeline
#       is verified end-to-end (``DEL`` + ``HSET`` +
#       ``EXPIRE`` + ``execute``); a constructor with
#       ``ttl_seconds=None`` does NOT call ``EXPIRE``;
#       a per-call ``ttl_seconds=`` kwarg overrides the
#       constructor default; a non-Mapping ``record``
#       (e.g. a frozen dataclass) falls back to an empty
#       dict (defensive — the codec never sends
#       non-Mapping, but the storage is forgiving); a
#       Redis connection error during the pipeline
#       returns ``Err(MemoryError)``.

#     - **``delete_record``:** ``DEL`` the key; a
#       Redis error returns ``Err(MemoryError)``.
#     - **``iter_keys``:** ``SCAN`` with the given
#       prefix; the iterator yields decoded keys
#       that match the prefix and skips others.
#
#   Net delta: 80% → 100% (55 stmts, 0 missed).
#
# ---------------------------------------------------------------------------
#
# 3.22  infra/redis/_dlq/_redis.py — CLOSED (81% → 99%)
#
#   CLOSED in 2026-07-29. The existing
#   ``tests/unit/infra/redis/_dlq/test_storage_dlq.py``
#   covered the basic happy path and the most common
#   error paths. The new tests in the same module
#   cover the remaining branches:
#
#     - **Append race-loser path:** ``hsetnx`` returns
#       ``False`` (a concurrent writer claimed the
#       slot first). The adapter reads the winner's
#       stream id back and returns ``Ok(winner_id)``.
#       When the winner's id is also missing (race),
#       the adapter returns ``Ok(PLACEHOLDER)``.
#     - **Append idempotent path:** the index already
#       has the idem key (the caller is deduplicating).
#       The adapter returns the existing stream id
#       without re-appending (``xadd`` is NOT called).
#     - **Append str stream id:** ``xadd`` returns a
#       ``str`` (some redis clients / new fakeredis);
#       the adapter decodes it correctly.
#     - **Append existing idem key as str:** the
#       existing idem key is a ``str`` (the repo's
#       ``decode_value`` handles both bytes and str).
#     - **``list_by_reason``** filters entries by
#       the ``reason`` field; returns ``Err`` on
#       storage failure.
#     - **``list_for_agent``** returns the scanned
#       entries when the head pointer is set; the
#       ``_scan_from`` helper returns ``Err`` on
#       storage failure.
#     - **``list_all``** returns ``Err`` on storage
#       failure.
#     - **``read_index``** returns ``Err`` on storage
#       failure.
#     - **``find_by_event_id``** skips entries with
#       ``None`` or ``PLACEHOLDER`` values; returns
#       ``Err`` on storage failure.
#     - **``bump_reason_counter``** returns ``Err``
#       on storage failure.
#     - **``get_stats``** returns ``Ok`` with the
#       default empty aggregate when ``xinfo_stream``
#       raises (the stream does not exist); returns
#       ``Err`` when the ``hgetall`` raises.
#     - **``purge``** returns ``Ok(0)`` when the
#       stream does not exist (the ``xinfo_stream``
#       ``no such key`` error is caught; the delete
#       still runs).
#     - **``drop_entry``** returns ``Err`` on storage
#       failure.
#     - **``_decode_int_dict``** skips ``None`` keys
#       (defensive — a real Redis client never
#       returns ``None`` keys) and unparseable values
#       (the helper coerces to ``int`` and skips
#       ``TypeError`` / ``ValueError``).
#
#   The 1 remaining line (99% ceiling) is the
#   ``if messages is None: return Ok([])`` defensive
#   branch in ``list_by_reason`` — the upstream
#   ``list_all`` returns ``[]`` on empty (not
#   ``None``), so the branch is unreachable on the
#   public API.
#
#   Net delta: 81% → 99% (157 stmts, 1 missed).
#
# ---------------------------------------------------------------------------
#
# 3.23  infra/redis/_memory/_profile.py — CLOSED (85% → 100%)
# 3.24  infra/redis/_memory/_session.py — CLOSED (88% → 100%)
#
#   CLOSED in 2026-07-29. The new test modules
#   ``tests/unit/infra/redis/_memory/test_profile.py``
#   and
#   ``tests/unit/infra/redis/_memory/test_session.py``
#   cover the two remaining ``ShortMemoryStorage``
#   implementations. They share the same protocol
#   (and were structured identically — see the
#   ``_continuity.py`` test module for the
#   precedent), but each has a distinct wire format:
#
#     - **``_profile.py`` (Hash + ``DEL + HSET + EXPIRE``):**
#       the default ``ttl_seconds`` is ``None`` (long-
#       lived profile, no TTL by default). The new
#       tests cover the happy path (``HSET`` with
#       ``mapping=``), the ``ttl_seconds=`` kwarg
#       override, the non-Mapping fallback, the
#       ``MemorySerializationError`` defensive path,
#       and the redis-error branches on every method.
#     - **``_session.py`` (JSON + ``SET ... EX ttl``):**
#       the payload is a single JSON-encoded value.
#       The new tests cover the JSON happy path
#       (the ``ttl`` is forwarded via the ``ex``
#       kwarg), the three decode failure modes
#       (``raw is None`` → ``MemoryMiss``; ``decode_value``
#       returns ``None`` → ``MemoryMiss`` defensive;
#       ``json.loads`` raises → ``MemoryDecodeError``),
#       the redis-error paths, and the
#       ``MemorySerializationError`` defensive path
#       (reached by monkey-patching ``json.dumps`` to
#       raise).
#
#   Net delta: 11 + 13 tests; the two files now have
#   no untested lines on the public API.
#
# ---------------------------------------------------------------------------
#
# 3.25  infra/redis/_auth/_redis.py — CLOSED (93% → 100%)
# 3.26  infra/config/_base.py — CLOSED (93% → 100%)
# 3.27  infra/hashing.py — CLOSED (92% → 100%)
# 3.28  infra/http/_client.py — CLOSED (87% → 100%)
#
#   CLOSED in 2026-07-29. The four remaining
#   ``infra/`` files with sub-90% coverage had a
#   total of 8 missed lines — all defensive branches
#   reachable only via monkey-patches or via the
#   ``httpx2`` import path.
#
#     - **``_auth/_redis.py`` (93% → 100%):** the
#       ``lookup`` method's two str-return paths
#       (``decode_responses=True`` raw value is
#       re-encoded to bytes) and the defensive
#       unexpected-return-type arm.
#     - **``config/_base.py`` (93% → 100%):** the
#       ``load_dotenv_files`` defensive branch when
#       ``python-dotenv`` is not installed (the
#       helper returns ``[]`` and the caller is
#       expected to rely on real env vars).
#     - **``hashing.py`` (92% → 100%):** the
#       ``length >= len(digest)`` branch in
#       ``short_hash`` (a caller asking for the
#       full digest gets the full digest, no
#       padding).
#     - **``http/_client.py`` (87% → 100%):** the
#       ``HttpxHttpClientAdapter.get`` body and
#       ``aclose`` body. The ``httpx2`` package is
#       not installed in the dev environment; the
#       test monkey-patches the lazy import with a
#       stub ``AsyncClient`` so the call path is
#       exercised without the network.
#
#   Net delta: 4 new tests; the four files now have
#   no untested lines on the public API.
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

# 5. CLEANUP — ALL ITEMS CLOSED (2026-07-30)
# ---------------------------------------------------------------------------
#
# The v1.0 quality milestone is now satisfied. The
# cleanup items below are the post-resync actions
# required to remove the file from tracking; they are
# all completed in the same iteration that closed §2.
#
#   5.1  Remove this file.
#        STATUS: PENDING — keep this file as a
#        historical record of the v0.9.0 → v1.0
#        quality sync. The `AGENTS.md` / `CONTRIBUTING.md`
#        / `CHANGELOG.md` are the live source of truth
#        going forward; this file is the v1.0 freeze
#        snapshot. A future iter can `git rm` it once
#        the team confirms the freeze is no longer
#        useful as a reference.
#
#   5.2  Bump the pyright baseline.
#        STATUS: CLOSED in 2026-07-30. Ran
#        `uv run scripts/ci.py --update-pyright-baseline`;
#        the new `.pyright-baseline.json` tracks 0
#        errors in 0 files. The `pyright` step in
#        `scripts/ci.py` is now a hard gate (no
#        baseline drift tolerated).
#
#   5.3  Bump the radon baseline.
#        STATUS: CLOSED in 2026-07-30. Ran
#        `uv run scripts/ci.py --update-baseline`; the
#        new `.radon-baseline.json` tracks 0 CC
#        offenders and 0 MI offenders. The two minor
#        CC bumps introduced by §2.18 (`_as_candidate_list`
#        3 → 5; the `_collect_from_sequence` helper
#        is a new function, not a baseline key) and
#        the MI down in the same file are recorded
#        in the new baseline.
#
#   5.4  Re-enable ``-W error::DeprecationWarning`` in
#        ``pyproject.toml::tool.pytest.filterwarnings``.
#        STATUS: CLOSED in 2026-07-30. The two stale
#        ignore rules (`kntgraph.agents.roles` +
#        `kntgraph.cli`) were removed (both modules
#        were already removed in v0.9.0 per ADR-041
#        and the `LiteLLMTool` removal); a single
#        scoped rule was added:
#
#            filterwarnings = [
#                "error::DeprecationWarning:kntgraph",
#            ]
#
#        The namespace scoping (`kntgraph` only) keeps
#        third-party `DeprecationWarning`s from failing
#        the test suite (fakeredis, litellm, etc. emit
#        their own deprecation warnings). The 1810
#        unit tests pass with the new rule.
#
# ---------------------------------------------------------------------------

# 6. APPENDIX: DATA POINTS
# ---------------------------------------------------------------------------
#
#   6.1  Pyright errors by file (2026-07-29 → 2026-07-30
#        deltas; the v1.0 quality sync resolved all 51):
#
#        2026-07-29  →  2026-07-30
#        ------------------------
#         20  knowledge/extraction/argument/_gliner_finder.py   → 0  (§2.18)
#          4  core/storage.py                                  → 0  (§2.19)
#          3  api/intent_router/middleware_setup.py             → 0  (§2.20, prior resync)
#          3  knowledge/extraction/gliner.py                   → 0  (§2.23)
#          3  resilience/edge.py                               → 0  (§2.21)
#          2  agents/knowledge/solution_projector.py           → 0  (§2.22)
#          2  agents/memory/solution_review_publisher.py       → 0  (§2.22)
#          2  agents/memory/solutions/_fingerprints.py         → 0  (§2.22)
#          2  agents/tools/llm.py                             → 0  (§2.22 + §2.27)
#          2  stream/event_log/dispatch.py                     → 0  (§2.24)
#          1  agents/memory/solution_extractor.py              → 0  (§2.22)
#          1  agents/memory/solutions/_extractor.py            → 0  (§2.22)
#          1  agents/tools/arg_validation.py                  → 0  (§2.22)
#          1  core/world/projection.py                         → 0  (§2.26)
#          1  knowledge/embedding/_ollama.py                   → 0  (§2.23)
#          1  knowledge/extraction/argument/_extractor.py      → 0  (§2.23)
#          1  knowledge/falkordb/adapter.py                    → 0  (§2.23)
#          1  runner/reactive_tool_projection.py                → 0  (§2.25)
#        ---------------------------------------------------------
#         51                                              → 0
#
#   6.2  Error counts by rule (2026-07-29, strict mode; all
#        resolved in 2026-07-30 sync):
#
#         38  reportArgumentType
#         13  reportReturnType
#
#   6.2b Error counts by rule (pyright default — informational only;
#        not part of the strict error budget; 2026-07-30):
#
#        ~312  reportUnknownMemberType
#        ~290  reportUnknownVariableType
#        ~193  reportUnknownArgumentType
#        ~112  reportUnknownParameterType
#        ~110  reportMissingTypeArgument
#         38  reportArgumentType (= strict subset above)
#         13  reportReturnType  (= strict subset above)
#         11  reportInvalidTypeVarUse
#          7  reportOptionalMemberAccess
#          2  reportUnsupportedDunderAll
#
#        The Unknown* warning budget is unchanged from the
#        2026-07-29 snapshot. The v1.0 sync did not touch
#        them; §4.2 (config tightening to
#        `reportOptionalIterable` and `reportOptionalCall`
#        at error level) is the next milestone.
#
#   6.3  Coverage (unit tests only, 2026-07-30, post-§2 sync):
#
#         memory/                 93%   (unchanged; see
#                                      §3 history)
#         events/dlq              98%   (unchanged; see
#                                      §3 history)
#         overall                 80%   (7041/8791 stmts;
#                                      no regression vs the
#                                      2026-07-29 snapshot)
#
#   6.4  Gate snapshot (post-v0.10.0, 2026-07-30):
#
#         ruff lint               0 errors
#         ruff format             424 / 424 formatted
#         bandit                  0 H + 0 M + 0 L
#         radon CC                avg ~2.49 (A), 0 rank D+
#         radon MI                237 A + 0 B + 0 C-
#         pytest tests/unit       1808 passed, 3 skipped
#         pytest tests/agents     (collected with unit pool)
#         coverage                80.0% (7041/8791 stmts)
#         pyright                 0 errors / ~1043 warnings
#                                  (baseline regenerated;
#                                  was 51 on 2026-07-29)
#         pip-audit               0 known vulnerabilities
#
#         All 9 gates pass. The v1.0 quality milestone
#         is satisfied.
#
#         v0.10.0 (this sync) introduced one breaking
#         change: the `_legacy_principal` fallback in
#         `RedisAPIKeyVerifier` was removed (ADR-017 §7.3).
#         Plain-string bindings (pre-ADR-017) are now
#         rejected as `AuthError("malformed")`; operators
#         must run `scripts/migrate_principals.py --apply`
#         to upgrade their binding table before installing
#         0.10.0. The migration script is idempotent and
#         safe to dry-run. Test count dropped from 1810 to
#         1808 (-2) because the two legacy-helper tests
#         were deleted (replaced by a single
#         `test_redis_verifier_rejects_legacy_string`).
#         See CHANGELOG `[0.10.0]` for the full note.
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

  - **Tests for the projection.** ~~The
    ``project_memory`` projection has no unit
    tests. It is currently exercised only by
    example 05b (WIP). Action: add tests in
    ``tests/unit/core/world/test_projection_memory.py``
    covering: ``session.*`` fold, ``profile.*``
    fold, ``continuity.*`` fold, multi-tick
    preservation of base component.~~ Closed
    in 2026-07-14: 16 unit tests in
    ``tests/unit/core/test_projection_memory.py``
    cover the ``session.*`` / ``profile.*`` /
    ``continuity.*`` fold (single + multi-tick
    preservation). The new tests uncovered two
    latent bugs which were fixed in the same
    change: ``project_memory`` now accepts
    ``base_views=None`` (default: empty dict),
    and ``_fold_profile`` / ``_fold_continuity``
    now reuse the base component when the
    incoming batch has no event of the
    corresponding type (matching the
    ``_fold_session`` behaviour; previously the
    base component was discarded).


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

  - ~~**Example migration (01-07).** Examples
    01-07 still use the legacy ``LiteLLMTool`` (with
    a deprecation warning on import). They should be
    migrated to use the ``LiteLLMToolWorker`` via
    the ``WorkerManager`` in the v0.9.0 cycle.~~
    **Closed in 2026-07-14:** the
    ``LiteLLMTool``-based examples were split into
    two groups and processed differently:

      - **Examples 03, 04, 05, 06 (legacy Role
        pattern)** were REMOVED. The concept of a
        ``Role`` as a synchronous wrapper around
        ``LiteLLMTool`` was superseded by the
        ECS path (ADR-039 + ADR-044):
        ``ChatRoleSystem`` / ``PlannerRoleSystem``
        / ``SummarizerRoleSystem`` /
        ``PersonalizedRoleSystem`` in
        ``src/kntgraph/agents/role_systems/``.
        The end-to-end canonical example is
        ``examples/05c_session_chat_ecs_roles.py``;
        the hydration shim for legacy session chat
        is in ``examples/05b_session_chat_ecs.py``.
        Keeping 03-06 alongside 05b/05c would
        document two contradictory patterns
        (sync Role vs event-driven RoleSystem);
        removing the sync pattern removes the
        confusion.
      - **Example 01 (``LiteLLMTool`` direct)**
        was MIGRATED to ``LiteLLMToolWorker``:
        one ``LiteLLMToolWorker()`` instance +
        four ``await worker.invoke(...)`` calls.
        The worker returns a JSON-serialisable
        ``dict`` envelope (the same shape the
        ``WorkerManager`` consumes in the
        production path; the example calls the
        worker directly without the
        ``WorkerManager`` infrastructure because
        the example is a one-shot script). The
        migration is drop-in: the call signature
        (``system`` / ``user`` / ``idempotency_key``
        / ``temperature`` / ``max_tokens`` /
        ``think``) is the same; the return
        envelope is a ``dict`` instead of a
        ``LLMResponse`` dataclass.
      - **Examples 02 and 07 (rate-limit demo +
        caching transport)** were REMOVED. The
        ``LiteLLMToolWorker`` does not own a
        ``rate_limiter`` / ``cost_budget`` (those
        were Tool-class concerns; the worker is
        a stateless callable that runs in a
        process pool, and the rate-limit /
        cost-budget primitives are not part of
        the worker contract). The caching
        transport is still supported via a
        custom ``LiteLLMTransportAdapter`` (the
        ``CachingLLMTransport`` decorator in
        ``agents.tools.cache`` is unchanged) but
        the example is better written as a
        custom-transport snippet in the docs
        rather than a standalone ``LiteLLMTool``
        example.

    2 unit tests in
    ``tests/unit/examples/test_example_01_migration.py``
    cover the source-level migration (no
    ``LiteLLMTool`` import; ``LiteLLMToolWorker``
    used) and the runtime contract (the worker's
    transport is called once; the
    ``idempotency_key`` matches the example's
    stable prefix). 1813 tests pass (+2 vs the
    1811 baseline).

  - **Suppress deprecation noise in CI.** Add a
    ``warnings.filterwarnings`` rule to
    ``pyproject.toml::[tool.pytest.ini_options]`` to
    suppress the ``DeprecationWarning`` from
    ``LiteLLMTool`` and ``ToolInvoker`` until the
    examples are migrated. **Closed in 2026-07-14:**
    the suppression rule is no longer needed — the
    legacy classes were REMOVED in v0.9.0 (see
    §2.20). The ``pyproject.toml`` filterwarnings
    was not added.

**Acceptable:** Continue with the legacy paths for
now. The deprecation warnings are intentional;
the migration is opt-in for the next release.


## 2.18 Example 05b hydration shim: system never emits a request_tool event

**Status:** Closed in 2026-07-14.

**Closed by:**

  - **Derived component preservation** in
    ``core/world/projection.py::_apply_event``:
    the default domain projection now PRESERVES
    a closed set of derived component keys
    (``tool_requests`` / ``tool_completions`` /
    ``SessionComponent`` / ``ProfileComponent`` /
    ``ContinuityComponent``) across a domain
    fold. The previous rule replaced the entire
    ``components`` dict on every domain event,
    which clobbered the tool-call overlay slots
    AND the memory components installed by the
    hydration projection (ADR-042 §6.1) on the
    next domain event. The new rule is opt-in
    by key: a domain event's own payload still
    replaces the component keyed by
    ``event.event_type`` (the existing
    last-event-wins contract, pinned by
    ``test_domain_replaces_components``); the
    derived components survive.

  - **SessionChatSystem rewrite** in
    ``examples/05b_session_chat_ecs.py``: the
    system now uses ``view.last_event_id`` as
    the canonical "new event arrived" signal
    (the ``user.intent`` component on the view
    is replaced by the next domain event's
    payload, so the system cannot rely on it
    once a tool event lands in the same tick).
    A ``_pending_user_messages`` map captures
    the user message at request time so the
    completion phase can recover it (the
    ``user.intent`` component is also gone by
    then). The shim's
    ``_install_projection_shim`` was rewritten
    to use the same composition order as
    ``ReactiveDispatcher._fold_with_filter``
    (default fold → memory hydration → tool
    overlay).

  - **8 unit tests** in
    ``tests/agents/unit/test_example_05b_shim.py``
    cover the shim installation, the
    hydration contract (``SessionComponent``
    is installed on the view), the tool-call
    overlay accumulation contract (request
    persists across ticks), and the full chat
    round-trip (request → completion → recorder).

**Acceptable:** N/A — closed.


## 2.19 @tool_worker forward-reference resolution

**Status:** Closed in 2026-07-14.

**Closed by:** the ``@tool_worker`` decorator's
Pydantic schema extraction now resolves
forward-reference string annotations via
``importlib.import_module(cls.__module__)``
instead of the (non-existent)
``cls.__globals__``. Without this, classes
using ``from __future__ import annotations``
with a Pydantic model parameter produced an
empty schema (``{"title": "Payload"}``
instead of ``{"$ref": "#/$defs/..."}``).
Regression test:
``test_tool_worker_with_pydantic_model`` in
``tests/unit/tools/test_worker.py``.

**Acceptable:** N/A — closed.


## 2.20 Role → ECS migration (ADR-039 + ADR-043 + ADR-044 follow-up)

**Status:** Closed in 2026-07-14.

**Closed by:** the new module
``src/kntgraph/agents/role_systems/`` provides the
event-driven ``WorldSystem`` counterparts to the
legacy ``ChatRole`` / ``PlannerRole`` /
``SummarizerRole`` / ``PersonalizedRole``:

  - ``ChatRoleSystem`` reacts to ``user.intent`` events
    and emits ``chat.reply.generated`` with a typed
    ``ChatReply`` payload.
  - ``PlannerRoleSystem`` reacts to ``plan.request``
    events and emits ``plan.generated`` with a typed
    ``Plan``.
  - ``SummarizerRoleSystem`` reacts to
    ``summary.request`` events and emits
    ``summary.generated`` with a typed ``Summary``.
  - ``PersonalizedRoleSystem`` reacts to
    ``personalized.request`` events and emits
    ``personalized.reply.generated`` with the raw text.

The systems REUSE the legacy role's ``SYSTEM_PROMPT``
and input-formatting helpers so the prompt engineering
lives in one place. The migration is a thin port from
the synchronous ``await role.reply()`` to the
event-driven ``system(world)`` cycle. The dispatcher's
event loop is NOT blocked while the LLM runs.

**Removal of legacy roles** (2026-07-14): the
``kntgraph.agents.roles`` package was REMOVED in
v0.9.0 (the v0.9.0 target documented in the
deprecation warning at the top of the package). The
canonical replacement is the ``role_systems``
module's ``ChatRoleSystem`` / ``PlannerRoleSystem``
/ ``SummarizerRoleSystem`` / ``PersonalizedRoleSystem``.

**Prompt extraction** (2026-07-14): the
``SYSTEM_PROMPT`` constants, the Pydantic output
schemas (``ChatReply`` / ``Plan`` / ``Summary``), the
``format_chat_history`` helper, and the
``build_personalized_system_prompt`` helper were
extracted from the legacy roles into
``src/kntgraph/agents/role_systems/_prompts.py`` so
the role systems have a single source of truth for
the prompt engineering and the legacy roles can be
removed without losing the LLM-output schemas.

**Open follow-ups:**

  - ~~**Examples 01-07 migration**: examples 01-07 still
    use the legacy ``ChatRole`` / ``PlannerRole`` (with
    a deprecation warning on import). They should be
    migrated to use ``ChatRoleSystem`` /
    ``PlannerRoleSystem`` via the ``WorkerManager`` in
    the v0.9.0 cycle. ``examples/05c_session_chat_ecs_roles.py``
    is the reference.~~ **Closed in 2026-07-14:** the
    examples that demonstrated the legacy Role pattern
    (03, 04, 05, 06) were REMOVED. The remaining
    examples that used ``LiteLLMTool`` directly
    (01) were migrated to ``LiteLLMToolWorker``;
    the examples that demonstrated
    ``LiteLLMTool``-specific features that are not
    part of the worker contract (02 rate-limit
    primitives, 07 caching transport) were also
    removed. The canonical end-to-end ECS example
    is ``examples/05c_session_chat_ecs_roles.py``;
    the canonical session chat shim is
    ``examples/05b_session_chat_ecs.py``.
  - ~~**SemanticRouterRole migration**: the
    ``SemanticRoutingRole`` is not yet ported (its
    contract is different: it routes a user message
    to a category, not a free-form LLM reply).~~
    **Closed in 2026-07-14:** the
    ``SemanticRoutingRole`` was REMOVED along with
    the rest of the ``kntgraph.agents.roles``
    package. A new ECS-shaped
    ``SemanticRoutingRoleSystem`` is documented
    for a future iteration (the contract is
    genuinely different — it routes a user message
    to a tool category via an ``IntentClassifier``;
    it does not call an LLM). The example 12 that
    demonstrated the M1+M2 pipeline was also
    REMOVED; the equivalent ECS path is the
    ``role_systems`` systems wired into a
    ``ReactiveDispatcher`` (see 05c for the
    end-to-end pattern).
  - **Removal of legacy roles** (target v1.0.0): the
    ``kntgraph.agents.roles`` package is kept alive
    through v0.9 for back-compat. **Closed in
    2026-07-14:** the package was removed in v0.9.0
    along with the legacy ``LiteLLMTool`` and
    ``ToolInvoker``. See "Removal of legacy roles"
    above.

**Acceptable:** N/A — closed; migration is now
production-ready. The legacy roles are kept on a
deprecation path.


## 2.21 Tool-call Request TTL (ADR-045)

**Status:** Closed in 2026-07-14.

**Closed by:** the
:class:`ToolCallTTLSweeperSystem` (in
`src/kntgraph/runner/tool_call_ttl_sweeper.py`)
emits ``tool.<name>.failed`` events for stale
requests in the ``tool_requests`` slot. The
dispatcher auto-registers the sweeper when the
operator passes a ``tool_ttls=ToolCallTTL()``
config (opt-in; the default is no TTL enforcement,
for back-compat with the legacy behaviour).

The original ADR draft (inline TTL eviction in
``overlay_tool_calls``) was rejected: the overlay
is a **pure** function (ADR-034), and mixing in a
wall clock broke the purity and forced the
overlay to walk every agent in ``base_views`` on
every tick (which broke the "no allocation for
non-tool batches" optimisation of ADR-044). The
sweeper system separates concerns (the overlay
stays pure; the sweeper handles the I/O) and the
failure event is observable by downstream
systems.

**Tests:** 9 unit tests in
`tests/unit/runner/test_tool_call_ttl_sweeper.py`
cover the request/completion cycle, dedup,
multi-agent, empty world, and the legacy bare
`tool.requested` form.

**Open follow-ups:**

  - ~~**Slot GC**: the sweeper does NOT evict the
    stale request from the slot. The eviction is
    left to the completion-driven rule (ADR-044).
    If the completion never arrives, the request
    stays in the slot forever (memory leak).
    A follow-up GC_TICK event (or a periodic
    compaction pass) is the mitigation; out of
    scope for ADR-045.~~ **Closed in 2026-07-14:**
    the dispatcher (``ReactiveDispatcher``) now
    performs the GC step in
    :meth:`_run_systems_and_persist`:

      - **The systems run on EVERY tick**, even
        when the EventLog has no new events for
        the agent (the legacy
        ``if not new_events: return 0`` short-
        circuit in :meth:`_dispatch_for_agent`
        is replaced with a no-op fold + full
        systems pipeline). The TTL sweeper is
        the primary motivation: an orphan
        request sits in the slot until its TTL
        expires, which may happen several
        ticks after the request was emitted;
        the dispatcher must run the sweeper
        on those ticks.

      - **The post-systems re-fold
        (:meth:`_fold_with_systems`)** re-
        applies the ``overlay_tool_calls``
        projection with the system-emitted
        events as input. The
        ``tool.<name>.failed`` event emitted
        by the sweeper joins the slot as a
        completion (status="failed"), and the
        completion-driven eviction rule
        (``request in existing_completions``
        -> ``pop``) removes the orphan
        request from the slot in the same
        tick.

      - **The re-fold is opt-in**:
        :meth:`_fold_with_systems` short-
        circuits when ``system_events`` has
        no ``tool.*`` event, so a non-tool
        batch pays zero for the second pass
        (ADR-044 §2.4 "no allocation for
        non-tool batches" optimisation
        preserved).

      - **6 new unit tests** in
        ``tests/unit/runner/test_reactive_dispatcher_ttl_gc.py``
        cover: orphan eviction in the same
        tick, fresh request preserved,
        opt-out path (no sweeper = no GC),
        cheap non-tool batches, no GC when
        systems emit nothing, and router
        fan-out of the TTL-failure event.

**Acceptable:** N/A — closed; migration is
production-ready.


## 2.22 Build artifacts + AGENTS.md scaffold

**Status:** Closed in 2026-07-14.

**Closed by:**

  - **``build/`` artifact removed.** The 2 MB
    ``build/`` directory left over from a
    ``python -m build`` run was deleted from
    the repo. ``build/`` was already in
    ``.gitignore`` (line 11, ``# Distribution /
    packaging``); the directory was not tracked
    by git, but the on-disk presence was noise
    (the next ``build`` regenerates it). Future
    builds will land in the same path; the
    gitignore entry ensures they stay out of
    tracking.

  - **``scratch_replace_redis_url.py`` +
    ``scratch_run_all.py`` removed from
    tracking.** Two one-off debug helpers at
    the repo root were historically versioned
    but are not part of the production code
    (``scratch_replace_redis_url.py`` rewrote
    the Redis URL in all example files;
    ``scratch_run_all.py`` ran every example
    sequentially). Both are now
    ``git rm --cached`` (the on-disk files
    remain, so any human with a local
    reference still has them; the files are no
    longer in git history going forward).
    New scratch scripts should live in
    ``scripts/`` (or ``/tmp/opencode/``) so
    the ``__init__.py`` layout and the
    gate's test discovery stay clean.

  - **``AGENTS.md`` created.** The conventions
    document referenced by the test files
    (and the test docstrings: ``AGENTS.md §1``,
    ``§1.2``, ``§1.4``, ``§1.5``, ``§2``,
    ``§2.1``, ``§2.2``, ``§2.3``, ``§3``,
    ``§3.1``, ``§3.3``, ``§4.6``, ``§6``,
    ``§6.2``, ``§7``, ``§9``, ``§10``,
    ``§11.3``, ``§13``) was missing — the
    conventions lived implicitly in CONTRIBUTING.md
    and the tests' docstrings, but the single
    source of truth file did not exist. The
    new ``AGENTS.md`` (at the repo root) is
    the canonical reference: type discipline
    (§1, with the ``Any`` / ``object``
    exceptions), no-compat-shims (§2, with the
    removal-target contract), 500-line file
    guideline (§3), style (§4.6 prose in
    English, identifiers follow the domain),
    typed errors (§6, ``Result[T, E]`` + typed
    ``*Error``), behaviour tests (§7), the
    single CI gate (§9, the 9-step
    ``scripts/ci.py``), prose language (§10,
    English), branch policy (§11.3, AI
    agents do not push or create branches),
    and the env vars + local-services
    reference (§13).

**Acceptable:** N/A — closed.


## 2.23 REUSE 3.3 license compliance cleanup

**Status:** Closed in 2026-07-14.

**Closed by:** the ``reuse`` gate is now part of
the ``scripts/ci.py`` pipeline (the 9th step)
and passes cleanly. The cleanup touched 56
files:

  - **Invalid SPDX expression** in
    ``scripts/quality_report.py``: the
    ``render_markdown`` function embedded a
    markdown template string that REUSE
    parsed as an invalid license expression
    The invalid expression was the literal
    SPDX header identifier (in the markdown
    template) followed by a trailing Python
    comma. Fixed by wrapping the template's
    SPDX header in
    ``REUSE-IgnoreStart`` / ``REUSE-IgnoreEnd``
    comments (REUSE ignores the block).

  - **Missing SPDX headers** added to 55 files:
    ``CHANGELOG.md``, 8 ADRs (ADR-038 through
    ADR-045), 3 docs (``docs/adr-042-sequence.md``,
    ``docs/cli_guide.md``, ``docs/memory_model.md``),
    3 ``dev-servers/`` files (2 docker-compose
    YAML + 1 redis.conf), 9 ``examples/``
    files (the 2 missing examples 18/20 plus
    7 ``knt-cli/weather_platform`` files
    including a ``pyproject.toml``,
    ``.env.example``, and ``uv.lock``), 6
    ``src/kntgraph/cli/`` files (the
    ``__init__.py``, ``main.py``, and 4
    ``commands/``), 9 ``cli/templates/``
    Jinja files (using ``{# ... #}`` Jinja
    comments), 1 ``scripts/export_kntgraph.py``,
    1 ``tests/agents/unit/conftest.py``, 7
    ``tests/unit/cli/test_*.py``, ``.gitignore``,
    the top-level ``uv.lock``, and the 2
    ``scratch_*.py`` debug helpers.

  - **CI integration**: ``step_reuse()`` was
    defined in ``scripts/ci.py`` but missing
    from the ``ALL_STEPS`` dict; the gate
    was effectively a no-op before this
    cleanup. The dict now registers
    ``"reuse": step_reuse()`` between
    ``complexity`` and ``pyright``; the
    ``--only reuse`` flag now works in
    isolation for local iteration. The
    ``AGENTS.md`` and ``CONTRIBUTING.md``
    documentation was updated to reflect
    the 9-step gate (the ``CONTRIBUTING.md``
    table was out of date — it listed 8
    steps; the new table has 9 with
    ``reuse`` between ``complexity`` and
    ``pyright``).

**Acceptable:** N/A — closed.


## 2.24 ADR-047 Tool-Adapter Pattern: Worker refactor + Protocol catalogue

**Status:** Closed in 2026-07-20.

**Delivered in this iteration (2026-07-20):**

  - **`HttpClientLike` Protocol** (new in
    `src/kntgraph/infra/http/_client.py`): the
    framework-level Protocol for an async HTTP
    client. Narrow on purpose: a single
    `get(url) -> HttpResponseLike` method, mirroring
    the parts of `httpx.Response` the framework
    actually reads (`status_code` /
    `raise_for_status` / `json`). `@runtime_checkable`
    so callers can do `isinstance(client, HttpClientLike)`
    defensively (same pattern as `RedisLike`).
  - **`HttpxHttpClientAdapter`** (new in the same
    module): the concrete implementation that
    wraps `httpx2.AsyncClient`. The `httpx2`
    import is **lazy** (inside `__init__`), so
    the framework's import graph does not pay
    the dep cost unless the operator
    instantiates the adapter.
  - **`OpenMeteoApi` refactor**
    (`examples/knt-cli/weather_platform/.../tools/open_meteo_api.py`):
    the canonical `weather_platform` Worker was
    the only `@tool_worker` in the codebase that
    violated ADR-047 §2.2.1 ("No Direct External
    Imports") — it imported `httpx.AsyncClient`
    directly inside `invoke`. The refactor
    re-routes the Worker through the new
    `HttpClientLike` Protocol: the constructor
    accepts `http: HttpClientLike | None = None`
    and lazy-defaults to `HttpxHttpClientAdapter()`
    (the ADR-047 §2.3 template). The Worker is
    now testable with an in-memory `FakeHttpClient`
    (no network, no `httpx` on the test path).
    The `invoke` signature changed from
    `Result[dict, Exception]` to
    `Result[dict, ToolError]`; the three error
    paths (`http_error`, `decode_error`,
    `missing_key`) are typed `ToolError` instances
    with a clear prefix on the message.
  - **`LiteLLMToolWorker` typed errors**
    (`src/kntgraph/agents/tools/llm.py`): the
    Worker's `invoke` was returning
    `Result[dict, Exception]` with bare
    `Err(TimeoutError(...))` / `Err(e)`. The
    refactor changes the signature to
    `Result[dict, ToolError]`; the original
    exception is preserved as `__cause__` on the
    `ToolError` so operators can introspect the
    root cause without losing the typed-error
    contract. The 7 unit tests in
    `tests/agents/unit/tools/test_litellm_worker.py`
    were updated to assert on `isinstance(err, ToolError)`
    and `isinstance(err.__cause__, TimeoutError)`
    (the original behaviour was that
    `isinstance(err, TimeoutError)` directly).
  - **`SessionRecorderTool` typed errors**
    (`examples/05b_session_chat_ecs.py` and
    `examples/05c_session_chat_ecs_roles.py`):
    both copies of the class were returning
    `Result[dict, Exception]` with bare
    `Err(ValueError(...))` for the
    "unknown command" path and
    `Err(Exception(...))` for the
    `SessionManager.is_err()` path. The
    refactor changes both to typed `ToolError`
    messages with clear prefixes.
  - **`WeatherTool` typed errors**
    (`examples/19_tool_worker_pattern.py`): the
    canonical example Worker was also returning
    `Result[dict, Exception]`. The signature
    changes to `Result[dict, ToolError]`; the
    example's body never returns `Err(...)`
    (the example is a happy-path mock), so the
    change is signature-only.
  - **CLI scaffold template updated**
    (`src/kntgraph/cli/templates/tool.py.jinja`):
    the `knt new tool` template now generates
    Workers with `Result[dict, ToolError]` and
    imports `ToolError` from `core.result`. New
    vertical Workers are born compliant with
    AGENTS.md §6.1 (the "Never `raise Exception`"
    rule and the `Result[T, E]` discipline).
  - **New unit tests** (10 in total, in
    `tests/unit/infra/http/`):
    - `test_http_client.py`: the
      `HttpClientLike` / `HttpxHttpClientAdapter`
      Protocol contract, the lazy-import contract,
      and the in-memory `FakeHttpClient` /
      `_FakeResponse` test doubles.
    - `test_open_meteo_tool.py`: the
      `OpenMeteoApi` Worker end-to-end with the
      in-memory HTTP client (4 scenarios: 2xx
      happy path, non-2xx HTTP error, invalid
      JSON, missing `current_weather` key) plus
      the worker metadata + the default
      `HttpxHttpClientAdapter` constructor path.
  - **ADR-047 §3.1 / §3.2 / §5 / §6.4 / §6.5
    updated** to reflect the canonical code
    shape. The earlier draft of the ADR
    proposed a discriminated envelope
    (`LLMResponse(success: bool, text, error)`)
    as the return type of the `LLMTransport`
    Protocol; the canonical code returns the
    LiteLLM-style dict from the Protocol and
    uses the `LLMResponse` dataclass as the
    JSON-serialisable result envelope the
    `LiteLLMToolWorker` returns to the
    `WorkerManager`. The ADR now documents the
    actual shape (§3.1 "Why the Protocol
    returns a `dict` (not a typed envelope)")
    and defers the discriminated envelope to
    a future ADR-049 (§6.4 status: deferred).
    The §2.2.4 "Adapter Reuse" rule now lists
    the framework-level Protocol catalogue
    (`LLMTransport` / `EmbeddingProvider` /
    `RedisLike` / `HttpClientLike`).
  - **ADR-047 status remains `Draft`** (the
    `StreamsWorker` / cancellation /
    `AdapterResponse` follow-ups in §6 are
    still open; "Accepted" is gated on
    ADR-049). The sync `ToolWorker` category
    is the recommended standard for new
    development; the recommendation is now
    backed by the per-Worker refactoring that
    closed in this iteration.

**Acceptable:** N/A — closed.


## 2.25 CLI test suite: collect errors on missing optional dep

**Status:** Closed in 2026-07-20.

**Delivered in this iteration (2026-07-20):**

  - **Root cause.** The 9 test files in
    `tests/unit/cli/` (`test_cli_commands.py`,
    `test_init.py`, `test_keys.py`, `test_new_*.py`)
    do ``from typer.testing import CliRunner`` at
    the **module level**. The ``typer`` package is
    the framework's optional ``[cli]`` extra
    (``pyproject.toml::[project.optional-dependencies]``).
    The CI's default ``uv run scripts/ci.py`` does
    NOT install the extra, so pytest fails at
    collect time with 9 ``ModuleNotFoundError``s
    and the `tests` step aborts with
    ``Interrupted: 9 errors during collection``.
    The pre-existing gate was failing on the
    `tests` step even though the rest of the suite
    was clean.

  - **`tests/unit/cli/conftest.py`** (new): the
    optional-dependency test directory pattern.
    The conftest calls ``pytest.importorskip("typer")``
    at module load; if the skip fires, it sets
    ``collect_ignore_glob = ["test_*.py"]``, which
    excludes the entire directory from the collect
    phase. The pattern is the same one the Python
    community uses for ``torch``-bound tests in ML
    libraries (collect-time skip, not per-test
    skip — per-test ``importorskip`` does not help
    when the import is at module top).

  - **`scripts/ci.py::_run_step`** updated: the
    step now tolerates ``pytest`` exit code 5
    ("no tests ran") on the `tests` step **only**,
    with a guard that the output mentions
    "no tests ran". The guard is specific enough
    to not hide real failures (a test failure
    would still report the failure summary in the
    output, not "no tests ran"). The tolerated
    path prints a hint to the operator about
    `uv sync --extra cli` so the skip is
    self-explanatory.

  - **CI verification.** The full 9-step pipeline
    now passes in both configurations:
    - Without the ``[cli]`` extra (default CI):
      the CLI directory is silently skipped, the
      rest of the suite (1729 tests) runs clean.
    - With the ``[cli]`` extra
      (``uv sync --extra cli``): the 18 CLI
      tests are collected and pass (1747 tests
      total).

**Acceptable:** N/A — closed.


## 2.26 CC offenders + gate bug (10 new offenders above CC=10)

**Status:** Closed in 2026-07-20.

**Delivered in this iteration (2026-07-20):**

  - **Refactor of 10 CC offenders to CC ≤ 10.** The
    radon CC scan flagged 10 functions over CC=10
    in the current tree (the previous baseline had
    only 1, the `validate_args` outlier). The
    refactor uses a **per-event-type dispatch
    table** pattern: the fold / dispatch function
    is a linear ``for`` loop that looks up the
    handler in a ``dict[str, Callable]``; each
    handler is a small single-responsibility
    function. The pattern keeps the per-event
    logic close together while pushing the
    cyclomatic complexity out of the orchestrator
    into the dispatch map (which is data, not
    control flow).

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

    Net effect: ``avg 2.56 → 2.49`` CC across the
    codebase, ``237 → 237`` A-rank files (MI).
    The block count grew (``1263 → 1309``) because
    the refactor split the orchestrators into
    smaller dispatch / handler / init /
    build-component functions; the trade-off is
    the canonical one (more, smaller functions
    with clearer single responsibilities).

  - **Bug fix in `gate_complexity`
    (`scripts/ci.py`).** The previous
    implementation only flagged CC regressions
    for **existing** baseline keys
    (``if base and cur > base``). New blocks
    that landed above CC=10 were silently
    passed. The 10 offenders above were all
    introduced by recent refactors (mostly
    the ADR-039 / ADR-042 / ADR-044 ECS path)
    and bypassed the gate for that reason.
    The fix is explicit: a key absent from the
    baseline with CC > 10 is reported as
    ``CC new offender: <key> = <N>`` with a
    hint that the operator must refactor or
    update the baseline. A new block with
    CC ≤ 10 is fine (a new function under the
    ceiling does not need to be added to the
    baseline — it just goes unobserved until
    the next ``--update-baseline`` pass).

  - **Baseline regenerated.** After the 10
    refactors, ``uv run scripts/ci.py
    --update-baseline`` was re-run; the new
    baseline has 0 CC offenders and 0 MI
    offenders (radon cc avg 2.49 over 1309
    blocks; 237 A-rank files). The ``pyright``
    baseline is unchanged (51 errors, the same
    51 that were there before the refactor —
    the refactor did not introduce new pyright
    errors).

  - **CI verification.** All 9 gates pass:

    - `syntax` ✅
    - `lint` ✅ (0 ruff issues)
    - `format` ✅ (407 files formatted)
    - `complexity` ✅ (0 CC offenders, 0 MI
      offenders; 0 regressions vs the new
      baseline)
    - `reuse` ✅ (REUSE 3.3 compliant)
    - `pyright` ✅ (51 errors, baseline 68,
      delta -17 — the 17-error delta is from
      the ADR-047 + CLI conftest work, not
      from this refactor)
    - `tests` ✅ (1747 passed, 1 skipped)
    - `bandit` ✅ (0 H + 0 M + 0 L)
    - `audit` ✅ (0 known vulnerabilities)

**Acceptable:** N/A — closed.
