# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
The registration-time strategy gate (ADR-078 §5.5).

``WorkerManager.register`` refuses a tool whose ``invoke`` is
synchronous when the caller declared no ``executor_factory``:
since v0.18 a tool must say how it runs, because the Manager no
longer creates a process pool implicitly and a silent fallback
to the loop's default *thread* executor stalls the Manager's
event loop on a blocking tool.

The gate is its own module because the operator-facing message
is long and does not belong inline in ``register()``, where it
would push the Manager's cyclomatic complexity past the gate's
ceiling and drag the file's maintainability index down with it.
"""

from __future__ import annotations

import inspect

# The operator-facing migration menu appended to every
# ``WorkerStrategyError`` raised here. It names every exit the
# caller has, including the ``legacy_pool=True`` escape hatch:
# an operator who hits the gate must see that the deprecated
# pool is still reachable rather than conclude it was removed.
_STRATEGY_EXITS = (
    "(a) pass executor_factory=ProcessPoolFactory(your_pool, concurrency=N) "
    "when it needs a process boundary; "
    "(b) convert the tool to ``async def invoke`` for I/O-bound work; "
    "(c) pass executor_factory=AsyncInProcFactory() if it is "
    "genuinely non-blocking. "
    "Setting __tool_worker_cpu_bound__ or max_pool_workers alone no "
    "longer supplies a pool implicitly. To keep a sync tool on the "
    "deprecated internal pool for the rest of the v0.18 migration "
    "window, pass legacy_pool=True AND max_pool_workers=N to "
    "WorkerManager(...) (ADR-078 §12.4); that pool is removed in "
    "v0.19 along with this escape hatch."
)


def reject_undeclared_sync_tool(tool_cls: type) -> None:
    """Raise ``WorkerStrategyError`` when ``tool_cls`` has a sync
    ``invoke`` — the shape that cannot run in-proc.

    A no-op for async tools and for classes with no ``invoke`` at
    all (rejected earlier by ``register``'s ``@tool_worker``
    check). The caller is responsible for the
    ``executor_factory is None`` and ``legacy_pool`` exemptions.
    """
    invoke = getattr(tool_cls, "invoke", None)
    if invoke is None or inspect.iscoroutinefunction(invoke):
        return

    from kntgraph.tools.executors import WorkerStrategyError

    raise WorkerStrategyError(
        f"Tool {tool_cls.__name__!r} has a sync ``invoke`` "
        f"but no ``executor_factory``. Declare how it runs "
        f"(ADR-078 v0.18 §5.5): {_STRATEGY_EXITS}"
    )


__all__ = ["reject_undeclared_sync_tool"]
