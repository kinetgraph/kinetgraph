# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Pure dispatch for ``WorkerManager`` (ADR-069).

The ``dispatch_to_tool`` free function encapsulates the
three-strategy decision tree that ``WorkerManager`` uses
to route a single tool message to a runner:

  1. Per-tool ``executor_factory`` registered via
     ``register(executor_factory=...)``: opaque async
     callable that receives the sync ``_invoke_tool_sync``
     plus the idempotency key and tool params. Lets the
     caller route heavy tools (ML models, sidecars, GPU
     pins) to dedicated pools without forking the
     framework.
  2. CPU-bound tool (legacy): ``loop.run_in_executor`` into
     the internal ``ProcessPoolExecutor``.
  3. Async tool (default): direct ``await tool.invoke()``
     in the Manager's event loop.

Extracted from ``WorkerManager._dispatch_to_tool`` so the
decision tree is testable WITHOUT instantiating a Manager
(``_dispatch_helpers.is_cpu_bound`` already moved to a free
function; this completes the extraction). The Manager's
``_dispatch_to_tool`` becomes a thin wrapper that gathers
state and calls ``dispatch_to_tool``.

The function is **pure in its decisions**: it reads from
the ``executors`` and ``pool`` parameters and writes to
its return value. The Manager's ACL, observability, XACK
side effects stay on the Manager side (they are not
dispatch concerns).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ProcessPoolExecutor
from typing import cast

# v0.18.0 default switch: the async-path fallback uses the
# built-in ``AsyncInProcFactory`` via ``default_factory_for``.
# We import the helper lazily to avoid a circular import with
# ``executors`` (the helper sits in ``executors``; ``_dispatch``
# sits in ``tools``; ``executors`` imports from ``tools``).
from kntgraph.core._typing import JsonValue
from kntgraph.tools._dispatch_helpers import is_cpu_bound
from kntgraph.tools._result import ToolResult
from kntgraph.tools._worker_invocation import _invoke_tool_sync

# ADR-069: the dispatch factory protocol. The signature is
# ``(sync_invoke_fn, idempotency_key, tool_params) -> awaitable[ToolResult]``
# — the factory receives the sync ``_invoke_tool_sync`` (so it
# can offload to its own pool) and the params dict, and returns
# an awaitable that resolves to a ``ToolResult`` (the
# discriminated-union replacement for the legacy
# ``result_dict``).
type ToolSyncInvoke = Callable[
    [type, str, "Mapping[str, JsonValue]"],
    ToolResult,
]
ExecutorFactory = Callable[
    [ToolSyncInvoke, str, "Mapping[str, JsonValue]"],
    Awaitable[ToolResult],
]


async def dispatch_to_tool(
    tool_name: str,
    tool_cls: type,
    idempotency_key: str,
    tool_params: Mapping[str, JsonValue],
    *,
    executors: Mapping[str, ExecutorFactory],
    pool: ProcessPoolExecutor | None,
) -> ToolResult:
    """The three-strategy dispatch decision tree (ADR-069).

    See ``WorkerManager._dispatch_to_tool`` for the strategy
    rationale. This free function is testable in isolation
    (a fake ``executors`` mapping + a fake ``pool`` is enough);
    the Manager method that calls it is a thin wrapper that
    gathers the state from ``self``.

    The function does NOT perform ACL checks, observability
    updates, or Redis XACK. Those side effects stay on the
    Manager side (they are not dispatch concerns).
    """
    # 1. Per-tool ``executor_factory`` override (preferred).
    factory: ExecutorFactory | None = executors.get(tool_name)
    if factory is not None:
        return await factory(_invoke_tool_sync, idempotency_key, tool_params)

    # 2. CPU-bound: run the sync wrapper in the shared process pool.
    # ``pool`` may be ``None`` (e.g. when tests construct the
    # Manager via ``WorkerManager(redis, event_log)`` without
    # calling ``start()``); ``run_in_executor(None, ...)`` uses
    # the loop's default executor, which is the correct behavior
    # for the test path (the patch on ``_invoke_tool_sync`` then
    # runs the patched sync function in the loop's thread).
    tool_instance = tool_cls()
    if is_cpu_bound(tool_cls, tool_instance):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            pool,
            _invoke_tool_sync,
            tool_cls,
            idempotency_key,
            tool_params,
        )

    # 3. Async tool: v0.18.0 default switch — use the
    # ``default_factory_for`` fallback (which selects
    # ``AsyncInProcFactory`` for async tools) instead of
    # calling the tool's ``invoke`` directly. The factory
    # route preserves the ``ExecutorFactory`` contract for
    # any custom factory the user might register in the
    # future (per ADR-078 v0.18).
    from kntgraph.tools.executors import default_factory_for as _default_factory_for

    default_factory = _default_factory_for(tool_cls)
    # Inject __tool_cls__ into the params dict so the dispatcher's
    # ``_invoke_tool_sync`` helper can extract it. The factory
    # contract is ``__call__(sync_fn, idempotency_key, tool_params)``
    # — the helper reads tool_cls from the params dict.
    params_with_cls: dict[str, JsonValue] = {
        **dict(tool_params),
        "__tool_cls__": cast(type, tool_cls),  # type: ignore[arg-type]
    }
    return await default_factory(_invoke_tool_sync, idempotency_key, params_with_cls)


__all__ = ["ExecutorFactory", "ToolSyncInvoke", "dispatch_to_tool"]
