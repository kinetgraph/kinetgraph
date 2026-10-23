# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``WorkerManager._compute_max_workers`` — the
helper extracted from ``start()`` (ADR-019 CC ≤ 10 refactor).

These are pure functions on the Manager's state, so they
run as ``unittest.TestCase`` without a Redis fixture. The
behaviour tests in
``tests/integration/tools/test_executor_factory.py`` cover
the ``executor_factory`` happy path; these tests cover the
extracted helper and the ADR-078 §4.4 registration gate in
isolation.
"""

from __future__ import annotations

import unittest

from kntgraph.core.result import Ok
from kntgraph.tools._dispatch_helpers import compute_max_workers
from kntgraph.tools.executors import WorkerStrategyError
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


# ``__tool_worker_cpu_bound__`` is an internal marker read by
# ``default_factory_for`` (``executors.py``). The
# ``@tool_worker`` decorator does not expose it; tools set it
# directly when they wrap a blocking C extension and need the
# process-pool factory selected. It no longer exempts a sync
# tool from supplying a pool — see ``TestStrictStrategyGate``.
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
        tools = {
            "a": type("A", (), {"__tool_worker_max_concurrency__": 8}),
            "b": type("B", (), {"__tool_worker_max_concurrency__": 8}),
            "c": type("C", (), {"__tool_worker_max_concurrency__": 8}),
        }
        self.assertEqual(compute_max_workers(tools, explicit_cap=2), 2)

    def test_default_formula_is_sum_capped_32_floored_1(self) -> None:
        """Without ``max_pool_workers``, the default is
        ``max(1, min(32, sum(max_concurrency)))``. Three tools
        with ``max_concurrency=4`` each = 12; the ``min(32, 12)``
        keeps 12; the ``max(1, 12)`` keeps 12."""
        tools = {
            f"t{i}": type(f"T{i}", (), {"__tool_worker_max_concurrency__": 4})
            for i in range(3)
        }
        self.assertEqual(compute_max_workers(tools, explicit_cap=None), 12)

    def test_default_formula_floors_at_1(self) -> None:
        """Zero registered tools → ``sum=0`` → ``max(1, min(32, 0)) = 1``.
        The previous baseline used ``max(2, ...)``; the post-mortem
        2026-10-07 change moves the floor to 1 so the caller can
        reach a single-worker pool without overrides."""
        self.assertEqual(compute_max_workers({}, explicit_cap=None), 1)

    def test_default_formula_caps_at_32(self) -> None:
        """``sum=200`` → ``min(32, 200) = 32`` → ``max(1, 32) = 32``."""
        tools = {
            f"t{i}": type(f"T{i}", (), {"__tool_worker_max_concurrency__": 4})
            for i in range(50)  # 50 * 4 = 200
        }
        self.assertEqual(compute_max_workers(tools, explicit_cap=None), 32)

    def test_method_delegate_keeps_call_shape(self) -> None:
        """``WorkerManager._compute_max_workers`` is a thin
        delegate to the helper. The method exists so the
        ``start()`` call site stays ``self._compute_max_workers()``
        without a kwargs dance; the helper is the tested surface.
        """
        mgr = self._manager(max_pool_workers=4)
        mgr._tools = {"a": type("A", (), {"__tool_worker_max_concurrency__": 8})}
        self.assertEqual(mgr._compute_max_workers(), 4)


class TestStrictStrategyGate(unittest.TestCase):
    """ADR-078 §4.4: a tool must declare how it runs.

    Before this change the gate was defeated by the
    ``__tool_worker_cpu_bound__`` marker — ``is_cpu_bound``
    returned True for *every* sync ``invoke``, so a sync tool
    with no factory always reached the implicit process pool
    instead of raising. These tests pin the corrected contract.
    """

    def _manager(self) -> WorkerManager:
        mgr = WorkerManager.__new__(WorkerManager)
        mgr._tools = {}
        mgr._acls = {}
        mgr._executors = {}
        return mgr

    def test_sync_tool_without_factory_raises(self) -> None:
        class SyncDoubler:
            def invoke(self, *, idempotency_key: str, number: int) -> int:
                return number * 2

        SyncDoubler.name = "sync_doubler"
        SyncDoubler.description = "doubles"
        SyncDoubler.input_schema = {}
        SyncDoubler.__tool_worker_max_concurrency__ = 1
        SyncDoubler.__tool_worker_retries__ = 0

        with self.assertRaises(WorkerStrategyError) as ctx:
            self._manager().register(SyncDoubler, acl=None)
        self.assertIn("executor_factory", str(ctx.exception))

    def test_cpu_bound_marker_no_longer_buys_an_implicit_pool(self) -> None:
        """The marker selects a built-in factory for a caller
        who already owns a pool; it does not exempt a sync tool
        from declaring one."""

        class MarkedSync:
            __tool_worker_cpu_bound__ = True

            def invoke(self, *, idempotency_key: str, number: int) -> int:
                return number * 2

        MarkedSync.name = "marked_sync"
        MarkedSync.description = "marked"
        MarkedSync.input_schema = {}
        MarkedSync.__tool_worker_max_concurrency__ = 1
        MarkedSync.__tool_worker_retries__ = 0

        with self.assertRaises(WorkerStrategyError):
            self._manager().register(MarkedSync, acl=None)

    def test_sync_tool_with_factory_registers(self) -> None:
        class SyncDoubler:
            def invoke(self, *, idempotency_key: str, number: int) -> int:
                return number * 2

        SyncDoubler.name = "sync_doubler"
        SyncDoubler.description = "doubles"
        SyncDoubler.input_schema = {}
        SyncDoubler.__tool_worker_max_concurrency__ = 1
        SyncDoubler.__tool_worker_retries__ = 0

        async def factory(sync_fn, idempotency_key, tool_params):
            return {"status": "ok", "value": None}

        mgr = self._manager()
        mgr.register(SyncDoubler, acl=None, executor_factory=factory)
        self.assertIn("sync_doubler", mgr)

    def test_async_tool_without_factory_registers(self) -> None:
        """Async tools still need no factory — they default to
        ``AsyncInProcFactory`` (the v0.18 default)."""
        mgr = self._manager()
        mgr.register(AsyncDoublerTool, acl=None)
        self.assertIn("async_doubler", mgr)


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
        from concurrent.futures import ProcessPoolExecutor

        mgr._pool = ProcessPoolExecutor()  # refactored dispatch: pass real pool so the
        # async path doesn't run a real tool when the test patches _invoke_tool_sync
        mgr._event_log = EventLog(RedisEventLogAdapter(object()))  # ditto

        result = await mgr._dispatch_to_tool(
            tool_name="async_doubler",
            tool_cls=AsyncDoublerTool,
            idempotency_key="k",
            tool_params={"number": 7},
        )

        # The async path passes the tool's ``Ok(...)`` value
        # through as the ``value`` field of ``ToolResult``
        # (ADR-079). The tool returns ``Ok({"result": 14})``
        # so the value is the dict itself.
        from kntgraph.tools._result import ToolResult

        self.assertEqual(result, ToolResult.ok({"result": 14}))

    async def test_executor_factory_overrides_default(self) -> None:
        """Sanity: when a factory is registered, the default
        path is NOT taken. The factory's result flows through
        unchanged."""
        mgr = WorkerManager.__new__(WorkerManager)
        mgr._tools = {"async_doubler": AsyncDoublerTool}
        mgr._acls = {}
        mgr._executors = {}
        mgr._pool = None  # __new__ bypasses __init__

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
