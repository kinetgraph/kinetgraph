# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.graph._sub._solution._adapter -- ``GraphSolutionAdapter``
for the Solution sub-graph.

Owns the Cypher templates and parameter mapping for:

  - ``(:Tool {name})`` -- static class of tool
  - ``(:Problem {fingerprint})`` -- canonical hash
  - ``(:Action {request_event_id})`` -- re-executable
  - ``(:Outcome)`` -- terminal result
  - ``(:Problem)-[:SOLVED_BY]->(:Action)``
  - ``(:Action)-[:ON_TOOL]->(:Tool)``
  - ``(:Action)-[:PRODUCED]->(:Outcome)``

This is the most complex sub-adapter (4 node types + 3
edges). It is consumed by both the projector (write
path) and the retriever (read path).

The read-path query composition (templates + WHERE-clause
builders + row mappers) is split into
:mod:`kntgraph.knowledge.graph._sub._solution._read_filters`;
the module-level row helpers (typed extraction primitives)
live in
:mod:`kntgraph.knowledge.graph._sub._solution._row_helpers`.
This module exposes only the public class.

Iter 13 (ADR-019 epílogo + Iter 13 do sharding).
"""

from __future__ import annotations

from typing import Optional

from ..._protocol import GraphAdapter

from ._read_filters import (
    BASE_FIND_SOLUTIONS_BY_PROBLEM,
    BASE_FIND_SOLUTIONS_BY_TOOL,
    build_status_clause,
    build_tags_clause,
    build_tool_name_clause,
    edge_match_for_status,
    row_to_solution_by_problem,
    row_to_solution_by_tool,
)


class GraphSolutionAdapter:
    """
    Cypher + parameter adapter for the Solution sub-graph.
    """

    # --- cypher templates ---------------------------------------------------

    CYPHER_UPSERT_TOOL = """
    MERGE (t:Tool {name: $name})
    SET t.description = $description,
        t.input_schema_json = $input_schema_json
    """

    CYPHER_UPSERT_PROBLEM = """
    MERGE (p:Problem {fingerprint: $fingerprint})
    SET p.embedding = vecf32($embedding),
        p.tags_json = $tags_json,
        p.last_validated_at = $last_validated_at
    """

    CYPHER_UPSERT_ACTION = """
    MERGE (a:Action {request_event_id: $request_event_id})
    SET a.tool_name = $tool_name,
        a.params_fingerprint = $params_fingerprint,
        a.params_json = $params_json
    """

    CYPHER_CREATE_OUTCOME = """
    MATCH (a:Action {request_event_id: $request_event_id})
    OPTIONAL MATCH (a)-[old:PRODUCED]->(old_o:Outcome)
    DELETE old
    WITH a
    CREATE (a)-[:PRODUCED]->(o:Outcome)
    SET o.status = $status,
        o.confidence = $confidence,
        o.result_json = $result_json,
        o.error_message = $error_message
    """

    CYPHER_SOLVED_BY_EDGE = """
    MATCH (p:Problem {fingerprint: $problem_fingerprint}),
          (a:Action {request_event_id: $action_request_event_id})
    MERGE (p)-[r:SOLVED_BY]->(a)
    SET r.confidence = $confidence,
        r.validated_count = $validated_count
    """

    CYPHER_FAILED_WITH_EDGE = """
    MATCH (p:Problem {fingerprint: $problem_fingerprint}),
          (a:Action {request_event_id: $action_request_event_id})
    MERGE (p)-[r:FAILED_WITH]->(a)
    SET r.confidence = $confidence,
        r.validated_count = $validated_count
    """

    CYPHER_ON_TOOL_EDGE = """
    MATCH (a:Action {request_event_id: $action_request_event_id}),
          (t:Tool {name: $tool_name})
    MERGE (a)-[:ON_TOOL]->(t)
    """

    # --- API ---------------------------------------------------------------

    def __init__(self, graph: GraphAdapter) -> None:
        self._graph = graph

    async def upsert_tool(
        self,
        *,
        name: str,
        description: str,
        input_schema_json: str,
    ) -> None:
        """
        Idempotent merge of a ``(:Tool)`` node.

        The Tool is the static class -- a single node
        per ``name``. The adapter does NOT create the
        edge to ``(:Problem)`` here; that is the
        ``link_action_to_tool`` responsibility (anchored
        on the Action side).
        """
        await self._graph.query(
            self.CYPHER_UPSERT_TOOL,
            params={
                "name": name,
                "description": description,
                "input_schema_json": input_schema_json,
            },
        )

    async def upsert_problem(
        self,
        *,
        fingerprint: str,
        embedding: list[float],
        tags_json: str,
        last_validated_at: str,
    ) -> None:
        """Idempotent merge of a ``(:Problem)`` node.

        The fingerprint is the canonical hash of the
        problem data (so two calls with the same
        fingerprint produce one node).
        """
        await self._graph.query(
            self.CYPHER_UPSERT_PROBLEM,
            params={
                "fingerprint": fingerprint,
                "embedding": embedding,
                "tags_json": tags_json,
                "last_validated_at": last_validated_at,
            },
        )

    async def upsert_action(
        self,
        *,
        request_event_id: str,
        tool_name: str,
        params_fingerprint: str,
        params_json: str,
    ) -> None:
        """
        Idempotent merge of an ``(:Action)`` node.

        The request_event_id is the EventLog id of the
        ``tool.<x>.requested`` event -- the deterministic
        anchor for the action.
        """
        await self._graph.query(
            self.CYPHER_UPSERT_ACTION,
            params={
                "request_event_id": request_event_id,
                "tool_name": tool_name,
                "params_fingerprint": params_fingerprint,
                "params_json": params_json,
            },
        )

    async def create_outcome(
        self,
        *,
        request_event_id: str,
        status: str,
        confidence: float,
        result_json: str,
        error_message: str,
    ) -> None:
        """
        Create a fresh ``(:Outcome)`` node for the
        action, replacing any prior ``[:PRODUCED]`` edge.

        ``Outcome`` is NOT MERGEd because each action
        has at most one outcome; replaying the
        completed/failed event must overwrite the prior
        outcome.
        """
        await self._graph.query(
            self.CYPHER_CREATE_OUTCOME,
            params={
                "request_event_id": request_event_id,
                "status": status,
                "confidence": confidence,
                "result_json": result_json,
                "error_message": error_message,
            },
        )

    async def link_problem_to_action(
        self,
        *,
        problem_fingerprint: str,
        action_request_event_id: str,
        confidence: float,
        validated_count: int,
        outcome_status: str = "completed",
    ) -> None:
        """
        Idempotent merge of the ``[:SOLVED_BY]`` or
        ``[:FAILED_WITH]`` edge from Problem to Action.

        The edge type depends on the outcome status:
        completed outcomes use ``SOLVED_BY``, failed
        outcomes use ``FAILED_WITH``. Both edges carry
        the same ``confidence`` / ``validated_count``
        properties.

        Splitting the edges (rather than a single
        ``SOLVED_BY`` with a status property) makes
        the graph queryable: "find all failed-with
        patterns" is a single MATCH, not a filter on
        a property.
        """
        cypher = (
            self.CYPHER_SOLVED_BY_EDGE
            if outcome_status == "completed"
            else self.CYPHER_FAILED_WITH_EDGE
        )
        await self._graph.query(
            cypher,
            params={
                "problem_fingerprint": problem_fingerprint,
                "action_request_event_id": action_request_event_id,
                "confidence": confidence,
                "validated_count": validated_count,
            },
        )

    async def link_action_to_tool(
        self,
        *,
        action_request_event_id: str,
        tool_name: str,
    ) -> None:
        """Idempotent merge of the ``[:ON_TOOL]`` edge."""
        await self._graph.query(
            self.CYPHER_ON_TOOL_EDGE,
            params={
                "action_request_event_id": action_request_event_id,
                "tool_name": tool_name,
            },
        )

    # --- read path: solutions by problem / tool ----------------------------

    async def find_solutions_by_problem(
        self,
        *,
        query_embedding: list[float],
        k: int = 5,
        tags: Optional[dict[str, str]] = None,
        tool_name: Optional[str] = None,
        status: str = "completed",
    ) -> list[dict]:
        """
        Top-k solutions by Problem similarity.

        Optional filters:

          - ``tags``: dict of ``key -> value`` that the
            ``Problem.tags_json`` must contain. Multi-tag
            is AND'd: every needle must match. The
            values are JSON-encoded and inlined
            (FalkorDB CONTAINS does not accept params).
          - ``tool_name``: when set, restricts to actions
            on this tool (``t.name = $tool_name``).
          - ``status``: ``"completed"`` (default, uses
            ``[:SOLVED_BY]``), ``"failed"`` (uses
            ``[:FAILED_WITH]``), or ``"all"`` (either
            edge type, no status filter).

        The cypher joins the path
        ``(:Problem)-[r:<status_edge>]->(:Action)-[:ON_TOOL]->(:Tool)``
        and ``(:Action)-[:PRODUCED]->(:Outcome)`` so the
        caller gets all four nodes in a single round
        trip.
        """
        edge_match = edge_match_for_status(status)
        tags_clause, _needles = build_tags_clause(tags)
        tool_clause, params = build_tool_name_clause(tool_name)
        status_clause, status_params = build_status_clause(status)
        where_clause = tags_clause + tool_clause + status_clause

        cypher = BASE_FIND_SOLUTIONS_BY_PROBLEM.safe_substitute(
            edge_match=edge_match,
            where_clause=where_clause,
        )
        result = await self._graph.query(
            cypher,
            params={"vec": query_embedding, "k": k, **params, **status_params},
        )
        return [row_to_solution_by_problem(row) for row in result.result_set]

    async def find_solutions_by_tool(
        self,
        *,
        tool_name: str,
        k: int = 5,
        tags: Optional[dict[str, str]] = None,
        status: str = "completed",
    ) -> list[dict]:
        """
        Top-k solutions for a given tool, ordered by
        outcome confidence. Structural-only (no vector
        search).

        Optional filters:

          - ``tags``: same as find_solutions_by_problem.
          - ``status``: ``"completed"`` (default, uses
            ``[:SOLVED_BY]``), ``"failed"`` (uses
            ``[:FAILED_WITH]``), or ``"all"``.
        """
        edge_match = edge_match_for_status(status)
        tags_clause, _needles = build_tags_clause(tags)
        status_clause, status_params = build_status_clause(status)
        where_clause = tags_clause + status_clause

        cypher = BASE_FIND_SOLUTIONS_BY_TOOL.safe_substitute(
            edge_match=edge_match,
            where_clause=where_clause,
        )
        result = await self._graph.query(
            cypher,
            params={
                "tool_name": tool_name,
                "k": k,
                **status_params,
            },
        )
        return [row_to_solution_by_tool(row) for row in result.result_set]


__all__ = ["GraphSolutionAdapter"]
