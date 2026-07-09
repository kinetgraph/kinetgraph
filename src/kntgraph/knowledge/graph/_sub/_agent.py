# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.graph._sub._agent -- ``GraphAgentAdapter`` for the
``(:Agent)`` node.

Owns the Cypher templates and the parameter mapping for
``(:Agent)`` writes and reads. Composes a ``GraphAdapter``
to dispatch queries; does NOT own the connection.

The adapter is the single source of truth for:

  - the ``MERGE (a:Agent ...)`` template
  - the ``MATCH (a:Agent ...) RETURN ...`` template
  - the parameter names used in both

``FalkorDBProjector._merge_agent_node`` becomes a
one-liner that calls ``GraphAgentAdapter.upsert``.

Iter 11 (ADR-019 epílogo + Iter 11 do sharding).
"""

from __future__ import annotations

from typing import Optional

from .._protocol import GraphAdapter


class GraphAgentAdapter:
    """
    Cypher + parameter adapter for the ``(:Agent)`` node.

    Composition over inheritance: the adapter holds a
    reference to a ``GraphAdapter`` (the framework-level
    I/O boundary). Tests inject a mock graph adapter; the
    production path uses ``FalkorDBGraphAdapter``.

    Why a class, not a module of functions: the cypher
    constants live on the class so callers can introspect
    them (e.g. for graph-explorer tools or for tests
    that verify which cypher a given method emits).
    """

    # --- cypher templates ---------------------------------------------------

    CYPHER_UPSERT = """
    MERGE (a:Agent {agent_id: $agent_id})
    SET a.last_seen = $last_seen,
        a.tenant_id = $tenant_id
    """

    CYPHER_FIND_BY_ID = """
    MATCH (a:Agent {agent_id: $agent_id})
    RETURN a.agent_id AS agent_id,
           a.tenant_id AS tenant_id,
           a.last_seen AS last_seen
    """

    # --- API ---------------------------------------------------------------

    def __init__(self, graph: GraphAdapter) -> None:
        self._graph = graph

    async def upsert(
        self,
        *,
        agent_id: str,
        tenant_id: str,
        last_seen: str,
    ) -> None:
        """
        Idempotent merge of an ``(:Agent)`` node.

        ``MERGE`` on ``agent_id`` means: the same
        ``agent_id`` across replays / restarts produces
        a single node; the ``SET`` updates the mutable
        fields (``last_seen``, ``tenant_id``).

        Parameters
        ----------
        agent_id:
            The EventLog agent_id (e.g. ``"NF-001"``).
        tenant_id:
            The owning tenant id. Stored on the node so
            a multi-tenant query can filter on it without
            switching graphs.
        last_seen:
            ISO timestamp of the latest event seen. Empty
            string when the agent has no events yet.
        """
        await self._graph.query(
            self.CYPHER_UPSERT,
            params={
                "agent_id": agent_id,
                "tenant_id": tenant_id,
                "last_seen": last_seen,
            },
        )

    async def find_by_id(self, agent_id: str) -> Optional[dict]:
        """
        Look up an ``(:Agent)`` node by ``agent_id``.

        Returns the node as a dict, or ``None`` if the
        node does not exist. The dict shape is the same
        as the ``RETURN`` columns in ``CYPHER_FIND_BY_ID``
        — the adapter treats ``GraphQueryResult`` rows
        as JSON-safe tuples / dicts.

        The framework does NOT use this method in the
        projection path; it exists for tests, the
        retriever, and one-off admin tooling.
        """
        result = await self._graph.query(
            self.CYPHER_FIND_BY_ID,
            params={"agent_id": agent_id},
        )
        if not result.result_set:
            return None
        row = result.result_set[0]
        # Rows may be tuples (no headers) or dicts
        # (when the backend returns ``QueryResult`` with
        # column metadata). Support both shapes.
        if isinstance(row, dict):
            return row
        # Tuple shape: align with the RETURN order in
        # ``CYPHER_FIND_BY_ID``.
        return {
            "agent_id": row[0],
            "tenant_id": row[1],
            "last_seen": row[2],
        }


__all__ = ["GraphAgentAdapter"]
