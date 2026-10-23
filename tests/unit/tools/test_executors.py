# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the built-in ``ExecutorFactory`` implementations
(ADR-078 v0.17.0 step 1, skill §9 ratchet).

The contract (per ADR-079): ``ToolSyncInvoke`` returns
``ToolResult`` (the discriminated union). The
``AsyncInProcFactory`` invokes the tool's ``invoke`` directly
on the running event loop (ADR-078 §3.1 — the canonical
default for async tools). The ``ProcessPoolFactory`` runs
the dispatcher-supplied ``sync_fn`` in a caller-owned pool
(the canonical path for sync / CPU-bound tools).

These tests use real ``ThreadPoolExecutor`` instances (a thread
pool, not process) to exercise the same code path without
pickling overhead. The tool classes are simple function objects
— no ``@tool_worker`` decorator (the decorator is unrelated
to the factory contract) and no mocks (per the skill §1.3
ratchet).
"""

from __future__ import annotations

import asyncio
import contextlib
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from kntgraph.core.result import Ok, Result
from kntgraph.tools._result import ToolResult
from kntgraph.tools.executors import (
    AsyncInProcFactory,
    PoolDeprecationWarning,
    ProcessPoolFactory,
    WorkerStrategyError,
    default_factory_for,
)

# ---------------------------------------------------------------------------
# Test tool classes (plain classes with ``invoke`` method, no
# ``@tool_worker`` decorator)
# ---------------------------------------------------------------------------


class SyncEcho:
    """Sync tool — used with ``ProcessPoolFactory`` (where the
    pool-hosted sync helper wraps it). Async factories
    (``AsyncInProcFactory``) ignore sync tools; the dispatch
    picks ``AsyncInProcFactory`` for async tools and
    ``ProcessPoolFactory`` for sync tools.
    """

    def invoke(self, *, idempotency_key: str) -> Any:
        return Ok({"echo": idempotency_key})


class FailingEcho:
    """Sync tool that raises. Used to verify exception
    propagation through ``ProcessPoolFactory``.
    """

    def invoke(self, *, idempotency_key: str) -> Any:
        raise RuntimeError("worker crashed")


class AsyncEcho:
    """Async tool — the canonical target of
    ``AsyncInProcFactory``. Returns the idempotency key as
    the echo payload wrapped in ``Result.ok`` (the
    v0.18 contract per ADR-079).
    """

    async def invoke(self, *, idempotency_key: str) -> Result[Any, Exception]:
        return Ok({"echo": idempotency_key})


class AsyncFailingEcho:
    """Async tool that raises. Used to verify exception
    propagation through ``AsyncInProcFactory`` (the exception
    bubbles through ``await`` directly, no thread).
    """

    async def invoke(self, *, idempotency_key: str) -> Result[Any, Exception]:
        raise RuntimeError("worker crashed")


# ---------------------------------------------------------------------------
# Module-level sync helpers (real implementations, no mocks)
# ---------------------------------------------------------------------------


def _ok_sync_fn(
    tool_cls: type, idempotency_key: str, kwargs: dict[str, Any]
) -> ToolResult:
    """Real sync helper for ``ProcessPoolFactory`` tests: instantiate
    the tool, call ``invoke`` (which returns a ``Result``), unwrap
    the success value, and wrap it in ``ToolResult.ok``. Mirrors
    the real ``_invoke_tool_sync`` shape (ADR-079) so the
    pool's wire result is bit-identical to the dispatcher's.
    """
    instance = tool_cls()
    invoke_kwargs = {k: v for k, v in kwargs.items() if k != "__tool_cls__"}
    result = instance.invoke(idempotency_key=idempotency_key, **invoke_kwargs)
    if result.is_ok():
        return ToolResult.ok(result.unwrap())
    return ToolResult.err(str(result.err_value_or_raise()))


def _failing_sync_fn(
    tool_cls: type, idempotency_key: str, kwargs: dict[str, Any]
) -> ToolResult:
    """Sync helper that always raises (for the
    exception-propagation test).
    """
    raise RuntimeError("worker crashed")


# ---------------------------------------------------------------------------
# AsyncInProcFactory
# ---------------------------------------------------------------------------


class TestAsyncInProcFactory:
    def test_invokes_tool_directly_on_event_loop(self) -> None:
        """The factory invokes ``tool_cls().invoke(...)`` directly
        on the running event loop (ADR-078 §3.1) and the
        resulting awaitable resolves to a ``ToolResult.ok``
        carrying the tool's return value.
        """
        factory = AsyncInProcFactory()

        params: dict[str, Any] = {"__tool_cls__": AsyncEcho}
        result = asyncio.run(factory(_ok_sync_fn, "k-1", params))

        assert isinstance(result, ToolResult)
        assert result.status == "ok"
        assert result.value == {"echo": "k-1"}

    def test_warning_fires_once_per_tool_across_instances(self) -> None:
        """ADR-078 §7.4: the ``PoolDeprecationWarning`` fires
        once per (tool, process), NOT once per factory
        instance.

        The dedup used to live on the instance, and
        ``default_factory_for`` builds a fresh
        ``AsyncInProcFactory`` on every dispatch — so the
        warning fired on every single message. The dedup is
        now module-level, keyed by tool class.
        """
        from kntgraph.tools import executors as executors_mod

        executors_mod._WARNED_AUTO_PROMOTION.discard(AsyncEcho)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            # A fresh factory per call, mirroring the default
            # dispatch path.
            for _ in range(3):
                factory = AsyncInProcFactory()
                params: dict[str, Any] = {"__tool_cls__": AsyncEcho}
                asyncio.run(factory(_ok_sync_fn, "k", params))

        pool_warns = [
            w for w in caught if issubclass(w.category, PoolDeprecationWarning)
        ]
        assert len(pool_warns) == 1

    def test_warning_fires_once_per_distinct_tool(self) -> None:
        """Two distinct tools each warn once; a repeat of one
        tool does not warn again."""
        from kntgraph.tools import executors as executors_mod

        executors_mod._WARNED_AUTO_PROMOTION.discard(AsyncEcho)
        executors_mod._WARNED_AUTO_PROMOTION.discard(AsyncFailingEcho)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for cls in (AsyncEcho, AsyncFailingEcho, AsyncEcho):
                params: dict[str, Any] = {"__tool_cls__": cls}
                with contextlib.suppress(RuntimeError):
                    asyncio.run(AsyncInProcFactory()(_ok_sync_fn, "k", params))

        pool_warns = [
            w for w in caught if issubclass(w.category, PoolDeprecationWarning)
        ]
        assert len(pool_warns) == 2

    def test_propagates_exceptions_from_invoke(self) -> None:
        """If the tool's ``invoke`` raises, the exception
        propagates through ``await`` directly (no thread
        hop, no ``asyncio.to_thread`` re-wrap).
        """
        factory = AsyncInProcFactory()

        params: dict[str, Any] = {"__tool_cls__": AsyncFailingEcho}
        with pytest.raises(RuntimeError, match="worker crashed"):
            asyncio.run(factory(_ok_sync_fn, "k", params))


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

        params: dict[str, Any] = {"__tool_cls__": SyncEcho}
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

        params: dict[str, Any] = {"__tool_cls__": SyncEcho}
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

        params: dict[str, Any] = {"__tool_cls__": FailingEcho}
        with pytest.raises(RuntimeError, match="worker crashed"):
            asyncio.run(factory(_failing_sync_fn, "k", params))


# ---------------------------------------------------------------------------
# default_factory_for
# ---------------------------------------------------------------------------


class SyncEchoWithMarker(SyncEcho):
    """A sync tool that explicitly opts in to the
    ``__tool_worker_cpu_bound__ = True`` marker.
    """

    __tool_worker_cpu_bound__ = True


class TestDefaultFactoryFor:
    def test_cpu_bound_marker_wins(self) -> None:
        """``__tool_worker_cpu_bound__ = True`` selects the
        process pool factory. The pool is passed as the
        ``pool=`` argument — ADR-078 §4.4 removed the implicit
        Manager-owned pool, so there is nothing to inject.
        """
        pool = ThreadPoolExecutor(max_workers=1)
        factory = default_factory_for(SyncEchoWithMarker, pool=pool)
        assert isinstance(factory, ProcessPoolFactory)
        assert factory._pool is pool  # type: ignore[attr-defined]

    def test_sync_tool_requires_explicit_pool(self) -> None:
        """A sync tool reaching the default path with no pool
        raises ``WorkerStrategyError`` naming the fix, instead
        of silently falling back to a thread (ADR-078 §4.4)."""
        with pytest.raises(WorkerStrategyError, match="ProcessPoolExecutor"):
            default_factory_for(SyncEcho)

    def test_async_invoke_selects_async_factory(self) -> None:
        """Tools with an async ``invoke`` default to the async
        factory (no pool required).
        """
        factory = default_factory_for(AsyncEcho)
        assert isinstance(factory, AsyncInProcFactory)


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

        params: dict[str, Any] = {"__tool_cls__": AsyncEcho}
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

        params: dict[str, Any] = {"__tool_cls__": SyncEcho}
        result = asyncio.run(factory(_ok_sync_fn, "k", params))
        assert result.value == {"echo": "k"}
