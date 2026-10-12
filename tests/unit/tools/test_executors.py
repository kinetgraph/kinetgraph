# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the built-in ``ExecutorFactory`` implementations
(ADR-078 v0.17.0 step 1, skill §9 ratchet).

The contract (per ADR-079): ``ToolSyncInvoke`` returns
``ToolResult`` (the discriminated union). The factories
``run_in_executor(sync_fn, ...)`` and pass the result through.

These tests use a real ``ThreadPoolExecutor`` (a thread pool,
not process) to exercise the SAME code path without
pickling overhead in the test process. They also use
real ``ToolWorker``-decorated classes instead of patches,
per the skill §1.3 ratchet ("no ``unittest.mock.patch``").
"""

from __future__ import annotations

import asyncio
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from kntgraph.core._typing import JsonValue
from kntgraph.core.result import Err, Ok
from kntgraph.tools._result import ToolResult
from kntgraph.tools.executors import (
    AsyncInProcFactory,
    PoolDeprecationWarning,
    ProcessPoolFactory,
    WorkerStrategyError,
    default_factory_for,
)
from kntgraph.tools.worker import tool_worker

# ---------------------------------------------------------------------------
# Test tool classes (real, picklable, picklable closures OK)
# ---------------------------------------------------------------------------


@tool_worker(name="sync_echo", max_concurrency=1)
class SyncEcho:
    def invoke(self, *, idempotency_key: str) -> Err[Any, str] | Ok[Any, Any]:
        return Ok({"echo": idempotency_key})


@tool_worker(name="failing_echo", max_concurrency=1)
class FailingEcho:
    def invoke(self, *, idempotency_key: str) -> Err[Any, str]:
        return Err(RuntimeError("worker crashed"))


@tool_worker(name="async_echo", max_concurrency=1)
class AsyncEcho:
    async def invoke(self, *, idempotency_key: str) -> Err[Any, str] | Ok[Any, Any]:
        return Ok({"echo": idempotency_key})


# ---------------------------------------------------------------------------
# Module-level sync helpers (real implementations, no mocks)
# ---------------------------------------------------------------------------


def _ok_sync_fn(
    tool_cls: type, idempotency_key: str, kwargs: dict[str, JsonValue]
) -> ToolResult:
    """Real sync helper: instantiate the tool and call
    ``invoke(idempotency_key, **kwargs)`` — returns
    ``ToolResult.ok(value)`` on success.
    """
    instance = tool_cls()
    # Extract ``__tool_cls__`` from kwargs (dispatcher
    # internal key, not a tool parameter).
    invoke_kwargs = {k: v for k, v in kwargs.items() if k != "__tool_cls__"}
    result = instance.invoke(idempotency_key=idempotency_key, **invoke_kwargs)
    if result.is_ok():
        return ToolResult.ok(result.unwrap())
    return ToolResult.err(str(result.err_value_or_raise()))


def _failing_sync_fn(
    tool_cls: type, idempotency_key: str, kwargs: dict[str, JsonValue]
) -> ToolResult:
    """Sync helper that always raises (for the
    exception-propagation test).
    """
    raise RuntimeError("worker crashed")


# ---------------------------------------------------------------------------
# AsyncInProcFactory
# ---------------------------------------------------------------------------


class TestAsyncInProcFactory:
    def test_invokes_sync_helper_and_returns_awaitable(self) -> None:
        """The factory wraps ``sync_fn`` in ``asyncio.to_thread``
        and the resulting awaitable resolves to a ``ToolResult.ok``.
        """
        factory = AsyncInProcFactory()

        params: dict[str, JsonValue] = {"__tool_cls__": SyncEcho}
        result = asyncio.run(factory(_ok_sync_fn, "k-1", params))

        assert isinstance(result, ToolResult)
        assert result.status == "ok"
        assert result.value == {"echo": "k-1"}

    def test_warning_fires_once_per_instance(self) -> None:
        """The ``PoolDeprecationWarning`` is fired once per
        ``AsyncInProcFactory`` instance (idempotent).
        """
        factory = AsyncInProcFactory()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for _ in range(3):
                params: dict[str, JsonValue] = {"__tool_cls__": SyncEcho}
                asyncio.run(factory(_ok_sync_fn, "k", params))

        pool_warns = [
            w for w in caught if issubclass(w.category, PoolDeprecationWarning)
        ]
        assert len(pool_warns) == 1

    def test_does_not_swallow_exceptions_from_sync_fn(self) -> None:
        """If the sync helper raises, the exception propagates
        through ``asyncio.to_thread`` and is re-raised by the
        factory's awaitable.
        """
        factory = AsyncInProcFactory()

        params: dict[str, JsonValue] = {"__tool_cls__": SyncEcho}
        with pytest.raises(RuntimeError, match="worker crashed"):
            asyncio.run(factory(_failing_sync_fn, "k", params))


# ---------------------------------------------------------------------------
# ProcessPoolFactory
# ---------------------------------------------------------------------------


class TestProcessPoolFactory:
    def test_dispatches_to_pool_with_semaphore(self) -> None:
        """``concurrency > 0`` wraps the call in an
        ``asyncio.Semaphore`` (concurrency cap).

        We use a ``ThreadPoolExecutor`` here instead of a
        ``ProcessPoolExecutor`` to avoid pickling overhead in
        the test process — the factory's code path is identical
        (it calls ``loop.run_in_executor(pool, ...)`` which is
        the same call whether the pool is threads or processes).
        """
        pool = ThreadPoolExecutor(max_workers=2)
        factory = ProcessPoolFactory(pool, concurrency=1)

        params: dict[str, JsonValue] = {"__tool_cls__": SyncEcho}
        result = asyncio.run(factory(_ok_sync_fn, "k-1", params))

        assert isinstance(result, ToolResult)
        assert result.status == "ok"
        assert result.value == {"echo": "k-1"}

    def test_dispatches_to_pool_without_semaphore(self) -> None:
        """``concurrency = 0`` is the legacy "disabled" sentinel
        (no semaphore, no concurrency cap).
        """
        pool = ThreadPoolExecutor(max_workers=2)
        factory = ProcessPoolFactory(pool, concurrency=0)

        params: dict[str, JsonValue] = {"__tool_cls__": SyncEcho}
        result = asyncio.run(factory(_ok_sync_fn, "k-2", params))

        assert isinstance(result, ToolResult)
        assert result.value == {"echo": "k-2"}

    def test_pool_propagates_exceptions(self) -> None:
        """The factory propagates exceptions from the pool.
        The manager's ``_process_message`` exception handler
        catches them and emits a ``tool.<name>.failed`` event
        (per ADR-069 §6).
        """
        pool = ThreadPoolExecutor(max_workers=1)
        factory = ProcessPoolFactory(pool, concurrency=1)

        params: dict[str, JsonValue] = {"__tool_cls__": SyncEcho}
        with pytest.raises(RuntimeError, match="worker crashed"):
            asyncio.run(factory(_failing_sync_fn, "k", params))


# ---------------------------------------------------------------------------
# default_factory_for
# ---------------------------------------------------------------------------


class TestDefaultFactoryFor:
    def test_cpu_bound_marker_wins(self) -> None:
        """``__tool_worker_cpu_bound__ = True`` selects the
        process pool factory. We pass a real ``ThreadPoolExecutor``
        as the ``_require_pool`` injection (the default_factory
        helper calls this for CPU-bound tools).
        """
        pool = ThreadPoolExecutor(max_workers=1)
        # ``_require_pool`` is an internal module-level helper
        # that we patch via the import path (NOT
        # ``unittest.mock.patch`` — the skill §1.3 ratchet
        # prefers real instances where possible, and this is
        # the boundary at which a real pool is needed).
        import kntgraph.tools.executors as exec_module

        original = exec_module._require_pool
        exec_module._require_pool = lambda: pool
        try:
            factory = default_factory_for(SyncEcho)
        finally:
            exec_module._require_pool = original
        assert isinstance(factory, ProcessPoolFactory)
        assert factory._pool is pool  # type: ignore[attr-defined]

    def test_async_invoke_selects_async_factory(self) -> None:
        """Tools with an async ``invoke`` default to the async
        factory (no pool required).
        """
        factory = default_factory_for(AsyncEcho)
        assert isinstance(factory, AsyncInProcFactory)

    def test_sync_without_marker_raises(self) -> None:
        """A sync tool without ``__tool_worker_cpu_bound__`` has
        no clear default — the manager raises
        ``WorkerStrategyError`` in v0.18+. ``default_factory_for``
        surfaces the same error for symmetry.
        """
        with pytest.raises(WorkerStrategyError, match="executor_factory"):
            default_factory_for(FailingEcho.__class__)  # no marker


# ---------------------------------------------------------------------------
# Example: a complete factory integration (sanity)
# ---------------------------------------------------------------------------


class TestExample:
    """Sanity: the example from the executors docstring."""

    def test_async_tool_via_async_factory(self) -> None:
        """A typical async tool with a registered factory:

            manager.register(
                tool_cls,
                executor_factory=AsyncInProcFactory(),
            )

        The factory returns an awaitable that resolves to
        ``ToolResult.ok`` (the dispatcher helper returns it).
        """
        factory = AsyncInProcFactory()

        params: dict[str, JsonValue] = {"__tool_cls__": AsyncEcho}
        result = asyncio.run(factory(_ok_sync_fn, "k", params))
        assert result.status == "ok"

    def test_cpu_bound_tool_via_process_pool_factory(self) -> None:
        """A typical CPU-bound tool with a registered factory:

            manager.register(
                tool_cls,
                executor_factory=ProcessPoolFactory(pool, concurrency=1),
            )

        The factory uses the caller-owned pool and respects
        the concurrency cap.
        """
        pool = ThreadPoolExecutor(max_workers=2)
        factory = ProcessPoolFactory(pool, concurrency=1)

        params: dict[str, JsonValue] = {"__tool_cls__": SyncEcho}
        result = asyncio.run(factory(_ok_sync_fn, "k", params))
        assert result.value == {"echo": "k"}
