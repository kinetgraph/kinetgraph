<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-083: Dedup-by-`event_id` on the `ToolRouter` `XADD` boundary (opt-in via `dedup=True`)

- **Status:** Proposed
- **Date:** 2026-10-14
- **Author:** kinetgraph architecture team (proposed by the backoffice engineering team — see §1.1)
- **Related to:**
  - [`soldi/backoffice/docs/ADRs/adr-013-xadd-tool-queue-sem-dedup-por-event-id.md`](../../soldi/backoffice/docs/ADRs/adr-013-xadd-tool-queue-sem-dedup-por-event-id.md) — the backoffice ADR that opened this workstream. §1.2 quotes the relevant post-mortem excerpt verbatim. The backoffice ADR was the input; this ADR is the upstream response.
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — the Tool Worker Pattern. §2.5 names `ToolRouter.route_batch` as the fan-out point that this ADR hardens.
  - [ADR-066](./ADR-066-Single-Tool-Path.md) — the single tool execution path. The dedup is a `ToolRouter` concern (transport boundary); it does NOT touch the `WorkerManager` consumer path that ADR-066 hardened.
  - [ADR-076](./ADR-076-Service-Scoped-Redis-Key-Prefix.md) — the namespace prefix. §3.1 of this ADR threads the prefix through the new idem-key helper so multi-service Redis isolation holds for the dedup index too.
  - [ADR-077](./ADR-077-Storage-Adapter-Exception-Translation.md) — narrow-catch pattern at the storage boundary. §3.2 of this ADR follows the same discipline when `claim_tool_queue_slot` raises.
  - [ADR-082](./ADR-082-Promote-Redis-Transport-Defense-In-Depth.md) — the parallel workstream from the same 2026-10-07 post-mortem. Both ADRs are responses to the backoffice `resilience_patch.py` stop-gap; both target the same goal: let `soldi/backoffice` consume `main` unmodified.
  - PR #71 of kntgraph (commit `f544b9113`) — `idempotency_key` on `generate_deterministic_event_id`. This ADR does NOT modify `id_helpers.py`; it consumes the same `event_id` the existing EventLog dedup consumes (the EventLog uses `event_id → stream_id` in `infra/redis/_event_log/_idempotency.py:115`).
  - [`infra/redis/_event_log/_idempotency.py`](./../../src/kntgraph/infra/redis/_event_log/_idempotency.py) — the three-phase `claim_event_id_slot` pattern. §3.2 of this ADR reuses it verbatim for tool queues.
  - [`kntgraph-typed-errors`](../.agents/skills/kntgraph-typed-errors/SKILL.md) §6.2 — mutating storage adapters return `Result[T, E]`. §3.2 follows the convention.
  - [`kntgraph-file-layout`](../.agents/skills/kntgraph-file-layout/SKILL.md) §3.3 — the public/private boundary. The new helper module is private (`_idempotency.py`); the public `__init__.py` re-exports the helper.
  - [`kntgraph-testing`](../.agents/skills/kntgraph-testing/SKILL.md) §7.2 — happy-path-plus-one-failure-mode. §5 enumerates the per-public-function coverage gate.
  - [`kntgraph-environment`](../.agents/skills/kntgraph-environment/SKILL.md) — `KNT_REDIS_FAKE=1` is the unit-test path that exercises the new helper via `fakeredis`.

---

## 1. Context

### 1.1 The 2026-10-14 post-mortem — tool-queue replay storm

The backoffice `document_text_extractor` tool hit a 5.5-hour latency on 2026-10-14 (full post-mortem in the backoffice ADR §1.1, quoted in §1.2 below). The trigger was a re-emit storm on the reactive dispatcher loop: each wake of the supervisor emitted the same `tool.text_extractor.requested` event, but the `XADD` for the tool queue did NOT deduplicate, so the per-tool queue grew to 67 entries with the same `event_id`. With `max_concurrency=1` on the worker pool, the 67 entries × ~5min = 5.5 hours to drain, blocking the real user request behind 66 replays.

The EventLog (agent-side stream) deduplicates correctly via `idempotency_key` — the PR #71 (`f544b9113`) fix on `generate_deterministic_event_id` covers that path. The bug is **only** in the `ToolRouter.route_batch` fan-out path (`src/kntgraph/tools/router.py:79`): the `XADD` writes the payload unconditionally and never consults a dedup index.

### 1.2 Verbatim from the backoffice ADR-013 §1.1

> Em uma execução do agente `e5fb9bff-...` no dia 14/Out/2026:
>
> - **18:06** — `text_extractor queue` tinha **67 entries** com o **mesmo `event_id=8012158d-5040-53ad-b49c-591b9384c765`** (apenas `idempotency_key` diferente em cada wake via causation_id chain).
> - Worker `document_text_extractor` (`max_concurrency=1`) processa **1 por vez** (75s cada → 4-5min/unidade).
> - 67 entries × ~5min = **~5.5 horas** para drenar a queue.
> - Supervisor continua emitindo XADD a cada wake (5 wakes em 11min), e a queue continua crescendo.

The scenario is reproducible by design: the `ToolRouter` is a pure fan-out, so any reactive wake that re-emits the same logical request amplifies the queue N-fold (N = number of wakes before the worker drains the real entry). The backoffice mitigation (`scripts/ci.py`-style manual `XDEL` cleanup, ADR-013 §4) is operational, not durable.

### 1.3 The dispatch-side dedup is the right boundary

Three candidate call sites exist for the fix:

| Call site | Boundary | Verdict |
|---|---|---|
| `ToolRouter.route_batch` (`src/kntgraph/tools/router.py:79`) | Transport: writes to `knt:tools:<name>:queue` | **Chosen.** The XADD site is the only one where the dedup cost is paid once; the dedup index lives next to the stream it protects. |
| `WorkerManager._dispatch_to_tool` (`src/kntgraph/tools/manager.py:721`) | Worker: invokes the registered tool | **Wrong boundary.** The WorkerManager consumes an existing stream entry; deduping here means "process and ack" duplicates, but the queue itself still grows — the backoffice's `worker_manager` profile does not benefit. |
| `ReactiveDispatcher._dispatch_for_agent` (`src/kntgraph/runner/reactive.py`) | Reactive loop: the upstream of `ToolRouter` | **Wrong boundary.** The dispatcher emits `Event` objects to the EventLog, which already dedups. Deduping a second time at the dispatcher would conflate concerns and would not survive a second dispatcher instance. |

The `ToolRouter` is the **transport** boundary between the agent's logical state and the worker's physical queue. Dedup at the transport boundary is the same choice the EventLog made (the EventLog dedup lives in `_event_log/_idempotency.py`, called from `RedisEventLogAdapter.append`).

### 1.4 The replays are not concurrent writes — they are the SAME caller's re-emit

The naive reading of the bug ("two writers race on the same event_id") is wrong. The replays in the backoffice incident come from the SAME dispatcher process re-emitting the SAME `Event` (carrying the SAME `event_id`) on each wake. The dedup window is therefore not "concurrent writes" (milliseconds) but "re-emit on the next wake" (seconds to minutes).

The 3-phase `SET NX placeholder` pattern from `_event_log/_idempotency.py` is therefore **overkill**: a 2-phase `EXISTS` + `SET NX` is sufficient for the backoffice use case. We adopt the 3-phase pattern anyway because (a) it is the existing framework precedent and (b) it is provably correct under multi-process dispatchers (the backoffice runs more than one dispatcher process per Fargate task). The cost difference (one extra Redis `SET`) is irrelevant at the tool-queue write rate.

### 1.5 Standards alignment

This ADR aligns with the existing framework standards:

| Standard | How this ADR complies |
|---|---|
| `kntgraph-typed-errors` §6.2 (mutating storage returns `Result[T, E]`) | §3.2 — `claim_tool_queue_slot` returns `Result[str, PersistenceError]`. |
| `kntgraph-file-layout` §3.3 (private modules, `__init__.py` re-export) | §3.2 — the new helper lives in `_idempotency.py` (private); the public `__init__.py` re-exports the orchestrator. |
| `kntgraph-deprecation-policy` §2.1 (no compat shims) | §3.3 — the new kwarg is opt-in; the default behaviour is byte-for-byte identical to pre-ADR behaviour, so no shim is needed. |
| `kntgraph-type-discipline` (no `Any`, primitives) | §3.2 — the helper signature uses `JsonValue`-compatible `dict[str, JsonValue]`; no `Any` is introduced. |
| `kntgraph-testing` §7.2 (happy + 1 failure mode per public function) | §5 — every new public function has 2+ tests. |
| `kntgraph-ci-gate` | §6 — the ratchet rule (corrections + coverage + complexity + typing) holds at every commit. |

No new standard is created. No standard is violated.

---

## 2. Decision

> **Add an opt-in `dedup: bool = False` kwarg to `ToolRouter.__init__`. When `True`, `ToolRouter.route_batch` consults a per-`(tool_name, event_id)` idempotency index BEFORE the `XADD` and SKIPS the write if the index already holds a final value.** The default `False` preserves byte-for-byte the pre-ADR-083 wire format.

The dedup index is a Redis `SET` key (`knt:tools:{tool_name}:idem:{event_id}`) holding the stream_id of the first successful `XADD` for that event. The TTL is 3600 seconds (1 hour) by default — long enough to cover the re-emit window of any sane reactive dispatcher, short enough to bound the index size per tool queue.

The flag is **opt-in**, not always-on, for three reasons:

1. **Back-compat.** The current wire format is `xadd(stream_key, {"payload": <json>})`. Adding a `SET` call before every `xadd` is a behavioural change visible to any test that asserts on `redis.xadd.call_args`. Opt-in keeps existing tests green without modification.
2. **Operational rollout.** The backoffice team can flip `dedup=True` per-environment after the fix lands; canaries can compare `XLEN` before and after; the rollback is a one-line config revert.
3. **Cost.** Each `dedup=True` write costs one extra Redis `SET` per `XADD`. For deployments that do not have a re-emit storm, the cost is pure overhead.

The 3600s TTL rationale: the backoffice scenario replays on a 1-2 minute cadence; the EventLog uses 86400s (24h) because agent-side event replays span longer sagas. Tool queues are per-request; 1h is the right shape.

---

## 3. The four changes

### 3.1 New key convention in `infra/redis/_tools/_keys.py`

```python
TOOL_EVENT_ID_INDEX: str = "knt:tools:{tool_name}:idem:{event_id}"
"""Per-``(tool_name, event_id)`` SET-based idempotency index.
Maps event_id -> stream_id of the first XADD for that event.
"""

DEDUP_TTL_SECONDS_DEFAULT: int = 3_600
"""Default TTL (1 hour) for the tool-queue dedup index. Bounds the
index size per tool; long enough to cover the re-emit window of
any sane reactive dispatcher.
"""


def tool_event_id_key(prefix: str, tool_name: str, event_id: str) -> str:
    """Build the dedup index key under ``prefix``. Empty
    ``prefix`` returns the unprefixed key (``"knt:tools:echo:idem:abc-123"``);
    non-empty prefix composes via :func:`namespaced` so the
    multi-service isolation from ADR-076 holds."""
    return namespaced(
        prefix, TOOL_EVENT_ID_INDEX.format(tool_name=tool_name, event_id=event_id)
    )
```

The key is in a different Redis namespace (`knt:tools:<name>:idem:<id>`) from the EventLog (`knt:eventids:<id>`) so the two dedup indices do not collide under a shared Redis (the ADR-076 multi-service scenario).

### 3.2 New helper `infra/redis/_tools/_idempotency.py` (3-phase pattern)

The helper mirrors `infra/redis/_event_log/_idempotency.py:claim_event_id_slot` (the EventLog precedent) so the codebase has a single pattern for "Redis Streams write with dedup". The three phases:

```python
# Phase 1: check the index. Returns the stream_id if final;
# raises IdempotencyConflict if a placeholder is in flight.
async def _check_phase(redis, idem_key) -> str | None: ...


# Phase 2: XADD + SET placeholder (NX, EX) in one pipeline.
# The NX is the atomic guard against two writers on the same key.
async def _claim_phase(redis, stream_key, payload, maxlen, idem_key) -> str: ...


# Phase 3: replace the placeholder with the final stream_id.
async def _finalize_phase(redis, idem_key, stream_id, ttl_seconds) -> None: ...


# Public orchestrator: returns Ok(stream_id) on first write,
# Err(PersistenceError("tool_queue_dedup_hit")) on dedup hit,
# Err(PersistenceError("redis: ...")) on transport error.
async def claim_tool_queue_slot(
    redis, *, idem_key, stream_key, payload, maxlen, ttl_seconds
) -> Result[str, PersistenceError]: ...
```

The `PersistenceError("tool_queue_dedup_hit")` is **NOT** a failure — it is the desired outcome. The `ToolRouter` maps it to a `worker.router.skipped_dedup` debug log line and continues; the EventLog already uses this same `PersistenceError` wrapper for `IdempotencyConflict` and the existing `RedisEventLogAdapter` swallows it (no, actually it returns `Err(PersistenceError(...))` to the caller — but the `ToolRouter` interprets the `dedup_hit` reason as success-with-skip, distinct from the EventLog's `concurrent_insert_in_flight` reason).

The helper is private (`_idempotency.py`); the public `__init__.py` re-exports `claim_tool_queue_slot`. `kntgraph-file-layout` §3.3 holds.

### 3.3 `ToolRouter` opt-in kwarg + dispatch wiring

`ToolRouter.__init__` gains two kwargs:

```python
def __init__(
    self,
    redis: RedisLike,
    *,
    key_prefix: str = "",
    dedup: bool = False,
    dedup_ttl_seconds: int = DEDUP_TTL_SECONDS_DEFAULT,
):
```

`ToolRouter.route_batch` branches once per `tool.*.requested` event:

```python
# Pseudocode — see tests/unit/tools/test_router.py for the
# behavioural contract.
if tool_name is None:
    continue
stream_key = self._stream_key(tool_name)
payload_dict = {"payload": event.to_json()}
try:
    if self._dedup:
        result = await claim_tool_queue_slot(
            self._redis,
            idem_key=tool_event_id_key(
                self._key_prefix, tool_name, str(event.event_id)
            ),
            stream_key=stream_key,
            payload=payload_dict,
            maxlen=None,
            ttl_seconds=self._dedup_ttl_seconds,
        )
        if result.is_ok():
            logger.debug("worker.router.appended", ...)
        else:
            err = result.err_value()
            if "tool_queue_dedup_hit" in str(err):
                logger.debug("worker.router.skipped_dedup", ...)
                continue
            logger.error("worker.router.dedup_error", error=str(err))
            # fall through to plain XADD on infra error so
            # the dispatcher loop never drops a request
            await self._redis.xadd(stream_key, payload_dict)
    else:
        await self._redis.xadd(stream_key, payload_dict)
except (RedisError, ConnectionError, OSError, TimeoutError, TypeError, ValueError) as e:
    logger.error("worker.router.error", ...)
```

The `fall through to plain XADD on infra error` is the principle of "best-effort fan-out, never drop": a transient Redis error in the dedup index MUST NOT cost the dispatcher a real request. The narrow-catch pattern (`RedisError, ConnectionError, OSError, TimeoutError`) is the same set the existing `route_batch` uses (see ADR-077 §4.3 precedent).

### 3.4 The TTL is configurable, not env-var-driven

The TTL is a `ToolRouter.__init__` kwarg (`dedup_ttl_seconds`), not a `KNT_*` env var. Reason: the TTL is a per-deployment tuning knob (longer TTLs cost Redis memory; shorter TTLs lose dedup coverage), and the constructor is the right place for that knob — the backoffice team sets it once when wiring the `ReactiveDispatcher` and never touches it again. The env-var-driven TTL would also require a Settings field, which is heavier than the kwarg warrants.

If a future deployment needs to tune TTL across many `ToolRouter` instances, the migration path is documented in §4 (one extra Settings field + one extra kwarg).

---

## 4. Configuration surface

| New symbol | Where | Default | Override |
|---|---|---|---|
| `dedup: bool` | `ToolRouter.__init__` kwarg | `False` | per-router (caller wires it) |
| `dedup_ttl_seconds: int` | `ToolRouter.__init__` kwarg | `3_600` (1h) | per-router |
| `DEDUP_TTL_SECONDS_DEFAULT` | `infra/redis/_tools/_keys.py` | `3_600` | framework default |

**No Settings field.** No env var. No public constant exported from `kntgraph.infra.config`. The complexity does not warrant a Settings entry.

The `ReactiveDispatcher` constructor does NOT forward `dedup=` to `ToolRouter`; the caller that wires the dispatcher wires the router. This is the same pattern the existing `key_prefix` kwarg follows (`reactive.py:215-216`).

---

## 5. Tests

### 5.1 Unit (fakeredis via `KNT_REDIS_FAKE=1`)

The `ToolRouter` unit tests in `tests/unit/tools/test_router.py` get a new `TestDedup` class:

1. **`test_dedup_off_default_keeps_legacy_behaviour`** — `ToolRouter(redis_mock)` (no `dedup=`) calls `xadd` once per event, no `set` call. Pins the back-compat invariant.
2. **`test_dedup_on_skips_duplicate_event_id`** — `ToolRouter(redis_mock, dedup=True)`; same event routed twice → `xadd` called once, second event logged as `skipped_dedup`. Happy path.
3. **`test_dedup_on_passes_through_distinct_event_ids`** — two events with distinct `event_id`s → `xadd` called twice, no skips. Happy path (negative).
4. **`test_dedup_on_falls_through_on_redis_set_error`** — `redis_mock.set` raises `ConnectionError`; the dispatcher still calls `xadd` (fall-through on infra error). Failure mode 1.
5. **`test_dedup_on_falls_through_on_redis_xadd_error`** — `redis_mock.xadd` raises after `set` succeeded; the second `set` (finalize phase) is best-effort, the existing `except` in `route_batch` logs the error. Failure mode 2.
6. **`test_dedup_idem_key_includes_prefix`** — `ToolRouter(redis_mock, key_prefix="acme:", dedup=True)`; `set` is awaited with `"acme:knt:tools:echo:idem:<uuid>"`. Pins ADR-076 alignment.
7. **`test_dedup_ttl_passed_to_set`** — `redis_mock.set.assert_awaited_with(..., ex=3600)` pins the default TTL.
8. **`test_dedup_disabled_does_not_call_set`** — `dedup=False`; `redis_mock.set.assert_not_awaited()`. Pins the cost claim.

The `_idempotency.claim_tool_queue_slot` helper itself gets:

9. **`test_claim_phase_xadd_then_set_nx`** — pipeline order verified.
10. **`test_check_phase_returns_none_when_key_absent`** — first call, no entry yet.
11. **`test_check_phase_raises_idempotency_conflict_on_placeholder`** — concurrent writer in flight; the `ToolRouter` caller catches and skips.
12. **`test_finalize_phase_replaces_placeholder_with_stream_id`** — phase 3 happy path.

### 5.2 Integration (real Redis via `clean_redis` fixture)

`tests/integration/tools/test_router.py` gets a new `TestDedup` class:

1. **`test_dedup_skips_second_route_of_same_event`** — two batches containing the same `event_id`; `XRANGE knt:tools:echo:queue` returns 1 entry, not 2. End-to-end happy path.
2. **`test_dedup_does_not_block_distinct_event_ids`** — two batches with distinct `event_id`s; `XRANGE` returns 2 entries. End-to-end happy path (negative).
3. **`test_dedup_ttl_expires_allows_re_route`** — first `XADD`; sleep past TTL; second `XADD` succeeds. Pins the TTL contract.
4. **`test_dedup_idem_key_visible_to_redis_with_prefix`** — `key_prefix="acme-billing:"`; `EXISTS acme:knt:tools:echo:idem:<uuid>` returns 1 after first route.

### 5.3 Failure-mode coverage (the `kntgraph-testing` §7.2 bar)

Every new branch in §3.3 has a corresponding test:

| Branch | Test |
|---|---|
| `dedup=False` → bare `xadd` | 5.1.1 |
| `dedup=True` + new event_id → `claim_tool_queue_slot` returns `Ok` | 5.1.2 (negative), 5.1.3 |
| `dedup=True` + duplicate event_id → `claim_tool_queue_slot` returns `Err("dedup_hit")` → skip + log | 5.1.2 |
| `claim_tool_queue_slot` raises `RedisError` (SET phase) → fall through to bare `xadd` | 5.1.4 |
| `claim_tool_queue_slot` raises `RedisError` (XADD phase) → existing `except` logs | 5.1.5 |
| TTL expiry → second route is not skipped | 5.2.3 |

The unit tests are in `tests/unit/tools/test_router.py` (existing file). The integration tests are in `tests/integration/tools/test_router.py` (existing file). The new `_idempotency.py` helper gets its own `tests/unit/infra/redis/_tools/test_idempotency.py` (new file under the existing convention `tests/unit/infra/redis/_event_log/test_idempotency.py`).

### 5.4 TDD ordering (RED → GREEN → REFACTOR)

Per the user request, the implementation is TDD-driven. The commits are:

1. **RED.** Add the new tests (5.1, 5.2, 5.3). They fail because the helper, kwargs, and code path do not exist yet. The CI gate reports `tests` step red.
2. **GREEN (1).** Implement `infra/redis/_tools/_idempotency.py` (`claim_tool_queue_slot` + 3 phases). The helper tests pass; the `ToolRouter` tests still fail because the kwargs are not wired.
3. **GREEN (2).** Implement `infra/redis/_tools/_keys.py` additions (`TOOL_EVENT_ID_INDEX`, `DEDUP_TTL_SECONDS_DEFAULT`, `tool_event_id_key`). Re-export from `infra/redis/_tools/__init__.py`.
4. **GREEN (3).** Modify `tools/router.py` (`__init__` kwargs + `route_batch` branch). The unit `ToolRouter` tests pass.
5. **GREEN (4).** Run the integration tests (`KNT_REDIS_FAKE=0`, real Redis via the `clean_redis` fixture). The integration tests pass.
6. **REFACTOR.** Apply the ratchet rule (skill §8) on every touched file: no new `Any`, complexity ≤ 10 per block, branch coverage at or above the baseline.

The CI gate (`scripts/ci.py`) is the post-merge safety net. TDD ensures the new code is exercised before it is merged; the gate ensures no regression in the rest of the framework.

---

## 6. Alternatives considered

| Alternative | Pros | Cons | Verdict |
|---|---|---|---|
| **A. Status quo** | No code change | The backoffice replays the manual cleanup; every new dispatcher risks the same incident | Rejected |
| **B. Always-on dedup (no opt-in)** | Simpler API; zero rollout risk per deployment | Behavioural change for every existing test; surprise for callers counting `xadd.call_args`; one-line rollback impossible | Rejected — opt-in preserves rollout |
| **C. Opt-in via env var (`KNT_TOOL_QUEUE_DEDUP=1`)** | No API change; canary by env var | Tests must mutate env var to exercise the path; doesn't compose with the `ReactiveDispatcher.__init__` wiring (which is the natural place); env vars are global, not per-router | Rejected — kwarg is more testable |
| **D. Worker-side dedup (LRU cache of recent `event_id`s in the worker process)** | No upstream change | The queue itself still grows; the backoffice's `XLEN` alert still fires; the worker only saves the tool invocation, not the queue pressure | Rejected — backoffice ADR-013 §2.3 evaluated this and rejected |
| **E. Dispatch-side dedup (`ReactiveDispatcher._dispatch_for_agent` checks before emitting)** | Catches the bug at the source | Conflates concerns with EventLog dedup; does not survive a second dispatcher instance; breaks the `EventLog.append` idempotency contract (the dispatcher would have to track which `event_id`s were routed) | Rejected — `ToolRouter` is the right boundary |
| **F. Lua script for atomic check-and-set** | One round-trip; provably atomic | New Lua dependency; harder to test (`fakeredis` Lua support is uneven); adds a script registration step | Rejected — the SET NX pattern is provably correct and uses primitives `redis-py` already supports |
| **G. (this ADR) Opt-in `dedup=True` kwarg + `claim_tool_queue_slot` mirroring the EventLog pattern** | Consistent with the codebase precedent (EventLog uses the same 3-phase pattern); opt-in preserves back-compat; tests cover happy + failure modes per `kntgraph-testing` §7.2; aligns with `kntgraph-typed-errors` §6.2 (`Result[T, E]`), `kntgraph-file-layout` §3.3 (private helper), `kntgraph-deprecation-policy` §2.1 (no shim), and `kntgraph-ci-gate` (ratchet) | Slightly larger diff than status quo; one new module + one new kwarg; two new test files (unit + integration) | **Recommended** |

### 6.1 Why the 3-phase pattern (and not a 2-phase EXISTS + SET)

The EventLog pattern (which this ADR reuses) was chosen for ADR-019 §3.2 reasons that apply here too: the placeholder pattern lets the **caller distinguish** "first write" from "concurrent write in flight" from "duplicate". A 2-phase `EXISTS + SET` cannot distinguish "concurrent write in flight" from "never written" (both return `False`), which means the caller cannot tell whether to retry or skip. For tool queues, the distinction matters because the caller (ToolRouter) wants to **skip silently on duplicate** but **fall through to XADD on concurrent-in-flight** (a rare-but-possible race where the second writer's XADD would otherwise be lost).

The cost is one extra Redis `SET` per write. At the tool-queue write rate (a few writes per second per tool even in the backoffice scenario), this is irrelevant.

---

## 7. Migration path

1. **This ADR lands.** `dedup=False` default. All existing tests pass unchanged. The backoffice's manual `XDEL` cleanup (ADR-013 §4) keeps working.
2. **Backoffice flips the flag.** `ToolRouter(redis, dedup=True)` in the backoffice's dispatcher wiring. Canary on one Fargate task; observe `XLEN` dropping to ~1 per wake (was ~67).
3. **Backoffice deletes the workaround.** The `XDEL` cleanup script in ADR-013 §4 is removed in the same release as the flag flip.
4. **ADR status flips to Accepted.** Once the backoffice has shipped `dedup=True` for one minor release without rollback, the ADR is ratified.
5. **Future ADR: always-on.** Once `dedup=True` has been the default behaviour for one minor release, a follow-up ADR proposes flipping the default to `True` (the deprecation-policy §2.1 one-minor cycle applies).

---

## 8. Open questions for review

1. **TTL default: 3600s vs 86400s (EventLog).** The 3600s default is tuned for the backoffice replay window (~1-2 min). Reviewer may prefer 86400s for consistency with the EventLog; the cost is a longer-lived index per tool queue. Recommendation: 3600s; override via `dedup_ttl_seconds=`. **This question is open.**
2. **`fall through to plain XADD on infra error`** (§3.3) — is silent fall-through the right behaviour, or should `route_batch` propagate the error to the dispatcher's tick loop? Recommendation: silent fall-through — the existing `route_batch` swallows transport errors (ADR-077 §4.3 precedent), and the dispatcher must not block on a fan-out error. Reviewer can veto.
3. **Public surface of `claim_tool_queue_slot`.** §3.2 proposes exporting from `infra/redis/_tools/__init__.py` so vertical tool-queue adapters (e.g. a future backoffice `SoldiToolQueueAdapter`) can call the helper. Alternative: keep the helper private (`_idempotency.py` only) until a vertical caller proves the need. Recommendation: export; the cost is zero and the precedent (the EventLog helper is also exported) holds. Reviewer can veto.
4. **Should `ReactiveDispatcher.__init__` forward `dedup=` to `ToolRouter`?** The current design (§3.4) leaves the wiring to the caller. Alternative: the dispatcher forwards a `dedup=bool` kwarg to the router it instantiates internally. Recommendation: do not forward — the `ReactiveDispatcher` is the framework primitive, and the `ToolRouter` is a vertical concern; coupling them via a forwarded kwarg would be a step backward. Reviewer can veto.

---

## 9. References

- **Post-mortem 2026-10-14** (backoffice) — full text in `soldi/backoffice/docs/ADRs/adr-013-xadd-tool-queue-sem-dedup-por-event-id.md`. §1.2 of this ADR quotes the relevant excerpt.
- **Backoffice ADR-013** — the backoffice ADR that triggered this workstream. The ADR will be marked "Resolvido (ADR-083 upstream)" once this ADR ships.
- **PR #71** (kntgraph, commit `f544b9113`) — `idempotency_key` on `generate_deterministic_event_id`. The EventLog-side dedup this ADR's `ToolRouter` change complements.
- **`src/kntgraph/infra/redis/_event_log/_idempotency.py:115-129`** — the `claim_event_id_slot` orchestrator. §3.2 of this ADR mirrors the same three-phase pattern.
- **`src/kntgraph/tools/router.py:56-91`** — the `ToolRouter.route_batch` method that §3.3 modifies.
- **`src/kntgraph/runner/reactive.py:128, 187, 307`** — the call sites that invoke `ToolRouter.route_batch`. Untouched by this ADR (the dispatcher does not need to know about dedup).
- **`src/kntgraph/infra/redis/_event_log/_keys.py:55-56`** — `IDEMPOTENCY_TTL_DEFAULT` (24h, EventLog). §3.1's `DEDUP_TTL_SECONDS_DEFAULT` (1h, tool queue) is intentionally different.
- **`src/kntgraph/infra/redis/_tools/_keys.py`** — the file §3.1 extends.
- **`src/kntgraph/infra/redis/_errors.py:18-19`** — `IdempotencyConflict`. The dedup-in-flight signal the helper raises (same pattern as the EventLog).
- **Skills** (under `.agents/skills/`):
  - `kntgraph-typed-errors` §6.2 — mutating storage returns `Result[T, E]`. §3.2 follows.
  - `kntgraph-file-layout` §3.3 — private modules + `__init__.py` re-export. §3.2 follows.
  - `kntgraph-deprecation-policy` §2.1 — no compat shims. The opt-in default follows.
  - `kntgraph-type-discipline` — no `Any` in framework code. §3.2 uses primitives from §3.
  - `kntgraph-testing` §7.2 — happy + 1 failure mode per public function. §5 enumerates.
  - `kntgraph-environment` — `KNT_REDIS_FAKE=1` for the unit path.
  - `kntgraph-ci-gate` — the 12-step gate; the regression safety net in §5.4 step 6.