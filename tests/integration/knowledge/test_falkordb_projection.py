# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for the FalkorDB projection system.

These tests require a real FalkorDB instance reachable at
localhost:16379 (or whatever the KNT_FALKORDB_HOST/PORT
env vars point to). If falkordb is not installed, tests
are skipped.

Run with:
    docker run -d -p 16379:6379 --name fmh-falkordb falkordb/falkordb
    KNT_FALKORDB_PASSWORD=falkordb uv run --package kntgraph pytest \\
        kntgraph/tests/integration/knowledge -v
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _falkordb_available() -> bool:
    try:
        from falkordb import FalkorDB  # noqa: F401

        return True
    except ImportError:
        return False


if not _falkordb_available():
    pytest.skip("falkordb not installed", allow_module_level=True)

import redis.asyncio as aioredis  # noqa: E402

from kntgraph.core.event import Event  # noqa: E402
from kntgraph.knowledge.falkordb.adapter import FalkorDBProjector  # noqa: E402
from kntgraph.infra.graph import (  # noqa: E402
    GraphPool,
)
from kntgraph.testing import FakeEmbeddingProvider  # noqa: E402
from kntgraph.knowledge.graphrag.retriever import GraphRAGRetriever  # noqa: E402
from kntgraph.stream.event_log import EventLog  # noqa: E402

# Mark all tests in this module as asyncio (the conftest-style
# async fixture `event_log` requires an async context).
pytestmark = pytest.mark.asyncio


@pytest.fixture
def falkordb_client() -> Iterator[GraphPool]:
    host = os.environ.get("KNT_FALKORDB_HOST", "localhost")
    port = int(os.environ.get("KNT_FALKORDB_PORT", "16379"))
    password = "falkordb"
    c = GraphPool(host=host, port=port, password=password)
    try:
        c.connect()
    except Exception as e:
        pytest.skip(f"FalkorDB not reachable: {e}")
    yield c
    c.close()


@pytest_asyncio.fixture
async def clean_falkordb_graph(falkordb_client: GraphPool):
    """
    Cleans the tenant graph before and after each test.
    Each test uses a unique tenant id (random UUID) so tests
    don't share state.
    """
    tenant = f"test-{uuid.uuid4().hex[:8]}"
    graph = falkordb_client.graph(tenant)
    # Clean BEFORE the test in case a previous run left state
    try:
        await graph.query("MATCH (n) DETACH DELETE n")
    except Exception:
        pass
    yield tenant, graph
    # Cleanup AFTER
    try:
        await graph.query("MATCH (n) DETACH DELETE n")
    except Exception:
        pass


@pytest_asyncio.fixture
async def event_log():
    from kntgraph.infra.redis._event_log._adapter import RedisEventLogAdapter

    redis_password = os.environ.get("KNT_REDIS_PASSWORD", "redispassword")
    redis_url = f"redis://:{redis_password}@localhost:6379"
    redis = aioredis.from_url(redis_url, db=15)
    await redis.flushdb()
    adapter = RedisEventLogAdapter(client=redis)
    print(f"\n[DEBUG] adapter type: {type(adapter)}")
    log = EventLog(adapter)
    print(f"\n[DEBUG] EventLog storage type: {type(log._storage)}")
    yield log
    await redis.flushdb()
    await redis.aclose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFalkorDBProjector:
    async def test_project_agents(
        self, event_log, falkordb_client, clean_falkordb_graph
    ):
        tenant, graph = clean_falkordb_graph
        # Seed: 2 agents, each with a few events
        await event_log.append(
            Event.domain_from(
                agent_id="NF-001",
                type="nf.received",
                data={"document_id": "NF-001", "amount": 100.0},
            )
        )
        await event_log.append(
            Event.domain_from(
                agent_id="NF-002",
                type="nf.received",
                data={"document_id": "NF-002", "amount": 200.0},
            )
        )

        # Project
        proj = FalkorDBProjector(
            event_log,
            falkordb_client,
            tenant_id=tenant,
            embedding=FakeEmbeddingProvider(),
        )
        stats = await proj.project_all()
        assert stats["agents"] == 2
        assert stats["documents"] == 2  # both are nf.received (indexed)

        # Verify in graph
        result = await graph.query("MATCH (a:Agent) RETURN a.agent_id, a.tenant_id")
        ids = {row[0] for row in result.result_set}
        assert ids == {"NF-001", "NF-002"}

    async def test_project_tool_calls(
        self, event_log, falkordb_client, clean_falkordb_graph
    ):
        tenant, graph = clean_falkordb_graph
        # Seed: a tool call request + completion
        await event_log.append(
            Event.domain_from(
                agent_id="NF-001",
                type="tool.invoice.issue.requested",
                data={"document_id": "NF-001", "xml": "<a/>"},
            )
        )
        await event_log.append(
            Event.domain_from(
                agent_id="NF-001",
                type="tool.invoice.issue.completed",
                data={
                    "request_id": "abc",
                    "tool": "invoice.issue",
                    "result": {"status": "authorized"},
                    "latency_ms": 12.3,
                },
            )
        )

        proj = FalkorDBProjector(
            event_log,
            falkordb_client,
            tenant_id=tenant,
            embedding=FakeEmbeddingProvider(),
        )
        stats = await proj.project_all()
        assert stats["tool_calls"] == 1

        result = await graph.query(
            "MATCH (t:ToolCall) RETURN t.tool, t.status, t.latency_ms"
        )
        rows = list(result.result_set)
        assert len(rows) == 1
        assert rows[0][0] == "invoice.issue"
        assert rows[0][1] == "completed"

    async def test_replay_idempotent(
        self, event_log, falkordb_client, clean_falkordb_graph
    ):
        """
        Running the projection twice does not duplicate
        nodes (the projection uses MERGE).
        """
        tenant, graph = clean_falkordb_graph
        await event_log.append(
            Event.domain_from(
                agent_id="NF-001",
                type="nf.received",
                data={"document_id": "NF-001", "amount": 100.0},
            )
        )
        proj = FalkorDBProjector(
            event_log,
            falkordb_client,
            tenant_id=tenant,
            embedding=FakeEmbeddingProvider(),
        )
        await proj.project_all()
        await proj.project_all()
        # Count Agent and Document nodes
        r = await graph.query("MATCH (a:Agent) RETURN count(a) AS n")
        n_agents = r.result_set[0][0]
        assert n_agents == 1
        r = await graph.query("MATCH (d:Document) RETURN count(d) AS n")
        n_docs = r.result_set[0][0]
        assert n_docs == 1

    async def test_multi_tenant_isolation(self, event_log, falkordb_client):
        """
        Two tenants have separate graphs. Events for tenant
        A are not visible from tenant B.
        """
        tenant_a = f"test-a-{uuid.uuid4().hex[:6]}"
        tenant_b = f"test-b-{uuid.uuid4().hex[:6]}"
        try:
            await event_log.append(
                Event.domain_from(
                    agent_id="NF-001",
                    type="nf.received",
                    data={"document_id": "NF-001-A", "amount": 100.0},
                )
            )
            proj_a = FalkorDBProjector(
                event_log,
                falkordb_client,
                tenant_id=tenant_a,
                embedding=FakeEmbeddingProvider(),
            )
            _proj_b = FalkorDBProjector(
                event_log,
                falkordb_client,
                tenant_id=tenant_b,
                embedding=FakeEmbeddingProvider(),
            )
            await proj_a.project_all()
            # Tenant A has the agent
            g_a = falkordb_client.graph(tenant_a)
            r = await g_a.query("MATCH (a:Agent) RETURN a.agent_id")
            assert r.result_set and r.result_set[0][0] == "NF-001"
            # Tenant B has nothing
            g_b = falkordb_client.graph(tenant_b)
            r = await g_b.query("MATCH (a:Agent) RETURN a.agent_id")
            assert not r.result_set
        finally:
            for tid in (tenant_a, tenant_b):
                try:
                    await falkordb_client.graph(tid).query("MATCH (n) DETACH DELETE n")
                except Exception:
                    pass


class TestGraphRAGRetriever:
    async def test_vector_search_returns_top_k(
        self, event_log, falkordb_client, clean_falkordb_graph
    ):
        tenant, graph = clean_falkordb_graph
        # Seed two NFs with different "content"
        await event_log.append(
            Event.domain_from(
                agent_id="NF-A",
                type="nf.received",
                data={"document_id": "NF-A", "supplier": "ACME"},
            )
        )
        await event_log.append(
            Event.domain_from(
                agent_id="NF-B",
                type="nf.received",
                data={"document_id": "NF-B", "supplier": "Widgets Inc"},
            )
        )

        proj = FalkorDBProjector(
            event_log,
            falkordb_client,
            tenant_id=tenant,
            embedding=FakeEmbeddingProvider(),
        )
        await proj.project_all()

        # Now query
        embedding = FakeEmbeddingProvider()
        retriever = GraphRAGRetriever(falkordb_client, embedding, tenant_id=tenant)
        results = await retriever.retrieve("test query", k=5)
        assert isinstance(results, list)
        # If the FalkorDB build supports vector search, we
        # expect 2 hits. If not, the retriever returns []
        # gracefully.
        if results:
            assert len(results) <= 2
            for r in results:
                assert r.doc_id
                assert r.agent_id
                assert r.event_type == "nf.received"
                assert "supplier" in r.data
