# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``WorkerManager._compute_max_workers`` and
``WorkerManager._is_cpu_bound`` — the two helpers extracted
from ``start()`` and ``_dispatch_to_tool`` (ADR-019 CC ≤ 10
refactor; ADR-069 dispatch primitive).

These are pure functions on the Manager's state, so they
run as ``unittest.TestCase`` without a Redis fixture. The
behaviour tests in
``tests/integration/tools/test_executor_factory.py`` cover
the ``executor_factory`` happy path; these tests cover the
two extracted helpers in isolation.
"""

from __future__ import annotations

import unittest

from kntgraph.core.result import Ok
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.worker import tool_worker


@tool_worker(name="async_doubler", max_concurrency=1)
class AsyncDoublerTool:
    async def invoke(self, *, idempotency_key: str, number: int):
        return Ok({"result": number * 2})


@tool_worker(name="cpu_doubler", max_concurrency=1)
class CpuDoublerTool:
    async def invoke(self, *, idempotency_key: str, number: int):
        return Ok({"result": number * 2})


# ``__tool_worker_cpu_bound__`` is an internal marker consumed by
# ``_is_cpu_bound`` in ``manager.py``. The ``@tool_worker``
# decorator does not expose it; tools set it directly when they
# need to opt into the process-pool path. This mirrors the
# production pattern for tools that wrap blocking C extensions.
# Set via a descriptor so the attribute survives the decorator's
# class mutation in both ``is_cpu_bound`` checks and the
# ``run_in_executor`` branch in ``_dispatch_to_tool``.
CpuDoublerTool.__tool_worker_cpu_bound__ = True


class TestComputeMaxWorkers(unittest.TestCase):
    """``_compute_max_workers`` is the size of the internal
    ``ProcessPoolExecutor``. The ``__init__`` kwarg ``max_pool_workers``
    is the explicit cap; the default formula is
    ``max(1, min(32, sum(max_concurrency)))`` (post-mortem
    2026-10-07 floor: ``max(1, ...)`` not ``max(2, ...)``)."""

    def _manager(self, max_pool_workers: int | None = None) -> WorkerManager:
        mgr = WorkerManager.__new__(WorkerManager)
        mgr._tools = {}
        mgr._acls = {}
        mgr._executors = {}
        mgr._max_pool_workers = max_pool_workers
        return mgr

    def test_explicit_cap_wins(self) -> None:
        """``max_pool_workers=N`` returns N regardless of how many
        tools are registered or what their ``max_concurrency`` is."""
        mgr = self._manager(max_pool_workers=2)
        mgr._tools = {
            "a": type("A", (), {"__tool_worker_max_concurrency__": 8}),
            "b": type("B", (), {"__tool_worker_max_concurrency__": 8}),
            "c": type("C", (), {"__tool_worker_max_concurrency__": 8}),
        }
        self.assertEqual(mgr._compute_max_workers(), 2)

    def test_default_formula_is_sum_capped_32_floored_1(self) -> None:
        """Without ``max_pool_workers``, the default is
        ``max(1, min(32, sum(max_concurrency)))``. Three tools
        with ``max_concurrency=4`` each = 12; the ``min(32, 12)``
        keeps 12; the ``max(1, 12)`` keeps 12."""
        mgr = self._manager(max_pool_workers=None)
        mgr._tools = {
            f"t{i}": type(f"T{i}", (), {"__tool_worker_max_concurrency__": 4})
            for i in range(3)
        }
        self.assertEqual(mgr._compute_max_workers(), 12)

    def test_default_formula_floors_at_1(self) -> None:
        """Zero registered tools → ``sum=0`` → ``max(1, min(32, 0)) = 1``.
        The previous baseline used ``max(2, ...)``; the post-mortem
        2026-10-07 change moves the floor to 1 so the caller can
        reach a single-worker pool without overrides."""
        mgr = self._manager(max_pool_workers=None)
        self.assertEqual(mgr._compute_max_workers(), 1)

    def test_default_formula_caps_at_32(self) -> None:
        """``sum=200`` → ``min(32, 200) = 32`` → ``max(1, 32) = 32``."""
        mgr = self._manager(max_pool_workers=None)
        mgr._tools = {
            f"t{i}": type(f"T{i}", (), {"__tool_worker_max_concurrency__": 4})
            for i in range(50)  # 50 * 4 = 200
        }
        self.assertEqual(mgr._compute_max_workers(), 32)


class TestIsCpuBound(unittest.TestCase):
    """``_is_cpu_bound`` decides between the async and the
    ``run_in_executor`` branches in ``_dispatch_to_tool``.
    Three conditions qualify: ``__tool_worker_cpu_bound__`` flag,
    sync (non-coroutine) ``invoke``, or a wrapped (non-original)
    sync helper."""

    def test_explicit_cpu_bound_flag(self) -> None:
        tool = CpuDoublerTool()
        self.assertTrue(WorkerManager._is_cpu_bound(CpuDoublerTool, tool))

    def test_async_tool_is_not_cpu_bound(self) -> None:
        tool = AsyncDoublerTool()
        self.assertFalse(WorkerManager._is_cpu_bound(AsyncDoublerTool, tool))

    def test_sync_invoke_is_treated_as_cpu_bound(self) -> None:
        """A tool whose ``invoke`` is a plain ``def`` (not a
        coroutine) still runs through the process pool. Same
        heuristic as the pre-refactor inline check."""

        class SyncDoubler:
            __tool_worker_cpu_bound__ = False  # not flagged

            def invoke(self, *, idempotency_key: str, number: int) -> int:
                return number * 2

        SyncDoubler.__tool_worker_name__ = "sync_doubler"
        SyncDoubler.__tool_worker_max_concurrency__ = 1
        SyncDoubler.__tool_worker_retries__ = 0
        instance = SyncDoubler()
        self.assertTrue(WorkerManager._is_cpu_bound(SyncDoubler, instance))

    def test_class_without_invoke_attribute_is_treated_as_cpu_bound(self) -> None:
        """Edge case: a class without ``invoke`` (e.g. someone
        registered a non-@tool_worker class by accident). The
        heuristic ``not inspect.iscoroutinefunction(None) = True``
        makes such classes fall into the CPU-bound branch; the
        actual ``invoke`` call later raises ``AttributeError``,
        which the existing hard_crash handler converts to a
        ``tool.<name>.failed`` event. Same behaviour as the
        pre-refactor inline check; we lock it in here to detect
        drift if anyone tries to special-case it."""

        class NoInvoke:
            __tool_worker_cpu_bound__ = False

        NoInvoke.__tool_worker_name__ = "no_invoke"
        NoInvoke.__tool_worker_max_concurrency__ = 1
        NoInvoke.__tool_worker_retries__ = 0
        instance = NoInvoke()
        self.assertTrue(WorkerManager._is_cpu_bound(NoInvoke, instance))


class TestDispatchToToolNonFactoryPath(unittest.IsolatedAsyncioTestCase):
    """The ``executor_factory`` always takes priority in
    ``_dispatch_to_tool``. The non-factory path (legacy CPU /
    async branches) is the default and must keep working when
    no factory is registered. This is the regression guard for
    the refactor."""

    async def test_async_tool_default_path(self) -> None:
        from kntgraph.infra.redis._event_log import RedisEventLogAdapter
        from kntgraph.stream.event_log.store import EventLog

        mgr = WorkerManager.__new__(WorkerManager)
        mgr._tools = {"async_doubler": AsyncDoublerTool}
        mgr._acls = {}
        mgr._executors = {}
        mgr._redis = object()  # not used in this branch
        mgr._event_log = EventLog(RedisEventLogAdapter(object()))  # ditto

        result = await mgr._dispatch_to_tool(
            tool_name="async_doubler",
            tool_cls=AsyncDoublerTool,
            idempotency_key="k",
            tool_params={"number": 7},
        )

        # The async path passes the tool's ``Ok(...)`` value
        # through as the ``value`` field of ``result_dict``. The
        # tool returns ``Ok({"result": 14})`` so the value is the
        # dict itself.
        self.assertEqual(result, {"status": "ok", "value": {"result": 14}})

    async def test_executor_factory_overrides_default(self) -> None:
        """Sanity: when a factory is registered, the default
        path is NOT taken. The factory's result flows through
        unchanged."""
        mgr = WorkerManager.__new__(WorkerManager)
        mgr._tools = {"async_doubler": AsyncDoublerTool}
        mgr._acls = {}
        mgr._executors = {}

        captured: list[dict] = []

        async def factory(sync_fn, idempotency_key, tool_params):
            captured.append({"ik": idempotency_key, "params": tool_params})
            return {"status": "ok", "value": "from-factory"}

        mgr._executors["async_doubler"] = factory

        result = await mgr._dispatch_to_tool(
            tool_name="async_doubler",
            tool_cls=AsyncDoublerTool,
            idempotency_key="ik-1",
            tool_params={"number": 7},
        )

        self.assertEqual(result, {"status": "ok", "value": "from-factory"})
        self.assertEqual(captured, [{"ik": "ik-1", "params": {"number": 7}}])


if __name__ == "__main__":
    unittest.main()
