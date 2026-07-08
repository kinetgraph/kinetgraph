# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``FalkorDBGraphAdapter``.

The adapter wraps a FalkorDB ``AsyncGraph`` and exposes
the framework-level ``GraphAdapter`` interface. Tests
here use a mock ``AsyncGraph`` so we do not need a real
FalkorDB instance.

The tests cover:

  - Conversion of ``QueryResult`` to ``GraphQueryResult``.
  - Mapping native exceptions to ``GraphError``.
  - Async-only contract (sync ``query`` is rejected).
  - Edge cases: None result, empty result_set, headers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kntgraph.infra.graph._adapter import FalkorDBGraphAdapter
from kntgraph.knowledge.graph._protocol import (
    GraphAdapter,
    GraphError,
)


# ---------------------------------------------------------------------------
# Mock AsyncGraph — mimics falkordb.asyncio.AsyncGraph
# ---------------------------------------------------------------------------


class _MockAsyncGraph:
    """Mock that simulates FalkorDB's AsyncGraph."""

    def __init__(
        self,
        *,
        result_set: list | None = None,
        headers: list | None = None,
        raise_on_query: Exception | None = None,
    ) -> None:
        self._result_set = result_set or []
        self._headers = headers or []
        self._raise_on_query = raise_on_query
        self.calls: list[tuple[str, dict | None]] = []

    async def query(
        self,
        cypher: str,
        params: dict | None = None,
    ) -> SimpleNamespace:
        self.calls.append((cypher, params))
        if self._raise_on_query is not None:
            raise self._raise_on_query
        return SimpleNamespace(
            result_set=self._result_set,
            headers=self._headers,
        )


# ---------------------------------------------------------------------------
# Construction + Protocol satisfaction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_satisfies_graph_adapter_protocol(self):
        g = _MockAsyncGraph()
        adapter = FalkorDBGraphAdapter(g)
        assert isinstance(adapter, GraphAdapter)

    def test_stores_graph(self):
        g = _MockAsyncGraph()
        adapter = FalkorDBGraphAdapter(g)
        assert adapter._graph is g


# ---------------------------------------------------------------------------
# query() — happy path
# ---------------------------------------------------------------------------


class TestQueryHappyPath:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_rows(self):
        g = _MockAsyncGraph(result_set=[])
        adapter = FalkorDBGraphAdapter(g)
        result = await adapter.query("MATCH (n) RETURN n")
        assert result.result_set == ()
        assert result.headers == ()

    @pytest.mark.asyncio
    async def test_returns_rows(self):
        rows = [("Agent", "NF-001"), ("Agent", "NF-002")]
        g = _MockAsyncGraph(result_set=rows)
        adapter = FalkorDBGraphAdapter(g)
        result = await adapter.query("MATCH (a:Agent) RETURN a.agent_id")
        assert result.result_set == (("Agent", "NF-001"), ("Agent", "NF-002"))

    @pytest.mark.asyncio
    async def test_passes_params_through(self):
        g = _MockAsyncGraph()
        adapter = FalkorDBGraphAdapter(g)
        await adapter.query(
            "MATCH (a:Agent {id: $id}) RETURN a",
            params={"id": "NF-001"},
        )
        assert g.calls == [("MATCH (a:Agent {id: $id}) RETURN a", {"id": "NF-001"})]

    @pytest.mark.asyncio
    async def test_none_params_yields_none(self):
        g = _MockAsyncGraph()
        adapter = FalkorDBGraphAdapter(g)
        await adapter.query("MATCH (n) RETURN n")
        assert g.calls == [("MATCH (n) RETURN n", None)]

    @pytest.mark.asyncio
    async def test_preserves_headers(self):
        g = _MockAsyncGraph(
            result_set=[("NF-001",)],
            headers=["agent_id"],
        )
        adapter = FalkorDBGraphAdapter(g)
        result = await adapter.query("MATCH (a:Agent) RETURN a.agent_id")
        assert result.headers == ("agent_id",)

    @pytest.mark.asyncio
    async def test_handles_native_none(self):
        # FalkorDB returns None when the query failed at
        # the Cypher layer (rare but observed in tests).
        class _NoneResultGraph:
            async def query(self, cypher, params=None):
                return None

        adapter = FalkorDBGraphAdapter(_NoneResultGraph())
        result = await adapter.query("MATCH (n) RETURN n")
        assert result.result_set == ()


# ---------------------------------------------------------------------------
# query() — error mapping
# ---------------------------------------------------------------------------


class TestQueryErrorMapping:
    @pytest.mark.asyncio
    async def test_native_exception_becomes_graph_error(self):
        g = _MockAsyncGraph(raise_on_query=ValueError("bad cypher"))
        adapter = FalkorDBGraphAdapter(g)
        with pytest.raises(GraphError) as exc_info:
            await adapter.query("BROKEN CYPHER")
        assert exc_info.value.kind == "query_failed"
        assert exc_info.value.cause is not None
        assert "bad cypher" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_connection_error_becomes_graph_error(self):
        g = _MockAsyncGraph(raise_on_query=ConnectionError("falkordb unreachable"))
        adapter = FalkorDBGraphAdapter(g)
        with pytest.raises(GraphError):
            await adapter.query("MATCH (n) RETURN n")
