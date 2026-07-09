# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 17 — GraphRAG Retrieval Modes (Docs).

Demonstrates the 3 main capabilities of `GraphRAGRetriever`
as documented in `docs/graphrag.md`:
  1. vector_search (Documents via cosine similarity)
  2. find_solutions_by_problem (Solutions via problem + tags)
  3. find_solutions_by_tool (Structural retrieval by tool)

Unlike examples 08 and 09 (which use projectors and event logs),
this script injects mock nodes directly into FalkorDB via Cypher
to keep the full focus on the Retrieval API.

Prerequisites:
  - FalkorDB running (`docker run -d -p 16379:6379 -e FALKORDB_PASSWORD=falkordb falkordb/falkordb:latest`)

Run:
    uv run --package kntgraph python examples/17_graphrag_retrieval.py
"""

import asyncio
import os

from kntgraph.infra.graph import GraphPool
from kntgraph.knowledge.embedding.provider import EmbeddingClient
from kntgraph.knowledge.graphrag.retriever import GraphRAGRetriever

TENANT = "demo-graphrag-tenant"


async def _mock_embedding(text: str) -> list[float]:
    """Returns a fixed vector to mock text embedding."""
    provider = EmbeddingClient()
    return asyncio.run(provider.embed(text))


async def setup_mock_data(fdb: GraphPool, tenant: str, embedding: EmbeddingClient):
    """Injects synthetic nodes into the graph to mock both approaches (Documents and Solutions)."""
    print("-> Populating mock graph...")
    g = fdb.graph(tenant)

    try:
        await g.query("MATCH (n) DETACH DELETE n")
    except Exception:
        pass

    # Create vector index if possible
    try:
        await g.query(
            "CREATE VECTOR INDEX FOR (d:Document) ON (d.embedding) "
            "OPTIONS {dim: 768, similarityFunction: 'cosine'}"
        )
        await g.query(
            "CREATE VECTOR INDEX FOR (p:Problem) ON (p.embedding) "
            "OPTIONS {dim: 768, similarityFunction: 'cosine'}"
        )
    except Exception:
        pass  # Ignore if unsupported or already exists

    # 1. Mock Documents Sub-graph
    emb_doc = await embedding.embed("test fiscal document")
    await g.query(
        """
        CREATE (d:Document {
            id: 'doc-001',
            agent_id: 'agent-99',
            event_type: 'nf.received',
            data_json: '{"type": "NF-e", "value": 500}',
            embedding: vecf32($emb)
        })
        """,
        params={"emb": emb_doc},
    )

    # 2. Mock Solutions Sub-graph
    emb_prob = await embedding.embed("generate invoice for SP client")
    await g.query(
        """
        CREATE (p:Problem {
            fingerprint: 'prob-hash-123',
            tags_json: '{"uf": "SP", "type": "service"}',
            embedding: vecf32($emb)
        })
        CREATE (a:Action {
            request_event_id: 'evt-456',
            params_fingerprint: 'param-hash-789',
            params_json: '{"cfop": "5405", "redacted_cnpj": "***"}'
        })
        CREATE (t:Tool {
            name: 'invoice.issue',
            description: 'Issues fiscal notes',
            input_schema_json: '{}'
        })
        CREATE (o:Outcome {
            status: 'completed',
            latency_ms: 125.0,
            result_signature: 'ok'
        })
        
        CREATE (p)-[:SOLVED_BY {confidence: 3, validated_count: 5}]->(a)
        CREATE (a)-[:ON_TOOL]->(t)
        CREATE (a)-[:PRODUCED]->(o)
        """,
        params={"emb": emb_prob},
    )


async def main():
    print("=" * 60)
    print("GraphRAG API Example")
    print("=" * 60)

    # 1. Connection
    fdb = GraphPool(
        host=os.environ.get("KNT_FALKORDB_HOST", "localhost"),
        port=int(os.environ.get("KNT_FALKORDB_PORT", "16379")),
        password=os.environ.get("KNT_FALKORDB_PASSWORD", "falkordb"),
    )
    fdb.connect()
    embedding = EmbeddingClient()

    await setup_mock_data(fdb, TENANT, embedding)

    # Initialize Retriever
    retriever = GraphRAGRetriever(fdb, embedding, tenant_id=TENANT)

    # -------------------------------------------------------------
    # MODE 1: vector_search (Documents)
    # -------------------------------------------------------------
    print("\n--- MODE 1: vector_search ---")
    query_doc = await embedding.embed("test fiscal document")
    doc_results = await retriever.vector_search(query_doc, k=2)

    if doc_results:
        for r in doc_results:
            print(f"Doc Found: {r.doc_id}")
            print(f"  Score: {r.score:.4f}")
            print(f"  Payload: {r.data}")
    else:
        print("(No vector results supported by local FalkorDB)")

    # -------------------------------------------------------------
    # MODE 2: find_solutions_by_problem (Solutions via Similarity + Tags)
    # -------------------------------------------------------------
    print("\n--- MODE 2: find_solutions_by_problem ---")
    # Emulate a query similar to the problem we created
    query_prob = await embedding.embed("generate invoice for SP client")

    # Example: Find solutions where UF = SP
    sol_results = await retriever.find_solutions_by_problem(
        query_prob, tags={"uf": "SP"}, tool_name="invoice.issue", k=2
    )

    for r in sol_results:
        print(f"Tool: {r.tool_name}")
        print(f"  Suggested Action (Example): {r.action_params_example}")
        print(f"  Vector Score: {r.score:.4f}")
        print(f"  Historical Confidence: {r.confidence}")

    # -------------------------------------------------------------
    # MODE 3: find_solutions_by_tool (Pure Structural Retrieval)
    # -------------------------------------------------------------
    print("\n--- MODE 3: find_solutions_by_tool ---")
    # Example: Fetch all action history for the 'invoice.issue' tool
    tool_results = await retriever.find_solutions_by_tool(
        "invoice.issue", status="completed", k=5
    )

    for r in tool_results:
        print(f"Tool History: {r.tool_name}")
        print(f"  Status: {r.outcome_status}")
        print(f"  Example of parameters that worked: {r.action_params_example}")

    # Cleanup
    print("\nCleaning up environment...")
    try:
        await fdb.graph(TENANT).query("MATCH (n) DETACH DELETE n")
    except Exception:
        pass

    fdb.close()
    print("OK.")


if __name__ == "__main__":
    asyncio.run(main())
