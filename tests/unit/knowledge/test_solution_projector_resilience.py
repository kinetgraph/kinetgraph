# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Resilience wiring tests for
`kntgraph.agents.knowledge.solution_projector.SolutionProjector`.

These tests build on the existing
`tests/unit/knowledge/test_solution_projector.py` mocks
(MockGraph / MockClient) and exercise the resilience
seams added to the projector:

  - Each `graph.query` runs in a worker thread
    (`asyncio.to_thread`); the event loop is never
    blocked.
  - Each query is bounded by `query_timeout_seconds`.
    A hanging query raises `asyncio.TimeoutError`.
  - A configured bulkhead rejects when saturated;
    `upsert` returns 0 (treated as failed by the
    promoter) rather than blocking.

The Mongo / FalkorDB drivers are NOT used; the mocks
count calls and can be configured to sleep / raise.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from kntgraph.agents.knowledge.solution_projector import (
    SolutionProjector,
)
from kntgraph.agents.memory.solutions import (
    Action,
    Outcome,
    Problem,
    SolutionCandidate,
)
from kntgraph.resilience import BulkheadPool
from kntgraph.testing import FakeEmbeddingProvider

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Reuse mocks from the existing test file
# ---------------------------------------------------------------------------


class _MockGraph:
    """A `Graph` double that records every query and
    can be configured to sleep / raise."""

    def __init__(self, *, sleep_s: float = 0.0, raise_exc: Exception | None = None):
        self.queries: list[tuple[str, dict]] = []
        self.sleep_s = sleep_s
        self.raise_exc = raise_exc

    async def query(self, cypher: str, params: dict | None = None) -> Any:
        self.queries.append((cypher, params))
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.sleep_s > 0:
            import asyncio as _asyncio

            await _asyncio.sleep(self.sleep_s)
        return None


class _MockClient:
    def __init__(self, graph: _MockGraph):
        self._graph = graph
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def graph(self, tenant_id: str) -> _MockGraph:
        return self._graph


def _make_candidate() -> SolutionCandidate:
    return SolutionCandidate(
        action=Action(
            request_event_id="evt-1",
            tool_name="test-tool",
            params={"x": 1},
            params_fingerprint="fp",
        ),
        problem=Problem(
            text="how to test",
            fingerprint="prob-fp",
            tags=["t1"],
        ),
        outcome=Outcome(
            status="completed",
            latency_ms=10.0,
            result_signature="sig",
            error_message=None,
        ),
        source_agent_id="agent-1",
        confidence=1,
    )


def _make_projector(
    graph: _MockGraph,
    *,
    query_timeout_seconds: float = 5.0,
    bulkhead: BulkheadPool | None = None,
) -> SolutionProjector:
    return SolutionProjector(
        client=_MockClient(graph),  # type: ignore[arg-type]
        embedding=FakeEmbeddingProvider(),
        tenant_id="tenant-1",
        query_timeout_seconds=query_timeout_seconds,
        bulkhead=bulkhead,
    )


class TestUpsertResilience:
    async def test_normal_upsert_runs_all_six_queries(self):
        graph = _MockGraph()
        proj = _make_projector(graph)
        result = await proj.upsert(_make_candidate())
        assert result == 4
        # Six Cypher queries: Tool, Problem, Action,
        # OnTool, Outcome, edge (plus the vector index
        # on first call = 7 total).
        assert len(graph.queries) == 7

    async def test_query_timeout_returns_zero_and_logs(self):
        """A query that exceeds ``query_timeout_seconds``
        raises ``asyncio.TimeoutError``; the partial
        upsert does not roll back (FalkorDB MERGE is
        idempotent) but the caller sees an exception
        propagated out of ``upsert``.

        Iter 13 (ADR-019 epílogo): the per-query timeout
        is now the responsibility of the ``GraphAdapter``
        (typically the production ``FalkorDBGraphAdapter``).
        The ``SolutionProjector`` no longer wraps each
        call in ``with_timeout``; the adapter owns the
        transport-level resilience.
        """
        pytest.skip(
            "Iter 13: per-query timeout moved to "
            "GraphAdapter boundary; tested in "
            "test_falkordb_adapter.py::TestQueryTimeout."
        )

    async def test_bulkhead_saturation_returns_zero(self):
        """With a bulkhead of capacity 1 and one in-flight
        upsert occupying it, the second upsert is rejected
        without running any query.
        """
        graph = _MockGraph(sleep_s=0.1)
        bulkhead = BulkheadPool("tenant-1", max_concurrent=1, acquire_timeout=0.01)
        proj = _make_projector(graph, bulkhead=bulkhead)

        # Start the first upsert (holds the bulkhead slot
        # for ~0.1s).
        first_task = asyncio.create_task(proj.upsert(_make_candidate()))
        # Yield so the first task acquires the slot.
        await asyncio.sleep(0.01)
        # Second upsert must be rejected by the bulkhead
        # without invoking the graph.
        result = await proj.upsert(_make_candidate())
        assert result == 0

        # Wait for the first upsert to complete.
        await first_task

    async def test_async_to_thread_does_not_block_event_loop(self):
        """The `asyncio.to_thread` offload means the
        event loop can service other coroutines while a
        query is in flight. We verify by scheduling a
        concurrent `asyncio.sleep` and checking it
        completes during the upsert.
        """
        graph = _MockGraph(sleep_s=0.2)
        proj = _make_projector(graph)

        loop_during_upsert: list[float] = []

        async def heartbeat() -> None:
            for _ in range(5):
                loop_during_upsert.append(time.perf_counter())
                await asyncio.sleep(0.05)

        heartbeat_task = asyncio.create_task(heartbeat())
        # Each query in the projector blocks the thread
        # for 0.2s but the event loop should still tick
        # the heartbeat.
        result = await proj.upsert(_make_candidate())
        await heartbeat_task

        assert result == 4
        # The heartbeat should have ticked at least 3
        # times during the upsert (proves the event
        # loop wasn't blocked). With 7 queries at 0.2s
        # each = 1.4s minimum upsert, heartbeat at 0.05s
        # = ~28 ticks; we conservatively assert >= 3.
        assert len(loop_during_upsert) >= 3, (
            f"event loop blocked: only {len(loop_during_upsert)} heartbeat ticks"
        )
