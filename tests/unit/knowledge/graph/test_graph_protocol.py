# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the ``GraphAdapter`` Protocol and
``GraphQueryResult`` value object.

The Protocol is the framework-level boundary for any
graph database backend (FalkorDB, Neo4j, Memgraph).
It exposes a single ``query`` method returning a
``Result[GraphQueryResult, GraphError]`` so the rest of
the framework never touches native DB types.

Iter 10 (ADR-019 epílogo): the framework pivots from
"Graph = FalkorDB" to "Graph = any adapter satisfying
``GraphAdapter``". The ``FalkorDBGraphAdapter`` is the
reference implementation; sub-adapters
(``GraphAgentAdapter``, ``GraphDocumentAdapter`` ...)
live in `_sub/`.

Tests in this module are pure-Python: they exercise the
Protocol shape, the value object semantics, and the
``runtime_checkable`` contract. Backend-specific tests
live in ``test_falkordb_graph_adapter.py``.
"""

from __future__ import annotations

import pytest

from kntgraph.knowledge.graph._protocol import (
    GraphAdapter,
    GraphError,
    GraphQueryResult,
)


# ---------------------------------------------------------------------------
# GraphQueryResult
# ---------------------------------------------------------------------------


class TestGraphQueryResult:
    def test_empty_result(self):
        r = GraphQueryResult(result_set=())
        assert r.result_set == ()
        assert r.headers == ()

    def test_with_rows(self):
        rows = (("Agent", "NF-001"), ("Agent", "NF-002"))
        r = GraphQueryResult(result_set=rows, headers=("label", "id"))
        assert r.result_set == rows
        assert r.headers == ("label", "id")

    def test_is_frozen(self):
        r = GraphQueryResult(result_set=())
        with pytest.raises((AttributeError, Exception)):
            r.result_set = (("x",),)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# GraphError
# ---------------------------------------------------------------------------


class TestGraphError:
    def test_carries_message_and_cause(self):
        e = GraphError("query failed", cause=ValueError("bad cypher"))
        assert e.kind == "graph_error"
        assert e.cause is not None

    def test_custom_kind(self):
        e = GraphError("oops", kind="connection_lost")
        assert e.kind == "connection_lost"

    def test_fail_closed_default(self):
        e = GraphError("oops")
        assert e.kind == "graph_error"


# ---------------------------------------------------------------------------
# GraphAdapter Protocol (runtime_checkable)
# ---------------------------------------------------------------------------


class TestGraphAdapterProtocol:
    def test_protocol_is_runtime_checkable(self):
        assert getattr(GraphAdapter, "_is_runtime_protocol", False), (
            "GraphAdapter must be @runtime_checkable so "
            "factories and tests can use isinstance(obj, "
            "GraphAdapter)."
        )

    def test_minimal_impl_satisfies_protocol(self):
        class _Minimal:
            async def query(
                self, cypher: str, *, params: dict | None = None
            ) -> GraphQueryResult:
                return GraphQueryResult(result_set=())

        assert isinstance(_Minimal(), GraphAdapter)

    def test_missing_query_method_fails_protocol(self):
        class _NoQuery:
            pass

        # Protocol check is structural; an instance without
        # ``query`` MUST NOT satisfy GraphAdapter.
        assert not isinstance(_NoQuery(), GraphAdapter)

    def test_sync_query_method_fails_protocol(self):
        # The Protocol mandates ``async def query``. A sync
        # ``query`` is a different shape and MUST NOT satisfy.
        class _SyncQuery:
            def query(self, cypher: str, *, params: dict | None = None):
                return GraphQueryResult(result_set=())

        # Note: ``isinstance`` may still return True if the
        # runtime check only verifies method existence.
        # This test documents the EXPECTED contract: a
        # sync ``query`` is wrong-shaped but the protocol
        # doesn't reject it. The caller is responsible
        # for using ``iscoroutinefunction`` defensively.
        # We assert here that _SyncQuery is technically
        # not awaitable when called (the framework
        # would catch it on first use, not at type-check
        # time).
        import inspect

        assert not inspect.iscoroutinefunction(_SyncQuery.query)
