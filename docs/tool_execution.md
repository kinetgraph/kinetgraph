<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Tool execution: the `ExecutorFactory` cookbook (ADR-069, ADR-078)

The Kinetgraph framework ships a per-tool pluggable execution
strategy via the `ExecutorFactory` callable type alias
(ADR-069). Each tool worker can register a custom factory
that decides how the tool's `invoke` runs — in the loop's
default executor, in a dedicated thread pool, in a separate
process pool, in a sidecar, or anywhere else.

This document is the user-facing cookbook: when to use the
built-in factories, when to write a custom one, and the
contract every factory must satisfy. Pair it with the
``kntgraph-type-discipline`` skill §9 for the framework-level
rationale (the ratchet that keeps the codebase improving as we
go).

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

## 2. Built-in factories (ADR-078 v0.17.0 step 1)

The framework ships two built-in factories in
``kntgraph.tools.executors`` so callers do not have to write
a custom factory for the common cases.

### 2.1 ``AsyncInProcFactory()`` — async tools

Use when the tool's ``invoke`` is ``async def`` (the default).
Runs the tool in the loop's default executor via
``asyncio.to_thread`` — **no extra process pool** is spawned.
Warns once per factory instance with
``PoolDeprecationWarning`` (a ``UserWarning`` subclass) so
the operator knows the legacy path is being used.

```python
from kntgraph.tools.executors import AsyncInProcFactory

manager.register(MyAsyncTool, executor_factory=AsyncInProcFactory())
```

This is the default factory for any async tool. Pick it
explicitly when the operator wants the call to be
self-documenting.

### 2.2 ``ProcessPoolFactory(pool, concurrency=1)`` — sync / CPU-bound tools

Use when the tool is CPU-bound or blocks I/O. Runs the tool
in a caller-owned ``ProcessPoolExecutor`` with a per-tool
``asyncio.Semaphore`` cap (``concurrency=0`` disables the
cap; positive integers cap the number of in-flight
invocations).

```python
from concurrent.futures import ProcessPoolExecutor
from kntgraph.tools.executors import ProcessPoolFactory

pool = ProcessPoolExecutor(max_workers=2)
manager.register(
    MyCpuBoundTool,
    executor_factory=ProcessPoolFactory(pool, concurrency=1),
)
```

Pick this when:

- The tool wraps a model loader (PyTorch, GLiNER2, Docling)
  that should not be re-imported by every fork of the
  Manager's pool.
- The tool wraps a blocking I/O library that should not
  contend with the async event loop.
- The tool needs a sidecar process (e.g. a dedicated browser
  process for Playwright).

### 2.3 ``default_factory_for(tool_cls)`` — the helper

A helper that picks the right factory based on the tool's
class-level markers. It is the default fallback used by the
manager in v0.18+; in v0.17 it is opt-in.

```python
from kntgraph.tools.executors import default_factory_for

factory = default_factory_for(MyTool)
manager.register(MyTool, executor_factory=factory)
```

Rules:

- If ``tool_cls.__tool_worker_cpu_bound__`` is ``True`` →
  ``ProcessPoolFactory`` (caller-supplied pool required;
  raises ``WorkerStrategyError`` when the caller has no pool
  in v0.18+).
- Else if ``inspect.iscoroutinefunction(tool_cls.invoke)`` is
  ``True`` → ``AsyncInProcFactory``.
- Else → ``ProcessPoolFactory`` (the factory is a hint that
  this is a sync tool that needs a pool; the manager raises
  ``WorkerStrategyError`` in v0.18+ when the caller has no
  pool).

### 2.4 When NOT to register a factory

For the common case (async tool, no heavy model), the
default async path is correct and cheap. Registering a
factory "just to be safe" wraps the call in an extra
awaitable and adds no value. In v0.17 you can also let the
legacy ``__tool_worker_cpu_bound__ = True`` marker route the
tool through the internal pool (the path the framework
decommissions in v0.19).

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
``ProcessPoolExecutor`` (managed by ``max_pool_workers=``) is
the past. The framework decommissions the internal pool over
three releases:

- **v0.17.0** (this release): built-in factories ship; the
  legacy internal pool still fires when no factory is
  registered; ``PoolDeprecationWarning`` warns once per tool
  when ``max_pool_workers`` is set; ``WorkerStrategyError`` is
  exported but not raised.
- **v0.18.0** (next release): ``register(executor_factory=None)``
  is no longer a silent fallback. Async tools get
  ``AsyncInProcFactory`` automatically (warns-once-per-tool);
  sync tools without a factory raise ``WorkerStrategyError``.
  The ``max_pool_workers`` kwarg is deprecated.
- **v0.19.0** (hard removal): ``max_pool_workers`` kwarg and
  ``__tool_worker_cpu_bound__`` attribute are removed; every
  tool must register an explicit ``ExecutorFactory`` or rely
  on one of the two built-ins.

The ratchet (skill §8) means each commit should leave the
codebase better than it found it. Apply the executor factory
pattern now — the v0.18 default switch is a few months away
and the migration window is generous.

## 5. Troubleshooting

### 5.1 "Factory is not picklable"

The factory is sent across the multiprocessing boundary
(when the manager uses the legacy internal pool). If the
factory is defined inside a test method or a local closure
that captures a non-picklable object (e.g. a connection
pool, a logger), pickling fails.

Fix: move the factory to a module-level function or use a
``__main__`` import-safe container (a ``dataclass`` with
``@dataclass(frozen=True)`` and primitive fields).

### 5.2 "Factory ignored"

The factory is registered per tool. The manager looks up
``self._executors[tool_name]`` in ``_dispatch_to_tool``. If
you register the factory against a different name (e.g. with
a typo or against the class instead of the registered
``@tool_worker(name=...)``), the lookup misses and the manager
falls through to the default async path or the legacy pool.

Fix: verify the ``name`` kwarg in ``@tool_worker(name=...)``
matches the ``tool_name`` you pass to
``manager.register``.

### 5.3 "Pool is full"

The ``ProcessPoolFactory`` has a per-tool
``asyncio.Semaphore`` cap. When the cap is hit, the dispatch
blocks (the factory awaits the semaphore). The manager's
reaper loop sees the message stay in the consumer-group PEL
and eventually retries (per ``__tool_worker_retries__``).

Fix: increase the cap (``concurrency=N``) or scale the pool
(``max_workers=M`` on the ``ProcessPoolExecutor``).

### 5.4 "Tool hangs at startup"

The factory is called inside the manager's event loop. If the
factory is itself async (e.g. it awaits a connection), the
dispatch path is ``async`` and the manager's ``_dispatch_to_tool``
awaits it. This is correct — the tool itself runs in the pool
or the default executor, NOT in the manager's event loop.

If your tool hangs at startup, check the factory's
initialization (the sidecar / model load), not the manager.

## 6. Reference

- ADR-069 — the original ``ExecutorFactory`` proposal
- ADR-078 — the decommissioning plan (v0.17 / v0.18 / v0.19)
- ``kntgraph-type-discipline`` skill §9 — the framework-level
  ratchet (the four axes: corrections, coverage, complexity,
  typing)
- ``kntgraph/tools/executors.py`` — the source of the built-in
  factories
- ``tests/unit/tools/test_executors.py`` — 13 tests covering
  the built-in factories and the ``default_factory_for`` helper
