# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
GraphRAG retriever — search over the FalkorDB projection.

Three retrieval modes are exposed:

  - `vector_search(query_embedding, k=5)`:
      Top-k nearest Documents by cosine similarity.
      Legacy MVP path. Returns the top-k `(doc, score)`
      pairs over `Document` nodes.

  - `find_solutions_by_problem(embedding, *, tags=None,
    tool_name=None, k=5)`:
      Top-k Solutions whose `Problem` is similar to
      the query embedding. Combines
      `vec.cosineDistance(p.embedding, ...)` with
      optional structural filters (tags on the
      `Problem.tags_json`, tool filter on the chained
      `(:Problem)-[:SOLVED_BY]->(:Action)-[:ON_TOOL]->
      (:Tool)`). Returns the **Action** (the
      re-executable thing) with the matched `Problem`
      and `Outcome` for context.

  - `find_solutions_by_tool(tool_name, *, tags=None,
    k=5)`:
      Structural-only retrieval. Returns top-k Actions
      for the given tool, ordered by confidence. No
      vector search.

The retrieval model assumes:

  - The Solution sub-graph has been populated by the
    `SolutionProjector` (Fase 3.2). Empty graphs return
    `[]` for every method.
  - The dimension of the query embedding matches the
    `EmbeddingProvider` used at write time. Mismatched
    dimensions produce empty results (the vector index
    is bound to the dimension).
  - The retriever is tenant-scoped. The graph name is
    `fmh_tenant_{tenant_id}`; the same tenant's
    `SolutionPromoter` and `SolutionProjector` write
    into it.

Failure mode: any `graph.query` error is logged
(structlog) and the method returns `[]`. The
retriever is fail-soft: FalkorDB outages do not
break the caller's coroutine.

See `docs/graphrag.md` §5 for usage examples.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import structlog

from ..embedding.provider import EmbeddingProvider
from ..graph._sub._document import GraphDocumentAdapter
from ..graph._sub._solution import GraphSolutionAdapter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kntgraph.infra.graph import GraphPool


logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# RetrievalResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Document-sub-graph retrieval (legacy MVP)."""

    doc_id: str
    agent_id: str
    event_type: str
    score: float
    data: dict


# ---------------------------------------------------------------------------
# SolutionResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SolutionResult:
    """
    A single Solution-sub-graph hit.

    The fields are the union of `Problem`, `Action`
    and `Outcome` plus the retrieval `score`. The
    caller usually wants `tool_name` and
    `action_params_example` to re-execute; the
    `Problem` and `Outcome` fields are context.
    """

    problem_fingerprint: str
    action_params_example: dict
    tool_name: str
    outcome_status: str
    latency_ms: Optional[float]
    confidence: int
    last_validated_at: Optional[str]
    score: float


# ---------------------------------------------------------------------------
# GraphRAGRetriever
# ---------------------------------------------------------------------------


class GraphRAGRetriever:
    def __init__(
        self,
        client: GraphPool,
        embedding: EmbeddingProvider,
        *,
        tenant_id: str = "default",
    ) -> None:
        self._client = client
        self._embedding = embedding
        self._tenant_id = tenant_id

    # ------------------------------------------------------------------ Document sub-graph (legacy)

    async def vector_search(
        self,
        query_embedding: list[float],
        k: int = 5,
    ) -> list[RetrievalResult]:
        """
        Top-k nearest Documents by cosine similarity
        over the Document sub-graph (ADR-004 §2.4).

        Iter 14 (ADR-019 epílogo): delegates the Cypher
        to ``GraphDocumentAdapter.vector_search``. The
        retriever is now a thin orchestrator: connect
        → adapter call → row mapping.
        """
        self._client.connect()
        graph = self._client.graph(self._tenant_id)
        doc_adapter = GraphDocumentAdapter(graph)
        try:
            rows = await doc_adapter.vector_search(query_embedding=query_embedding, k=k)
        except Exception as e:
            logger.warning(
                "graphrag.vector_search.failed",
                error=str(e),
            )
            return []
        out: list[RetrievalResult] = []
        for row in rows:
            try:
                data = json.loads(row["data_json"]) if row["data_json"] else {}
            except (TypeError, ValueError):
                data = {}
            out.append(
                RetrievalResult(
                    doc_id=row["id"],
                    agent_id=row["agent_id"],
                    event_type=row["event_type"],
                    score=row["score"],
                    data=data,
                )
            )
        return out

    async def retrieve(self, query: str, k: int = 5) -> list[RetrievalResult]:
        """
        Backward-compatible convenience: embeds the
        query and runs the Document vector channel.
        """
        emb = await self._embedding.embed(query)
        return await self.vector_search(emb, k=k)

    # ------------------------------------------------------------------ Solution sub-graph

    async def find_solutions_by_problem(
        self,
        query_embedding: list[float],
        *,
        tags: Optional[dict[str, str]] = None,
        tool_name: Optional[str] = None,
        k: int = 5,
        status: str = "completed",
    ) -> list[SolutionResult]:
        """
        Top-k Solutions whose `Problem` is similar to
        `query_embedding`.

        The Cypher combines
        `vec.cosineDistance(p.embedding, ...)` with
        optional structural filters. The path is:

          (:Problem) -[:SOLVED_BY]-> (:Action)
                              -[:ON_TOOL]->      (:Tool)
                              -[:PRODUCED]->     (:Outcome)

        Returns the `Action` (the re-executable thing)
        with `Problem` and `Outcome` for context.

        Filters (all optional):
          - `tags`: dict of `key → value` that the
            `Problem.tags_json` must contain. The match
            is substring-based: a problem with
            `tags_json = '{"cnpj":"111","uf":"SP"}'`
            matches `tags={"cnpj":"111"}`. Use this
            to scope to a tenant / context.
          - `tool_name`: when set, the path is further
            constrained to actions on this specific
            tool.
          - `status`: defaults to `"completed"` (the
            common case: solutions that worked). Pass
            `"failed"` to retrieve failure patterns.
            Pass `"all"` to ignore status.
        """
        self._client.connect()
        graph = self._client.graph(self._tenant_id)
        solution = GraphSolutionAdapter(graph)
        try:
            rows = await solution.find_solutions_by_problem(
                query_embedding=query_embedding,
                k=k,
                tags=tags,
                tool_name=tool_name,
                status=status,
            )
        except Exception as e:
            logger.warning(
                "graphrag.find_solutions_by_problem.failed",
                error=str(e),
            )
            return []
        out: list[SolutionResult] = []
        for row in rows:
            try:
                params_dict = (
                    json.loads(row["action_params_json"])
                    if row["action_params_json"]
                    else {}
                )
            except (TypeError, ValueError):
                params_dict = {}
            out.append(
                SolutionResult(
                    problem_fingerprint=row["problem_fingerprint"],
                    action_params_example=params_dict,
                    tool_name=row["tool_name"],
                    outcome_status=row["outcome_status"],
                    latency_ms=row["outcome_latency_ms"],
                    confidence=row["outcome_confidence"],
                    last_validated_at=row["last_validated_at"],
                    score=row["score"],
                )
            )
        return out

    async def find_solutions_by_tool(
        self,
        tool_name: str,
        *,
        tags: Optional[dict[str, str]] = None,
        k: int = 5,
        status: str = "completed",
    ) -> list[SolutionResult]:
        """
        Top-k Solutions for `tool_name`, ordered by
        confidence then by `last_validated_at`.

        Pure structural retrieval — no vector search.
        Useful for "list everything we ever did with
        tool X" and for debugging a particular tool's
        failure mode.

        Filters (all optional):
          - `tags`: same as `find_solutions_by_problem`.
          - `status`: `"completed"` (default), `"failed"`,
            or `"all"`.
        """
        self._client.connect()
        graph = self._client.graph(self._tenant_id)
        solution = GraphSolutionAdapter(graph)
        try:
            rows = await solution.find_solutions_by_tool(
                tool_name=tool_name,
                k=k,
                tags=tags,
                status=status,
            )
        except Exception as e:
            logger.warning(
                "graphrag.find_solutions_by_tool.failed",
                error=str(e),
            )
            return []
        out: list[SolutionResult] = []
        for row in rows:
            try:
                params_dict = (
                    json.loads(row["action_params_json"])
                    if row["action_params_json"]
                    else {}
                )
            except (TypeError, ValueError):
                params_dict = {}
            out.append(
                SolutionResult(
                    problem_fingerprint=row["problem_fingerprint"],
                    action_params_example=params_dict,
                    tool_name=row["tool_name"],
                    outcome_status=row["outcome_status"],
                    latency_ms=None,
                    confidence=row["outcome_confidence"],
                    last_validated_at=None,
                    score=1.0,
                )
            )
        return out


__all__ = [
    "GraphRAGRetriever",
    "RetrievalResult",
    "SolutionResult",
]
