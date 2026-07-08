# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``GraphDocumentAdapter``.

The adapter owns the Cypher template for the
``(:Document)`` node + the ``(:Agent)-[:HAS_DOC]->(:Document)``
edge. The Document id is ``"<agent_id>:<event_id>"`` so
two agents with the same event id do not collide.

Tests use a mock ``GraphAdapter`` to verify both the
Cypher templates and the parameter mapping. The shape
follows the ``GraphAgentAdapter`` pattern (Iter 11).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kntgraph.knowledge.graph._protocol import GraphError, GraphQueryResult
from kntgraph.knowledge.graph._sub._document import GraphDocumentAdapter


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
    async def test_emits_merge_document_cypher(self):
        g = _MockGraphAdapter()
        adapter = GraphDocumentAdapter(g)
        await adapter.upsert(
            doc_id="NF-001:abc-123",
            agent_id="NF-001",
            event_type="nf.received",
            data_json='{"k": "v"}',
            embedding=[0.1, 0.2, 0.3],
        )
        assert len(g.calls) == 1
        cypher, params = g.calls[0]
        assert "MERGE (d:Document" in cypher
        assert "{id: $id}" in cypher
        assert "vecf32($embedding)" in cypher
        assert params == {
            "id": "NF-001:abc-123",
            "agent_id": "NF-001",
            "event_type": "nf.received",
            "data_json": '{"k": "v"}',
            "embedding": [0.1, 0.2, 0.3],
        }

    @pytest.mark.asyncio
    async def test_returns_none(self):
        g = _MockGraphAdapter()
        adapter = GraphDocumentAdapter(g)
        result = await adapter.upsert(
            doc_id="a-1:e-1",
            agent_id="a-1",
            event_type="x",
            data_json="{}",
            embedding=[0.0],
        )
        assert result is None


# ---------------------------------------------------------------------------
# link_to_agent
# ---------------------------------------------------------------------------


class TestLinkToAgent:
    @pytest.mark.asyncio
    async def test_emits_has_doc_edge_cypher(self):
        g = _MockGraphAdapter()
        adapter = GraphDocumentAdapter(g)
        await adapter.link_to_agent(agent_id="NF-001", doc_id="NF-001:abc-123")
        assert len(g.calls) == 1
        cypher, params = g.calls[0]
        assert "MATCH (a:Agent" in cypher
        assert "MATCH (d:Document" in cypher or "MATCH" in cypher
        assert "MERGE (a)-[:HAS_DOC]->(d)" in cypher
        assert params == {
            "agent_id": "NF-001",
            "doc_id": "NF-001:abc-123",
        }

    @pytest.mark.asyncio
    async def test_uses_distinct_cypher_from_upsert(self):
        """The link-edge cypher MUST be different from
        the upsert cypher — a config bug could silently
        create a Document without the edge.
        """
        g = _MockGraphAdapter()
        adapter = GraphDocumentAdapter(g)
        await adapter.link_to_agent(agent_id="a-1", doc_id="a-1:e-1")
        cypher, _ = g.calls[0]
        assert cypher != GraphDocumentAdapter.CYPHER_UPSERT
        assert "MATCH" in cypher
        assert "MERGE" in cypher  # The edge is MERGEd
        assert "HAS_DOC" in cypher


# ---------------------------------------------------------------------------
# find_by_id
# ---------------------------------------------------------------------------


class TestFindById:
    @pytest.mark.asyncio
    async def test_returns_node_dict_when_present(self):
        g = _MockGraphAdapter(
            rows_by_cypher={
                GraphDocumentAdapter.CYPHER_FIND_BY_ID: [
                    (
                        "NF-001:abc-123",
                        "NF-001",
                        "nf.received",
                        '{"k": "v"}',
                    )
                ]
            }
        )
        adapter = GraphDocumentAdapter(g)
        result = await adapter.find_by_id("NF-001:abc-123")
        assert result == {
            "id": "NF-001:abc-123",
            "agent_id": "NF-001",
            "event_type": "nf.received",
            "data_json": '{"k": "v"}',
        }

    @pytest.mark.asyncio
    async def test_returns_none_when_absent(self):
        g = _MockGraphAdapter(
            rows_by_cypher={GraphDocumentAdapter.CYPHER_FIND_BY_ID: []}
        )
        adapter = GraphDocumentAdapter(g)
        result = await adapter.find_by_id("missing")
        assert result is None


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    @pytest.mark.asyncio
    async def test_upsert_propagates(self):
        g = _MockGraphAdapter(raise_on_query=GraphError("db down"))
        adapter = GraphDocumentAdapter(g)
        with pytest.raises(GraphError):
            await adapter.upsert(
                doc_id="a",
                agent_id="b",
                event_type="c",
                data_json="{}",
                embedding=[0.0],
            )

    @pytest.mark.asyncio
    async def test_link_propagates(self):
        g = _MockGraphAdapter(raise_on_query=GraphError("db down"))
        adapter = GraphDocumentAdapter(g)
        with pytest.raises(GraphError):
            await adapter.link_to_agent(agent_id="a", doc_id="b")
