# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
FalkorDB projection system.

Reads the EventLog and writes a derived projection into
FalkorDB. The projection is **rebuildable** at any time —
nothing in FalkorDB is unique. The EventLog is the only
source of truth.

Per ADR-004:

  - One graph per tenant (`fmh_tenant_{cnpj}`)
  - Nodes:
      (:Agent {agent_id, agent_type, last_seen})
      (:Document {doc_id, content, embedding: vec_f32})
      (:Entity {name, type, embedding: vec_f32})
      (:ToolCall {tool, request_id, status, latency_ms})
  - Edges:
      (a:Agent)-[:HAS_DOC]->(d:Document)
      (d:Document)-[:MENTIONS]->(e:Entity)
      (a:Agent)-[:CALLED]->(t:ToolCall)

For the MVP, we project:
  - `Agent` nodes for every agent that emitted any event.
  - `ToolCall` nodes for every `.completed`/`.failed` event
    we can identify as a tool call.
  - `Document` nodes for `data` payloads that are large
    enough to warrant indexing (e.g. NF-e with extracted_data).

Embedding is plugable via ``EmbeddingProvider``. The default
production implementation is ``EmbeddingClient`` (which
internally uses ``OllamaEmbeddingAdapter``); tests
should inject a ``FakeEmbeddingProvider`` (see
``kntgraph.testing.embedding``) to avoid the Ollama
dependency.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

import structlog

from ...core.event import Event
from ...core.tool_event import ToolEventKind
from ..embedding.provider import EmbeddingProvider
from ..graph._sub._agent import GraphAgentAdapter
from ..graph._sub._document import GraphDocumentAdapter
from ..graph._sub._tool_call import GraphToolCallAdapter
from ._categorize import CategorizedEvents, _categorize_events
from ._params import (
    _agent_node_params,
    _doc_text,
    _document_node_params,
    _tool_call_node_params,
)


if TYPE_CHECKING:
    from kntgraph.infra.graph import GraphAdapter, GraphPool
    from ...stream.event_log import EventLog


logger = structlog.get_logger()


# Cypher that creates a vector index on Document.embedding
# if the FalkorDB version supports it. The query is wrapped
# in a try/except because the syntax varies across FalkorDB
# versions; we degrade gracefully if not available.
VECTOR_INDEX_CYPHER = """
CREATE VECTOR INDEX FOR (d:Document) ON (d.embedding)
OPTIONS {dimension: $dimension, similarityFunction: 'cosine'}
"""


class FalkorDBProjector:
    """
    Projects events from the EventLog into a FalkorDB graph.

    The projector is a **cyclic system** (or a one-shot
    script). It reads the EventLog for a tenant, folds the
    events, and writes/updates nodes and edges in the
    tenant's graph.

    The projection is idempotent: `MERGE` on (id) prevents
    duplicate creation. The embed() function is called once
    per Document; on replay, if the embedding has not changed,
    we skip.
    """

    def __init__(
        self,
        log: EventLog,
        client: GraphPool,
        *,
        embedding: EmbeddingProvider,
        tenant_id: str = "default",
    ) -> None:
        self._log = log
        self._client = client
        self._embedding = embedding
        self._tenant_id = tenant_id
        self._vector_index_created = False

    # ------------------------------------------------------------------ top

    def _build_tool_call_pairs(
        self, categorized: CategorizedEvents
    ) -> list[tuple[Event, ToolEventKind]]:
        """Pair each tool event with its kind (COMPLETED/FAILED).

        Pure routing: the categoriser already separated
        the events; we only attach the enum tag.
        """
        pairs: list[tuple[Event, ToolEventKind]] = []
        for e in categorized.completed_tool_events:
            pairs.append((e, ToolEventKind.COMPLETED))
        for e in categorized.failed_tool_events:
            pairs.append((e, ToolEventKind.FAILED))
        return pairs

    async def project_all(self) -> dict[str, int]:
        """
        Project the full event log for the tenant. Returns
        counts of nodes/edges written.
        """
        # Read all events for this tenant. For the MVP, the
        # tenant is a single agent OR a set of agents; we
        # project every agent we find.
        stats = {
            "agents": 0,
            "documents": 0,
            "tool_calls": 0,
            "edges": 0,
        }
        for agent_id in await self._log.list_agents():
            events = await self._log.read(agent_id)
            n = await self._project_agent(agent_id, events)
            stats["agents"] += 1
            stats["documents"] += n["documents"]
            stats["tool_calls"] += n["tool_calls"]
            stats["edges"] += n["edges"]
        return stats

    async def project_agent(
        self, agent_id: str, events: Iterable[Event]
    ) -> dict[str, int]:
        """
        Writes Agent node, Document nodes (for large
        payloads), ToolCall nodes (for tool events), and
        edges connecting them.

        Iter 8 (ADR-019 epílogo): decomposed from a single
        god-method (CC=9) into:

          - ``_categorize_events`` — sync, pure routing.
          - ``_agent_node_params`` — sync, pure params builder.
          - ``_document_node_params`` — sync, pure params builder.
          - ``_tool_call_node_params`` — sync, pure params builder.
          - ``_merge_*_node`` — async, single Cypher emission each.

        The high-level orchestrator below delegates to
        three helpers (``_upsert_agent_node``,
        ``_upsert_document_nodes``,
        ``_upsert_tool_call_nodes``) and a pure categoriser.
        Each helper is unit-tested without a FalkorDB
        connection.
        """
        self._client.connect()
        graph = self._client.graph(self._tenant_id)

        categorized = _categorize_events(events)
        documents_with_embedding = await self._collect_document_embeddings(
            agent_id, categorized
        )
        tool_calls = self._build_tool_call_pairs(categorized)

        await self._ensure_vector_index()
        await self._upsert_agent_node(agent_id, events, graph)
        await self._upsert_document_nodes(agent_id, graph, documents_with_embedding)
        await self._upsert_tool_call_nodes(agent_id, graph, tool_calls)

        return {
            "documents": len(documents_with_embedding),
            "tool_calls": len(tool_calls),
            "edges": len(documents_with_embedding) + len(tool_calls),
        }

    async def _collect_document_embeddings(
        self,
        agent_id: str,
        categorized: CategorizedEvents,
    ) -> list[tuple[Event, list[float]]]:
        """Embed each document candidate. Pure async
        fan-out: one ``embed`` call per candidate.
        """
        out: list[tuple[Event, list[float]]] = []
        for e in categorized.document_candidates:
            embedding = await self._embedding.embed(_doc_text(agent_id, e))
            out.append((e, embedding))
        return out

    async def _upsert_agent_node(
        self, agent_id: str, events: Iterable[Event], graph: GraphAdapter
    ) -> None:
        """Write the Agent node via GraphAgentAdapter."""
        params = _agent_node_params(agent_id, events, tenant_id=self._tenant_id)
        adapter = GraphAgentAdapter(graph)
        await adapter.upsert(
            agent_id=params["agent_id"],
            tenant_id=params["tenant_id"],
            last_seen=params["last_seen"],
        )

    async def _upsert_document_nodes(
        self,
        agent_id: str,
        graph: GraphAdapter,
        documents_with_embedding: list[tuple[Event, list[float]]],
    ) -> None:
        """Write Document nodes + edges via GraphDocumentAdapter."""
        adapter = GraphDocumentAdapter(graph)
        for e, embedding in documents_with_embedding:
            params = _document_node_params(e, embedding=embedding)
            await adapter.upsert(
                doc_id=params["id"],
                agent_id=params["agent_id"],
                event_type=params["event_type"],
                data_json=params["data_json"],
                embedding=params["embedding"],
            )
            await adapter.link_to_agent(agent_id=agent_id, doc_id=params["id"])

    async def _upsert_tool_call_nodes(
        self,
        agent_id: str,
        graph: GraphAdapter,
        tool_calls: list[tuple[Event, ToolEventKind]],
    ) -> None:
        """Write ToolCall nodes + edges via GraphToolCallAdapter."""
        adapter = GraphToolCallAdapter(graph)
        for e, kind in tool_calls:
            params = _tool_call_node_params(e, kind=kind)
            await adapter.upsert(
                tool_call_id=params["id"],
                tool=params["tool"],
                request_id=params["request_id"],
                status=params["status"],
                latency_ms=params.get("latency_ms"),
                agent_id=params["agent_id"],
            )
            await adapter.link_to_agent(agent_id=agent_id, tool_call_id=params["id"])

    async def _ensure_vector_index(self) -> None:
        if self._vector_index_created:
            return
        self._client.connect()
        graph = self._client.graph(self._tenant_id)
        try:
            await graph.query(
                VECTOR_INDEX_CYPHER,
                params={"dimension": self._embedding.dimension},
            )
            self._vector_index_created = True
        except Exception as e:
            logger.warning(
                "falkordb.vector_index.create_failed",
                error=str(e),
            )
            # Don't raise; the projection still works for
            # non-vector queries.
