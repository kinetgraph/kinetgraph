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
  when a sync tool has no ``executor_factory``. Also raised by
  ``default_factory_for`` when a sync / cpu-bound tool reaches
  it without a caller-supplied pool.

v0.18 breaking change
---------------------
The Manager no longer creates a ``ProcessPoolExecutor``
implicitly. A tool with a sync ``invoke`` **must** declare its
strategy:

    manager.register(AsyncTool)              # still fine
    manager.register(SyncTool)               # WorkerStrategyError
    manager.register(SyncTool,
                     executor_factory=ProcessPoolFactory(pool, 1))  # ok

Pass ``legacy_pool=True`` to ``WorkerManager(...)`` to restore
the old behaviour for one more minor (removed in v0.19). See
ADR-078 §12.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Awaitable, Mapping
from concurrent.futures import ProcessPoolExecutor
from typing import Any, cast

from kntgraph.core._typing import JsonValue
from kntgraph.core.result.errors import ToolError
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


def _extract_tool_cls(tool_params: Mapping[str, JsonValue]) -> type:
    """Pull the tool class out of the dispatch's
    ``__tool_cls__``-augmented params. Raises
    :class:`WorkerStrategyError` when the key is missing
    (the dispatch contract was broken upstream).
    """
    tool_cls_raw = tool_params.get("__tool_cls__")
    if tool_cls_raw is None:
        raise WorkerStrategyError(
            "AsyncInProcFactory requires '__tool_cls__' in "
            "tool_params; the dispatch contract was broken "
            "upstream."
        )
    return cast(type, tool_cls_raw)


def _strip_internal_keys(
    tool_params: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    """Return ``tool_params`` without the framework's
    internal ``__tool_cls__`` key. The tool's ``invoke``
    does not accept this key (it is the dispatcher's
    contract-internal hand-off, not a tool parameter).
    """
    return {k: v for k, v in tool_params.items() if k != "__tool_cls__"}


def _result_to_tool_result(result: Any) -> ToolResult:
    """Map a tool's ``Result[T, ToolError]`` to the
    framework's ``ToolResult`` (ADR-079). The Err-branch
    classification is delegated to the helper below.
    """
    if result.is_ok():
        return ToolResult.ok(cast(JsonValue, result.unwrap()))
    return _toolresult_from_err_value(result.err_value_or_raise())


def _toolresult_from_err_value(err_value: object) -> ToolResult:
    """Read ``error_class`` from a ``ToolError``; fall back
    to ``"unknown"`` for non-ToolError values (preserves
    the legacy behaviour for tools that raised a bare
    string or a non-tool exception).
    """
    if isinstance(err_value, ToolError):
        return ToolResult.err(str(err_value), error_class=err_value.error_class)
    return ToolResult.err(str(err_value))


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
        """The factory is stateless: the warning dedup lives at
        module level (``_WARNED_AUTO_PROMOTION``) so it survives
        the fresh instance the default path builds per dispatch.
        """

    async def __call__(
        self,
        sync_fn: ToolSyncInvoke,
        idempotency_key: str,
        tool_params: Mapping[str, JsonValue],
    ) -> ToolResult:
        # ``sync_fn`` is the dispatcher's generic
        # ``_invoke_tool_sync``. For async tools we invoke the
        # tool's coroutine directly on the running event loop.
        del sync_fn  # explicit acknowledgement: ignored
        tool_cls = _extract_tool_cls(tool_params)
        _warn_auto_promoted_once(tool_cls)
        result = await tool_cls().invoke(
            idempotency_key=idempotency_key,
            **_strip_internal_keys(tool_params),
        )
        return _result_to_tool_result(result)


# ``PoolDeprecationWarning`` must fire once per (tool, process),
# not once per factory instance (ADR-078 §7.4): the default path
# builds a fresh factory on every dispatch, so an instance-scoped
# dedup warned on every message.
_WARNED_AUTO_PROMOTION: set[type] = set()


def _warn_auto_promoted_once(tool_cls: type) -> None:
    """Warn once per tool class per process that the tool
    reached ``AsyncInProcFactory`` without declaring it."""
    if tool_cls in _WARNED_AUTO_PROMOTION:
        return
    _WARNED_AUTO_PROMOTION.add(tool_cls)
    warnings.warn(
        (
            "Tool uses AsyncInProcFactory by default; "
            "register executor_factory= explicitly to "
            "pin the strategy (ADR-078)."
        ),
        PoolDeprecationWarning,
        stacklevel=3,
    )


class ProcessPoolFactory:
    """Factory for sync / CPU-bound tools — runs the tool
    in a caller-owned ``ProcessPoolExecutor``.

    The factory is constructed with a pool and a concurrency
    cap. ``concurrency=0`` is the legacy "disable TTL"
    sentinel (no concurrency limit); positive integers cap
    the number of in-flight invocations.

    Required for any tool that can kill its own process
    (``os._exit``, ``sys.exit``, ``SIGKILL``, a segfault in a
    native extension). Under ``AsyncInProcFactory`` such a tool
    runs on the Manager's event loop, so the crash takes down
    the Manager: no consume loop, no reaper, no ``XAUTOCLAIM``,
    and the pending message is never recovered. In a pool, the
    worker dies and the Manager reaps the message
    (ADR-078 §12.5).
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


def default_factory_for(
    tool_cls: type, *, pool: ProcessPoolExecutor | None = None
) -> ExecutorFactory:
    """Pick the right factory for a given tool class.

    Rules:

      - If ``tool_cls.__tool_worker_cpu_bound__`` is True →
        ``ProcessPoolFactory`` (``pool`` required).
      - Else if ``inspect.iscoroutinefunction(tool_cls.invoke)``
        is True → ``AsyncInProcFactory``.
      - Else → ``ProcessPoolFactory`` (``pool`` required).

    ``pool`` is the caller-owned executor handed to the
    ``ProcessPoolFactory``. There is no implicit pool to
    inherit: as of ADR-078 v0.18 the Manager's internal
    ``ProcessPoolExecutor`` is opt-in, so a sync or cpu-bound
    tool reaching this helper without one raises
    ``WorkerStrategyError`` naming the fix. Callers without a
    pool reach this helper only for async tools, which never
    touch the pool argument.
    """
    if _needs_process_pool(tool_cls):
        return ProcessPoolFactory(pool=_require_pool(pool), concurrency=1)
    # v0.18 default: async tools do NOT need a pool
    # (the ``AsyncInProcFactory`` invokes the tool's
    # coroutine directly on the Manager's event loop).
    return AsyncInProcFactory()


def _needs_process_pool(tool_cls: type) -> bool:
    """True when the tool must run in a separate process: an
    explicit ``__tool_worker_cpu_bound__`` marker, or a sync
    ``invoke``.
    """
    import inspect

    if getattr(tool_cls, "__tool_worker_cpu_bound__", False):
        return True
    return not inspect.iscoroutinefunction(getattr(tool_cls, "invoke", None))


def _require_pool(pool: ProcessPoolExecutor | None) -> ProcessPoolExecutor:
    """A sync or cpu-bound tool needs a caller-supplied
    ``ProcessPoolExecutor``. Since ADR-078 v0.18 the Manager
    no longer creates one implicitly, so a missing pool is a
    registration-time mistake, not something to paper over.
    """
    if pool is None:
        raise WorkerStrategyError(
            "A sync or CPU-bound tool needs an explicit "
            "ProcessPoolExecutor. Pass "
            "executor_factory=ProcessPoolFactory(your_pool, 1) to "
            "WorkerManager.register() and own the pool's lifecycle "
            "(ADR-078 §3.2), or convert the tool to `async def invoke` "
            "if it is I/O-bound."
        )
    return pool


__all__ = [
    "AsyncInProcFactory",
    "PoolDeprecationWarning",
    "ProcessPoolFactory",
    "WorkerStrategyError",
    "default_factory_for",
]
