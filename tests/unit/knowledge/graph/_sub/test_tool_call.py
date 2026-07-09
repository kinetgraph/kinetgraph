# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``GraphToolCallAdapter``.

The adapter owns the Cypher template for the
``(:ToolCall)`` node + the ``(:Agent)-[:CALLED]->(:ToolCall)``
edge. The ToolCall id is the EventLog ``event_id``
(the unique identifier of the tool-call event).

Tests use a mock ``GraphAdapter`` to verify both the
Cypher templates and the parameter mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kntgraph.knowledge.graph._protocol import GraphError, GraphQueryResult
from kntgraph.knowledge.graph._sub._tool_call import GraphToolCallAdapter


@dataclass
class _MockGraphAdapter:
    calls: list[tuple[str, dict | None]] = field(default_factory=list)
    rows_by_cypher: dict[str, list] = field(default_factory=dict)
    raise_on_query: Exception | None = None

    async def query(
        self,
        cypher: str,
        *,
        params: dict | None = None,
    ) -> GraphQueryResult:
        self.calls.append((cypher, params))
        if self.raise_on_query is not None:
            raise self.raise_on_query
        rows = self.rows_by_cypher.get(cypher, [])
        return GraphQueryResult(result_set=tuple(rows))


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------


class TestUpsert:
    @pytest.mark.asyncio
    async def test_completed_event_carries_latency(self):
        g = _MockGraphAdapter()
        adapter = GraphToolCallAdapter(g)
        await adapter.upsert(
            tool_call_id="tc-1",
            tool="invoice.issue",
            request_id="req-1",
            status="completed",
            latency_ms=12.3,
            agent_id="NF-001",
        )
        assert len(g.calls) == 1
        cypher, params = g.calls[0]
        assert "MERGE (t:ToolCall" in cypher
        assert "{id: $id}" in cypher
        assert "latency_ms = $latency_ms" in cypher
        assert params == {
            "id": "tc-1",
            "tool": "invoice.issue",
            "request_id": "req-1",
            "status": "completed",
            "latency_ms": 12.3,
            "agent_id": "NF-001",
        }

    @pytest.mark.asyncio
    async def test_failed_event_omits_latency(self):
        g = _MockGraphAdapter()
        adapter = GraphToolCallAdapter(g)
        await adapter.upsert(
            tool_call_id="tc-1",
            tool="x",
            request_id="r-1",
            status="failed",
            latency_ms=None,
            agent_id="a-1",
        )
        _, params = g.calls[0]
        assert params["status"] == "failed"
        assert params["latency_ms"] is None

    @pytest.mark.asyncio
    async def test_returns_none(self):
        g = _MockGraphAdapter()
        adapter = GraphToolCallAdapter(g)
        result = await adapter.upsert(
            tool_call_id="tc-1",
            tool="x",
            request_id="r-1",
            status="completed",
            latency_ms=0.0,
            agent_id="a-1",
        )
        assert result is None


# ---------------------------------------------------------------------------
# link_to_agent
# ---------------------------------------------------------------------------


class TestLinkToAgent:
    @pytest.mark.asyncio
    async def test_emits_called_edge_cypher(self):
        g = _MockGraphAdapter()
        adapter = GraphToolCallAdapter(g)
        await adapter.link_to_agent(agent_id="NF-001", tool_call_id="tc-1")
        assert len(g.calls) == 1
        cypher, params = g.calls[0]
        assert "MATCH" in cypher
        assert "MERGE (a)-[:CALLED]->(t)" in cypher
        assert params == {
            "agent_id": "NF-001",
            "tool_id": "tc-1",
        }

    @pytest.mark.asyncio
    async def test_uses_distinct_cypher_from_upsert(self):
        g = _MockGraphAdapter()
        adapter = GraphToolCallAdapter(g)
        await adapter.link_to_agent(agent_id="a-1", tool_call_id="tc-1")
        cypher, _ = g.calls[0]
        assert cypher != GraphToolCallAdapter.CYPHER_UPSERT
        assert "MATCH" in cypher
        assert "CALLED" in cypher


# ---------------------------------------------------------------------------
# find_by_id
# ---------------------------------------------------------------------------


class TestFindById:
    @pytest.mark.asyncio
    async def test_returns_node_dict_when_present(self):
        g = _MockGraphAdapter(
            rows_by_cypher={
                GraphToolCallAdapter.CYPHER_FIND_BY_ID: [
                    (
                        "tc-1",
                        "invoice.issue",
                        "req-1",
                        "completed",
                        12.3,
                        "NF-001",
                    )
                ]
            }
        )
        adapter = GraphToolCallAdapter(g)
        result = await adapter.find_by_id("tc-1")
        assert result == {
            "id": "tc-1",
            "tool": "invoice.issue",
            "request_id": "req-1",
            "status": "completed",
            "latency_ms": 12.3,
            "agent_id": "NF-001",
        }

    @pytest.mark.asyncio
    async def test_returns_none_when_absent(self):
        g = _MockGraphAdapter(
            rows_by_cypher={GraphToolCallAdapter.CYPHER_FIND_BY_ID: []}
        )
        adapter = GraphToolCallAdapter(g)
        result = await adapter.find_by_id("missing")
        assert result is None


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    @pytest.mark.asyncio
    async def test_upsert_propagates(self):
        g = _MockGraphAdapter(raise_on_query=GraphError("db down"))
        adapter = GraphToolCallAdapter(g)
        with pytest.raises(GraphError):
            await adapter.upsert(
                tool_call_id="tc-1",
                tool="x",
                request_id="r-1",
                status="completed",
                latency_ms=0.0,
                agent_id="a-1",
            )

    @pytest.mark.asyncio
    async def test_link_propagates(self):
        g = _MockGraphAdapter(raise_on_query=GraphError("db down"))
        adapter = GraphToolCallAdapter(g)
        with pytest.raises(GraphError):
            await adapter.link_to_agent(agent_id="a-1", tool_call_id="tc-1")
