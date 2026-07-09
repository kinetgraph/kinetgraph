# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``LiteGraphPool`` (Iter 28 follow-up 2).

``LiteGraphPool`` is the dev-only replacement for
``LiteFalkorDBClient`` — same purpose (in-process
FalkorDB for demos, CI, and dev), but uses the
``GraphPool`` / ``GraphAdapter`` architecture
introduced in Iter 24.

Why this exists:
  - ``LiteFalkorDBClient`` returned a sync handle
    shape (deprecated in Iter 24).
  - Iter 24's ADR said a follow-up would close this
    gap; this is that follow-up.
  - The new client uses the same dev-only stack
    (``redislite.falkordb_client.FalkorDB``) but
    returns a ``GraphAdapter`` (async, the
    framework's current shape).

The tests use mocks for ``redislite`` and
``falkordblite`` (dev-only deps that may not be
installed in CI). The actual integration is
exercised manually in dev environments.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class TestLiteGraphPool:
    """The ``LiteGraphPool`` mirrors ``GraphPool``
    (production) but uses ``falkordblite`` for
    in-process testing."""

    def test_default_db_path_is_tempfile(self) -> None:
        """No ``db_path`` -> ephemeral database under
        ``tempfile.gettempdir()``."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )

        client = LiteGraphPool()
        assert client._db_path.startswith("/tmp")  # noqa: SLF001
        assert client._db_path.endswith(".db")  # noqa: SLF001

    def test_explicit_db_path_preserved(self) -> None:
        """``db_path`` is forwarded to the underlying
        ``FalkorDB`` constructor."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )

        client = LiteGraphPool(db_path="/tmp/explicit.db")
        assert client._db_path == "/tmp/explicit.db"  # noqa: SLF001

    def test_connect_is_lazy(self) -> None:
        """Construction does NOT start the embedded
        server. The first call to ``graph()`` (or
        explicit ``connect()``) starts it."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )

        with patch("kntgraph.infra.graph._lite_pool._FalkorDB") as _FalkorDB:
            client = LiteGraphPool()
            _FalkorDB.assert_not_called()
            client.connect()
            _FalkorDB.assert_called_once()

    def test_connect_is_idempotent(self) -> None:
        """Calling ``connect()`` twice does NOT start
        the embedded server twice."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )

        with patch("kntgraph.infra.graph._lite_pool._FalkorDB") as _FalkorDB:
            client = LiteGraphPool()
            client.connect()
            client.connect()
            _FalkorDB.assert_called_once()

    def test_connect_import_error_raises_clear_message(
        self,
    ) -> None:
        """When ``redislite`` is not installed,
        ``connect()`` raises a clear ``ImportError``
        pointing at the extra."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )

        with patch.dict("sys.modules", {"redislite.falkordb_client": None}):
            client = LiteGraphPool()
            with pytest.raises(ImportError, match="falkordblite"):
                client.connect()

    def test_graph_returns_graph_adapter(self) -> None:
        """``graph(tenant_id)`` returns a
        ``GraphAdapter`` (the framework's current
        shape)."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )
        from kntgraph.knowledge.graph import (
            GraphAdapter,
        )

        # Mock the underlying FalkorDB so the test
        # does NOT require falkordblite to be
        # installed.
        mock_falkordb = MagicMock()
        mock_graph = MagicMock()
        mock_falkordb.return_value.select_graph.return_value = mock_graph
        with patch(
            "kntgraph.infra.graph._lite_pool._FalkorDB",
            mock_falkordb,
        ):
            client = LiteGraphPool(db_path="/tmp/test.db")
            adapter = client.graph("tenant-1")
        assert isinstance(adapter, GraphAdapter)
        # The adapter wraps the per-tenant graph.
        mock_falkordb.return_value.select_graph.assert_called_once_with(
            "fmh_tenant_tenant_1"
        )

    def test_graph_triggers_connect_if_not_open(self) -> None:
        """If the client has not been connected,
        ``graph()`` opens the connection implicitly."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )

        mock_falkordb = MagicMock()
        mock_falkordb.return_value.select_graph.return_value = MagicMock()
        with patch(
            "kntgraph.infra.graph._lite_pool._FalkorDB",
            mock_falkordb,
        ):
            client = LiteGraphPool()
            assert client._db is None  # noqa: SLF001
            _ = client.graph("t")
            mock_falkordb.assert_called_once()

    def test_graph_returns_same_adapter_for_same_tenant(
        self,
    ) -> None:
        """Two calls to ``graph(tenant)`` return
        adapters that wrap the same per-tenant
        graph (the underlying ``select_graph`` is
        cached by FalkorDB itself)."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )

        mock_falkordb = MagicMock()
        mock_falkordb.return_value.select_graph.return_value = MagicMock()
        with patch(
            "kntgraph.infra.graph._lite_pool._FalkorDB",
            mock_falkordb,
        ):
            client = LiteGraphPool()
            a1 = client.graph("t")
            a2 = client.graph("t")
        # Same wrapped graph (the mock's return
        # value is the same object).
        assert a1._graph is a2._graph  # noqa: SLF001

    def test_close_idempotent(self) -> None:
        """``close()`` is a no-op when not connected."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )

        client = LiteGraphPool()
        # Should not raise.
        client.close()
        assert client._db is None  # noqa: SLF001

    def test_close_terminates_embedded_server(self) -> None:
        """``close()`` calls ``FalkorDB.close()``."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )

        mock_falkordb = MagicMock()
        mock_falkordb.return_value.select_graph.return_value = MagicMock()
        with patch(
            "kntgraph.infra.graph._lite_pool._FalkorDB",
            mock_falkordb,
        ):
            client = LiteGraphPool()
            client.connect()
            client.close()
        mock_falkordb.return_value.close.assert_called_once()
        assert client._db is None  # noqa: SLF001


class TestLiteGraphAdapterQuery:
    """The ``LiteGraphAdapter`` returned by
    ``graph()`` is a real ``GraphAdapter`` (async)
    that wraps the sync ``falkordblite`` query via
    ``asyncio.to_thread``."""

    @pytest.mark.asyncio
    async def test_query_calls_underlying_query_via_to_thread(
        self,
    ) -> None:
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )

        # Mock the underlying falkordb graph.
        mock_graph = MagicMock()
        # ``falkordblite`` returns a QueryResult-like
        # object (we don't depend on the concrete
        # type).
        mock_result = MagicMock()
        mock_result.result_set = [("row1",), ("row2",)]
        mock_result.headers = ["h1"]
        mock_graph.query.return_value = mock_result

        mock_falkordb = MagicMock()
        mock_falkordb.return_value.select_graph.return_value = mock_graph
        with patch(
            "kntgraph.infra.graph._lite_pool._FalkorDB",
            mock_falkordb,
        ):
            client = LiteGraphPool()
            adapter = client.graph("t")
            result = await adapter.query("MATCH (n) RETURN n", params={"k": "v"})

        # The underlying query was called with the
        # params dict (positional).
        mock_graph.query.assert_called_once_with("MATCH (n) RETURN n", {"k": "v"})
        # The result was adapted to GraphQueryResult.
        assert result.result_set == (("row1",), ("row2",))
        assert result.headers == ("h1",)

    @pytest.mark.asyncio
    async def test_query_runs_in_thread(self) -> None:
        """``query()`` is async; the sync ``falkordblite``
        call is offloaded to a worker thread so the
        event loop stays responsive."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )

        # We can detect "called from event loop" by
        # raising a specific RuntimeError inside the
        # sync function if it's still on the loop.
        def _block_loop(*args: Any, **kwargs: Any) -> Any:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            result = MagicMock()
            result.result_set = []
            result.headers = []
            return result

        mock_graph = MagicMock()
        mock_graph.query.side_effect = _block_loop

        mock_falkordb = MagicMock()
        mock_falkordb.return_value.select_graph.return_value = mock_graph
        with patch(
            "kntgraph.infra.graph._lite_pool._FalkorDB",
            mock_falkordb,
        ):
            client = LiteGraphPool()
            adapter = client.graph("t")
            # This must NOT raise; the query is offloaded.
            result = await adapter.query("MATCH (n) RETURN n")
        assert result.result_set == ()
        mock_graph.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_query_wraps_native_exceptions_in_graph_error(
        self,
    ) -> None:
        """Native errors from falkordblite are wrapped
        in ``GraphError`` (per AGENTS.md §6 — concrete
        error types)."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )
        from kntgraph.knowledge.graph import GraphError

        mock_graph = MagicMock()
        mock_graph.query.side_effect = RuntimeError("boom")

        mock_falkordb = MagicMock()
        mock_falkordb.return_value.select_graph.return_value = mock_graph
        with patch(
            "kntgraph.infra.graph._lite_pool._FalkorDB",
            mock_falkordb,
        ):
            client = LiteGraphPool()
            adapter = client.graph("t")
            with pytest.raises(GraphError) as exc_info:
                await adapter.query("MATCH (n) RETURN n")
        assert "boom" in str(exc_info.value)


class TestLiteGraphPoolPublicSurface:
    """The public surface of ``LiteGraphPool`` matches
    ``GraphPool``'s public surface (minus the
    production-only params like ``host``, ``port``,
    ``password``)."""

    def test_public_methods(self) -> None:
        """The class exposes ``connect``, ``graph``,
        ``close`` — same as ``GraphPool``."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )

        for name in ("connect", "graph", "close"):
            assert hasattr(LiteGraphPool, name)
            assert callable(getattr(LiteGraphPool, name))

    def test_init_signature(self) -> None:
        """``__init__`` accepts only ``db_path`` (no
        host/port/password — those are Docker-only)."""
        import inspect
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphPool,
        )

        sig = inspect.signature(LiteGraphPool.__init__)
        params = list(sig.parameters)
        assert params == ["self", "db_path"]
        assert sig.parameters["db_path"].default is None
