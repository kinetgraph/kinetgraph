# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Pure helpers for ``WorkerManager`` dispatch sizing.

Extracted from ``src/kntgraph/tools/manager.py`` to keep that
file's LOC under the maintainability gate (ADR-019: MI ≥ 20
per file). The function is pure (no ``self`` access); the
``_dispatch_to_tool`` method stays on the Manager because it
needs ``self._pool`` and ``self._executors`` state.

``is_cpu_bound`` used to live here too. ADR-078 §4.4 removed
its last production caller (the implicit-pool dispatch branch
in ``_dispatch.py``): cpu-bound tools now declare a factory at
registration, and ``default_factory_for`` reads
``inspect.iscoroutinefunction`` directly. The heuristic is gone
rather than kept as an unused shim, per the deprecation policy.

Exercised by ``tests/unit/tools/test_manager_dispatch_helpers.py``.
"""

from __future__ import annotations

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
