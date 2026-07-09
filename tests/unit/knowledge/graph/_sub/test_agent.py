# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``GraphAgentAdapter``.

The adapter owns the Cypher template for the
``(:Agent)`` node and exposes typed methods
(``upsert``, ``find_by_id``) that delegate to the
underlying ``GraphAdapter``. Tests use a mock
``GraphAdapter`` to verify both the Cypher template
and the parameter mapping.

Iter 11 (ADR-019 epílogo + Iter 11 do sharding) — the
adapter is the canonical owner of the ``Agent`` node
Cypher. ``FalkorDBProjector._merge_agent_node`` becomes
a one-liner that delegates here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kntgraph.knowledge.graph._sub._agent import GraphAgentAdapter
from kntgraph.knowledge.graph._protocol import GraphError, GraphQueryResult


# ---------------------------------------------------------------------------
# Mock GraphAdapter — records all queries + returns canned rows
# ---------------------------------------------------------------------------


@dataclass
class _MockGraphAdapter:
    """Mock that records ``query`` calls and returns
    rows pre-configured per cypher."""

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
    async def test_emits_merge_agent_cypher(self):
        g = _MockGraphAdapter()
        adapter = GraphAgentAdapter(g)
        await adapter.upsert(
            agent_id="NF-001",
            tenant_id="t-1",
            last_seen="2026-06-28T12:00:00",
        )
        assert len(g.calls) == 1
        cypher, params = g.calls[0]
        # The cypher must MERGE on agent_id (idempotent).
        assert "MERGE (a:Agent" in cypher
        assert "{agent_id: $agent_id}" in cypher
        # Params match the typed signature.
        assert params == {
            "agent_id": "NF-001",
            "tenant_id": "t-1",
            "last_seen": "2026-06-28T12:00:00",
        }

    @pytest.mark.asyncio
    async def test_last_seen_empty_string(self):
        g = _MockGraphAdapter()
        adapter = GraphAgentAdapter(g)
        await adapter.upsert(
            agent_id="a-1",
            tenant_id="t-1",
            last_seen="",
        )
        _, params = g.calls[0]
        assert params["last_seen"] == ""

    @pytest.mark.asyncio
    async def test_returns_none(self):
        g = _MockGraphAdapter()
        adapter = GraphAgentAdapter(g)
        result = await adapter.upsert(agent_id="a-1", tenant_id="t-1", last_seen="")
        assert result is None


# ---------------------------------------------------------------------------
# find_by_id
# ---------------------------------------------------------------------------


class TestFindById:
    @pytest.mark.asyncio
    async def test_returns_node_dict_when_present(self):
        g = _MockGraphAdapter(
            rows_by_cypher={
                GraphAgentAdapter.CYPHER_FIND_BY_ID: [
                    {
                        "agent_id": "NF-001",
                        "tenant_id": "t-1",
                        "last_seen": "2026-06-28T12:00:00",
                    }
                ]
            }
        )
        adapter = GraphAgentAdapter(g)
        result = await adapter.find_by_id("NF-001")
        assert result == {
            "agent_id": "NF-001",
            "tenant_id": "t-1",
            "last_seen": "2026-06-28T12:00:00",
        }

    @pytest.mark.asyncio
    async def test_returns_none_when_absent(self):
        g = _MockGraphAdapter(rows_by_cypher={GraphAgentAdapter.CYPHER_FIND_BY_ID: []})
        adapter = GraphAgentAdapter(g)
        result = await adapter.find_by_id("missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_query_carries_agent_id_param(self):
        g = _MockGraphAdapter()
        adapter = GraphAgentAdapter(g)
        await adapter.find_by_id("NF-001")
        cypher, params = g.calls[0]
        assert params == {"agent_id": "NF-001"}

    @pytest.mark.asyncio
    async def test_uses_distinct_cypher_from_upsert(self):
        """The find-by-id cypher MUST be different from
        the upsert cypher — otherwise a config bug
        could silently merge instead of read.
        """
        g = _MockGraphAdapter()
        adapter = GraphAgentAdapter(g)
        await adapter.find_by_id("a-1")
        cypher, _ = g.calls[0]
        assert cypher != GraphAgentAdapter.CYPHER_UPSERT
        assert "MATCH" in cypher
        assert "MERGE" not in cypher


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    @pytest.mark.asyncio
    async def test_upsert_propagates_graph_error(self):
        g = _MockGraphAdapter(
            raise_on_query=GraphError("db down", kind="connection_lost")
        )
        adapter = GraphAgentAdapter(g)
        with pytest.raises(GraphError) as exc:
            await adapter.upsert(agent_id="a-1", tenant_id="t-1", last_seen="")
        assert exc.value.kind == "connection_lost"

    @pytest.mark.asyncio
    async def test_find_by_id_propagates_graph_error(self):
        g = _MockGraphAdapter(raise_on_query=GraphError("db down"))
        adapter = GraphAgentAdapter(g)
        with pytest.raises(GraphError):
            await adapter.find_by_id("a-1")
