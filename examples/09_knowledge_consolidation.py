# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Exemplo 09 — Knowledge Consolidation end-to-end (ADR-010).

Demonstra o pipeline do **Solution tier** (memória de
longo prazo de tool calls):

  1. Seed an EventLog in Redis com múltiplos agents
     emitindo `tool.X.requested` / `.completed` /
     `.failed`.
  2. Start a `KnowledgeConsolidator` (post-tick
     coroutine) that:
       - lê o EventLog
       - extrai `SolutionCandidate`s
       - bumpa confidence cross-agent
       - gateia por opt-in flag + approval list + review
         threshold
       - redata PII (level 1)
       - persiste o set auto-promoted no FalkorDB
         (sub-grafo de Solutions)
  3. Show the persisted counts and a few retrieval
     results (`find_solutions_by_problem` and
     `find_solutions_by_tool`).
  4. Print the cumulative metrics that the operator
     would scrape from structlog.

This is the **Solution tier** retrieval (Problem/Action/
Outcome). For the legacy Document sub-graph
retrieval, see `08_falkordb_projection.py`.

Pre-requisites:
  - Redis at localhost:6379
  - FalkorDB at localhost:16379 (password: falkordb)

Run:
    cd fmh_agents
    uv run --package kntgraph python \\
        examples/09_knowledge_consolidation.py
"""

from __future__ import annotations

import asyncio
import json

import redis.asyncio as aioredis

from kntgraph.core.event import Event
from kntgraph.knowledge.embedding.provider import EmbeddingClient
from kntgraph.infra.graph import GraphPool
from kntgraph.agents.knowledge.solution_projector import (
    SolutionProjector,
)
from kntgraph.knowledge.graphrag.retriever import GraphRAGRetriever
from kntgraph.agents.memory.knowledge_consolidator import (
    KnowledgeConsolidator,
)
from kntgraph.agents.memory.solutions import (
    SolutionExtractor,
    SolutionPromotionBus,
    SolutionPromoter,
)
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.stream.event_log import EventLog
from kntgraph.agents.tools.pii import PiiRedactionTool


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
    )
    return [req, res]


async def seed_event_log(log: EventLog) -> None:
    """Seed three agents with overlapping tool calls."""
    print("Seeding EventLog with 3 agents × 2 tool events each ...")
    for a in ["agent-001", "agent-002", "agent-003"]:
        for e in _pair(
            a,
            "invoice.issue",
            {
                "cnpj": "12.345.678/0001-90",
                "cfop": "5405",
                "valor": 100.00,
            },
        ):
            await log.append(e)
        for e in _pair(a, "bank.transfer", {"amount": 50.0}, status="failed"):
            await log.append(e)
    # Total: 12 events = 6 .requested + 4 .completed +
    # 2 .failed (2 of 3 bank.transfer calls get
    # failures? actually all 3 are failed). The
    # extractor dedups by request_event_id, so
    # candidates are 2 per agent (invoice + bank) × 3
    # agents = 6 candidates.


async def main() -> None:
    redis = aioredis.from_url("redis://localhost:6379", db=15)
    await redis.flushdb()
    log = EventLog(RedisEventLogAdapter(client=redis))

    fdb = GraphPool(host="localhost", port=16379, password="falkordb")
    fdb.connect()
    embedding = EmbeddingClient()

    # Clean the tenant graph before the run.
    g = fdb.graph(TENANT)
    try:
        await g.query("MATCH (n) DETACH DELETE n")
    except Exception:
        pass

    # Build the components.
    # registry = ToolRegistry()
    # No tools registered — `ensure_tool_nodes` is a
    # no-op; the projector auto-creates Tool nodes
    # from the candidates' `tool_name`. The registry
    # is reserved for Fase 4 polish.
    bus = SolutionPromotionBus()
    extractor = SolutionExtractor(entity_extractor=None)
    pii = PiiRedactionTool(level=1)
    promoter = SolutionPromoter(
        tenant_id=TENANT,
        projector=SolutionProjector(client=fdb, embedding=embedding, tenant_id=TENANT),
        pii_redactor=pii,
    )
    consolidator = KnowledgeConsolidator(
        log=log,
        bus=bus,
        extractor=extractor,
        promoter=promoter,
        interval=10.0,
        tenant_id=TENANT,
        # `redis=None` means: tenant is always enabled
        # (no opt-in flag check). Production tenants
        # wire a redis client + the env var.
        redis=None,
    )

    # Step 1: seed.
    await seed_event_log(log)

    # Step 2: one consolidation pass (the consolidator
    # also runs in a coroutine, but for the example
    # we drive a single pump and read the result).
    print("\nRunning one consolidation pass ...")
    stats = await consolidator.pump_once()
    print("Pump stats:")
    print(json.dumps(stats.as_dict(), indent=2, default=str))

    # Step 3: retrieval.
    print("\nRetrieval:")
    retriever = GraphRAGRetriever(client=fdb, embedding=embedding, tenant_id=TENANT)
    # Find solutions whose `Problem` is similar to a
    # "new" NF-e with the same CNPJ.
    query_emb = await embedding.embed(
        json.dumps(
            {
                "cnpj": "12.345.678/0001-90",
                "cfop": "5405",
                "valor": 100.0,
            },
            sort_keys=True,
        )
    )
    by_problem = retriever.find_solutions_by_problem(query_emb, k=5)
    print(f"  find_solutions_by_problem returned {len(by_problem)} hit(s):")
    for r in by_problem:
        print(
            f"    tool={r.tool_name!r} status={r.outcome_status!r} score={r.score:.4f}"
        )
        # The PII gate redacted the CNPJ in
        # `action_params_example`.
        print(f"    params (redacted)={r.action_params_example!r}")

    # Structural: all solutions for `bank.transfer`
    # (these are failures). `status="completed"` (the
    # default) does NOT match failed outcomes; we
    # need `status="failed"` to retrieve them, or
    # `status="all"` for the union.
    print("  find_solutions_by_tool('bank.transfer', status=...)")
    for status in ("completed", "failed", "all"):
        by_tool = retriever.find_solutions_by_tool("bank.transfer", k=5, status=status)
        print(f"    status={status!r:>12}: {len(by_tool)} hit(s)")
        for r in by_tool:
            print(f"      tool={r.tool_name!r} status={r.outcome_status!r}")

    # Step 4: cumulative metrics (what an operator
    # would scrape from structlog).
    print("\nCumulative promoter stats:")
    cum = promoter.cumulative_stats
    print(json.dumps(cum.as_dict(), indent=2, default=str))

    # Cleanup.
    print("\nCleaning up ...")
    try:
        g.query("MATCH (n) DETACH DELETE n")
    except Exception:
        pass
    await redis.flushdb()
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
