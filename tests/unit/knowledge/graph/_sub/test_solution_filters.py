# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``GraphSolutionAdapter.find_solutions_by_problem``
with optional filters (tags, tool_name, status).

Iter 16 (ADR-019 epílogo + Iter 16 do sharding): the
optional filters were dropped during the Iter 14 migration
(deferred). This test file exercises the rebuilt filter
support.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kntgraph.knowledge.graph._protocol import GraphQueryResult
from kntgraph.knowledge.graph._sub._solution import GraphSolutionAdapter


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
# find_solutions_by_problem — basic filters
# ---------------------------------------------------------------------------


class TestFindByProblemNoFilters:
    @pytest.mark.asyncio
    async def test_no_filters_uses_solved_by_default(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.find_solutions_by_problem(query_embedding=[0.0] * 768, k=5)
        cypher, _ = g.calls[0]
        # Default status="completed" → SOLVED_BY.
        assert "SOLVED_BY" in cypher
        assert "FAILED_WITH" not in cypher


class TestFindByProblemToolName:
    @pytest.mark.asyncio
    async def test_tool_name_adds_where_clause(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.find_solutions_by_problem(
            query_embedding=[0.0] * 768, k=5, tool_name="invoice.issue"
        )
        cypher, params = g.calls[0]
        assert "t.name = $tool_name" in cypher
        assert params["tool_name"] == "invoice.issue"

    @pytest.mark.asyncio
    async def test_no_tool_name_omits_where(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.find_solutions_by_problem(query_embedding=[0.0] * 768, k=5)
        cypher, params = g.calls[0]
        assert "t.name = $tool_name" not in cypher
        assert "tool_name" not in params


class TestFindByProblemStatus:
    @pytest.mark.asyncio
    async def test_status_completed_default(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.find_solutions_by_problem(
            query_embedding=[0.0] * 768, k=5, status="completed"
        )
        cypher, params = g.calls[0]
        assert "SOLVED_BY" in cypher
        assert "FAILED_WITH" not in cypher
        assert "o.status = $status" in cypher
        assert params["status"] == "completed"

    @pytest.mark.asyncio
    async def test_status_failed(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.find_solutions_by_problem(
            query_embedding=[0.0] * 768, k=5, status="failed"
        )
        cypher, params = g.calls[0]
        assert "FAILED_WITH" in cypher
        assert "SOLVED_BY" not in cypher
        assert "o.status = $status" in cypher
        assert params["status"] == "failed"

    @pytest.mark.asyncio
    async def test_status_all(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.find_solutions_by_problem(
            query_embedding=[0.0] * 768, k=5, status="all"
        )
        cypher, params = g.calls[0]
        # Either edge type matches.
        assert "SOLVED_BY|FAILED_WITH" in cypher
        # No status filter in WHERE.
        assert "o.status = $status" not in cypher
        assert "status" not in params


class TestFindByProblemTags:
    @pytest.mark.asyncio
    async def test_tags_inline_json_in_where(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.find_solutions_by_problem(
            query_embedding=[0.0] * 768,
            k=5,
            tags={"cnpj": "12.345.678/0001-90", "uf": "SP"},
        )
        cypher, params = g.calls[0]
        assert "tags_json CONTAINS" in cypher
        # The tags are inlined as a JSON-escaped string,
        # NOT passed via params (FalkorDB CONTAINS does not
        # support param substitution in the pattern).
        assert "cnpj" not in params
        assert "12.345.678" in cypher
        assert "uf" in cypher

    @pytest.mark.asyncio
    async def test_no_tags_omits_conta_ins(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.find_solutions_by_problem(query_embedding=[0.0] * 768, k=5)
        cypher, _ = g.calls[0]
        assert "tags_json CONTAINS" not in cypher


# ---------------------------------------------------------------------------
# find_solutions_by_tool — filters
# ---------------------------------------------------------------------------


class TestFindByToolTags:
    @pytest.mark.asyncio
    async def test_tags_inline_json_in_where(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.find_solutions_by_tool(
            tool_name="invoice.issue",
            k=5,
            tags={"cnpj": "12.345.678/0001-90"},
        )
        cypher, params = g.calls[0]
        assert "tags_json CONTAINS" in cypher
        assert "cnpj" not in params
        assert "12.345.678" in cypher

    @pytest.mark.asyncio
    async def test_no_tags_omits_conta_ins(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.find_solutions_by_tool(tool_name="invoice.issue", k=5)
        cypher, _ = g.calls[0]
        assert "tags_json CONTAINS" not in cypher


class TestFindByToolStatus:
    @pytest.mark.asyncio
    async def test_status_completed_default(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.find_solutions_by_tool(tool_name="x", k=5)
        cypher, params = g.calls[0]
        assert "SOLVED_BY" in cypher
        assert "FAILED_WITH" not in cypher

    @pytest.mark.asyncio
    async def test_status_failed(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.find_solutions_by_tool(tool_name="x", k=5, status="failed")
        cypher, params = g.calls[0]
        assert "FAILED_WITH" in cypher
        assert "SOLVED_BY" not in cypher
        assert params["status"] == "failed"

    @pytest.mark.asyncio
    async def test_status_all(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.find_solutions_by_tool(tool_name="x", k=5, status="all")
        cypher, params = g.calls[0]
        assert "SOLVED_BY|FAILED_WITH" in cypher
        assert "status" not in params
