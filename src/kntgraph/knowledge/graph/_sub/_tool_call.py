# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.graph._sub._tool_call -- ``GraphToolCallAdapter`` for
the ``(:ToolCall)`` node + ``[:CALLED]`` edge.

Owns the Cypher templates and parameter mapping for
ToolCall writes / reads. The ToolCall id is the
EventLog ``event_id`` of the tool-call event.

``FalkorDBProjector._merge_tool_call_node`` and
``_merge_called_edge`` become one-liners that delegate
to this adapter.

Iter 12 (ADR-019 epílogo + Iter 12 do sharding).
"""

from __future__ import annotations

from typing import Optional

from .._protocol import GraphAdapter


class GraphToolCallAdapter:
    """
    Cypher + parameter adapter for the ``(:ToolCall)``
    node and the ``(:Agent)-[:CALLED]->(:ToolCall)`` edge.
    """

    # --- cypher templates ---------------------------------------------------

    CYPHER_UPSERT = """
    MERGE (t:ToolCall {id: $id})
    SET t.tool = $tool,
        t.request_id = $request_id,
        t.status = $status,
        t.latency_ms = $latency_ms,
        t.agent_id = $agent_id
    """

    CYPHER_CALLED_EDGE = """
    MATCH (a:Agent {agent_id: $agent_id}),
          (t:ToolCall {id: $tool_id})
    MERGE (a)-[:CALLED]->(t)
    """

    CYPHER_FIND_BY_ID = """
    MATCH (t:ToolCall {id: $id})
    RETURN t.id AS id,
           t.tool AS tool,
           t.request_id AS request_id,
           t.status AS status,
           t.latency_ms AS latency_ms,
           t.agent_id AS agent_id
    """

    # --- API ---------------------------------------------------------------

    def __init__(self, graph: GraphAdapter) -> None:
        self._graph = graph

    async def upsert(
        self,
        *,
        tool_call_id: str,
        tool: str,
        request_id: str,
        status: str,
        latency_ms: Optional[float],
        agent_id: str,
    ) -> None:
        """
        Idempotent merge of a ``(:ToolCall)`` node.

        ``latency_ms`` is ``None`` for failed events
        (the operation aborted before timing).
        """
        await self._graph.query(
            self.CYPHER_UPSERT,
            params={
                "id": tool_call_id,
                "tool": tool,
                "request_id": request_id,
                "status": status,
                "latency_ms": latency_ms,
                "agent_id": agent_id,
            },
        )

    async def link_to_agent(
        self,
        *,
        agent_id: str,
        tool_call_id: str,
    ) -> None:
        """
        Idempotent merge of the ``[:CALLED]`` edge.
        """
        await self._graph.query(
            self.CYPHER_CALLED_EDGE,
            params={
                "agent_id": agent_id,
                "tool_id": tool_call_id,
            },
        )

    async def find_by_id(self, tool_call_id: str) -> Optional[dict]:
        """
        Look up a ``(:ToolCall)`` node by id.
        """
        result = await self._graph.query(
            self.CYPHER_FIND_BY_ID,
            params={"id": tool_call_id},
        )
        if not result.result_set:
            return None
        row = result.result_set[0]
        if isinstance(row, dict):
            return row
        return {
            "id": row[0],
            "tool": row[1],
            "request_id": row[2],
            "status": row[3],
            "latency_ms": row[4],
            "agent_id": row[5],
        }


__all__ = ["GraphToolCallAdapter"]
