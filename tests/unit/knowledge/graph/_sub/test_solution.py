# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``GraphSolutionAdapter``.

The adapter owns the Cypher templates for the Solution
sub-graph:

  - ``(:Tool {name})`` — static class of tool
  - ``(:Problem {fingerprint})`` — canonical hash
  - ``(:Action {request_event_id})`` — re-executable
  - ``(:Outcome)`` — terminal result
  - ``(:Problem)-[:SOLVED_BY]->(:Action)``
  - ``(:Action)-[:ON_TOOL]->(:Tool)``
  - ``(:Action)-[:PRODUCED]->(:Outcome)``

The adapter is the most complex of the Graph*Adapter
shards: 4 node types + 3 edges, used by both the
projector (write path) and the retriever (read path).

Tests use a mock ``GraphAdapter`` to verify both the
Cypher templates and the parameter mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kntgraph.knowledge.graph._protocol import GraphError, GraphQueryResult
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
# upsert_tool
# ---------------------------------------------------------------------------


class TestUpsertTool:
    @pytest.mark.asyncio
    async def test_emits_merge_tool_cypher(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.upsert_tool(
            name="invoice.issue",
            description="Issue a fiscal invoice",
            input_schema_json='{"type": "object"}',
        )
        assert len(g.calls) == 1
        cypher, params = g.calls[0]
        assert "MERGE (t:Tool" in cypher
        assert "{name: $name}" in cypher
        assert "input_schema_json = $input_schema_json" in cypher
        assert params == {
            "name": "invoice.issue",
            "description": "Issue a fiscal invoice",
            "input_schema_json": '{"type": "object"}',
        }


# ---------------------------------------------------------------------------
# upsert_problem
# ---------------------------------------------------------------------------


class TestUpsertProblem:
    @pytest.mark.asyncio
    async def test_emits_merge_problem_cypher(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.upsert_problem(
            fingerprint="abc-123",
            embedding=[0.1, 0.2, 0.3],
            tags_json='["t1", "t2"]',
            last_validated_at="2026-06-28T12:00:00",
        )
        assert len(g.calls) == 1
        cypher, params = g.calls[0]
        assert "MERGE (p:Problem" in cypher
        assert "{fingerprint: $fingerprint}" in cypher
        assert "vecf32($embedding)" in cypher
        assert params == {
            "fingerprint": "abc-123",
            "embedding": [0.1, 0.2, 0.3],
            "tags_json": '["t1", "t2"]',
            "last_validated_at": "2026-06-28T12:00:00",
        }


# ---------------------------------------------------------------------------
# upsert_action
# ---------------------------------------------------------------------------


class TestUpsertAction:
    @pytest.mark.asyncio
    async def test_emits_merge_action_cypher(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.upsert_action(
            request_event_id="req-1",
            tool_name="invoice.issue",
            params_fingerprint="fp-1",
            params_json='{"k": "v"}',
        )
        assert len(g.calls) == 1
        cypher, params = g.calls[0]
        assert "MERGE (a:Action" in cypher
        assert "{request_event_id: $request_event_id}" in cypher
        assert "params_json = $params_json" in cypher
        assert params == {
            "request_event_id": "req-1",
            "tool_name": "invoice.issue",
            "params_fingerprint": "fp-1",
            "params_json": '{"k": "v"}',
        }


# ---------------------------------------------------------------------------
# create_outcome (overwrite)
# ---------------------------------------------------------------------------


class TestCreateOutcome:
    @pytest.mark.asyncio
    async def test_creates_outcome_and_edge(self):
        """Outcome is unique per Action: the cypher
        detaches any prior ``[:PRODUCED]`` edge and
        creates a fresh ``(:Outcome)`` node.
        """
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.create_outcome(
            request_event_id="req-1",
            status="completed",
            confidence=0.95,
            result_json='{"ok": true}',
            error_message="",
        )
        assert len(g.calls) == 1
        cypher, params = g.calls[0]
        assert "MATCH (a:Action" in cypher
        assert "DETACH DELETE old" in cypher or "DELETE old" in cypher
        assert "CREATE (a)-[:PRODUCED]->(o:Outcome)" in cypher
        assert params == {
            "request_event_id": "req-1",
            "status": "completed",
            "confidence": 0.95,
            "result_json": '{"ok": true}',
            "error_message": "",
        }


# ---------------------------------------------------------------------------
# link_problem_to_action
# ---------------------------------------------------------------------------


class TestLinkProblemToAction:
    @pytest.mark.asyncio
    async def test_emits_solved_by_edge(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.link_problem_to_action(
            problem_fingerprint="abc-123",
            action_request_event_id="req-1",
            confidence=1.0,
            validated_count=1,
            outcome_status="completed",
        )
        assert len(g.calls) == 1
        cypher, params = g.calls[0]
        assert "MATCH" in cypher
        assert ":SOLVED_BY" in cypher
        assert "FAILED_WITH" not in cypher
        assert params == {
            "problem_fingerprint": "abc-123",
            "action_request_event_id": "req-1",
            "confidence": 1.0,
            "validated_count": 1,
        }


# ---------------------------------------------------------------------------
# link_action_to_tool
# ---------------------------------------------------------------------------


class TestLinkActionToTool:
    @pytest.mark.asyncio
    async def test_emits_on_tool_edge(self):
        g = _MockGraphAdapter()
        adapter = GraphSolutionAdapter(g)
        await adapter.link_action_to_tool(
            action_request_event_id="req-1",
            tool_name="invoice.issue",
        )
        assert len(g.calls) == 1
        cypher, params = g.calls[0]
        assert "MATCH" in cypher
        assert "MERGE (a)-[:ON_TOOL]->(t)" in cypher
        assert params == {
            "action_request_event_id": "req-1",
            "tool_name": "invoice.issue",
        }


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    @pytest.mark.asyncio
    async def test_upsert_tool_propagates(self):
        g = _MockGraphAdapter(raise_on_query=GraphError("db down"))
        adapter = GraphSolutionAdapter(g)
        with pytest.raises(GraphError):
            await adapter.upsert_tool(name="x", description="y", input_schema_json="{}")

    @pytest.mark.asyncio
    async def test_create_outcome_propagates(self):
        g = _MockGraphAdapter(raise_on_query=GraphError("db down"))
        adapter = GraphSolutionAdapter(g)
        with pytest.raises(GraphError):
            await adapter.create_outcome(
                request_event_id="r-1",
                status="completed",
                confidence=1.0,
                result_json="{}",
                error_message="",
            )


# ---------------------------------------------------------------------------
# Distinct cyphers
# ---------------------------------------------------------------------------


class TestDistinctCypherConstants:
    """The Cypher constants MUST be distinct — a config
    bug that pointed two methods at the same cypher
    would silently fail."""

    def test_all_cyphers_distinct(self):
        cypher_constants = [
            attr for attr in dir(GraphSolutionAdapter) if attr.startswith("CYPHER_")
        ]
        cypher_values = [
            getattr(GraphSolutionAdapter, name) for name in cypher_constants
        ]
        assert len(set(cypher_values)) == len(cypher_values), (
            "Duplicate Cypher constant in GraphSolutionAdapter"
        )
