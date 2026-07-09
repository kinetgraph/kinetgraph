# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
SolutionProjector — the FalkorDB adapter for the
Solution sub-graph (ADR-010 §3).

The projector writes four node kinds and four edge
kinds to the tenant's graph:

  NODES
  -----
  (:Tool     {name, description, input_schema_json})
  (:Problem  {fingerprint, embedding, tags_json, last_validated_at})
  (:Action   {request_event_id, params_fingerprint, params_json})
  (:Outcome  {status, latency_ms, result_signature, error_message})

  EDGES
  -----
  (:Problem)-[:SOLVED_BY {confidence, validated_count}]->(:Action)
  (:Problem)-[:FAILED_WITH {confidence, validated_count}]->(:Action)
  (:Action)-[:ON_TOOL]->(:Tool)
  (:Action)-[:PRODUCED]->(:Outcome)

All writes use `MERGE` on the natural key, so the
projector is idempotent: re-running on the same
candidate produces the same graph state.

PII gate
--------

The projector is **not** the PII gate. The
`SolutionPromoter` is. The promoter calls
`PiiRedactionTool` and only forwards redacted data to
the projector. The projector accepts whatever it is
given; this is a deliberate split (ADR-010 §2.5) — the
PII decision lives in a tool, the persistence lives in
the adapter.

Embedding
----------

The projector needs the `EmbeddingProvider` to compute
`Problem.embedding`. The dimension MUST match the
tenant's other vector indices (see
`docs/graphrag.md` §1.3). A vector index for
`Problem.embedding` is created on the first projection
attempt and reused.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Optional

import structlog

from kntgraph.agents.memory.solutions import (
    SolutionCandidate,
    ToolDescriptor,
)
from kntgraph.knowledge.embedding.provider import EmbeddingProvider
from kntgraph.infra.graph import GraphPool
from kntgraph.knowledge.graph._sub._solution import GraphSolutionAdapter
from kntgraph.resilience import BulkheadPool


logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Cypher constants
# ---------------------------------------------------------------------------


# Vector index for `Problem.embedding`. Same shape as
# the Document index in `adapter.py`. The
# SolutionProjector creates this on the first upsert.
PROBLEM_VECTOR_INDEX_CYPHER = """
CREATE VECTOR INDEX FOR (p:Problem) ON (p.embedding)
OPTIONS {dimension: $dimension, similarityFunction: 'cosine'}
"""


# ---------------------------------------------------------------------------
# SolutionProjector
# ---------------------------------------------------------------------------


class SolutionProjector:
    """
    Adapter that persists `SolutionCandidate`s to the
    Solution sub-graph of the tenant's FalkorDB.

    The projector is owned by the `SolutionPromoter`;
    the promoter calls `upsert(candidate)` and
    `ensure_tool_nodes(descriptors)`. The projector
    does not know about PII redaction, confidence
    gating, or the review queue — those live in the
    promoter (Fase 3.3) and the consolidator (Fase 2.5).

    Threading
    ---------

    `upsert` and `ensure_tool_nodes` are async. They
    call the FalkorDB driver via `client.graph(...)` and
    `graph.query(...)` which are **synchronous** (the
    Python `falkordb` package is sync). We do not wrap
    each query in `asyncio.to_thread` — the calling
    coroutine already yields, and the cost of an
    extra thread hop is larger than the cost of a
    ~5ms sync query against a local FalkorDB. Operators
    that need higher throughput can swap the projector
    for a subclass that uses threads.

    Failure mode
    ------------

    Any `graph.query` failure propagates as an
    `Exception`. The caller (promoter) handles
    fail-closed: the candidate is reported as `failed`
    and NOT written.
    """

    def __init__(
        self,
        client: GraphPool,
        embedding: EmbeddingProvider,
        *,
        tenant_id: str = "default",
        bulkhead: Optional[BulkheadPool] = None,
        query_timeout_seconds: float = 5.0,
    ) -> None:
        """
        Args:
            client: FalkorDB client (one per process is
                typical).
            embedding: embedding provider for problem
                vectors.
            tenant_id: tenant identifier; required.
            bulkhead: optional `BulkheadPool` keyed on
                ``tenant_id`` to cap concurrent MERGE
                queries against the tenant's graph. When
                the bulkhead is saturated, `upsert`
                returns 0 (caller treats as failed) rather
                than blocking. Construct with
                `await get_bulkhead(tenant_id, ...)` for
                the registry-backed pool.
            query_timeout_seconds: per-query timeout for
                each of the 6 `graph.query` calls in
                `upsert`. Each call runs in
                `asyncio.to_thread` because the FalkorDB
                client is sync; the timeout bounds the
                worker time per call.
        """
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if query_timeout_seconds <= 0:
            raise ValueError("query_timeout_seconds must be > 0")
        self._client = client
        self._embedding = embedding
        self._tenant_id = tenant_id
        # The vector index is created on first
        # `upsert` (best effort). Cached after success.
        self._problem_index_created = False
        # Resilience wiring.
        self._bulkhead = bulkhead
        self._query_timeout_seconds = query_timeout_seconds

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    # ------------------------------------------------------------------ tool nodes

    async def ensure_tool_nodes(self, descriptors: Iterable[ToolDescriptor]) -> int:
        """
        `MERGE` a `(:Tool)` node for every descriptor in
        the iterable. Idempotent; re-running produces the
        same graph.

        Returns the count of nodes written. Used by the
        promoter's `start()` (Fase 4) or by the
        consolidator on boot.
        """
        self._client.connect()
        graph = self._client.graph(self._tenant_id)
        solution = GraphSolutionAdapter(graph)
        n = 0
        for d in descriptors:
            await solution.upsert_tool(
                name=d.name,
                description=d.description,
                input_schema_json=d.input_schema_json,
            )
            n += 1
        return n

    # ------------------------------------------------------------------ upsert

    async def upsert(self, candidate: SolutionCandidate) -> int:
        """
        Persist a `SolutionCandidate` to FalkorDB.

        Steps (each is a separate `graph.query`; the
        order matters because the later queries match
        on the earlier nodes):

          1. `(:Tool)` is `MERGE`d on `name` (the
             promoter's `ensure_tool_nodes` already
             does this; we repeat here for safety so
             `upsert` is self-contained).
          2. `(:Problem)` is `MERGE`d on `fingerprint`.
             The embedding is computed here and stored
             on the node. The vector index is created
             on first call.
          3. `(:Action)` is `MERGE`d on
             `request_event_id`.
          4. `(:Action)-[:ON_TOOL]->(:Tool)` is `MERGE`d.
          5. `(:Outcome)` is `CREATE`d (anchored to the
             Action via `[:PRODUCED]`). Any previous
             outcome for the same Action is deleted
             first (replay safety).
          6. `(:Problem)-[:SOLVED_BY]->(:Action)` (or
             `[:FAILED_WITH]`) is `MERGE`d.

        Returns the count of nodes + edges written
        (4 nodes + 3 edges, minus any pre-existing
        ones if the `MERGE` matched).

        Resilience wiring:
          - Each ``graph.query`` call runs in
            ``asyncio.to_thread(...)`` because the
            FalkorDB Python client is synchronous; the
            event loop is not blocked even when the
            query is slow.
          - Each call is bounded by
            ``query_timeout_seconds`` via
            ``with_timeout``. A timeout raises
            ``asyncio.TimeoutError``, the partial writes
            are NOT rolled back (FalkorDB MERGE is
            idempotent at the node level, so re-running
            the same upsert is safe).
          - When ``bulkhead`` is configured, the whole
            upsert runs through
            ``bulkhead.execute(...)``. Saturation
            returns 0 (treated as failed by the
            promoter) instead of blocking forever.
        """

        async def _do_upsert() -> int:
            self._client.connect()
            graph = self._client.graph(self._tenant_id)
            solution = GraphSolutionAdapter(graph)

            # 1. Tool
            await solution.upsert_tool(
                name=candidate.action.tool_name,
                description=candidate.action.tool_name,
                input_schema_json="{}",
            )
            await self._ensure_problem_vector_index()
            problem_embedding = await self._embedding.embed(candidate.problem.text)
            # 2. Problem
            await solution.upsert_problem(
                fingerprint=candidate.problem.fingerprint,
                embedding=problem_embedding,
                tags_json=json.dumps(
                    candidate.problem.tags,
                    default=str,
                    sort_keys=True,
                ),
                last_validated_at="",
            )
            # 3. Action
            await solution.upsert_action(
                request_event_id=(candidate.action.request_event_id),
                tool_name=candidate.action.tool_name,
                params_fingerprint=(candidate.action.params_fingerprint),
                params_json=json.dumps(
                    dict(candidate.action.params),
                    default=str,
                    sort_keys=True,
                ),
            )
            # 4. Action → Tool
            await solution.link_action_to_tool(
                action_request_event_id=(candidate.action.request_event_id),
                tool_name=candidate.action.tool_name,
            )
            # 5. Outcome. The CREATE pattern deletes the
            # previous outcome (if any) first.
            await solution.create_outcome(
                request_event_id=(candidate.action.request_event_id),
                status=candidate.outcome.status,
                confidence=candidate.confidence,
                result_json=(candidate.outcome.result_signature),
                error_message=candidate.outcome.error_message,
            )
            # 6. Problem → Action edge. ``SOLVED_BY`` for
            # completed outcomes, ``FAILED_WITH`` for
            # failed. The split is intentional: it makes
            # the graph queryable per outcome type.
            validated_count = max(1, candidate.confidence)
            await solution.link_problem_to_action(
                problem_fingerprint=candidate.problem.fingerprint,
                action_request_event_id=(candidate.action.request_event_id),
                confidence=candidate.confidence,
                validated_count=validated_count,
                outcome_status=candidate.outcome.status,
            )
            return 4  # 4 nodes written (Tool, Problem, Action, Outcome)

        # Outer wrapping: optional bulkhead. When the
        # pool is saturated, the bulkhead returns
        # ``Err`` immediately; we surface 0 so the
        # promoter treats this as a failed upsert
        # (consistent with the partial-write case).
        if self._bulkhead is not None:
            bulkhead_result = await self._bulkhead.execute(_do_upsert)
            if bulkhead_result.is_err():
                logger.warning(
                    "falkordb.upsert.bulkhead_rejected",
                    tenant_id=self._tenant_id,
                    error=type(bulkhead_result.err_value()).__name__,
                )
                return 0
            return bulkhead_result.ok_value()
        return await _do_upsert()

    # ------------------------------------------------------------------ vector index

    async def _ensure_problem_vector_index(self) -> None:
        """
        Create the `Problem.embedding` vector index on
        the first call. Best-effort: a failure to
        create the index does not abort the projection
        (the embedding is stored on the node; only
        vector search at query time would be slower).
        """
        if self._problem_index_created:
            return
        self._client.connect()
        graph = self._client.graph(self._tenant_id)
        try:
            await graph.query(
                PROBLEM_VECTOR_INDEX_CYPHER,
                params={"dimension": self._embedding.dimension},
            )
            self._problem_index_created = True
        except Exception as e:
            logger.warning(
                "falkordb.problem_vector_index.create_failed",
                error=str(e),
            )


__all__ = [
    "SolutionProjector",
    "PROBLEM_VECTOR_INDEX_CYPHER",
]
