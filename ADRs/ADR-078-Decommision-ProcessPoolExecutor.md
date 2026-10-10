<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-078: Decommission the internal `ProcessPoolExecutor`; make execution strategy a per-tool decision

- **Status:** Proposed
- **Date:** 2026-10-09
- **Author:** kinetgraph architecture team (proposed by the backoffice engineering team, see §1.4)
- **Supersedes:** [ADR-054](./ADR-054-WorkerManager-Transport-Evaluation.md) — WorkerManager transport evaluation; "keep `ProcessPoolExecutor`". The reopening criterion stated in ADR-054 §3.3 ("a worker whose body is CPU-bound for >1s at a time") was met by the post-mortem of 2026-10-07 (§1.4).
- **Related to:**
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — `@tool_worker` + `WorkerManager`; the substrate this ADR reconfigures.
  - [ADR-054](./ADR-054-WorkerManager-Transport-Evaluation.md) — the predecessor; `ThreadPoolExecutor` and pure-`asyncio` alternatives this ADR ratifies.
  - [ADR-066](./ADR-066-Single-Tool-Path.md) — three-gate ACL; ACL is orthogonal to the executor choice and is preserved untouched.
  - [ADR-068](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md) — wakeup stream; back-pressure interplay is in [ADR-070](./ADR-070-Worker-Level-Back-Pressure.md) (§6.4).
  - [ADR-070](./ADR-070-Worker-Level-Back-Pressure.md) — `max_in_flight` back-pressure knob; the per-tool semaphore persists.
  - [ADR-076](./ADR-076-Service-Scoped-Redis-Key-Prefix.md) — `key_prefix` plumbing; the new built-in factories must read the same prefix so messages route consistently under multi-service Redis.
  - [ADR-077](./ADR-077-Storage-Adapter-Exception-Translation.md) — typed exceptions at the adapter boundary; the new factories translate `ProcessPoolExecutor`-origin failures the same way the legacy pool did.
  - [ADR-041](./ADR-041-agents-roles-deprecation.md) — the `DeprecationWarning` → `git rm` lifecycle applied to the internal pool in §5.
  - [`docs/adr-069-pluggable-executor-factory.md`](../docs/adr-069-pluggable-executor-factory.md) — the design draft (Proposed 2026-10-08, never ratified) whose API surface (`register(executor_factory=...)`) this ADR promotes to a framework decision. **The doc moves under `ADRs/ADR-078...md` as §3-§4 and `docs/` keeps a redirect stub.**
  - External: `soldi/backoffice/docs/ADRs/adr-014-separacao-pools-cpu-io-bound.md` — the vertical ADR that triggered this one (separate repository; cited for context only).

---

## 1. Context

### 1.1 The substrate today

`WorkerManager` (`src/kntgraph/tools/manager.py:147`) dispatches every `@tool_worker` invocation through **one of three** paths, in this order (`_dispatch_to_tool` at lines 687-745):

1. A per-tool **`executor_factory`** registered via `WorkerManager.register(executor_factory=...)` — the caller's `Awaitable[dict]` wins.
2. `loop.run_in_executor(self._pool, _invoke_tool_sync, ...)` against the **internal `ProcessPoolExecutor`** for `__tool_worker_cpu_bound__` or sync `invoke`s (the legacy path).
3. `await tool.invoke(...)` directly on the Manager's event loop for async I/O tools.

The internal pool (`self._pool`, declared `manager.py:185`, instantiated `start():454-458` with `mp_context=spawn`) is sized via `compute_max_workers` (`tools/_dispatch_helpers.py:39-69`):

```
max_workers = max_pool_workers                   # explicit cap wins
        else max(1, min(32, Σ max_concurrency))  # default
```

The `max_pool_workers` knob was added as an OOM escape hatch after the post-mortem of 2026-10-07 (see `manager.py:198-205`), and `max_concurrency` is enforced per tool by an `asyncio.Semaphore` that **both** the consume loop and the reaper loop acquire (`manager.py:500-507`; DEBT §2.27).

### 1.2 The premature pruning

The internal pool is `ProcessPoolExecutor`, with `spawn` start method (`_SPAWN_METHOD = "spawn"`, `manager.py:144`). For each cold worker, the framework pays ~50-200 ms of import overhead before the first tool runs (`manager.py:441-453`). When a tool imports a heavy library (PyTorch, GLiNER2, Docling) the cold start dominates wall-clock, **and** the library is loaded once per worker — N workers × model size = the worst case the post-mortem in §1.4 makes concrete.

### 1.3 The pluggable factory exists but is opt-in

The `executor_factory` kwarg (commit `ab2c8f0`, October 2026) was added as a **per-tool** opt-in. The intent was that heavy ML verticals could isolate their shared model in one process via a custom factory. The integration test is `tests/integration/tools/test_executor_factory.py`.

The opt-in design is correct for what it was, but two things changed:

1. **The user-facing surface is wrong.** A vertical that registers five CPU-bound tools with five different `ModelPool` factories ends up with five Pools, each holding a copy of the same 1.5 GB model. The primitive enables sharing, but does not document or compose the sharing.
2. **No one is the owner of the default.** When a tool is registered without `executor_factory=...`, the Manager falls back to the internal pool **for both async and CPU-bound tools**. The "default path" is a CPU model that none of the shipped workers actually benefit from (LiteLLM is I/O — `agents/tools/llm.py:575`; PII regex is I/O; the SDK examples are HTTP), so the pool is mostly paying the import cost to do almost no work.

### 1.4 The post-mortem (2026-10-07) — what finally crossed the line

ADR-054 §3.3 said the executor decision would be reopened when *"a worker whose body is CPU-bound for >1s at a time"* appeared. The backoffice team hit exactly that on 2026-10-07 (the post-mortem referenced in `manager.py:50-52`, `:204`, `:432-435`):

- 13 tool registrations in the backoffice service (`document_parser`, `document_text_extractor`, plus 11 light I/O tools).
- `compute_max_workers` allocated 13 internal pool workers (because 13 was below the 32 cap and each tool had `max_concurrency >= 1`).
- On cold start the dispatcher spawned 13 `python` interpreters; each one imported `torch` + `gliner` + `docling` to discover the registered tools, **even for the 11 tools that did not need any of those imports**.
- Peak RSS on the 4 GB Fargate task crossed the OOM threshold during the first burst of 10 messages.
- The temporary workaround (`max_pool_workers=4`, applied as a config knob) prevented the crash but stalled 9 of the 13 workers for 200 ms each (sequential imports of torch ≈ 1.8 s on the affected image); the dispatcher handled 4 messages in parallel instead of the requested 10.

This is the **canonical case** the executor choice was supposed to scale toward. The internal `ProcessPoolExecutor` does not.

### 1.5 The same pattern will appear in any vertical with heterogeneous tools

The backoffice is not an outlier. Any service that registers a heavy tool alongside lighter ones hits the same wall:

| Tool category | Today's path | What it actually needs |
|---|---|---|
| HTTP / LLM / IO-bound async | Pool internal (waste) or `await` (correct) | `await tool.invoke(...)` |
| Pure CPU loop, sync `def invoke` | Pool internal (correct if the model fits; wrong if N instances load it) | A singleton model pool with `max_concurrency >= 1` |
| One-off heavy tool on a fat box | Pool internal (correct) | Opt-in `ProcessPoolExecutor` with explicit `max_pool_workers=1` |
| Browser / sidecar / GPU-pinned | Out of scope today | A custom factory (the future pattern) |

The internal pool is **never** the best answer:

- For async I/O tools it adds a process boundary they do not need (extra cold start, extra RAM, no concurrency benefit).
- For shared-model CPU-bound tools it duplicates the model N times.
- For long-tail categories (browser, GPU) it does not have any way to specialise.

### 1.6 What this ADR is, and what it is not

This ADR ratifies a **decision that has been implicit since the doc `adr-069-pluggable-executor-factory.md` was merged**: the executor strategy is a **per-tool decision** owned by the caller, and the framework's only job is to make that decision ergonomic.

It is **not**:

- A new primitive. The `executor_factory` API already exists and is bit-compatible.
- A removal in this release. The internal pool continues to work, with a deprecation warning. Removal is in §5.
- A claim that `ProcessPoolExecutor` is bad. It is the right answer for a non-shared CPU-bound tool on a fat box. It is the wrong default for a heterogeneous fleet.

---

## 2. Decision

### 2.1 Summary

> **The `WorkerManager`'s internal `ProcessPoolExecutor` is deprecated as the default execution strategy. From v0.18 (see §5), every tool declares how it should run, by registering an `executor_factory` or by relying on one of two built-in factories. The framework stops creating `self._pool` automatically; callers that need a process pool create one explicitly and pass it via `ProcessPoolFactory(...)`.**

### 2.2 The new default dispatch tree

`_dispatch_to_tool` is reduced from three paths to a single decision:

```python
# src/kntgraph/tools/manager.py — _dispatch_to_tool (post-ADR-078)

async def _dispatch_to_tool(self, *, tool_name, tool_cls, idempotency_key, tool_params):
    factory = self._executors.get(tool_name)
    if factory is None:
        # Built-in default for tools that did not declare a strategy.
        # AsyncInProcFactory for tools with async invoke;
        # raise WorkerStrategyError for tools with sync invoke
        # that did not opt into a process pool.
        factory = default_factory_for(tool_cls)
    return await factory(_invoke_tool_sync, idempotency_key, tool_params)
```

The `is_cpu_bound` heuristic (`_dispatch_helpers.py:72-110`) and the `__tool_worker_cpu_bound__` marker stay — they decide between the two built-in factories (§3) when the caller did not register one.

### 2.3 Built-in factories (the framework ships two; verticals ship their own)

| Factory | When it wins | Process cost | Concurrency |
|---|---|---|---|
| `AsyncInProcFactory` | `async def invoke(...)` and I/O-bound | Zero | Inherited from the Manager's `max_concurrency` semaphore |
| `ProcessPoolFactory` | Sync or explicit `__tool_worker_cpu_bound__=True`, one model per worker | One process per pool | Bounded by the pool's `max_workers` AND the Manager's `max_concurrency` |

Details in §3 (semantics), §4 (built-in), §5 (migration).

### 2.4 The `docs/adr-069-pluggable-executor-factory.md` design doc is promoted

The draft ADR that documented the original `executor_factory` proposal (in `docs/`, status *Proposed*, never ratified) becomes the historical design rationale §6-§7 below. The API surface there is unchanged; the original "alternatives considered" is preserved verbatim in §6.1.

The doc file becomes a short stub that points here:

```markdown
<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-069: Pluggable executor factory in WorkerManager.register() — design draft (SUPERSEDED)

> **Superseded by [ADR-078](./ADRs/ADR-078-Decommision-ProcessPoolExecutor.md).**
> The original design (API shape, alternatives A-D, factory contract) is
> preserved as ADR-078 §3.2 (API), §6.1 (alternatives), and §6 (factory
> contract). The doc remains here so existing review-history links
> resolve; do **not** edit — propose changes against ADR-078 instead.
```

### 2.5 The internal pool stays as a deprecated fallback during the migration window

Until §5's migration window closes (target v0.19), the `WorkerManager.__init__` keeps accepting `max_pool_workers=...` for back-compat. When set, the Manager constructs the deprecated pool internally; each invocation of a tool **without** `executor_factory=` and **without** `__tool_worker_cpu_bound__` runs on the pool (for compatibility) and emits a `DeprecationWarning` (`category=kntgraph.workers.PoolDeprecationWarning`, see §7.4).

The warning cadence is one per (tool, process) — `_warnings_registry[tool_name]: set[int]` keyed by `id(self)`, so test fixtures that re-register tools do not flap.

---

## 3. Built-in factories

### 3.1 `AsyncInProcFactory` — default for async I/O

```python
# src/kntgraph/tools/_executors/async_inproc.py

class AsyncInProcFactory:
    """Default factory for tools whose ``invoke`` is ``async def``.

    Runs ``tool.invoke`` directly on the Manager's event loop.
    Bounded by the per-tool ``asyncio.Semaphore`` owned by the
    Manager (see ADR-070 §6.4). No process boundary; no extra
    RAM; no fork/TLS overhead.
    """

    def __init__(self) -> None:
        self._seen: set[int] = set()

    async def __call__(
        self,
        sync_fn: Callable[[type, str, dict[str, Any]], dict[str, Any]],
        idempotency_key: str,
        tool_params: dict[str, Any],
    ) -> dict[str, Any]:
        tool_cls = tool_params.pop("__tool_cls__")
        if id(tool_cls) not in self._seen:
            self._seen.add(id(tool_cls))
            # Emit one warning per tool per process the first
            # time it lands here. After v0.19 (see §5) the
            # warning becomes a hard error in tests.
            warnings.warn(
                "Tool uses AsyncInProcFactory by default; "
                "register executor_factory= explicitly to "
                "pin the strategy (ADR-078).",
                PoolDeprecationWarning,
                stacklevel=3,
            )
        result = await tool_cls().invoke(idempotency_key=idempotency_key, **tool_params)
        return _result_to_dict(result)
```

### 3.2 `ProcessPoolFactory` — opt-in for genuine CPU-bound tools

```python
# src/kntgraph/tools/_executors/process_pool.py

class ProcessPoolFactory:
    """Opt-in factory backed by a caller-owned
    ``ProcessPoolExecutor``. Lifecycle is the caller's
    responsibility (start/stop); the Manager invokes the
    factory once per message.

    Use this when a tool is genuinely CPU-bound and the
    model is NOT shared between tools. For shared-model
    scenarios verticals should write a custom factory
    that re-uses one process across multiple tools —
    see ADR-078 §3.3 and the backoffice ``ModelWorkerPool``
    in ``soldi/backoffice`` for the reference pattern.
    """

    def __init__(
        self,
        *,
        pool: ProcessPoolExecutor,
        concurrency: int = 1,
    ) -> None:
        self._pool = pool
        self._sem = asyncio.Semaphore(concurrency)

    async def __call__(
        self,
        sync_fn: Callable[[type, str, dict[str, Any]], dict[str, Any]],
        idempotency_key: str,
        tool_params: dict[str, Any],
    ) -> dict[str, Any]:
        tool_cls = tool_params.pop("__tool_cls__")
        loop = asyncio.get_running_loop()
        async with self._sem:
            return await loop.run_in_executor(
                self._pool, sync_fn, tool_cls, idempotency_key, tool_params
            )
```

The critical detail is `concurrency`: it is the cap on **concurrent invocations** of this factory, separate from `ProcessPoolExecutor.max_workers`. A pool with `max_workers=1` and `concurrency=8` is fine; the pool just serialises the 8 callers through one worker. A pool with `max_workers=8` and `concurrency=1` gives you one process-pool worker **and** back-pressure to one invocation.

### 3.3 Sharing a process across multiple tools (the backoffice pattern)

The doc `adr-069-pluggable-executor-factory.md` §"Usage example (backoffice)" sketched a singleton `ModelWorkerPool`. Under this ADR that pattern is the **recommended** path for any vertical with ≥2 tools that share a heavy model:

```python
# backoffice/infra/model_worker_pool.py (sketch, non-normative)

class ModelWorkerPool:
    """Singleton pool for tools with heavy ML deps."""

    def __init__(self, *, concurrency: int = 1) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._pool: ProcessPoolExecutor | None = None

    async def start(self) -> None:
        self._pool = ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
        )

    def factory_for(self, tool_cls: type) -> ExecutorFactory:
        pool = self._pool  # capture
        sem = self._sem   # capture

        async def factory(sync_fn, idempotency_key, tool_params):
            tool_params = {**tool_params, "__tool_cls__": tool_cls}
            loop = asyncio.get_running_loop()
            async with sem:
                return await loop.run_in_executor(
                    pool, sync_fn, tool_cls, idempotency_key, tool_params,
                )

        return factory
```

Two tools register against the **same** pool instance → one process, one model load, two `tool.<name>.completed` events feeding their respective agent state machines. This is the case the backoffice post-mortem of §1.4 was unable to express against the old internal pool.

### 3.4 Why two built-in factories, not three

The temptation is to add a third "shared-model pool" built-in. **Resist:** the right answer for shared-model scenarios is vertical code (§3.3), because:

1. The lifecycle is vertical-specific (start a Sidecar? Load a checkpoint into CUDA? Pull an HTTP-sidecar health endpoint?).
2. The concurrency shape is vertical-specific (`concurrency=1` for GPU-bound; `concurrency=N` for shared-cache CPU).
3. The observability hooks are vertical-specific (model-load telemetry, GPU utilisation).

What the framework owns is **the protocol** (`ExecutorFactory` signature) and **the two extremes** (inproc vs explicit pool). Anything in between is the caller's, by design.

---

## 4. Configuration surface

### 4.1 `WorkerManager.__init__` after ADR-078

```python
def __init__(
    self,
    redis: RedisLike,
    event_log: EventLog,
    group_name: str = "fmh_tool_workers",
    consumer_name: str = "worker-1",
    reaper_interval: float = 60.0,
    reaper_idle_time: float = 300.0,
    heartbeat_interval_seconds: float = 30.0,
    *,
    key_prefix: str = "",
    # Deprecated as of v0.17; remove in v0.19 (§5).
    max_pool_workers: int | None = None,
) -> None:
    ...
    self._max_pool_workers: int | None = max_pool_workers
    self._pool: ProcessPoolExecutor | None = None  # only constructed when
                                                   # `_maybe_open_deprecated_pool`
                                                   # returns truthy (see §5.2).
    self._deprecated_pool_opened: bool = False
```

### 4.2 New public surface

| Symbol | Location | Purpose |
|---|---|---|
| `AsyncInProcFactory` | `tools/_executors/async_inproc.py` | Default for async I/O tools |
| `ProcessPoolFactory` | `tools/_executors/process_pool.py` | Default for genuinely CPU-bound tools |
| `PoolDeprecationWarning` | `tools/_executors/_warnings.py` | `Warning` subclass; filterable in tests |
| `default_factory_for(tool_cls)` | `tools/_executors/__init__.py` | Returns the right factory given a class (async → `AsyncInProcFactory`; sync / `__tool_worker_cpu_bound__` → `ProcessPoolFactory` if the Manager has a pool open, else raise `WorkerStrategyError`) |
| `WorkerStrategyError` | `tools/_executors/_errors.py` | Raised at `register()` time when no factory and no pool — operator-visible, not a silent default |

### 4.3 `register()` after ADR-078

```python
def register(
    self,
    tool_cls: type,
    *,
    acl: ToolACL | None = _UNSET,
    executor_factory: ExecutorFactory | None = None,
) -> None:
    if not hasattr(tool_cls, "name"):
        raise TypeError("Tool must be decorated with @tool_worker")
    # ... ACL handling unchanged ...
    if executor_factory is not None:
        self._executors[tool_cls.name] = executor_factory
    else:
        self._executors[tool_cls.name] = default_factory_for(
            tool_cls, has_deprecated_pool=self._deprecated_pool_opened
        )
```

`register()` is no longer silent about the executor choice: a sync `def invoke` with no factory and no pool raises `WorkerStrategyError` at registration time. The error message names the two ways out:

```
WorkerStrategyError: tool 'document_parser' has a sync invoke and no
executor_factory registered, and the WorkerManager has no
deprecated pool open (max_pool_workers=None — deprecated in v0.17,
removed in v0.19). Either:

  (a) pass executor_factory=ProcessPoolFactory(pool, concurrency=1)
      with a caller-owned ProcessPoolExecutor, OR
  (b) declare the tool async and rely on AsyncInProcFactory, OR
  (c) set max_pool_workers=N at WorkerManager.__init__
      to opt into the deprecated pool during the v0.17-v0.18
      migration window.
```

### 4.4 `start()` after ADR-078

```python
async def start(self) -> None:
    if self._running:
        return

    self._running = True

    # §5.2 migration knob: only open the internal pool if the
    # operator explicitly opts into the deprecated path.
    self._deprecated_pool_opened = self._maybe_open_deprecated_pool()

    for tool_name in self._tools:
        stream_key = self._stream_key(tool_name)
        try:
            await self._redis.xgroup_create(stream_key, self._group_name, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                logger.exception("worker.xgroup_create.failed", tool=tool_name, error=str(e))

        task = asyncio.create_task(self._consume_loop(tool_name))
        self._tasks.append(task)

        reaper_task = asyncio.create_task(self._reaper_loop(tool_name))
        self._tasks.append(reaper_task)

def _maybe_open_deprecated_pool(self) -> bool:
    """Open the internal ProcessPoolExecutor only when
    explicitly opted into. Triggers a one-shot
    DeprecationWarning at startup so operators see the
    migration path in their boot logs.
    """
    if self._max_pool_workers is None:
        return False
    n = compute_max_workers(self._tools, explicit_cap=self._max_pool_workers)
    self._mp_context = multiprocessing.get_context(_SPAWN_METHOD)
    self._pool = ProcessPoolExecutor(max_workers=n, mp_context=self._mp_context)
    warnings.warn(
        f"WorkerManager opened the internal ProcessPoolExecutor "
        f"(max_pool_workers={self._max_pool_workers}). This path "
        f"is deprecated and will be removed in v0.19 (ADR-078 §5). "
        f"Tools that need a process pool should register "
        f"executor_factory=ProcessPoolFactory(...) instead.",
        PoolDeprecationWarning,
        stacklevel=2,
    )
    return True
```

### 4.5 `stop()` after ADR-078

If `_deprecated_pool_opened`, `stop()` calls `self._pool.shutdown(wait=True)` as today; otherwise it does nothing for the pool.

---

## 5. Migration plan

### 5.1 Timeline (the "no surprise" rule)

| Version | State | What ships |
|---|---|---|
| **v0.17.0** (next minor) | **soft warning** | `register()` accepts `executor_factory=None`; `_dispatch_to_tool` consults the registered factory (or falls back to the **existing** internal pool); `PoolDeprecationWarning` is emitted once per Manager boot when `max_pool_workers` is set; sync `def invoke` without factory and without pool STILL fails loud at `register()`. The `WorkerManager(max_pool_workers=N)` knob is preserved bit-for-bit. |
| **v0.18.0** | **default switch** | `register(executor_factory=None)` is no longer a silent fallback. Async tools get `AsyncInProcFactory` automatically (warns-once-per-tool); sync tools without factory and without pool raise `WorkerStrategyError`; tools that want a pool pass `executor_factory=ProcessPoolFactory(...)`. The `max_pool_workers` kwarg is deprecated (still accepted, still warns at boot, still creates the pool). `__tool_worker_cpu_bound__` becomes optional (the marker tells the framework which default-built-in to use for sync tools). |
| **v0.19.0** | **hard removal** | `max_pool_workers` is removed. `__tool_worker_cpu_bound__` is removed (the choice between the two built-ins becomes a single explicit declaration). `self._pool`, `compute_max_workers`, `_dispatch_helpers.compute_max_workers`, `is_cpu_bound`, `_ORIGINAL_INVOKE_TOOL_SYNC` are deleted. `WorkerManager._compute_max_workers` and `_dispatch_to_tool` references the deprecated stuff are deleted. `PoolDeprecationWarning` symbol is kept for one more minor (`v0.20.0`) for downstream filtering, then removed. |

### 5.2 The migration window is 2 minors

A single migration window (v0.17 → v0.18) is too short for backoffice: the post-mortem of §1.4 is not yet a v0.17 GA blocker, but it will be by v0.19 when the pool is gone. Two minors lets the backoffice team migrate their five call sites in v0.18 while still having v0.17 as a low-risk upgrade.

### 5.3 Backwards-compatible defaults per release

| Aspect | v0.17.0 | v0.18.0 | v0.19.0 |
|---|---|---|---|
| `register(tool_cls)` with no factory | Falls back to internal pool; no warning | Async → `AsyncInProcFactory` (warn); sync → `WorkerStrategyError` | `WorkerStrategyError` (no pool) |
| `WorkerManager(max_pool_workers=N)` | Opens internal pool; one boot warning | Opens internal pool; one boot warning | `TypeError` (kwarg removed) |
| `__tool_worker_cpu_bound__ = True` | Routes via internal pool | Routes via `ProcessPoolFactory(pool, concurrency=1)` (auto-built from caller pool, if any) | `AttributeError` (marker removed) |
| `executor_factory=AsyncInProcFactory()` | Works | Works | Works (canonical async default) |
| `executor_factory=ProcessPoolFactory(pool, 1)` | Works | Works | Works (canonical CPU default) |
| `executor_factory=custom_async_callable` | Works | Works | Works |
| `_dispatch_to_tool` branches | 3 (factory, pool, async) | 2 (factory, default via `default_factory_for`) | 1 (factory only) |

### 5.4 What v0.17.0 ships concretely (the first migration step)

1. `AsyncInProcFactory` and `ProcessPoolFactory` exported from `kntgraph.tools.executors`.
2. `default_factory_for()` helper.
3. `PoolDeprecationWarning` and `WorkerStrategyError`.
4. `_dispatch_to_tool` continues to call the pool when no factory is registered; the change is purely additive (new symbols, new helper).
5. `register()` does NOT yet raise for sync-without-factory-without-pool; it still falls back to the pool unconditionally.
6. Test coverage for the two factories and the helper.
7. `CHANGELOG.md [Unreleased]` entry: "ADR-078 step 1 ships. Two new built-in `ExecutorFactory` classes (`AsyncInProcFactory`, `ProcessPoolFactory`) and `default_factory_for()` helper. The `executor_factory=None` path is unchanged. The internal `ProcessPoolExecutor` is now formally deprecated — the public symbol is kept through v0.18 and removed in v0.19."

### 5.5 What v0.18.0 ships concretely

1. `register()` raises `WorkerStrategyError` for sync-without-factory-without-pool. Error message names the three exits (§4.3).
2. `_dispatch_to_tool` falls back to `default_factory_for(tool_cls)` instead of the legacy pool when the tool has an async `invoke`.
3. `_maybe_open_deprecated_pool()` triggers only on `max_pool_workers is not None`.
4. `__tool_worker_cpu_bound__` marker is honored but emits `PoolDeprecationWarning` (it goes away in v0.19).
5. Backoffice migrates its five CPU-bound tool call sites to `ProcessPoolFactory(pool, concurrency=1)`.
6. `CHANGELOG.md [Unreleased]` entry: "ADR-078 step 2. `executor_factory=None` now selects `AsyncInProcFactory` for async tools. Sync tools require an explicit `executor_factory`. The internal `ProcessPoolExecutor` is still available under `max_pool_workers=N` but is deprecated; removal in v0.19."

### 5.6 What v0.19.0 ships concretely

1. `max_pool_workers` kwarg removed from `WorkerManager.__init__`.
2. `__tool_worker_cpu_bound__` attribute no longer consulted (the marker is still present on the class but ignored; downstream can `git grep` and remove).
3. `self._pool`, `_compute_max_workers`, `_maybe_open_deprecated_pool`, `_deprecated_pool_opened` removed.
4. `_dispatch_helpers.compute_max_workers` and `is_cpu_bound` removed.
5. `_dispatch_helpers._is_wrapped_sync_helper` removed; the test-instrumentation monkey-patch in `tests/integration/tools/test_worker_manager.py` migrates to a `WorkerStrategyRegistry` test hook (see §7.1).
6. `_ORIGINAL_INVOKE_TOOL_SYNC` removed.
7. `PoolDeprecationWarning` retained for one more release to give downstream `warnings.filterwarnings` policies a chance to detect.
8. `CHANGELOG.md [Unreleased]` entry: "ADR-078 step 3. Internal `ProcessPoolExecutor` removed. Every tool must register an `executor_factory` or rely on one of the two built-in factories. This is a breaking change; see ADR-078 §5 migration."

### 5.7 Migration recipes (operator-visible)

| Symptom | Fix |
|---|---|
| `KeyError: 'concurrency'` at boot | Tool registered before backend set `concurrency`; pass it to `ProcessPoolFactory(pool, concurrency=N)` or use `AsyncInProcFactory()` |
| `WorkerStrategyError: tool 'X' has a sync invoke` | Pass `executor_factory=ProcessPoolFactory(your_pool, 1)` or convert the tool to `async def invoke` |
| Pool stays empty and dispatch latency spikes | Check that the registered factory's `concurrency` is set; a `ProcessPoolFactory(pool, concurrency=0)` deadlocks |
| `PoolDeprecationWarning` flood in tests | `warnings.filterwarnings("ignore", category=PoolDeprecationWarning)` in the conftest, OR migrate the offending tool to `ProcessPoolFactory` |
| `AttributeError: __tool_worker_cpu_bound__` after v0.19 | The marker is gone — pass `executor_factory=ProcessPoolFactory(pool, 1)` instead |

---

## 6. Factory contract (the original design rationale, preserved verbatim)

The contract below is the design the pluggable executor factory was proposed under in `docs/adr-069-pluggable-executor-factory.md`. It is unchanged here — §3 instantiates it.

### 6.1 API

```python
# kntgraph/tools/manager.py

from collections.abc import Awaitable, Callable

ExecutorFactory = Callable[
    [Callable[[type, str, dict[str, Any]], dict[str, Any]], str, dict[str, Any]],
    Awaitable[dict[str, Any]],
]

class WorkerManager:
    def register(
        self,
        tool_cls: type,
        *,
        acl: ACL | None = None,
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        ...
        self._tools[tool_name] = tool_cls
        self._executors[tool_name] = executor_factory
```

The factory receives:

1. `sync_fn` — the canonical `_invoke_tool_sync` callable; it is the sync wrapper that owns `tool_cls().invoke(idempotency_key=..., **tool_params)` and the error translation. Tests monkey-patch it in `tests/integration/tools/test_worker_manager.py`; this contract is what makes the patch ergonomic.
2. `idempotency_key` — the message's `event_id` (a UUID string), used as the `causation_id` of the `tool.<name>.completed` / `.failed` event.
3. `tool_params` — the parsed tool parameters, including the framework's own `correlation_id`, `causation_id`, `agent_id` (when set).

The factory returns:

- A coroutine that resolves with a `result_dict` of shape `{"status": "ok"|"err", "value"|"error": ...}`.
- The Manager appends `tool.<name>.completed` or `.failed` to the EventLog; the factory is **not** responsible for that side effect.

### 6.2 What the Manager keeps owning

The factory is a black-box dispatch hook. The Manager keeps full ownership of:

- **XREADGROUP** in the consume loop (`manager.py:497-572`).
- **XACK** after success / error / reaper timeout.
- **Reaper** with `XAUTOCLAIM` against the per-tool queue.
- **ACL gate** (ADR-066 §4.1).
- **Domain events** (`.completed` / `.failed`) appended to the EventLog.
- **Observability** (`worker.consume_loop.heartbeat`, `worker.xack`, `worker.tool.hard_crash`).
- **Cancellation** (`asyncio.CancelledError` propagation in `_consume_loop`).

The factory only affects **"how do I run `invoke()`"**. Everything else is preserved.

### 6.3 Traceability — what the factory does NOT receive

The factory does **not** receive the Redis Stream `message_id` (e.g. `1791460835580-0`). That ID is a transport position used by `XACK` / `XREADGROUP` / `XAUTOCLAIM`. Application-level observability uses:

| ID | How it reaches the factory | Use |
|---|---|---|
| `event_id` (= `idempotency_key`) | `idempotency_key` parameter | Log key, metric, trace point |
| `correlation_id` | `tool_params["correlation_id"]` | Group events in the same flow |
| `causation_id` | `tool_params["causation_id"]` | "This event was caused by that" |
| `agent_id` | `tool_params` (when set) | Agent scope |

The Redis `message_id` is reachable via `XPENDING <stream> <group>` if an operator needs to debug "where the message is stuck in the stream" — it is a transport detail, not an application ID. Exposing it through the factory would be speculative complexity (YAGNI).

### 6.4 Per-tool semaphore

The Manager's `asyncio.Semaphore(max_concurrency)` (ADR-070 §6.4) wraps **every** factory invocation regardless of the executor. The semaphore is per-tool, owned by `_consume_loop`, and **also** acquired by `_reaper_loop` (so the reaper cannot bypass the cap and re-introduce the parallelism that caused the post-mortem of §1.4). The factory's own `concurrency` field is the **secondary** cap, applied inside the factory.

### 6.5 Lifecycle

The factory is called once per message. The executor's lifecycle (start, stop, fork, health check) is **the caller's responsibility**. The Manager does not own a process pool; it does not own a Sidecar; it does not own a GPU context. Vertical code does.

---

## 7. Consequences

### 7.1 Positive

- **Backoffice OOM is structurally fixed.** 13 tools can register against 1 model pool with `max_pool_workers=1`, `concurrency=1`. Peak RSS drops from ~3.5 GB to ~2.8 GB on Fargate 4 GB (numbers from `docs/adr-069-pluggable-executor-factory.md` §313-315; the post-mortem of §1.4 confirms).
- **Async I/O tools stop paying the fork cost.** 11 of the 13 backoffice tools are I/O; the framework stops spawning a worker for each of them on cold start.
- **The default strategy is no longer wrong.** Today `executor_factory=None` lands on the internal pool for any sync `invoke`. After v0.18 it lands on `AsyncInProcFactory` for any `async def invoke` (which is the right thing) and on `WorkerStrategyError` for sync invocations until the caller declares a strategy (which is the right thing).
- **Vertical heterogeneity is expressible.** Same `WorkerManager` instance can dispatch:
  - HTTP LLM calls on the event loop.
  - Docling via `ProcessPoolFactory(pool_700mb, 1)`.
  - GLiNER via `ProcessPoolFactory(pool_840mb, 1)` — and the pool is **shared** with Docling if the vertical wants one process per model.
  - Browser tools via a `sidecar_factory()` that wraps `playwright.async_api`.
  - GPU pinning via a `cuda_factory()` that binds a `torch.cuda` device.
- **Bit-for-bit compatibility at v0.17.** All existing callers that registered a tool without `executor_factory=` see no behaviour change in v0.17. The warning is a one-shot boot log line, not a runtime exception.
- **Test surface shrinks.** `tests/integration/tools/test_dispatcher_to_worker.py` (the bit that monkey-patches `_invoke_tool_sync` for cold-start measurement) simplifies once `_dispatch_helpers.is_cpu_bound` and `_ORIGINAL_INVOKE_TOOL_SYNC` are removed.

### 7.2 Negative

- **Three releases of churn.** v0.17 adds, v0.18 switches defaults, v0.19 removes the legacy. Downstream services that pin to specific releases must track all three.
- **More boilerplate per heavy tool.** A backoffice-style service with 5 CPU-bound tools has to construct 1 pool (or 5, depending on model sharing), wire each `register()` call, and own the start/stop lifecycle. The previous `register(tool_cls)` was a one-liner.
- **`__tool_worker_cpu_bound__` is going away.** Users that today document the marker as a tool-level knob have to migrate to `executor_factory=ProcessPoolFactory(pool, N)` — a small re-write, but a public surface change.
- **Test fixtures need to provide pools.** Tests that exercise a sync `def invoke` tool today rely on the Manager's internal pool to host it. After v0.18 the fixture must construct a `ProcessPoolExecutor` and pass `executor_factory=ProcessPoolFactory(pool, 1)`. The tests' load time goes up slightly.
- **Operational confusion window.** The `PoolDeprecationWarning` filters downstream may need to be updated (`warnings.filterwarnings("default", category=PoolDeprecationWarning)` is a one-liner, but operators will trip on it if they pinned `filterwarnings("error")`).

### 7.3 Risks and mitigations

| Risk | Mitigation |
|---|---|
| `_maybe_open_deprecated_pool` introduces a hard race with `start()`-time `register()` | The deprecated pool is opened **before** any `_consume_loop` task is created; `register()` AFTER `start()` is not a documented pattern (and is rejected by the `acl_for` test). |
| A factory raises before reaching `_invoke_tool_sync` | The try/except in `_process_message` already wraps the dispatch; the factory exception becomes a `tool.<name>.failed` event. The doc `adr-069-pluggable-executor-factory.md` §347-354 already named this risk and the mitigation (structured log). |
| Async factory does blocking I/O | Doc-block on `ExecutorFactory` Protocol mandates `await`-only. Test fixtures can use `pytest-asyncio` strict mode to assert non-blocking. |
| A vertical forgets to close its `ProcessPoolExecutor` | Standard `__aexit__` / `try/finally` discipline; the backoffice's `ModelWorkerPool` already does this. |
| `max_concurrency` × `factory.concurrency` × `pool.max_workers` interact in surprising ways | Documented in §3.2 and §6.4. The Manager's `_sem` is the source of truth for back-pressure; the pool's `max_workers` is the source of truth for RAM; the factory's `concurrency` is the source of truth for "how many in-flight against this executor". Three independent caps, each tunable. |

### 7.4 Warning taxonomy

- `PoolDeprecationWarning` — `Warning` subclass, exported from `tools/_executors/_warnings.py`. Emitted:
  - Once per Manager boot when `_maybe_open_deprecated_pool` opens the internal pool (v0.17-0.18).
  - Once per tool per process when `AsyncInProcFactory` runs a tool that is auto-promoted to it (v0.18 only; removed in v0.19).
  - Removed entirely in v0.20.

- `WorkerStrategyError` — `Exception` subclass, exported from `tools/_executors/_errors.py`. Raised:
  - At `register()` time when a sync `def invoke` tool has no factory and no pool (v0.18 onwards).
  - Never at dispatch time (the registration-time check means dispatch always has a factory).

### 7.5 Compatibility matrix summary

| Caller pattern | v0.17 | v0.18 | v0.19 |
|---|---|---|---|
| `register(tool_cls)` async | OK | OK (AsyncInProc) | OK (AsyncInProc) |
| `register(tool_cls)` sync + internal pool | OK | deprecated boot warn | TypeError on kwarg |
| `register(tool_cls, executor_factory=AsyncInProcFactory())` | OK | OK | OK |
| `register(tool_cls, executor_factory=ProcessPoolFactory(p, 1))` | OK | OK | OK |
| `register(tool_cls, executor_factory=backoffice_model_pool.factory_for(cls))` | OK | OK | OK |
| `register(sync_tool_cls)` with no factory, no pool | OK (pool) | `WorkerStrategyError` | `WorkerStrategyError` |
| `__tool_worker_cpu_bound__ = True` | OK (heuristic) | OK (heuristic) | ignored |

---

## 8. Open questions

1. **Factory configuration surface.** Should `ProcessPoolFactory` accept an `mp_context=` kwarg, or rely on the caller having set `multiprocessing.set_start_method("spawn")` globally? Current sketch inherits whatever the caller passed; documenting "the framework assumes `spawn`" is consistent with `_SPAWN_METHOD` but might surprise users.
2. **Concurrency observability.** Should `ProcessPoolFactory` emit a `process_pool.executor.saturated` event when `concurrency` becomes the bottleneck? The framework already emits `worker.consume_loop.heartbeat`; adding tool-specific pool saturation is a separate ADR worth a discussion.
3. **Shared semaphore across factories.** If two factories point to the same `ProcessPoolExecutor`, do they share a semaphore? The backoffice pattern shares one pool but uses two factory instances (one per tool class) so they naturally have separate semaphores. Is that desired, or should the Manager allow registering a semaphore against a pool explicitly?
4. **Removing `__tool_worker_cpu_bound__`.** v0.19 removes the marker. The auto-detection (`inspect.iscoroutinefunction(invoke)`) stays; the only loss is "explicit marker". Test coverage in `tests/unit/tools/test_manager_dispatch_helpers.py::test_is_cpu_bound_with_marker` is removed in v0.19.
5. **`compute_max_workers` callers.** The `start()` pre-flight compute is unnecessary when no internal pool is opened. Confirmed safe to remove in v0.19 along with `self._pool`.

---

## 9. References

- Evans, E. *Domain-Driven Design*, 2003 — closed-set semantics for built-in vs vertical extension.
- [ADR-001 — Pure ECS + Event Sourcing](./ADR-001-Arquitetura.md).
- [ADR-036 — Tool Worker Pattern](./ADR-036-Tool-Worker-Pattern.md).
- [ADR-054 — WorkerManager transport evaluation](./ADR-054-WorkerManager-Transport-Evaluation.md) — the predecessor this ADR supersedes.
- [ADR-066 — Single tool path / three-gate ACL](./ADR-066-Single-Tool-Path.md).
- [ADR-068 — Idle Redis traffic and EventLog subscribe](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md).
- [ADR-070 — Worker-level back-pressure](./ADR-070-Worker-Level-Back-Pressure.md).
- [ADR-076 — Service-scoped Redis key prefix](./ADR-076-Service-Scoped-Redis-Key-Prefix.md).
- [ADR-077 — Storage adapter exception translation](./ADR-077-Storage-Adapter-Exception-Translation.md).
- [AGENTS.md](../AGENTS.md) — §1.1 (no `Any` / no bare `object`), §1.2 (framework never imports from vertical), §6 (errors are typed), §11 (branch policy).
- [`kntgraph-typed-errors`](../.agents/skills/kntgraph-typed-errors/SKILL.md) — `Result` discipline applied to factory-error translation.
- [`docs/adr-069-pluggable-executor-factory.md`](../docs/adr-069-pluggable-executor-factory.md) — preserved as the original design draft (now superseded by this ADR).
- External: `soldi/backoffice/docs/ADRs/adr-014-separacao-pools-cpu-io-bound.md` — the vertical ADR that surfaced this framework ADR.

---

## 10. Implementation plan (cumulative across v0.17, v0.18, v0.19)

### 10.1 v0.17.0 (additive — no behaviour change)

Files touched:

- **New** `src/kntgraph/tools/executors/__init__.py` — exports `AsyncInProcFactory`, `ProcessPoolFactory`, `default_factory_for`, `PoolDeprecationWarning`, `WorkerStrategyError`.
- **New** `src/kntgraph/tools/executors/async_inproc.py` — `AsyncInProcFactory` class.
- **New** `src/kntgraph/tools/executors/process_pool.py` — `ProcessPoolFactory` class.
- **New** `src/kntgraph/tools/executors/_warnings.py` — `PoolDeprecationWarning` (subclass of `Warning`).
- **New** `src/kntgraph/tools/executors/_errors.py` — `WorkerStrategyError` (subclass of `Exception`).
- **New** `src/kntgraph/tools/executors/_default.py` — `default_factory_for(tool_cls, *, has_deprecated_pool: bool)` returning the right built-in factory.
- **New** `src/kntgraph/tools/_executors/__init__.py` — re-export alias (`from kntgraph.tools.executors import AsyncInProcFactory` is the canonical path going forward).
- **Edit** `src/kntgraph/tools/manager.py` — comment block at line 110-126 (currently misattribute the design to ADR-069) updates to cite ADR-078 and `docs/adr-069-pluggable-executor-factory.md` as the design history.
- **Edit** `CHANGELOG.md [Unreleased]` — "ADR-078 step 1" entry per §5.4.
- **New** tests:
  - `tests/unit/tools/executors/test_async_inproc.py` — happy path; verify the factory produces a `result_dict` of the right shape; verify the warning fires once per tool.
  - `tests/unit/tools/executors/test_process_pool.py` — happy path with a real `ProcessPoolExecutor(max_workers=1)`; verify semaphore behaviour; verify that an explicit sync-failure inside the pool becomes a `result_dict["status"] == "err"`.
  - `tests/unit/tools/executors/test_default_factory_for.py` — async invoke → `AsyncInProcFactory()`; sync invoke + pool → `ProcessPoolFactory(...)`; sync invoke + no pool → `WorkerStrategyError`.
  - `tests/unit/tools/executors/test_warnings_filtering.py` — `PoolDeprecationWarning` is filterable.

Acceptance:

- All existing tests pass unchanged (v0.17 is purely additive).
- New tests green.
- `scripts/ci.py` gate green.
- CHANGELOG entry under `[Unreleased]`.

### 10.2 v0.18.0 (default switch — soft warning to hard)

Files touched:

- **Edit** `src/kntgraph/tools/manager.py` — `register()` calls `default_factory_for(...)` when no factory is passed; sync-without-factory-without-pool raises `WorkerStrategyError` at register-time (not at dispatch-time).
- **Edit** `src/kntgraph/tools/manager.py` — `_dispatch_to_tool` always dispatches via factory; the legacy pool branch is gated behind `self._deprecated_pool_opened`.
- **Edit** `src/kntgraph/tools/manager.py` — `_maybe_open_deprecated_pool` (extracted from `start()`) and the `_deprecated_pool_opened` flag.
- **Edit** `src/kntgraph/tools/_dispatch_helpers.py` — keep `is_cpu_bound` and `compute_max_workers` (used by `_maybe_open_deprecated_pool`), but `compute_max_workers` is now only called when the pool is being opened.
- **Edit** `docs/adr-069-pluggable-executor-factory.md` — replace with the redirect stub from §2.4. Do **not** delete the file (existing review-history links should resolve).
- **Edit** `CHANGELOG.md [Unreleased]` — "ADR-078 step 2" entry per §5.5.
- **Edit** README.md / tools.md — examples migrate to `executor_factory=ProcessPoolFactory(pool, 1)` for the CPU-bound demo (the `WeatherTool` example today uses the internal pool).

Acceptance:

- v0.17 tests still pass.
- New tests for `register()` raising `WorkerStrategyError` for sync-without-factory-without-pool.
- New tests for `_dispatch_to_tool` routing through `default_factory_for`.
- New tests for `__tool_worker_cpu_bound__` triggering the boot warning (deprecation).
- Backoffice migrates its call sites in the same release window (or, more conservatively, in the v0.18 → v0.19 window — see §5.2).

### 10.3 v0.19.0 (hard removal — breaking change for any caller that did not migrate)

Files touched:

- **Edit** `src/kntgraph/tools/manager.py` — remove `max_pool_workers` kwarg; remove `_maybe_open_deprecated_pool`, `_deprecated_pool_opened`, `_dispatch_helpers.compute_max_workers` callers; reduce `_dispatch_to_tool` to the single factory branch.
- **Edit** `src/kntgraph/tools/_dispatch_helpers.py` — delete `compute_max_workers`, `is_cpu_bound`, `_is_wrapped_sync_helper`, `_DEFAULT_PER_TOOL_MAX_CONCURRENCY`, `_MIN_POOL_WORKERS`, `_MAX_POOL_WORKERS` (the constants move to where they are used, or are no longer needed).
- **Edit** `src/kntgraph/tools/_worker_invocation.py` — `_ORIGINAL_INVOKE_TOOL_SYNC` references removed; the monkey-patch test path migrates to a `WorkerStrategyRegistry` test hook (§7.1).
- **Edit** `src/kntgraph/tools/manager.py` — remove the `__tool_worker_cpu_bound__` reference path. The marker attribute on tool classes can stay (downstream may have read it), but the framework no longer reads it.
- **Edit** `tests/integration/tools/test_worker_manager.py` — the `_invoke_tool_sync` monkey-patch migrates to the test hook.
- **Edit** `tests/integration/tools/test_executor_factory.py` — no API change; existing tests pass.
- **Edit** `CHANGELOG.md [Unreleased]` — "ADR-078 step 3" entry per §5.6. This is the only release where the entry cites a breaking change.

Acceptance:

- A grep for `__tool_worker_cpu_bound__` returns only documentation comments (no code reads).
- A grep for `max_pool_workers` returns zero results in `src/kntgraph`.
- A grep for `_pool: ProcessPoolExecutor` returns zero results in `src/kntgraph`.
- All previous tests pass with their updated fixture wiring.
- New migration recipe tests in `tests/integration/tools/test_migration.py` confirm the §5.7 fixes for common errors.

### 10.4 Test plan summary

| Layer | File | Coverage |
|---|---|---|
| Unit (executors) | `tests/unit/tools/executors/*` | Each built-in factory in isolation; the helper; the warnings; the errors |
| Unit (manager) | `tests/unit/tools/test_manager_register_strategy.py` | The `register()` decision tree for all 6 rows of §7.5 |
| Unit (dispatch) | `tests/unit/tools/test_manager_dispatch_helpers.py` (existing, edited) | `is_cpu_bound` removed; the new `default_factory_for` tested in isolation |
| Integration (real Redis) | `tests/integration/tools/test_executor_factory.py` (existing) | End-to-end factory routing |
| Integration (migration) | `tests/integration/tools/test_migration.py` (new in v0.19) | The §5.7 recipes — every error case becomes a test |
| Stress | `tests/stress/test_business_fsm_stress.py` | Confirm that 13 backoffice-style tools under one Manager instance no longer trip OOM on a 4 GB Fargate task (gate at commit time) |

### 10.5 Roll-out

- **v0.17** ships first; backoffice opt-in is voluntary; no service is forced to migrate.
- **v0.18** ships second; backoffice migrates; the framework's grep test (`grep -r "__tool_worker_cpu_bound__" src/kntgraph` returns only `worker.py`'s class attribute definition) confirms no live reads.
- **v0.19** ships third; the migration window is closed; the backoffice service must be on a v0.18+ release by this point or the framework breaks the build at install time (CI gate: `pip install` rejects v0.19 against a backoffice pinned to v0.16 with a friendly error pointing at the migration recipe).

---

## 11. Revision history

- **2026-10-09** — Initial draft (this version). Promotes `docs/adr-069-pluggable-executor-factory.md` to a formal ADR; supersedes ADR-054; ships built-in factories; inverts the v0.18 default; plans a 3-release migration window.
