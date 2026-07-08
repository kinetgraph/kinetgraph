# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Exemplo 08 — FalkorDB projection + GraphRAG retrieval
(ADR-004 F8.3).

Demonstra o sub-grafo de **Documentos** do FalkorDB
(populado pelo `FalkorDBProjector`):

  1. Seed an EventLog in Redis with the same NF workflow
     used in fechamento_mensal.py.
  2. Project the events into a FalkorDB graph
     (per-tenant, vector-indexed).
  3. Run a vector search via the GraphRAGRetriever.
  4. Show the final graph (counts of nodes/edges).

This is the **legacy MVP** retrieval (Document
sub-graph). For the Solution sub-graph (problem/action/
outcome retrieval), see `09_knowledge_consolidation.py`.

Pre-requisites:
  - Redis at localhost:6379 (default)
  - FalkorDB at localhost:16379 (password: falkordb)

Run:
    uv run --package kntgraph python \\
        examples/08_falkordb_projection.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import date

import redis.asyncio as aioredis

from kntgraph.core.event import (
    Event,
    OperationalEventType,
    correlation_middleware,
)
from kntgraph.testing import FakeEmbeddingProvider
from kntgraph.knowledge.falkordb.adapter import FalkorDBProjector
from kntgraph.infra.graph import (
    GraphPool,
    graph_name_for_tenant,
)
from kntgraph.knowledge.graphrag.retriever import GraphRAGRetriever
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.stream.event_log import EventLog

CNPJ = "12.345.678/0001-90"


def spawn_empresa() -> Event:
    return Event.operation_from(
        agent_id=CNPJ,
        type=OperationalEventType.SPAWNED,
        data={"cnpj": CNPJ, "razao_social": "Padaria do Zé"},
        correlation=correlation_middleware.current(),
    )


def emit_nf_received(numero: str, valor: float, fornecedor: str) -> Event:
    return Event.domain_from(
        agent_id=numero,
        type="nf.received",
        data={
            "numero": numero,
            "cnpj_emitente": "11.222.333/0001-44",
            "cnpj_destinatario": CNPJ,
            "valor_total": valor,
            "fornecedor": fornecedor,
            "data_emissao": date(2026, 1, 15).isoformat(),
        },
        correlation=correlation_middleware.current(),
    )


def emit_invoice_call(numero: str) -> list[Event]:
    """Emit a tool request + completion pair (mocked)."""
    request = Event.domain_from(
        agent_id=numero,
        type="tool.invoice.issue.requested",
        data={"document_id": numero, "xml": "<sim/>"},
        correlation=correlation_middleware.current(),
    )
    completion = Event.domain_from(
        agent_id=numero,
        type="tool.invoice.issue.completed",
        data={
            "request_id": str(request.event_id),
            "tool": "invoice.issue",
            "result": {
                "status": "authorized",
                "protocol": f"INV-{numero}-SIMULATED",
            },
            "latency_ms": 12.0,
        },
        causation_id=request.event_id,
        correlation=correlation_middleware.continue_from(request),
    )
    return [request, completion]


async def main() -> None:
    print("=" * 70)
    print("FMH — FalkorDB Projection + GraphRAG Retrieval (F8.3)")
    print("=" * 70)
    print()

    # 1. Connect
    redis = aioredis.from_url("redis://localhost:6379", db=15)
    await redis.flushdb()
    fdb = GraphPool(
        host=os.environ.get("FMH_FALKORDB_HOST", "localhost"),
        port=int(os.environ.get("FMH_FALKORDB_PORT", "16379")),
        password=os.environ.get("FMH_FALKORDB_PASSWORD", "falkordb"),
    )
    fdb.connect()
    print(
        f"✓ Connected to FalkorDB at "
        f"{os.environ.get('FMH_FALKORDB_HOST', 'localhost')}:"
        f"{os.environ.get('FMH_FALKORDB_PORT', '16379')}"
    )

    # 2. Seed the EventLog
    log = EventLog(RedisEventLogAdapter(client=redis))
    with correlation_middleware.scope(metadata={"flow": "f8.3-demo"}):
        await log.append(spawn_empresa())
        print(f"[seed] empresa {CNPJ}")

        nfs = [
            ("NF-2026-001", 1500.50, "ACME Padaria"),
            ("NF-2026-002", 2300.00, "Widgets Inc"),
            ("NF-2026-003", 450.75, "ACME Padaria"),
        ]
        for numero, valor, forn in nfs:
            await log.append(emit_nf_received(numero, valor, forn))
            for ev in emit_invoice_call(numero):
                await log.append(ev)
            print(f"[seed] {numero} R${valor:.2f} forn={forn}")

    # 3. Project into FalkorDB
    print()
    print("=" * 70)
    print("PROJECTING EVENT LOG → FALKORDB")
    print("=" * 70)
    projector = FalkorDBProjector(
        log,
        fdb,
        tenant_id=CNPJ,
        embedding=FakeEmbeddingProvider(),
    )
    # Drop existing data first (clean slate)
    try:
        fdb.graph(CNPJ).query("MATCH (n) DETACH DELETE n")
    except Exception:
        pass
    stats = await projector.project_all()
    print(f"  Agents: {stats['agents']}")
    print(f"  Documents: {stats['documents']}")
    print(f"  Tool calls: {stats['tool_calls']}")
    print(f"  Edges: {stats['edges']}")

    # 4. Verify in the graph
    print()
    print("=" * 70)
    print("GRAPH CONTENTS")
    print("=" * 70)
    g = fdb.graph(CNPJ)
    for label in ("Agent", "Document", "ToolCall"):
        result = g.query(f"MATCH (n:{label}) RETURN count(n) AS n")
        n = result.result_set[0][0]
        print(f"  :{label} = {n}")

    # 5. GraphRAG vector search
    print()
    print("=" * 70)
    print("GRAPHRAG VECTOR SEARCH")
    print("=" * 70)
    embedding = FakeEmbeddingProvider()
    retriever = GraphRAGRetriever(fdb, embedding, tenant_id=CNPJ)
    # Query for "ACME" (semantically, in this hash-based
    # embedding, it just means the query embedding is close to
    # whatever we want).
    results = await retriever.retrieve("ACME fornecedor", k=3)
    if not results:
        print(
            "  (no results — FalkorDB may not support vector "
            "search in this build; the projection still wrote the nodes.)"
        )
    else:
        for r in results:
            print(
                f"  doc_id={r.doc_id} score={r.score:.4f} "
                f"agent_id={r.agent_id} event_type={r.event_type}"
            )

    # 6. Cleanup
    print()
    print("=" * 70)
    print("CLEANUP")
    print("=" * 70)
    try:
        fdb.graph(CNPJ).query("MATCH (n) DETACH DELETE n")
        print(f"  ✓ Dropped graph {graph_name_for_tenant(CNPJ)}")
    except Exception as e:
        print(f"  ! Failed to drop: {e}")
    await redis.flushdb()
    print("  ✓ Flushed Redis db=15")

    await redis.aclose()
    fdb.close()
    print()
    print("OK.")


if __name__ == "__main__":
    asyncio.run(main())
