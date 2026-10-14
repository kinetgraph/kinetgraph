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
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue

from kntgraph.core.result.errors import ToolError
from kntgraph.tools._result import ToolResult


def _invoke_tool_sync(
    tool_cls: type, idempotency_key: str, kwargs: Mapping[str, JsonValue]
) -> ToolResult:
    """
    Synchronous wrapper that runs a tool's ``invoke``
    coroutine inside the worker process.

    Instantiates ``tool_cls`` with no arguments, opens
    a fresh event loop for this process (the worker is
    a fresh Python under ``spawn`` and has no running
    loop), runs ``tool_instance.invoke(...)`` to
    completion, and serialises the ``Result`` to a
    ``ToolResult`` (ADR-079) so the parent can read it
    back across the multiprocessing boundary.

    The on-wire shape for callers that need a dict is
    ``ToolResult.to_wire()``; the multiprocessing
    boundary itself uses ``ToolResult`` directly.
    """
    tool_instance = tool_cls()

    # ``__tool_cls__`` is the dispatcher's internal key for
    # passing the tool class through the factory contract;
    # it is NOT a tool parameter and must be stripped from
    # ``kwargs`` before forwarding to the tool's ``invoke``.
    invoke_kwargs = {k: v for k, v in kwargs.items() if k != "__tool_cls__"}

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            tool_instance.invoke(idempotency_key=idempotency_key, **invoke_kwargs)
        )
        if result.is_ok():
            # The tool's ``Result[T, ToolError].unwrap()``
            # returns ``T``, which the framework treats as
            # ``JsonValue`` at the wire boundary (ADR-067).
            # ``cast`` is the explicit acknowledgement of
            # that boundary — see ADR-079 §7.2.
            return ToolResult.ok(cast("JsonValue", result.unwrap()))
        # On the Err branch, prefer the structured
        # ``error_class`` from a ``ToolError`` (the tool
        # classified it). Falls back to ``"unknown"`` for
        # non-``ToolError`` err values — preserves the
        # legacy behaviour for tools that raised a bare
        # string or non-tool exception.
        err_value = result.err_value_or_raise()
        if isinstance(err_value, ToolError):
            return ToolResult.err(str(err_value), error_class=err_value.error_class)
        return ToolResult.err(str(err_value))
    finally:
        loop.close()
