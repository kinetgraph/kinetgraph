# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Built-in ``ExecutorFactory`` implementations (ADR-078 v0.17.0
step 1).

The framework ships two factory implementations so callers
do not have to write a custom ``ExecutorFactory`` for
the common cases (async tool, sync tool that needs a
process pool). Custom factories are still supported via
``register(executor_factory=...)`` and should be used for
specialised cases (ML models, sidecars, GPU pinning —
per the skill §9 ratchet).

The ``default_factory_for(tool_cls)`` helper picks the
right factory for a given tool class based on the
``__tool_worker_cpu_bound__`` marker (and the async-ness of
``invoke``). It is the default fallback used by the
Manager in v0.18.0.

Symbols
-------

- ``AsyncInProcFactory``: factory for async tools — invokes
  the tool directly on the Manager's event loop (no
  thread, no extra pool).
- ``ProcessPoolFactory``: factory for sync / CPU-bound
  tools — runs the tool in a caller-owned
  ``ProcessPoolExecutor``.
- ``default_factory_for(tool_cls)``: helper that picks the
  right factory based on the tool's class-level markers.
- ``PoolDeprecationWarning``: warning category for callers
  that still pass ``max_pool_workers`` to ``WorkerManager``
  (the internal pool is being decommissioned).
- ``WorkerStrategyError``: raised by ``register()`` in v0.18+
  when a sync tool has no factory and no pool. The error
  message names the three exits (factory / async / pool).
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Awaitable, Mapping
from concurrent.futures import ProcessPoolExecutor
from typing import cast

from kntgraph.core._typing import JsonValue
from kntgraph.tools._dispatch import (
    ExecutorFactory,
    ToolSyncInvoke,
)
from kntgraph.tools._result import ToolResult


class PoolDeprecationWarning(UserWarning):
    """The internal ``ProcessPoolExecutor`` is being decommissioned
    (per ADR-078). New code should register an
    ``executor_factory`` via ``WorkerManager.register``; the
    legacy ``max_pool_workers`` kwarg remains for one more
    minor (v0.18) and is removed in v0.19.

    The ``PoolDeprecationWarning`` symbol is kept for one
    more release (v0.20) to give downstream
    ``warnings.filterwarnings`` policies a chance to detect.
    """


class WorkerStrategyError(Exception):
    """Raised by ``WorkerManager.register`` (v0.18+) when a
    sync tool has no ``executor_factory`` and no
    ``ProcessPoolExecutor`` to back it.

    The error message names the three exits:

      (a) pass ``executor_factory=ProcessPoolFactory(your_pool, concurrency=N)``;
      (b) convert the tool to ``async def invoke``;
      (c) set ``max_pool_workers=N`` at ``WorkerManager.__init__`` (legacy).
    """


class AsyncInProcFactory:
    """Factory for async tools — invokes the tool directly
    in the Manager's event loop (ADR-078 §3.1).

    This is the canonical v0.18 default for tools with an
    async ``invoke``. The factory is stateless: ``__call__``
    is awaited once per message by the Manager. The
    ``sync_fn`` parameter is part of the ``ExecutorFactory``
    Protocol (the dispatcher's generic contract) but is
    ignored here — the factory invokes ``tool_cls().invoke``
    directly on the running event loop instead.

    Bounded by the per-tool ``asyncio.Semaphore`` owned by
    the Manager (ADR-070 §6.4): no process boundary, no
    extra RAM, no fork/TLS overhead, and no cross-thread
    event-loop handoff. Running on the Manager's event
    loop also avoids the fakeredis-specific thread-lock
    starvation documented in
    ``tests/integration/tools/test_worker_manager_parallel_starvation.py``
    (a real-Redis smoke that passes 5/5 against the legacy
    thread-bound path faked 0/8 here).
    """

    def __init__(self) -> None:
        self._seen: set[type] = set()

    async def __call__(
        self,
        sync_fn: ToolSyncInvoke,
        idempotency_key: str,
        tool_params: Mapping[str, JsonValue],
    ) -> ToolResult:
        # ``sync_fn`` is the dispatcher's generic
        # ``_invoke_tool_sync`` (the sync wrapper that opens
        # a fresh event loop in a thread). For async tools
        # we don't need that wrapper — we invoke the tool's
        # coroutine directly on the running event loop.
        del sync_fn  # explicit acknowledgement: ignored
        tool_cls_raw = tool_params.get("__tool_cls__")
        if tool_cls_raw is None:
            raise WorkerStrategyError(
                "AsyncInProcFactory requires '__tool_cls__' in "
                "tool_params; the dispatch contract was broken "
                "upstream."
            )
        tool_cls = cast(type, tool_cls_raw)
        if tool_cls not in self._seen:
            self._seen.add(tool_cls)
            warnings.warn(
                (
                    "Tool uses AsyncInProcFactory by default; "
                    "register executor_factory= explicitly to "
                    "pin the strategy (ADR-078)."
                ),
                PoolDeprecationWarning,
                stacklevel=3,
            )
        # ``__tool_cls__`` is the dispatcher's internal key for
        # passing the tool class through the factory contract;
        # it is NOT a tool parameter and must be stripped from
        # the kwargs before forwarding to the tool's ``invoke``.
        invoke_kwargs = {
            k: v for k, v in tool_params.items() if k != "__tool_cls__"
        }
        tool_instance = tool_cls()
        result = await tool_instance.invoke(
            idempotency_key=idempotency_key,
            **invoke_kwargs,
        )
        if result.is_ok():
            return ToolResult.ok(cast(JsonValue, result.unwrap()))
        return ToolResult.err(str(result.err_value_or_raise()))


class ProcessPoolFactory:
    """Factory for sync / CPU-bound tools — runs the tool
    in a caller-owned ``ProcessPoolExecutor``.

    The factory is constructed with a pool and a concurrency
    cap. ``concurrency=0`` is the legacy "disable TTL"
    sentinel (no concurrency limit); positive integers cap
    the number of in-flight invocations.
    """

    def __init__(
        self,
        pool: ProcessPoolExecutor,
        concurrency: int = 1,
    ) -> None:
        self._pool = pool
        self._sem = asyncio.Semaphore(concurrency) if concurrency > 0 else None

    def __call__(
        self,
        sync_fn: ToolSyncInvoke,
        idempotency_key: str,
        tool_params: Mapping[str, JsonValue],
    ) -> Awaitable[ToolResult]:
        async def _runner() -> ToolResult:
            if self._sem is not None:
                async with self._sem:
                    loop = asyncio.get_running_loop()
                    return cast(
                        ToolResult,
                        await loop.run_in_executor(
                            self._pool,
                            sync_fn,
                            cast(type, tool_params.get("__tool_cls__")),
                            idempotency_key,
                            cast("dict[str, JsonValue]", dict(tool_params)),
                        ),
                    )
            loop = asyncio.get_running_loop()
            return cast(
                ToolResult,
                await loop.run_in_executor(
                    self._pool,
                    sync_fn,
                    cast(type, tool_params.get("__tool_cls__")),
                    idempotency_key,
                    cast("dict[str, JsonValue]", dict(tool_params)),
                ),
            )

        return _runner()


def default_factory_for(tool_cls: type) -> ExecutorFactory:
    """Pick the right factory for a given tool class.

    Rules:

      - If ``tool_cls.__tool_worker_cpu_bound__`` is True →
        ``ProcessPoolFactory`` (caller-supplied pool required).
      - Else if ``inspect.iscoroutinefunction(tool_cls.invoke)``
        is True → ``AsyncInProcFactory``.
      - Else → ``ProcessPoolFactory`` (the factory is a hint
        that this is a sync tool that needs a pool; the
        Manager raises ``WorkerStrategyError`` in v0.18+ when
        the caller has no pool).

    In v0.17.0 the helper exists for the skill §7 ratchet and
    for test fixtures; the Manager's
    ``_dispatch_to_tool`` does not yet call it (the legacy
    pool path still fires; v0.18 will switch the default).
    """
    if getattr(tool_cls, "__tool_worker_cpu_bound__", False):
        return ProcessPoolFactory(pool=_require_pool(), concurrency=1)
    import inspect

    if inspect.iscoroutinefunction(getattr(tool_cls, "invoke", None)):
        # v0.18 default: async tools do NOT need a pool (the
        # ``AsyncInProcFactory`` uses the loop's default
        # executor). Only sync / CPU-bound tools require a
        # caller-supplied pool via ``ProcessPoolFactory``.
        return AsyncInProcFactory()
    return ProcessPoolFactory(pool=_require_pool(), concurrency=1)


def _require_pool() -> ProcessPoolExecutor:
    """Helper for ``default_factory_for`` — the caller must
    supply a pool. Used in the test path where the Manager's
    internal pool is None; the test fixture provides one.
    """
    raise WorkerStrategyError(
        "default_factory_for requires a ProcessPoolExecutor; "
        "the test fixture must supply one. In production, "
        "callers pass an explicit "
        "executor_factory=ProcessPoolFactory(your_pool, 1) "
        "or convert the tool to async def invoke."
    )


__all__ = [
    "AsyncInProcFactory",
    "PoolDeprecationWarning",
    "ProcessPoolFactory",
    "WorkerStrategyError",
    "default_factory_for",
]
