# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Exemplo 09 — Knowledge Consolidation end-to-end (ADR-034).

Demonstra o pipeline do **Solution tier** (memória de
longo prazo de tool calls):

  1. Seed an EventLog in Redis com múltiplos agents
     emitindo `tool.X.requested` / `.completed` /
     `.failed`.
  2. Start a `ReactiveDispatcher` with:
       - SolutionExtractorSystem (extracts solutions)
       - SolutionPromoterSystem (writes to FalkorDB)
  3. Show the persisted counts and a few retrieval
     results (`find_solutions_by_problem` and
     `find_solutions_by_tool`).

Pre-requisites:
  - Redis at localhost:6379
  - FalkorDB at localhost:16379 (password: falkordb)

Run:
    uv run --package kntgraph python \
        examples/09_knowledge_consolidation.py
"""

from __future__ import annotations

import asyncio
import json

import redis.asyncio as aioredis

from kntgraph.core.event import Event, correlation_middleware
from kntgraph.knowledge.embedding.provider import EmbeddingClient
from kntgraph.infra.graph import GraphPool
from kntgraph.agents.knowledge.solution_projector import (
    SolutionProjector,
)
from kntgraph.knowledge.graphrag.retriever import GraphRAGRetriever
from kntgraph.agents.memory.solution_extractor import SolutionExtractorSystem
from kntgraph.agents.memory.solution_promoter import SolutionPromoterSystem
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.stream.event_log import EventLog
from kntgraph.runner.reactive import ReactiveDispatcher


TENANT = "demo-cnpj-12.345.678/0001-99"


def _pair(
    agent_id: str, tool: str, data: dict, status: str = "completed"
) -> list[Event]:
    """Build a request + completed/failed pair with proper causation."""
    suffix = ".completed" if status == "completed" else ".failed"
    req = Event.domain_from(
        agent_id=agent_id,
        type=f"tool.{tool}.requested",
        data=data,
        correlation=correlation_middleware.current(),
    )
    extra_data: dict = {"request_id": "r", "tool": tool}
    if status == "completed":
        extra_data["result"] = {"status": "ok"}
        extra_data["latency_ms"] = 12.0
    else:
        extra_data["error"] = "rejeicao 561"
    res = Event.domain_from(
        agent_id=agent_id,
        type=f"tool.{tool}{suffix}",
        data=extra_data,
        causation_id=req.event_id,
        correlation=correlation_middleware.current(),
    )
    return [req, res]


async def seed_event_log(log: EventLog) -> None:
    """Seed three agents with overlapping tool calls."""
    from kntgraph.core.event import correlation_middleware
    correlation_middleware.start(metadata={"example": "09"})
    print("Seeding EventLog with 3 agents × 2 tool events each ...")
    for a in ["agent-001", "agent-002", "agent-003"]:
        for e in _pair(
            a,
            "invoice.issue",
            {
                "cnpj": "12.345.678-0001-90",
                "cfop": "5405",
                "valor": 100.00,
            },
        ):
            await log.append(e)
        for e in _pair(a, "bank.transfer", {"amount": 50.0}, status="failed"):
            await log.append(e)


async def main() -> None:
    import os
    os.environ["FMH_EMBEDDING_TIMEOUT_SECONDS"] = "120.0"

    redis = aioredis.from_url("redis://:redispassword@localhost:6379", db=15)
    await redis.flushdb()
    log = EventLog(RedisEventLogAdapter(client=redis))

    fdb = GraphPool(host="localhost", port=16379, password="falkordb")
    fdb.connect()
    embedding = EmbeddingClient()

    g = fdb.graph(TENANT)
    try:
        await g.query("MATCH (n) DETACH DELETE n")
    except Exception:
        pass

    extractor = SolutionExtractorSystem(bump_min_agents=1)
    # SolutionProjector provides upsert_solution
    projector = SolutionProjector(client=fdb, embedding=embedding, tenant_id=TENANT)
    promoter = SolutionPromoterSystem(tenant_id=TENANT, graph_pool=projector)

    try:
        await seed_event_log(log)

        print("\nRunning one consolidation pass ...")
        from kntgraph.stream.projection import fold_world
        world = await fold_world(log)

        extracted = extractor(world)
        if not isinstance(extracted, list):
            extracted = await extracted
        print(f"Extractor emitted {len(extracted)} candidate(s)")
        if extracted:
            await log.append_batch(extracted)

        promoted = promoter(extracted)
        if not isinstance(promoted, list):
            promoted = await promoted
        print(f"Promoter emitted {len(promoted)} event(s)")
        if promoted:
            await log.append_batch(promoted)
    finally:
        from kntgraph.core.event import correlation_middleware
        correlation_middleware.clear()

    print("\nRetrieval:")
    retriever = GraphRAGRetriever(client=fdb, embedding=embedding, tenant_id=TENANT)
    query_emb = await embedding.embed(
        json.dumps(
            {
                "cnpj": "12.345.678-0001-90",
                "cfop": "5405",
                "valor": 100.0,
            },
            sort_keys=True,
        )
    )
    by_problem = await retriever.find_solutions_by_problem(query_emb, k=5)
    print(f"  find_solutions_by_problem returned {len(by_problem)} hit(s):")
    for r in by_problem:
        print(f"    tool={r.tool_name!r} status={r.outcome_status!r} score={r.score:.4f}")
        print(f"    params={r.action_params_example!r}")

    print("  find_solutions_by_tool('bank.transfer', status=...)")
    for status in ("completed", "failed", "all"):
        by_tool = await retriever.find_solutions_by_tool("bank.transfer", k=5, status=status)
        print(f"    status={status!r:>12}: {len(by_tool)} hit(s)")
        for r in by_tool:
            print(f"      tool={r.tool_name!r} status={r.outcome_status!r}")

    print("\nCumulative promoter stats:")
    print(json.dumps(promoter.stats.__dict__, indent=2, default=str))

    print("\nCleaning up ...")
    try:
        g.query("MATCH (n) DETACH DELETE n")
    except Exception:
        pass
    await redis.flushdb()
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
