# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``GraphPool`` (no real connection).

The client is async-only and lazy: ``connect()`` is
called on first ``graph()`` access. The tests block the
``falkordb`` import to simulate an environment where
the package is not installed; this confirms the lazy
pattern is honoured.
"""

from __future__ import annotations

import pytest

from kntgraph.infra.graph._pool import (
    GRAPH_NAME_PREFIX,
    GraphPool,
    graph_name_for_tenant,
)


class TestGraphNameForTenant:
    def test_simple_id(self):
        assert graph_name_for_tenant("abc") == f"{GRAPH_NAME_PREFIX}abc"

    def test_cnpj_with_separators(self):
        n = graph_name_for_tenant("12.345.678/0001-90")
        assert n == f"{GRAPH_NAME_PREFIX}12_345_678_0001_90"

    def test_alphanumeric_preserved(self):
        n = graph_name_for_tenant("T-1_a")
        assert n == f"{GRAPH_NAME_PREFIX}T_1_a"


class TestGraphPool:
    def test_init_does_not_connect(self):
        c = GraphPool(host="localhost", port=6379)
        assert c is not None
        assert c._db is None

    def test_connect_raises_without_falkordb(self, monkeypatch):
        """``connect()`` does ``from falkordb import FalkorDB``
        inside the function body. We block that import to
        simulate the env where falkordb is not installed.
        """
        import builtins

        original_import = builtins.__import__

        def blocked_import(name, *args, **kwargs):
            if name == "falkordb" or name.startswith("falkordb."):
                raise ImportError("No module named 'falkordb'")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        c = GraphPool()
        with pytest.raises(ImportError):
            c.connect()

    def test_close_clears_connection(self):
        c = GraphPool()
        c._db = object()  # simulate a connected state
        c.close()
        assert c._db is None

    def test_graph_returns_graph_adapter(self):
        """A connected client returns a ``GraphAdapter``
        for the requested tenant. We simulate the
        connection by stubbing the ``_db`` attribute.
        """
        c = GraphPool()

        # Stub the underlying AsyncGraph so we don't need
        # a real FalkorDB.
        class _StubAsyncGraph:
            async def query(self, cypher, params=None):
                return None

        class _StubFalkorDB:
            def select_graph(self, name):
                return _StubAsyncGraph()

        c._db = _StubFalkorDB()
        adapter = c.graph("tenant-A")
        from kntgraph.knowledge.graph._protocol import (
            GraphAdapter,
        )

        assert isinstance(adapter, GraphAdapter)

    def test_graph_lazy_connects(self, monkeypatch):
        """``graph()`` must call ``connect()`` if the
        connection is not yet established.
        """
        c = GraphPool()
        # Track whether connect was called.
        connect_calls = {"count": 0}
        _original_connect = c.connect

        def _spy_connect():
            connect_calls["count"] += 1

            # Simulate a successful connect by stubbing
            # the underlying connection.
            class _StubAsyncGraph:
                async def query(self, cypher, params=None):
                    return None

            class _StubFalkorDB:
                def select_graph(self, name):
                    return _StubAsyncGraph()

            c._db = _StubFalkorDB()

        monkeypatch.setattr(c, "connect", _spy_connect)
        # Connection is None at this point.
        assert c._db is None
        c.graph("tenant-A")
        assert connect_calls["count"] == 1
