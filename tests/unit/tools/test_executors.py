# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the built-in ``ExecutorFactory`` implementations
(ADR-078 v0.17.0 step 1, skill §9 ratchet).

The ``__tool_cls__`` key in the test's ``tool_params`` dict
is a runtime hack — the real ``dispatch_to_tool`` strips it
out before passing the dict to the factory. In these tests
we simulate that by passing a dict that pyright would
otherwise reject (it doesn't conform to ``Mapping[str,
JsonValue]``). Each call site uses ``# type: ignore[...]``
to acknowledge the discrepancy.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Awaitable, Callable
from concurrent.futures import ProcessPoolExecutor
from typing import Any
from unittest.mock import patch

import pytest

from kntgraph.core.result import Ok
from kntgraph.tools._result import ToolResult
from kntgraph.tools.executors import (
    AsyncInProcFactory,
    PoolDeprecationWarning,
    ProcessPoolFactory,
    WorkerStrategyError,
    default_factory_for,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _ok_result(key: str) -> Any:
    """Return a successful ``_invoke_tool_sync`` payload."""
    return Ok({"echo": key})


def _failing_sync_fn(_cls: type, _key: str, **_kwargs: Any) -> Any:
    raise RuntimeError("worker crashed")


def _echo_factory(
    fn: Callable[..., Any] = _ok_result,
    *,
    key: str = "k",
) -> Awaitable[Any]:
    """Run ``fn`` in a thread and return the awaitable, mirroring
    the real factory's contract (sync helper wrapped in
    ``asyncio.to_thread``).
    """
    return asyncio.to_thread(fn, type("Echo", (), {}), key)


class _SyncEcho:
    __tool_worker_cpu_bound__ = True

    def invoke(self, *, idempotency_key: str) -> Any:
        return Ok({"echo": idempotency_key})


class _AsyncEcho:
    async def invoke(self, *, idempotency_key: str) -> Any:
        return Ok({"echo": idempotency_key})


class _SyncNoMarker:
    def invoke(self, *, idempotency_key: str) -> Any:
        return Ok({"echo": idempotency_key})


# ---------------------------------------------------------------------------
# AsyncInProcFactory
# ---------------------------------------------------------------------------


class TestAsyncInProcFactory:
    def test_invokes_sync_helper_and_returns_awaitable(self) -> None:
        """The factory wraps ``sync_fn`` in ``asyncio.to_thread``
        and the resulting awaitable resolves to a ``ToolResult``.
        """
        factory = AsyncInProcFactory()
        captured: dict[str, Any] = {}

        def sync_fn(tool_cls: type, idempotency_key: str, **_kwargs: Any) -> Any:
            captured["tool_cls"] = tool_cls
            captured["key"] = idempotency_key
            return Ok({"echo": idempotency_key})

        params: dict[str, Any] = {"__tool_cls__": _SyncEcho}
        result = asyncio.run(
            factory(sync_fn, "k-1", params)  # type: ignore[arg-type]
        )

        assert isinstance(result, ToolResult)
        assert result.status == "ok"
        assert result.value == {"echo": "k-1"}
        assert captured["tool_cls"] is _SyncEcho
        assert captured["key"] == "k-1"

    def test_warning_fires_once_per_instance(self) -> None:
        """The ``PoolDeprecationWarning`` is fired once per
        ``AsyncInProcFactory`` instance (idempotent).
        """
        factory = AsyncInProcFactory()

        def sync_fn(_cls: type, _key: str, **_kwargs: Any) -> Any:
            return Ok({})

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for _ in range(3):
                params: dict[str, Any] = {"__tool_cls__": _SyncEcho}
                asyncio.run(
                    factory(sync_fn, "k", params)  # type: ignore[arg-type]
                )

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
            asyncio.run(
                factory(_failing_sync_fn, "k", params)  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# ProcessPoolFactory
# ---------------------------------------------------------------------------


class TestProcessPoolFactory:
    def test_dispatches_to_pool_with_semaphore(self) -> None:
        """``concurrency > 0`` wraps the call in an
        ``asyncio.Semaphore`` (concurrency cap).
        """
        pool = ProcessPoolExecutor(max_workers=2)
        factory = ProcessPoolFactory(pool, concurrency=1)

        def sync_fn(_cls: type, key: str, **_kwargs: Any) -> Any:
            return Ok({"k": key})

        params: dict[str, Any] = {"__tool_cls__": _SyncEcho}
        result = asyncio.run(
            factory(sync_fn, "k-1", params)  # type: ignore[arg-type]
        )

        assert isinstance(result, ToolResult)
        assert result.status == "ok"
        assert result.value == {"k": "k-1"}

    def test_dispatches_to_pool_without_semaphore(self) -> None:
        """``concurrency = 0`` is the legacy "disabled" sentinel
        (no semaphore, no concurrency cap).
        """
        pool = ProcessPoolExecutor(max_workers=2)
        factory = ProcessPoolFactory(pool, concurrency=0)

        def sync_fn(_cls: type, key: str, **_kwargs: Any) -> Any:
            return Ok({"k": key})

        params: dict[str, Any] = {"__tool_cls__": _SyncEcho}
        result = asyncio.run(
            factory(sync_fn, "k-2", params)  # type: ignore[arg-type]
        )

        assert isinstance(result, ToolResult)
        assert result.value == {"k": "k-2"}


# ---------------------------------------------------------------------------
# default_factory_for
# ---------------------------------------------------------------------------


class TestDefaultFactoryFor:
    def test_cpu_bound_marker_wins(self) -> None:
        """``__tool_worker_cpu_bound__ = True`` selects the
        process pool factory.
        """
        pool = ProcessPoolExecutor(max_workers=1)
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

        The factory returns an awaitable that wraps
        ``_invoke_tool_sync`` in ``asyncio.to_thread``.
        """
        factory = AsyncInProcFactory()

        def sync_fn(_cls: type, key: str, **_kwargs: Any) -> Any:
            return Ok({"echo": key})

        params: dict[str, Any] = {"__tool_cls__": _AsyncEcho}
        result = asyncio.run(
            factory(sync_fn, "k", params)  # type: ignore[arg-type]
        )
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
        pool = ProcessPoolExecutor(max_workers=2)
        factory = ProcessPoolFactory(pool, concurrency=1)

        def sync_fn(_cls: type, key: str, **_kwargs: Any) -> Any:
            return Ok({"echo": key})

        params: dict[str, Any] = {"__tool_cls__": _SyncEcho}
        result = asyncio.run(
            factory(sync_fn, "k", params)  # type: ignore[arg-type]
        )
        assert result.value == {"echo": "k"}
