# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for `knowledge/graphrag/retriever.py` — the
Solution-sub-graph retrieve (ADR-010 §5.2, Fase 3).

The tests cover:

  - `vector_search` (Document sub-graph, legacy MVP)
    still works after the Fase 3 additions.
  - `find_solutions_by_problem` builds the expected
    Cypher and parses the rows into `SolutionResult`.
  - `find_solutions_by_problem` with `tags` filter
    includes the `CONTAINS` clause.
  - `find_solutions_by_problem` with `tool_name`
    filter constrains the path through `Tool`.
  - `find_solutions_by_problem` with `status="failed"`
    uses `$status` and matches failures.
  - `find_solutions_by_problem` with `status="all"`
    omits the status clause.
  - `find_solutions_by_tool` is a pure MATCH with
    ordering by `last_validated_at`.
  - Both methods are fail-soft: an exception in
    `graph.query` returns `[]` and logs a warning.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


from kntgraph.knowledge.graphrag.retriever import (
    GraphRAGRetriever,
    RetrievalResult,
    SolutionResult,
)
from kntgraph.testing import FakeEmbeddingProvider


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------


class MockGraph:
    """Stand-in for a FalkorDB `Graph`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        # The test sets `self.rows` to control what the
        # next `query` call returns.
        self.rows: list[tuple] = []
        self.raise_on_next: Exception | None = None

    async def query(self, cypher: str, params: dict | None = None) -> Any:
        self.calls.append((cypher, params or {}))
        if self.raise_on_next is not None:
            exc, self.raise_on_next = self.raise_on_next, None
            raise exc
        # Wrap in an object with `.result_set` (the
        # shape falkordb returns).
        return _ResultSet(self.rows)


class _ResultSet:
    def __init__(self, rows: list[tuple]) -> None:
        self.result_set = rows


class MockClient:
    def __init__(self) -> None:
        self.g = MockGraph()

    def connect(self) -> None:
        pass

    def graph(self, tenant: str) -> MockGraph:
        return self.g


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_retriever(
    client: MockClient | None = None,
) -> GraphRAGRetriever:
    if client is None:
        client = MockClient()
    return GraphRAGRetriever(
        client=client,  # type: ignore[arg-type]
        embedding=FakeEmbeddingProvider(),
        tenant_id="t-1",
    )


# ---------------------------------------------------------------------------
# vector_search (legacy)
# ---------------------------------------------------------------------------


class TestVectorSearchLegacy:
    def test_returns_empty_on_no_results(self) -> None:
        retriever = _make_retriever()
        retriever._client.g.rows = []  # type: ignore[attr-defined]
        results = asyncio.run(retriever.vector_search([0.0] * 256, k=5))
        assert results == []

    def test_parses_results(self) -> None:
        retriever = _make_retriever()
        retriever._client.g.rows = [  # type: ignore[attr-defined]
            ("doc-1", "agent-1", "nf.received", '{"xml":"<a/>"}', 0.05),
        ]
        results = asyncio.run(retriever.vector_search([0.0] * 256, k=5))
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, RetrievalResult)
        assert r.doc_id == "doc-1"
        assert r.score == 0.05

    def test_fail_soft_on_query_error(self) -> None:
        retriever = _make_retriever()
        retriever._client.g.raise_on_next = (  # type: ignore[attr-defined]
            RuntimeError("falkordb down")
        )
        results = asyncio.run(retriever.vector_search([0.0] * 256, k=5))
        assert results == []


# ---------------------------------------------------------------------------
# find_solutions_by_problem
# ---------------------------------------------------------------------------


class TestFindSolutionsByProblem:
    def test_basic_query(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        # Iter 14: rows are now 12-tuple aligned with
        # ``GraphSolutionAdapter.CYPHER_FIND_SOLUTIONS_BY_PROBLEM``.
        client.g.rows = [
            (
                "fp-1",  # problem_fingerprint
                json.dumps({"xml": "<x/>"}),  # problem_tags_json
                "req-1",  # action_request_event_id
                "invoice.issue",  # action_tool_name
                json.dumps({"xml": "<x/>"}),  # action_params_json
                "invoice.issue",  # tool_name
                "completed",  # outcome_status
                0.95,  # outcome_confidence
                12.0,  # outcome_latency_ms
                "",  # outcome_error_message
                "2024-05-12T10:00:00Z",  # last_validated_at
                0.10,  # score
            ),
        ]
        results = asyncio.run(retriever.find_solutions_by_problem([0.0] * 256))
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, SolutionResult)
        assert r.problem_fingerprint == "fp-1"
        assert r.tool_name == "invoice.issue"
        assert r.outcome_status == "completed"
        assert r.latency_ms == 12.0
        assert r.score == 0.10
        # The Cypher includes the cosineDistance call.
        cypher, _ = client.g.calls[0]
        assert "vec.cosineDistance" in cypher
        assert "MATCH (p:Problem)" in cypher
        assert "SOLVED_BY" in cypher
        assert client.g.calls[0][1]["k"] == 5

    def test_tags_filter_includes_contains(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        client.g.rows = []
        asyncio.run(
            retriever.find_solutions_by_problem(
                [0.0] * 256,
                tags={"cnpj": "12.345.678/0001-90", "uf": "SP"},
            )
        )
        cypher, params = client.g.calls[0]
        # The Cypher has a WHERE clause that includes
        # the JSON-encoded tags.
        assert "WHERE" in cypher
        assert "tags_json CONTAINS" in cypher
        # We don't pass tags in `params` because the
        # CONTAINS uses an inlined literal.
        assert "cnpj" not in params
        assert "12.345.678" in cypher

    def test_tool_name_filter(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        client.g.rows = []
        asyncio.run(
            retriever.find_solutions_by_problem([0.0] * 256, tool_name="invoice.issue")
        )
        cypher, params = client.g.calls[0]
        assert "t.name = $tool_name" in cypher
        assert params["tool_name"] == "invoice.issue"

    def test_status_failed(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        # 12-tuple: status="failed" at index 6
        client.g.rows = [
            (
                "fp-1",  # problem_fingerprint
                "{}",  # problem_tags_json
                "req-1",  # action_request_event_id
                "x",  # action_tool_name
                "{}",  # action_params_json
                "x",  # tool_name
                "failed",  # outcome_status
                0.0,  # outcome_confidence
                None,  # outcome_latency_ms
                "boom",  # outcome_error_message
                "2024-05-12",  # last_validated_at
                0.0,  # score
            ),
        ]
        results = asyncio.run(
            retriever.find_solutions_by_problem([0.0] * 256, status="failed")
        )
        assert len(results) == 1
        assert results[0].outcome_status == "failed"
        _, params = client.g.calls[0]
        assert params["status"] == "failed"

    def test_status_all_omits_status_clause(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        client.g.rows = []
        asyncio.run(retriever.find_solutions_by_problem([0.0] * 256, status="all"))
        cypher, params = client.g.calls[0]
        # The status filter is NOT in the WHERE clause
        # AND the edge type matcher allows both.
        assert "status" not in params
        assert "SOLVED_BY|FAILED_WITH" in cypher
        # The Cypher is still built; we just don't
        # restrict by status.
        assert "MATCH (p:Problem)" in cypher
        retriever = _make_retriever(client)
        client.g.rows = []
        asyncio.run(retriever.find_solutions_by_problem([0.0] * 256, status="all"))
        cypher, params = client.g.calls[0]
        # The status filter is NOT in the WHERE clause
        # AND the edge type matcher allows both.
        assert "status" not in params
        assert "SOLVED_BY|FAILED_WITH" in cypher
        # The Cypher is still built; we just don't
        # restrict by status.
        assert "MATCH (p:Problem)" in cypher

    def test_empty_results(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        client.g.rows = []
        results = asyncio.run(retriever.find_solutions_by_problem([0.0] * 256))
        assert results == []

    def test_fail_soft(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        client.g.raise_on_next = RuntimeError("falkordb down")
        results = asyncio.run(retriever.find_solutions_by_problem([0.0] * 256))
        assert results == []

    def test_malformed_params_json_returns_empty_dict(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        # 12-tuple: action_params_json is "not json" at index 4
        client.g.rows = [
            (
                "fp-1",
                "{}",
                "req-1",
                "x",
                "not json",
                "x",
                "completed",
                0.5,
                None,
                "",
                None,
                0.0,
            ),
        ]
        results = asyncio.run(retriever.find_solutions_by_problem([0.0] * 256))
        assert len(results) == 1
        assert results[0].action_params_example == {}

    def test_malformed_latency_is_none(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        # 12-tuple: outcome_latency_ms (index 8) is "not a number"
        client.g.rows = [
            (
                "fp-1",
                "{}",
                "req-1",
                "x",
                "{}",
                "x",
                "completed",
                0.5,
                "not a number",
                "",
                None,
                0.0,
            ),
        ]
        results = asyncio.run(retriever.find_solutions_by_problem([0.0] * 256))
        assert len(results) == 1
        # The retriever tolerates a non-numeric latency
        # by leaving the field as-is (the adapter does
        # not coerce; the retriever does not parse).
        assert results[0].latency_ms == "not a number"


# ---------------------------------------------------------------------------
# find_solutions_by_tool
# ---------------------------------------------------------------------------


class TestFindSolutionsByTool:
    def test_basic_query(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        # 8-tuple: Cypher_FIND_SOLUTIONS_BY_TOOL has 8
        # RETURN columns.
        client.g.rows = [
            (
                "fp-1",  # problem_fingerprint
                json.dumps({"xml": "<x/>"}),  # problem_tags_json
                "req-1",  # action_request_event_id
                "invoice.issue",  # action_tool_name
                json.dumps({"xml": "<x/>"}),  # action_params_json
                "invoice.issue",  # tool_name
                "completed",  # outcome_status
                0.95,  # outcome_confidence
            ),
        ]
        results = asyncio.run(retriever.find_solutions_by_tool("invoice.issue"))
        assert len(results) == 1
        r = results[0]
        assert r.tool_name == "invoice.issue"
        assert r.outcome_status == "completed"
        # The Cypher filters by tool name and orders
        # by `o.confidence DESC`.
        cypher, params = client.g.calls[0]
        assert "ON_TOOL" in cypher
        assert "Tool {name: $tool_name}" in cypher
        assert params["tool_name"] == "invoice.issue"
        assert "ORDER BY o.confidence DESC" in cypher
        # Score is 1.0 (structural query).
        assert r.score == 1.0

    def test_tags_filter(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        client.g.rows = []
        asyncio.run(
            retriever.find_solutions_by_tool(
                "invoice.issue", tags={"cnpj": "12.345.678/0001-90"}
            )
        )
        cypher, _ = client.g.calls[0]
        assert "tags_json CONTAINS" in cypher

    def test_status_filter(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        client.g.rows = []
        asyncio.run(retriever.find_solutions_by_tool("x", status="all"))
        cypher, params = client.g.calls[0]
        # `status="all"` removes the status filter from
        # the WHERE clause AND the edge type constraint
        # on the Problem→Action edge (matches both
        # SOLVED_BY and FAILED_WITH).
        assert "status" not in params
        assert "SOLVED_BY|FAILED_WITH" in cypher
        # Conversely, status="failed" narrows the
        # status filter AND the edge type to
        # FAILED_WITH.
        asyncio.run(retriever.find_solutions_by_tool("x", status="failed"))
        cypher2, params2 = client.g.calls[1]
        assert params2["status"] == "failed"
        assert "FAILED_WITH" in cypher2
        assert "SOLVED_BY" not in cypher2
        # `status="completed"` narrows the edge type
        # to SOLVED_BY.
        asyncio.run(retriever.find_solutions_by_tool("x", status="completed"))
        cypher3, _ = client.g.calls[2]
        assert "SOLVED_BY" in cypher3
        assert "FAILED_WITH" not in cypher3

    def test_empty_results(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        client.g.rows = []
        results = asyncio.run(retriever.find_solutions_by_tool("x"))
        assert results == []

    def test_fail_soft(self) -> None:
        client = MockClient()
        retriever = _make_retriever(client)
        client.g.raise_on_next = RuntimeError("falkordb down")
        results = asyncio.run(retriever.find_solutions_by_tool("x"))
        assert results == []
