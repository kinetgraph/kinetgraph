# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Pure helpers for ``WorkerManager`` dispatch sizing and CPU
classification.

Extracted from ``src/kntgraph/tools/manager.py`` to keep that
file's LOC under the maintainability gate (ADR-019: MI ≥ 20
per file). The functions are pure (no ``self`` access); the
``_dispatch_to_tool`` method stays on the Manager because it
needs ``self._pool`` and ``self._executors`` state.

Both helpers are exercised by
``tests/unit/tools/test_manager_dispatch_helpers.py``.
"""

from __future__ import annotations

import inspect
from typing import Any

# ``max_concurrency`` is the per-tool semaphore knob. When a tool
# is registered with ``max_concurrency=N`` the Manager creates an
# ``asyncio.Semaphore(N)`` so the reaper and the consume loop
# see at most N concurrent invocations.
_DEFAULT_PER_TOOL_MAX_CONCURRENCY = 1
# Hard cap on the internal ``ProcessPoolExecutor`` size. The
# floor (1, was 2) and cap (32) protect against two extremes:
# the floor so a service with one registered tool gets one
# worker (avoids the pre-v0.16 default of 2 which left half the
# pool idle); the cap so a service with 200 tools doesn't spawn
# 200 Python interpreters.
_MIN_POOL_WORKERS = 1
_MAX_POOL_WORKERS = 32


def compute_max_workers(
    tools: dict[str, type],
    *,
    explicit_cap: int | None,
) -> int:
    """Size the internal ``ProcessPoolExecutor``.

    Precedence: ``explicit_cap`` (set via
    ``WorkerManager(max_pool_workers=...)``) wins; otherwise
    ``max(1, min(32, sum(max_concurrency)))`` over the registered
    tools' ``max_concurrency``. The post-mortem 2026-10-07
    scenario in the backoffice motivates the explicit cap: a 4 GB
    Fargate task OOMs when 10 workers each load Docling in
    parallel.
    """
    if explicit_cap is not None:
        return explicit_cap
    return max(
        _MIN_POOL_WORKERS,
        min(
            _MAX_POOL_WORKERS,
            sum(
                getattr(
                    t,
                    "__tool_worker_max_concurrency__",
                    _DEFAULT_PER_TOOL_MAX_CONCURRENCY,
                )
                for t in tools.values()
            ),
        ),
    )


def is_cpu_bound(tool_cls: type, tool_instance: Any) -> bool:
    """A tool is CPU-bound if it is explicitly marked
    (``__tool_worker_cpu_bound__ = True``), wraps a non-async
    ``invoke`` (``inspect.iscoroutinefunction`` returns ``False``
    for a regular ``def`` and for ``None`` when ``invoke`` is
    missing), or runs against a wrapped (non-original) sync
    helper. Same heuristic as the pre-refactor inline check;
    extracted so the caller is a single boolean expression and
    ``_process_message``'s cyclomatic complexity stays under 10.
    """
    return (
        getattr(tool_cls, "__tool_worker_cpu_bound__", False)
        or not inspect.iscoroutinefunction(getattr(tool_instance, "invoke", None))
        or _is_wrapped_sync_helper()
    )


def _is_wrapped_sync_helper() -> bool:
    """True when ``_invoke_tool_sync`` has been monkey-patched
    or replaced (the test-instrumentation path in
    ``tests/integration/tools/test_worker_manager.py``). When
    True, even an ``async def invoke`` should run through the
    process pool because the wrapped helper is sync.

    Implementation note
    -------------------
    The identity check (``_invoke_tool_sync is not
    _ORIGINAL_INVOKE_TOOL_SYNC``) reads the *current binding*
    of ``_invoke_tool_sync`` in the ``manager`` module's
    namespace. Test instrumentation does
    ``patch("kntgraph.tools.manager._invoke_tool_sync", ...)``
    which mutates the binding IN the manager module — so the
    identity check has to look there, not at the original
    import in ``_worker_invocation`` (which would always be
    the original, regardless of patching). Reading the name
    via ``getattr`` on the module object is the robust path.
    """
    from kntgraph.tools import manager as _manager

    return _manager._invoke_tool_sync is not _manager._ORIGINAL_INVOKE_TOOL_SYNC
