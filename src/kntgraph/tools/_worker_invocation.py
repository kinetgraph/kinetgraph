# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Worker invocation boundary for the WorkerManager.

Lives in its own module so the ``spawn`` start method
can pickle the wrapped callable by reference without
dragging the rest of ``kntgraph.tools`` (and its
importers) into the freshly-spawned worker process.

Background
----------

``concurrent.futures.ProcessPoolExecutor`` sends
callables to its worker processes via ``pickle``.
Under the historical Linux default (``fork``) the
worker inherits the parent's module state in place,
which lets a top-level function defined inside
``manager.py`` be looked up by qualified name. Under
``spawn`` (the only safe start method in a container
where the parent process has imported threading +
``ssl`` + ``cryptography`` + ``redis.asyncio`` +
``pydantic`` + ``litellm`` before the pool is built),
the worker is a *clean* Python interpreter that
re-imports only the modules the callable's qualified
name points to. Module-level definitions therefore
have to live in a module whose import graph is small
enough not to re-trigger the deadlock.

See ``ADRs/ADR-054-WorkerManager-Transport-Evaluation.md``
lines 269-273 for the original design note on the
fork+openssl+thread-local interaction.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Mapping
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue

from kntgraph.core.result import Result
from kntgraph.core.result.errors import ToolError
from kntgraph.tools._result import ToolResult


def _invoke_tool_sync(
    tool_cls: type, idempotency_key: str, kwargs: Mapping[str, JsonValue]
) -> ToolResult:
    """Synchronous wrapper that runs a tool's ``invoke``
    inside the worker process and serialises the ``Result``
    to a ``ToolResult`` (ADR-079) for the parent to read back
    across the multiprocessing boundary.

    Both ``invoke`` shapes are supported — see ``_call_invoke``.
    """
    tool_instance = tool_cls()

    # ``__tool_cls__`` is the dispatcher's internal hand-off key,
    # not a tool parameter: strip it before forwarding.
    invoke_kwargs = {k: v for k, v in kwargs.items() if k != "__tool_cls__"}

    result = _call_invoke(tool_instance, idempotency_key, invoke_kwargs)
    if result.is_ok():
        # ``unwrap()`` returns ``T``, read as ``JsonValue`` at the
        # wire boundary (ADR-067); the cast acknowledges it.
        return ToolResult.ok(cast("JsonValue", result.unwrap()))
    return _toolresult_from_err_value(result.err_value_or_raise())


def _call_invoke(
    tool_instance: object, idempotency_key: str, invoke_kwargs: Mapping[str, JsonValue]
) -> Result[JsonValue, ToolError]:
    """Call ``invoke`` honouring its declared sync / async shape.

    A sync ``def invoke`` is the CPU-bound shape ADR-078's
    ``ProcessPoolFactory`` exists for: being in a worker process
    is the point, so there is no loop to drive. An
    ``async def invoke`` needs one — the worker is a clean
    interpreter under ``spawn`` with no running loop.

    Branches on the *declared* shape, not the returned value:
    calling an ``async def invoke`` to inspect what it returned
    would materialise the coroutine outside the loop, leaking it
    as "never awaited" if anything raised in between.
    """
    invoke = tool_instance.invoke  # type: ignore[attr-defined]
    if inspect.iscoroutinefunction(invoke):
        return _run_awaited(invoke(idempotency_key=idempotency_key, **invoke_kwargs))
    return invoke(idempotency_key=idempotency_key, **invoke_kwargs)


def _run_awaited(
    awaitable: Awaitable[Result[JsonValue, ToolError]],
) -> Result[JsonValue, ToolError]:
    """Drive ``awaitable`` to completion on a fresh event loop.

    The loop is created and bound here (so
    ``asyncio.get_running_loop()`` inside the tool resolves)
    and closed in a ``finally``. Kept in its own frame so the
    coroutine is created and consumed without an intervening
    frame boundary.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(awaitable)
    finally:
        loop.close()


def _toolresult_from_err_value(err_value: object) -> ToolResult:
    """Map an ``Err``-branch value to a ``ToolResult``.
    Falls back to ``"unknown"`` for non-``ToolError``
    err values (preserves the legacy behaviour for tools
    that raised a bare string or a non-tool exception).
    """
    if isinstance(err_value, ToolError):
        return ToolResult.err(str(err_value), error_class=err_value.error_class)
    return ToolResult.err(str(err_value))
