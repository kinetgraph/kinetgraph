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

    # 3. Async tool: invoke directly in the event loop.
    result = await tool_instance.invoke(idempotency_key=idempotency_key, **tool_params)
    if result.is_ok():
        # ``result.unwrap()`` is ``T``; the framework
        # treats it as ``JsonValue`` at the wire boundary
        # (ADR-067 + ADR-079 §7.2 — slight type-erasure at
        # the wire).
        return ToolResult.ok(cast("JsonValue", result.unwrap()))
    return ToolResult.err(str(result.err_value_or_raise()))


__all__ = ["ExecutorFactory", "ToolSyncInvoke", "dispatch_to_tool"]
