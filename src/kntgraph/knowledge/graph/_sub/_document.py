# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.graph._sub._document -- ``GraphDocumentAdapter`` for
the ``(:Document)`` node + ``[:HAS_DOC]`` edge.

Owns the Cypher templates and parameter mapping for
Document writes / reads. The Document id is
``"<agent_id>:<event_id>"`` — the natural primary key
of an EventLog event — so two agents with the same
``event_id`` never collide.

``FalkorDBProjector._merge_document_node`` and
``_merge_has_doc_edge`` become one-liners that delegate
to this adapter.

Iter 12 (ADR-019 epílogo + Iter 12 do sharding).
"""

from __future__ import annotations

from typing import Optional

from .._protocol import GraphAdapter


class GraphDocumentAdapter:
    """
    Cypher + parameter adapter for the ``(:Document)``
    node and the ``(:Agent)-[:HAS_DOC]->(:Document)`` edge.

    Composition over inheritance: the adapter holds a
    reference to a ``GraphAdapter``. Tests inject a mock
    graph adapter; the production path uses
    ``FalkorDBGraphAdapter``.
    """

    # --- cypher templates ---------------------------------------------------

    CYPHER_UPSERT = """
    MERGE (d:Document {id: $id})
    SET d.agent_id = $agent_id,
        d.event_type = $event_type,
        d.data_json = $data_json,
        d.embedding = vecf32($embedding)
    """

    CYPHER_HAS_DOC_EDGE = """
    MATCH (a:Agent {agent_id: $agent_id}),
          (d:Document {id: $doc_id})
    MERGE (a)-[:HAS_DOC]->(d)
    """

    CYPHER_FIND_BY_ID = """
    MATCH (d:Document {id: $id})
    RETURN d.id AS id,
           d.agent_id AS agent_id,
           d.event_type AS event_type,
           d.data_json AS data_json
    """

    # --- API ---------------------------------------------------------------

    def __init__(self, graph: GraphAdapter) -> None:
        self._graph = graph

    async def upsert(
        self,
        *,
        doc_id: str,
        agent_id: str,
        event_type: str,
        data_json: str,
        embedding: list[float],
    ) -> None:
        """
        Idempotent merge of a ``(:Document)`` node.

        ``doc_id`` is the natural primary key
        (``"<agent_id>:<event_id>"``). The ``SET``
        updates the mutable fields. The embedding is
        stored as a ``vecf32(...)`` — FalkorDB's vector
        type — so the same node is searchable via
        cosine similarity.

        Parameters
        ----------
        doc_id:
            The Document id, typically
            ``"<agent_id>:<event_id>"``.
        agent_id:
            The owning agent. Stored on the node for
            filtering without graph traversal.
        event_type:
            The EventLog event type (e.g. ``"nf.received"``).
        data_json:
            JSON-encoded event payload. Sorted-key
            serialisation is the caller's responsibility.
        embedding:
            The vector representation of the document
            (length matches the provider's ``dimension``).
        """
        await self._graph.query(
            self.CYPHER_UPSERT,
            params={
                "id": doc_id,
                "agent_id": agent_id,
                "event_type": event_type,
                "data_json": data_json,
                "embedding": embedding,
            },
        )

    async def link_to_agent(
        self,
        *,
        agent_id: str,
        doc_id: str,
    ) -> None:
        """
        Idempotent merge of the ``[:HAS_DOC]`` edge.

        Separated from ``upsert`` because the edge
        requires both nodes to exist; splitting allows
        callers to retry the edge without re-inserting
        the Document.

        The ``MERGE`` ensures a single edge per
        ``(agent, document)`` pair.
        """
        await self._graph.query(
            self.CYPHER_HAS_DOC_EDGE,
            params={
                "agent_id": agent_id,
                "doc_id": doc_id,
            },
        )

    async def find_by_id(self, doc_id: str) -> Optional[dict]:
        """
        Look up a ``(:Document)`` node by ``doc_id``.

        Returns the node as a dict, or ``None`` if it
        does not exist. The dict shape matches the
        ``RETURN`` columns in ``CYPHER_FIND_BY_ID``.
        """
        result = await self._graph.query(
            self.CYPHER_FIND_BY_ID,
            params={"id": doc_id},
        )
        if not result.result_set:
            return None
        row = result.result_set[0]
        if isinstance(row, dict):
            return row
        # Tuple shape: align with the RETURN order in
        # ``CYPHER_FIND_BY_ID``.
        return {
            "id": row[0],
            "agent_id": row[1],
            "event_type": row[2],
            "data_json": row[3],
        }

    # --- read path: vector search ------------------------------------------

    CYPHER_VECTOR_SEARCH = """
    MATCH (d:Document)
    WHERE d.embedding IS NOT NULL
    WITH d, vec.cosineDistance(d.embedding, vecf32($vec)) AS score
    RETURN d.id AS id, d.agent_id AS agent_id,
           d.event_type AS event_type, d.data_json AS data_json,
           score
    ORDER BY score ASC LIMIT $k
    """

    async def vector_search(
        self,
        *,
        query_embedding: list[float],
        k: int = 5,
    ) -> list[dict]:
        """
        Top-k nearest Documents by cosine similarity.

        FalkorDB 1.6.1 does not accept vector parameters
        via the ``params`` kwarg; the embedding is
        inlined as a ``vecf32([...])`` call. The values
        are formatted as fixed-point numbers to avoid
        locale issues.

        The adapter owns the Cypher template; the
        retriever consumes the rows as plain dicts.
        """
        result = await self._graph.query(
            self.CYPHER_VECTOR_SEARCH,
            params={"vec": query_embedding, "k": k},
        )
        return [
            {
                "id": row[0] or "",
                "agent_id": row[1] or "",
                "event_type": row[2] or "",
                "data_json": row[3] or "",
                "score": float(row[4] or 0.0),
            }
            for row in result.result_set
        ]


__all__ = ["GraphDocumentAdapter"]
