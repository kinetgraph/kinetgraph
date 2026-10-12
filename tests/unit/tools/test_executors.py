# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the built-in ``ExecutorFactory`` implementations
(ADR-078 v0.17.0 step 1, skill §9 ratchet).

The contract (per ADR-079): ``ToolSyncInvoke`` returns
``ToolResult`` (the discriminated union). The factories
``run_in_executor(sync_fn, ...)`` and pass the result through.
The test ``sync_fn`` functions use ``*args, **kwargs`` so
they can be pickled by both the ``ProcessPoolExecutor`` (in
the manager's real path) and the ``ThreadPoolExecutor``
(in the tests).
"""

from __future__ import annotations

import asyncio
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch

import pytest

from kntgraph.tools._result import ToolResult
from kntgraph.tools.executors import (
    AsyncInProcFactory,
    PoolDeprecationWarning,
    ProcessPoolFactory,
    WorkerStrategyError,
    default_factory_for,
)

# ---------------------------------------------------------------------------
# Test tool classes
# ---------------------------------------------------------------------------


class _SyncEcho:
    __tool_worker_cpu_bound__ = True


class _AsyncEcho:
    async def invoke(self, *, idempotency_key: str) -> Any:
        return {"echo": idempotency_key}


class _SyncNoMarker:
    pass


# ---------------------------------------------------------------------------
# Module-level sync helpers (picklable, returns ToolResult per ADR-079)
# ---------------------------------------------------------------------------


def _echo_sync_fn(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Sync helper in the dispatcher dict shape (legacy v0.17)."""
    idempotency_key = args[1] if len(args) > 1 else "k"
    return {"status": "ok", "value": {"echo": idempotency_key}}


def _failing_sync_fn(*args: Any, **kwargs: Any) -> dict[str, Any]:
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

        params: dict[str, Any] = {"__tool_cls__": _SyncEcho}
        result = asyncio.run(factory(_echo_sync_fn, "k-1", params))

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
                params: dict[str, Any] = {"__tool_cls__": _SyncEcho}
                asyncio.run(factory(_echo_sync_fn, "k", params))

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

        params: dict[str, Any] = {"__tool_cls__": _SyncEcho}
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
        the test process — the factory's code path is identical.
        """
        pool = ThreadPoolExecutor(max_workers=2)
        factory = ProcessPoolFactory(pool, concurrency=1)

        params: dict[str, Any] = {"__tool_cls__": _SyncEcho}
        result = asyncio.run(factory(_echo_sync_fn, "k-1", params))

        assert isinstance(result, ToolResult)
        assert result.status == "ok"
        assert result.value == {"echo": "k-1"}

    def test_dispatches_to_pool_without_semaphore(self) -> None:
        """``concurrency = 0`` is the legacy "disabled" sentinel
        (no semaphore, no concurrency cap).
        """
        pool = ThreadPoolExecutor(max_workers=2)
        factory = ProcessPoolFactory(pool, concurrency=0)

        params: dict[str, Any] = {"__tool_cls__": _SyncEcho}
        result = asyncio.run(factory(_echo_sync_fn, "k-2", params))

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

        params: dict[str, Any] = {"__tool_cls__": _SyncEcho}
        with pytest.raises(RuntimeError, match="worker crashed"):
            asyncio.run(factory(_failing_sync_fn, "k", params))


# ---------------------------------------------------------------------------
# default_factory_for
# ---------------------------------------------------------------------------


class TestDefaultFactoryFor:
    def test_cpu_bound_marker_wins(self) -> None:
        """``__tool_worker_cpu_bound__ = True`` selects the
        process pool factory.
        """
        pool = ThreadPoolExecutor(max_workers=1)
        with patch(
            "kntgraph.tools.executors._require_pool",
            return_value=pool,
        ):
            factory = default_factory_for(_SyncEcho)
        assert isinstance(factory, ProcessPoolFactory)

    def test_async_invoke_selects_async_factory(self) -> None:
        """Tools with an async ``invoke`` default to the async
        factory (no pool required).
        """
        factory = default_factory_for(_AsyncEcho)
        assert isinstance(factory, AsyncInProcFactory)

    def test_sync_without_marker_raises(self) -> None:
        """A sync tool without ``__tool_worker_cpu_bound__`` has
        no clear default — the manager raises
        ``WorkerStrategyError`` in v0.18+. ``default_factory_for``
        surfaces the same error for symmetry.
        """
        with pytest.raises(WorkerStrategyError, match="executor_factory"):
            default_factory_for(_SyncNoMarker)


# ---------------------------------------------------------------------------
# Exception / warning types
# ---------------------------------------------------------------------------


class TestSymbolShapes:
    def test_pool_deprecation_warning_is_user_warning(self) -> None:
        """``PoolDeprecationWarning`` is a ``UserWarning``
        subclass (caller's ``warnings.filterwarnings`` policies
        can target it).
        """
        assert issubclass(PoolDeprecationWarning, UserWarning)

    def test_worker_strategy_error_inherits_exception(self) -> None:
        """``WorkerStrategyError`` is a regular ``Exception``
        that the manager raises in v0.18+.
        """
        assert issubclass(WorkerStrategyError, Exception)
        assert "executor_factory" in (WorkerStrategyError.__doc__ or "")


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

        params: dict[str, Any] = {"__tool_cls__": _AsyncEcho}
        result = asyncio.run(factory(_echo_sync_fn, "k", params))
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

        params: dict[str, Any] = {"__tool_cls__": _SyncEcho}
        result = asyncio.run(factory(_echo_sync_fn, "k", params))
        assert result.value == {"echo": "k"}
