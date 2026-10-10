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


def _invoke_tool_sync(
    tool_cls: type, idempotency_key: str, kwargs: Mapping[str, JsonValue]
) -> Mapping[str, str | JsonValue]:
    """
    Synchronous wrapper that runs a tool's ``invoke``
    coroutine inside the worker process.

    Instantiates ``tool_cls`` with no arguments, opens
    a fresh event loop for this process (the worker is
    a fresh Python under ``spawn`` and has no running
    loop), runs ``tool_instance.invoke(...)`` to
    completion, and serialises the ``Result`` to a
    JSON-safe ``dict`` so the parent can read it back
    across the multiprocessing boundary.
    """
    tool_instance = tool_cls()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            tool_instance.invoke(idempotency_key=idempotency_key, **kwargs)
        )
        if result.is_ok():
            return cast(
                "Mapping[str, str | JsonValue]",
                {"status": "ok", "value": result.unwrap()},
            )
        return cast(
            "Mapping[str, str | JsonValue]",
            {"status": "err", "error": str(result.err_value_or_raise())},
        )
    finally:
        loop.close()
