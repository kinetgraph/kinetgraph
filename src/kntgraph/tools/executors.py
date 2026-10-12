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
  the tool directly in the loop's default executor.
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
    in the loop's default executor (no extra pool).

    This is the canonical v0.18 default for tools with an
    async ``invoke``. The factory is stateless: ``create``
    returns a closure that the Manager calls per message.
    """

    def __init__(self) -> None:
        self._seen: set[int] = set()

    def __call__(
        self,
        sync_fn: ToolSyncInvoke,
        idempotency_key: str,
        tool_params: Mapping[str, JsonValue],
    ) -> Awaitable[ToolResult]:
        tool_id = id(self)
        if tool_id not in self._seen:
            self._seen.add(tool_id)
            warnings.warn(
                (
                    "Tool uses AsyncInProcFactory by default; "
                    "register executor_factory= explicitly to "
                    "pin the strategy (ADR-078)."
                ),
                PoolDeprecationWarning,
                stacklevel=3,
            )
        return _run_sync(sync_fn, tool_params, idempotency_key)


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
                    return await loop.run_in_executor(
                        self._pool,
                        sync_fn,
                        cast(type, tool_params.get("__tool_cls__")),
                        idempotency_key,
                        cast("dict[str, JsonValue]", dict(tool_params)),
                    )
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._pool,
                sync_fn,
                cast(type, tool_params.get("__tool_cls__")),
                idempotency_key,
                cast("dict[str, JsonValue]", dict(tool_params)),
            )

        return _runner()


async def _run_sync(
    sync_fn: ToolSyncInvoke,
    tool_params: Mapping[str, JsonValue],
    idempotency_key: str,
) -> ToolResult:
    """Run the sync helper in a thread and await its result.

    The ``dispatch_to_tool`` contract says the factory returns
    an awaitable that resolves to a ``ToolResult``. Calling
    the sync ``ToolSyncInvoke`` directly returns a plain dict
    (per the legacy ``_invoke_tool_sync``), so we wrap it in
    ``asyncio.to_thread`` (v0.17+) to honour the
    ``Awaitable[ToolResult]`` contract.
    """
    return cast(
        ToolResult,
        await asyncio.to_thread(
            sync_fn,
            cast(type, tool_params.get("__tool_cls__")),
            idempotency_key,
            cast("dict[str, JsonValue]", dict(tool_params)),
        ),
    )


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
