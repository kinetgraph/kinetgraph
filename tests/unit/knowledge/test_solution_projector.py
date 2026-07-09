# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for `knowledge/falkordb/solution_projector.py` —
the FalkorDB adapter for the Solution sub-graph
(ADR-010 §3, Fase 3).

The tests use a `MockGraph` and `MockClient` to
intercept `graph.query(...)` calls. They assert the
sequence of Cypher statements and the parameters
without requiring a real FalkorDB instance.

Coverage:

  - `ensure_tool_nodes` MERGE for every descriptor.
  - `upsert` issues the right Cypher sequence
    (Tool, Problem, Action, OnTool, Outcome, edge).
  - Failed outcome uses `[:FAILED_WITH]` instead of
    `[:SOLVED_BY]`.
  - Idempotent re-upsert (no duplicates).
  - Vector-index creation is best-effort and
    cached.
  - Validation: tenant_id must be non-empty.
"""

from __future__ import annotations

import asyncio

import pytest

from kntgraph.testing import FakeEmbeddingProvider

from kntgraph.agents.knowledge.solution_projector import (
    PROBLEM_VECTOR_INDEX_CYPHER,
    SolutionProjector,
)
from kntgraph.agents.memory.solutions import (
    Action,
    Outcome,
    Problem,
    SolutionCandidate,
    ToolDescriptor,
)


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------


class MockGraph:
    """Stand-in for a FalkorDB `Graph`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def query(self, cypher: str, params: dict | None = None) -> None:
        self.calls.append((cypher, params or {}))


class MockClient:
    """Stand-in for `GraphPool` (Iter 24)."""

    def __init__(self) -> None:
        self.g = MockGraph()
        self.connect_count = 0

    def connect(self) -> None:
        self.connect_count += 1

    def graph(self, tenant: str) -> MockGraph:
        return self.g


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candidate(
    *,
    problem_fp: str = "abc123",
    tool: str = "invoice.issue",
    request_event_id: str = "req-1",
    status: str = "completed",
    confidence: int = 1,
) -> SolutionCandidate:
    return SolutionCandidate(
        problem=Problem(
            fingerprint=problem_fp,
            tags={"cnpj": "12.345.678/0001-90"},
            text="NF-001 para CNPJ 12.345.678/0001-90",
        ),
        action=Action(
            request_event_id=request_event_id,
            tool_name=tool,
            params_fingerprint="p123",
            params={"xml": "<x/>", "document_id": "NF-001"},
        ),
        outcome=Outcome(
            status=status,
            latency_ms=12.0,
            result_signature="r456",
            error_message=("boom" if status == "failed" else None),
        ),
        source_agent_id="agent-1",
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# ensure_tool_nodes
# ---------------------------------------------------------------------------


class TestEnsureToolNodes:
    def test_writes_one_query_per_descriptor(self) -> None:
        client = MockClient()
        proj = SolutionProjector(
            client=client, embedding=FakeEmbeddingProvider(), tenant_id="t-1"
        )
        descs = [
            ToolDescriptor(
                name="invoice.issue",
                description="Issues an invoice via external service",
                input_schema_json='{"type":"object"}',
            ),
            ToolDescriptor(
                name="bank.transfer",
                description="Bank transfer",
                input_schema_json="{}",
            ),
        ]
        n = asyncio.run(proj.ensure_tool_nodes(descs))
        assert n == 2
        assert len(client.g.calls) == 2
        for cypher, params in client.g.calls:
            assert cypher.strip().startswith("MERGE (t:Tool")
            assert "name" in params
            assert "description" in params
            assert "input_schema_json" in params

    def test_empty_iterable(self) -> None:
        client = MockClient()
        proj = SolutionProjector(
            client=client, embedding=FakeEmbeddingProvider(), tenant_id="t-1"
        )
        n = asyncio.run(proj.ensure_tool_nodes([]))
        assert n == 0
        assert client.g.calls == []


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_completed_outcome_sequence(self) -> None:
        client = MockClient()
        proj = SolutionProjector(
            client=client, embedding=FakeEmbeddingProvider(), tenant_id="t-1"
        )
        cand = _make_candidate(status="completed")
        n = asyncio.run(proj.upsert(cand))
        # 4 nodes written (Tool, Problem, Action, Outcome).
        assert n == 4
        # Inspect the call sequence.
        cyphers = [c for c, _ in client.g.calls]
        # The vector index creation is the second call
        # (CREATE VECTOR INDEX) — but the index is
        # cached on the second upsert.
        assert any("CREATE VECTOR INDEX" in c for c in cyphers)
        # MERGE Tool, MERGE Problem, MERGE Action,
        # MERGE OnTool, MATCH Outcome (CREATE),
        # MATCH SOLVED_BY.
        assert any("MERGE (t:Tool" in c for c in cyphers)
        assert any("MERGE (p:Problem" in c for c in cyphers)
        assert any("MERGE (a:Action" in c for c in cyphers)
        assert any("MERGE (a)-[:ON_TOOL]->(t" in c for c in cyphers)
        assert any("CREATE (a)-[:PRODUCED]->(o" in c for c in cyphers)
        assert any("SOLVED_BY" in c for c in cyphers)

    def test_failed_outcome_uses_failed_with(self) -> None:
        client = MockClient()
        proj = SolutionProjector(
            client=client, embedding=FakeEmbeddingProvider(), tenant_id="t-1"
        )
        cand = _make_candidate(status="failed")
        asyncio.run(proj.upsert(cand))
        cyphers = [c for c, _ in client.g.calls]
        # FAILED_WITH replaces SOLVED_BY for failed
        # outcomes.
        assert any("FAILED_WITH" in c for c in cyphers)
        assert not any("SOLVED_BY" in c for c in cyphers)

    def test_idempotent_re_upsert(self) -> None:
        client = MockClient()
        proj = SolutionProjector(
            client=client, embedding=FakeEmbeddingProvider(), tenant_id="t-1"
        )
        cand = _make_candidate()
        n1 = asyncio.run(proj.upsert(cand))
        n2 = asyncio.run(proj.upsert(cand))
        # Both calls return 4 (MERGE writes are
        # idempotent: same query, same params,
        # same outcome).
        assert n1 == 4
        assert n2 == 4

    def test_vector_index_cached(self) -> None:
        client = MockClient()
        proj = SolutionProjector(
            client=client, embedding=FakeEmbeddingProvider(), tenant_id="t-1"
        )
        # First upsert creates the index.
        asyncio.run(proj.upsert(_make_candidate()))
        first_index_calls = sum(
            1 for c, _ in client.g.calls if "CREATE VECTOR INDEX" in c
        )
        # Second upsert re-uses the cached flag.
        asyncio.run(proj.upsert(_make_candidate()))
        second_index_calls = sum(
            1 for c, _ in client.g.calls if "CREATE VECTOR INDEX" in c
        )
        assert first_index_calls == 1
        assert second_index_calls == 1  # cached, not re-issued

    def test_problem_fingerprint_passed_through(self) -> None:
        client = MockClient()
        proj = SolutionProjector(
            client=client, embedding=FakeEmbeddingProvider(), tenant_id="t-1"
        )
        cand = _make_candidate(problem_fp="deadbeef")
        asyncio.run(proj.upsert(cand))
        # Find the Problem MERGE call.
        for cypher, params in client.g.calls:
            if "MERGE (p:Problem" in cypher:
                assert params["fingerprint"] == "deadbeef"
                assert params["tags_json"]  # JSON string
                assert "embedding" in params  # list[float]
                return
        pytest.fail("Problem MERGE not found in calls")

    def test_action_params_passed_through(self) -> None:
        client = MockClient()
        proj = SolutionProjector(
            client=client, embedding=FakeEmbeddingProvider(), tenant_id="t-1"
        )
        cand = _make_candidate()
        asyncio.run(proj.upsert(cand))
        for cypher, params in client.g.calls:
            if "MERGE (a:Action" in cypher:
                assert params["request_event_id"] == "req-1"
                assert params["tool_name"] == "invoice.issue"
                assert params["params_fingerprint"] == "p123"
                assert "params_json" in params
                return
        pytest.fail("Action MERGE not found in calls")

    def test_confidence_passed_to_edge(self) -> None:
        client = MockClient()
        proj = SolutionProjector(
            client=client, embedding=FakeEmbeddingProvider(), tenant_id="t-1"
        )
        cand = _make_candidate(confidence=5)
        asyncio.run(proj.upsert(cand))
        for cypher, params in client.g.calls:
            if "SOLVED_BY" in cypher or "FAILED_WITH" in cypher:
                assert params["confidence"] == 5
                return
        pytest.fail("Edge cypher not found")

    def test_outcome_status_passed_through(self) -> None:
        client = MockClient()
        proj = SolutionProjector(
            client=client, embedding=FakeEmbeddingProvider(), tenant_id="t-1"
        )
        cand = _make_candidate(status="failed")
        asyncio.run(proj.upsert(cand))
        for cypher, params in client.g.calls:
            if "CREATE (a)-[:PRODUCED]->(o" in cypher:
                assert params["status"] == "failed"
                assert params["error_message"] == "boom"
                return
        pytest.fail("Outcome CREATE not found")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_tenant_id_required(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            SolutionProjector(
                client=MockClient(),
                embedding=FakeEmbeddingProvider(),
                tenant_id="",
            )


# ---------------------------------------------------------------------------
# Vector index cypher constant
# ---------------------------------------------------------------------------


class TestCypherConstant:
    def test_problem_vector_index_cypher_shape(self) -> None:
        assert "CREATE VECTOR INDEX" in PROBLEM_VECTOR_INDEX_CYPHER
        assert "Problem" in PROBLEM_VECTOR_INDEX_CYPHER
        assert "embedding" in PROBLEM_VECTOR_INDEX_CYPHER
        assert "$dimension" in PROBLEM_VECTOR_INDEX_CYPHER
