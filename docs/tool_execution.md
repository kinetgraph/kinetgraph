<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Tool execution: the `ExecutorFactory` cookbook (ADR-078)

The Kinetgraph framework gives every tool worker an explicit
execution strategy. An ``async def invoke`` runs in-proc on
the Manager's event loop by default; a **sync** ``def invoke``
must declare how it runs — via ``executor_factory=`` on
``register()``, or by opting back into the deprecated internal
pool. There is no silent fallback, because the old implicit
path spawned one interpreter per tool and was the cause of the
2026-10-07 OOM (ADR-078 §1.4).

This document is the user-facing cookbook: when to use the
built-in factories, when to write a custom one, the contract
every factory must satisfy, and what to do when you hit
``WorkerStrategyError``. Pair it with the
``kntgraph-type-discipline`` skill §9 for the framework-level
rationale.

## 1. The contract

The ``ExecutorFactory`` is a callable with one signature:

```python
type ToolSyncInvoke = Callable[
    [type, str, "Mapping[str, JsonValue]"],
    ToolResult,
]
ExecutorFactory = Callable[
    [ToolSyncInvoke, str, "Mapping[str, JsonValue]"],
    Awaitable[ToolResult],
]
```

The factory receives three arguments:

1. ``sync_fn`` (``ToolSyncInvoke``) — the dispatcher helper
   that actually invokes the tool. The factory decides whether
   to call it in its own thread pool, in its own process pool,
   to forward it to a sidecar, etc.
2. ``idempotency_key`` (``str``) — the message's event id, used
   by the manager as the ``causation_id`` of completion / failure
   events. The factory may pass it to the sidecar or the pool
   for tracing.
3. ``tool_params`` (``Mapping[str, JsonValue]``) — the parsed
   tool arguments, including a ``__tool_cls__`` key the manager
   injects so the helper can instantiate the tool. The factory
   may inspect this mapping (read-only) to decide routing.

The factory returns an awaitable that resolves to a
``ToolResult`` (the discriminated union per ADR-079: ``ToolResult.ok(value)``
or ``ToolResult.err(error)``).

## 2. Built-in factories (ADR-078)

The framework ships two built-in factories in
``kntgraph.tools.executors`` so callers do not have to write
a custom factory for the common cases.

### 2.1 ``AsyncInProcFactory()`` — async tools

Use when the tool's ``invoke`` is ``async def`` — which is
the default, so **you normally do not register a factory at
all**. Awaits ``tool_cls().invoke(...)`` **directly on the
Manager's event loop**: no thread, no process, no extra RAM,
no cold-start import cost.

When a tool is *auto-promoted* to this factory (registered
with no ``executor_factory``) it warns once per (tool,
process) with ``PoolDeprecationWarning`` so the operator
knows the strategy was picked implicitly rather than
declared. Registering explicitly silences that warning.

```python
from kntgraph.tools.executors import AsyncInProcFactory

# usually unnecessary — this is already the default:
manager.register(MyAsyncTool)

# explicit, self-documenting, no warning:
manager.register(MyAsyncTool, executor_factory=AsyncInProcFactory())
```

:::caution

Running on the Manager's event loop is what makes this
factory cheap, but it means **a tool that crashes its own
process takes the Manager down with it**. If the tool calls
``os._exit``, ``sys.exit``, raises ``SIGKILL``, or hits a
segfault in a native extension, you **must** register a
``ProcessPoolFactory`` — see §2.2. Under this factory there
is no consume loop, no reaper, no ``XAUTOCLAIM``, and the
pending message is never recovered. ADR-078 §12.5.

:::

### 2.2 ``ProcessPoolFactory(pool, concurrency=1)`` — sync / CPU-bound tools

Use when the tool has a **sync ``def invoke``**, is CPU-bound,
or can crash its own process. Runs the tool in a
**caller-owned** ``ProcessPoolExecutor`` with a per-tool
``asyncio.Semaphore`` cap (``concurrency=0`` disables the
cap; positive integers cap the number of in-flight
invocations). You own the pool's lifecycle — start it, shut it
down; the Manager does not.

```python
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from kntgraph.tools.executors import ProcessPoolFactory

pool = ProcessPoolExecutor(
    max_workers=1,
    mp_context=multiprocessing.get_context("spawn"),
)
manager.register(
    MyCpuBoundTool,
    executor_factory=ProcessPoolFactory(pool, concurrency=1),
)
```

Pick this when:

- The tool has a sync ``def invoke``. Since v0.17 this is
  **mandatory**: a sync tool registered without a factory
  raises ``WorkerStrategyError`` (§4.2).</path>
- The tool wraps a model loader (PyTorch, GLiNER2, Docling)
  that should not be re-imported by every fork of the
  Manager's pool.
- The tool wraps a blocking I/O library that should not
  contend with the async event loop.
- The tool can crash its own process (see the caution above).

``concurrency`` is the cap on **concurrent invocations** of
this factory, independent of the pool's ``max_workers``. A
pool with ``max_workers=1`` and ``concurrency=8`` is fine —
the pool just serialises the eight callers through one worker.

### 2.3 ``default_factory_for(tool_cls, *, pool=None)`` — the helper

Picks the right factory from the tool's class-level markers.
It is the fallback the Manager uses when no factory was
registered.

```python
from kntgraph.tools.executors import default_factory_for

factory = default_factory_for(MyAsyncTool)
manager.register(MyAsyncTool, executor_factory=factory)
```

Rules:

- If ``tool_cls.__tool_worker_cpu_bound__`` is ``True`` →
  ``ProcessPoolFactory`` (a pool is **required**; raises
  ``WorkerStrategyError`` when ``pool`` is ``None``).
- Else if ``inspect.iscoroutinefunction(tool_cls.invoke)`` →
  ``AsyncInProcFactory`` (never touches ``pool``).
- Else → ``ProcessPoolFactory`` (a pool is **required**).

Since v0.17 the ``__tool_worker_cpu_bound__`` marker no
longer exempts a sync tool from the registration gate. It
only *hints* this helper about which built-in to pick for a
caller who already owns a pool. ADR-078 §12.3.

### 2.4 When NOT to register a factory

For the common case — an ``async def invoke`` with no heavy
model — register nothing:

```python
manager.register(MyAsyncTool, acl=None)  # correct as-is
```

Registering ``AsyncInProcFactory()`` explicitly is fine when
you want the call site to be self-documenting, but it is not
required and adds nothing at runtime.

## 3. Custom factories

Reach for a custom factory when the built-in factories do
not cover the use case. Examples:

- **ML / DL models** that should be loaded by a single
  worker process (avoid per-fork re-imports of PyTorch / GLiNER2
  / Docling).
- **Sidecars** — a dedicated browser process for Playwright,
  a remote gRPC service, etc.
- **Specialised pools** — a separate ``ThreadPoolExecutor``
  for blocking I/O that should not contend with the async
  event loop, or a connection-pool proxy.

### 3.1 The factory contract

A custom factory is a callable with the ``ExecutorFactory``
signature (see §1). It must:

- Be picklable (the manager uses ``spawn`` start method for
  the legacy internal pool, so the factory is sent across
  the multiprocessing boundary when used by CPU-bound tools).
- Return an awaitable that resolves to a ``ToolResult``.
- Be idempotent per tool (the manager registers one factory
  per tool; the factory's state should not change between
  invocations).

### 3.2 Example: a sidecar factory

```python
import asyncio
from dataclasses import dataclass

from kntgraph.core._typing import JsonValue
from kntgraph.tools._dispatch import ToolSyncInvoke
from kntgraph.tools._result import ToolResult


@dataclass
class SidecarConnection:
    """A persistent gRPC connection to the sidecar
    service. Owned by the manager; one per process.
    """

    host: str
    port: int


def sidecar_factory(conn: SidecarConnection):
    """Closure factory: captures the sidecar connection."""

    async def _factory(
        sync_fn: ToolSyncInvoke,
        idempotency_key: str,
        tool_params: dict[str, JsonValue],
    ) -> ToolResult:
        # Forward to the sidecar over gRPC. The connection
        # is shared across all invocations (one per process).
        result = await conn.call(
            idempotency_key=idempotency_key,
            tool_params=tool_params,
        )
        return ToolResult.ok(result)

    return _factory


# In the manager setup:
connection = SidecarConnection(host="localhost", port=50051)
manager.register(
    MyPlaywrightTool,
    executor_factory=sidecar_factory(connection),
)
```

### 3.3 Example: a model-loader factory

```python
import asyncio
import torch

from kntgraph.core._typing import JsonValue
from kntgraph.tools._dispatch import ToolSyncInvoke
from kntgraph.tools._result import ToolResult


_model: torch.nn.Module | None = None  # populated lazily


def model_loader_factory():
    """Singleton factory: loads the model once per process.

    The factory is called by the manager per tool message;
    it loads the model lazily (first invocation) and caches it
    in a module-level variable. Subsequent invocations reuse
    the same in-memory model.
    """
    global _model

    async def _factory(
        sync_fn: ToolSyncInvoke,
        idempotency_key: str,
        tool_params: dict[str, JsonValue],
    ) -> ToolResult:
        if _model is None:
            _model = torch.load("/path/to/model.pt")
        # ... run the model's inference on tool_params ...
        return ToolResult.ok({"prediction": ...})

    return _factory


manager.register(
    MyModelTool,
    executor_factory=model_loader_factory(),
)
```

The factory is called inside the manager's event loop; the
``async def _factory`` body is a regular coroutine. The model
load happens once per worker process; the singleton lives
in the module's global state.

## 4. Migration window (ADR-078 §5)

The ``ExecutorFactory`` pattern is the future; the internal
``ProcessPoolExecutor`` is the past. The framework
decommissions the internal pool over the remaining releases:

- **v0.17.0** (current): a tool **must** declare how it runs.
  Async tools default to ``AsyncInProcFactory``; a sync tool
  with no factory raises ``WorkerStrategyError`` at
  registration time. The internal pool is opt-in via
  ``legacy_pool=True`` and ``max_pool_workers`` only *sizes*
  it — it no longer creates one on its own.

  The built-in factories (``AsyncInProcFactory``,
  ``ProcessPoolFactory``, ``default_factory_for``) shipped in
  the same release. There was no separate "soft warning"
  release: the planned three-step window collapsed into two.
  See ADR-078 §12.8.
- **v0.19.0** (hard removal): ``max_pool_workers``,
  ``legacy_pool``, and ``__tool_worker_cpu_bound__`` are
  removed. Every tool must declare a factory or rely on one of
  the two built-ins.

### 4.1 Staying on the legacy pool for now

If a migration is not yet possible, opt back in explicitly.
The pool opens in ``start()``, warns once at boot, and runs
your sync tools as before:

```python
manager = WorkerManager(
    redis,
    event_log=log,
    legacy_pool=True,
    max_pool_workers=4,
)
manager.register(MyUnmigratedSyncTool)  # no factory needed
```

:::caution

This restores the execution path that caused the 2026-10-07
OOM (ADR-078 §1.4): one spawned interpreter per pool worker,
each importing the full Python environment. Use the smallest
``max_pool_workers`` that keeps your throughput, and treat this
as a stopgap to remove before v0.19.

:::

### 4.2 What ``WorkerStrategyError`` means

Raised at ``register()`` when a tool has a **sync ``def
invoke``** and no ``executor_factory``. It is not a runtime
failure — nothing was dispatched. The message names every
exit. In short:

```python
# (a) needs a process boundary — own the pool:
pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
manager.register(MyTool, executor_factory=ProcessPoolFactory(pool, concurrency=1))

# (b) I/O-bound — make it async, no factory needed:
#     async def invoke(...)

# (c) non-blocking despite being sync:
manager.register(MyTool, executor_factory=AsyncInProcFactory())

# (d) not migrated yet — see §4.1
manager = WorkerManager(redis, log, legacy_pool=True, max_pool_workers=4)
```

## 5. Troubleshooting

### 5.1 "Factory is not picklable"

The factory is sent across the multiprocessing boundary when
it runs the tool in a process pool. If the factory — or the
tool class it closes over — is defined inside a test method or
a local closure that captures a non-picklable object (e.g. a
connection pool, a logger), pickling fails.

Note this also applies to the **tool class itself**: under
``spawn`` the class must be importable at module level, not
defined inside a test function. Define test tools at module
scope, as ``tests/integration/tools/test_worker_manager.py``
does.

Fix: move the factory and the tool to module level, or use an
import-safe container (a ``@dataclass(frozen=True)`` with
primitive fields).

### 5.2 "Factory ignored"

The factory is registered per tool. The manager looks up
``executors[tool_name]`` in ``dispatch_to_tool``. If you
register the factory against a different name (e.g. with a
typo or against the class instead of the registered
``@tool_worker(name=...)``), the lookup misses and the manager
falls through to ``default_factory_for(tool_cls, pool=...)`` —
which returns ``AsyncInProcFactory`` for async tools and
**raises** ``WorkerStrategyError`` for a sync tool with no
pool.

Fix: verify the ``name`` kwarg in ``@tool_worker(name=...)``
matches the registered tool name.

### 5.3 "Pool is full"

The ``ProcessPoolFactory`` has a per-tool
``asyncio.Semaphore`` cap. When the cap is hit, the dispatch
blocks (the factory awaits the semaphore). The manager's
reaper loop sees the message stay in the consumer-group PEL
and eventually retries (per ``__tool_worker_retries__``).

Fix: increase the cap (``concurrency=N``) or scale the pool
(``max_workers=M`` on the ``ProcessPoolExecutor``).

### 5.4 "Tool hangs at startup"

The factory is awaited inside the Manager's event loop. If the
factory is itself async (e.g. it awaits a connection), the
manager awaits it — correct, since the tool itself runs in the
pool or in-proc, not in the Manager's loop.

If your tool hangs at startup, check the factory's
initialization (the sidecar / model load), not the manager.

:::danger

A **blocking** ``def invoke`` registered under
``AsyncInProcFactory`` runs *on the Manager's event loop* and
will stall every other tool's consume loop, the reaper, and
the heartbeat. If a sync tool feels like it hangs the whole
worker, this is why — give it a ``ProcessPoolFactory``.

:::

## 6. Reference

- [ADR-078](../ADRs/ADR-078-Decommision-ProcessPoolExecutor.md)
  — the decommissioning plan; §12 is the implementation record,
  §12.7 the two post-ratification fixes, and §12.8 the corrected
  version timeline
- ADR-069 — the original ``ExecutorFactory`` proposal
  (superseded; kept at ``docs/adr-069-pluggable-executor-factory.md``)
- ``kntgraph-type-discipline`` skill §9 — the framework-level
  ratchet (the four axes: corrections, coverage, complexity,
  typing)
- ``kntgraph/tools/executors.py`` — the built-in factories
- ``kntgraph/tools/_strategy_gate.py`` — the registration gate
- ``tests/unit/tools/test_executors.py`` — the built-in factories
  and the ``default_factory_for`` helper
- ``tests/integration/tools/test_worker_manager.py`` — end-to-end,
  including a sync tool on the legacy pool
