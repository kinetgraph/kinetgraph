# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
LiteGraphPool — dev-only drop-in adapter over
``falkordblite`` (``redislite.falkordb_client.FalkorDB``).

The ``falkordblite`` package (https://docs.falkordb.com/
operations/falkordblite/falkordblite-py.html) ships an
embedded Redis server plus the FalkorDB module as a
single Python wheel. This adapter exposes the same
public surface as ``GraphPool`` (``connect``,
``graph``, ``close``) so the framework's graph
projectors can run in-process — useful for demos,
CI, and dev work without spinning up a Docker
container.

Iter 28 follow-up 2: replaces the legacy
``LiteFalkorDBClient`` (in
``kntgraph.infra.falkordblite_adapter``). The
legacy client returned a sync handle; this new
client returns ``GraphAdapter`` (the framework's
current shape), matching ``GraphPool``'s public
surface.

Public surface
--------------

  - ``LiteGraphPool(db_path=None)`` — same
    constructor as the legacy client (no host/port/
    password; those are Docker-only).
  - ``connect()`` — start the embedded server.
    Idempotent.
  - ``graph(tenant_id) -> GraphAdapter`` — return the
    per-tenant ``GraphAdapter`` (the framework's
    current shape). The adapter runs the underlying
    sync query in a worker thread (``asyncio.to_thread``)
    so the event loop stays responsive.
  - ``close()`` — stop the embedded server.
    Idempotent.

Caveats
-------

- One client per process is the supported pattern.
- Persistence is opt-in via ``db_path``; pass
  ``db_path=None`` for an ephemeral database.
- Not for production. The official ``falkordb``
  Docker image is the supported deployment target.
- The ``redislite`` package is a dev-only dep; not
  available in production deployments. The framework
  does not require it; the adapter is opt-in.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Sequence
from typing import TYPE_CHECKING, Optional, Protocol

import structlog

from kntgraph.knowledge.graph._protocol import (
    GraphAdapter,
    GraphError,
    GraphQueryResult,
)
from kntgraph.infra.graph._pool import (
    graph_name_for_tenant,
)


if TYPE_CHECKING:
    from redislite.falkordb_client import FalkorDB


# The concrete FalkorDB type is bound under
# TYPE_CHECKING (so we don't require ``redislite`` at
# import time). The lazy import inside ``connect()``
# rebinds ``_FalkorDB`` at runtime; the ``patch``
# decorator in tests targets ``_FalkorDB`` directly.
# We expose it as a module-level name so ``patch`` can
# reach it.
_FalkorDB = None


class _FalkorDBQueryResult(Protocol):
    result_set: Sequence[Sequence[object]]
    headers: Sequence[str] | None


class _FalkorDBGraphLike(Protocol):
    def query(
        self,
        cypher: str,
        params: Optional[dict[str, object]],
    ) -> _FalkorDBQueryResult: ...


logger = structlog.get_logger()


__all__ = ["LiteGraphPool", "LiteGraphAdapter"]


class LiteGraphAdapter(GraphAdapter):
    """
    ``GraphAdapter`` for the dev-only ``falkordblite``
    backend.

    Wraps a sync ``falkordblite`` graph (which has no
    async API) and exposes an async ``query`` method
    that runs the underlying sync call in a worker
    thread (``asyncio.to_thread``). This keeps the
    event loop responsive and matches the
    ``GraphAdapter`` Protocol's ``async def query``
    contract.

    Iter 28 follow-up 2: this is the canonical
    dev-only ``GraphAdapter`` impl. Replaces the
    legacy ``LiteFalkorDBClient`` (deleted in the
    same iter).
    """

    def __init__(self, graph: _FalkorDBGraphLike) -> None:
        # The graph is a ``falkordblite`` graph handle.
        # The framework only ever calls ``.query(cypher,
        # params)`` on it via the worker thread.
        self._graph = graph

    async def query(
        self,
        cypher: str,
        *,
        params: Optional[dict[str, object]] = None,
    ) -> GraphQueryResult:
        """
        Execute a Cypher query against the wrapped
        graph.

        Iter 28 follow-up 2: ``falkordblite`` is sync;
        we offload to ``asyncio.to_thread`` to keep the
        event loop responsive. The native result is
        converted to ``GraphQueryResult`` (tuple of
        tuples + tuple of headers) at the adapter
        boundary. Native exceptions are wrapped in
        ``GraphError`` per AGENTS.md §6.
        """
        try:
            native = await asyncio.to_thread(self._run_query, cypher, params)
        except GraphError:
            raise
        except Exception as e:
            raise GraphError(
                f"falkordblite query failed: {e!r}",
                kind="query_failed",
                cause=e,
            ) from e
        return self._to_graph_query_result(native)

    def _run_query(
        self, cypher: str, params: Optional[dict[str, object]]
    ) -> _FalkorDBQueryResult:
        """Sync helper run in a worker thread.

        ``falkordblite``'s ``query`` accepts a
        positional ``params`` argument; the framework
        passes it via the ``_run_query`` indirection.
        """
        return self._graph.query(cypher, params)

    @staticmethod
    def _to_graph_query_result(native: _FalkorDBQueryResult) -> GraphQueryResult:
        """Convert the native ``falkordblite`` result
        (a ``QueryResult``-like object) to the
        framework's ``GraphQueryResult``.

        The native shape is:
          - ``result_set``: list of tuples (rows)
          - ``headers``: list of column names (or empty)

        The framework shape freezes both to ``tuple``
        for safety (immutable, shareable across
        coroutines without copy).
        """
        result_set = getattr(native, "result_set", None) or ()
        headers = getattr(native, "headers", None) or ()
        return GraphQueryResult(
            result_set=tuple(tuple(row) for row in result_set),
            headers=tuple(headers),
        )


class LiteGraphPool:
    """
    In-process graph database client backed by
    ``falkordblite`` (dev-only).

    Construction does NOT start the embedded server
    (matches the ``GraphPool`` contract). The first
    call to ``graph()`` (or an explicit ``connect()``)
    starts it.

    Iter 28 follow-up 2: replaces the legacy
    ``LiteFalkorDBClient``. Same constructor, same
    public surface (``connect`` / ``graph`` / ``close``).
    The difference: ``graph(tenant_id)`` returns a
    ``GraphAdapter`` (the framework's current shape).

    Parameters
    ----------

    db_path:
      Filesystem path for the embedded database file.
      Pass ``None`` for an ephemeral database under
      ``tempfile.gettempdir()``. The directory is
      created if missing.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or os.path.join(
            tempfile.gettempdir(), "fmh-falkordblite.db"
        )
        # The concrete object is
        # ``redislite.falkordb_client.FalkorDB``
        # (declared under ``TYPE_CHECKING``). The
        # framework only ever calls ``.select_graph``
        # on it (via :meth:`graph`), so the type is
        # bound via TYPE_CHECKING. At runtime the
        # annotation is a string so we don't need to
        # import ``redislite`` (dev-only dependency).
        self._db: Optional["FalkorDB"] = None

    def connect(self) -> None:
        """Start the embedded Redis+FalkorDB process.
        Idempotent.

        The ``redislite`` import is local so a process
        that never calls ``connect()`` does NOT require
        the package to be installed. Tests can stub
        the import without monkey-patching at module
        level (the framework looks up ``_FalkorDB``
        module-global first; if it's set, the import
        is skipped).
        """
        if self._db is not None:
            return
        global _FalkorDB
        if _FalkorDB is None:
            try:
                from redislite.falkordb_client import (
                    FalkorDB as _FalkorDB_class,
                )
            except ImportError as e:
                raise ImportError(
                    "falkordblite is not installed. Run "
                    "`uv sync --extra dev` (it is declared as a [dev] "
                    "extra in kntgraph/pyproject.toml) or fall back "
                    "to the Docker-based FalkorDB."
                ) from e
            _FalkorDB = _FalkorDB_class
        self._db = _FalkorDB(self._db_path)
        logger.info(
            "falkordblite.connected",
            db_path=self._db_path,
        )

    def graph(self, tenant_id: str) -> GraphAdapter:
        """
        Returns the ``GraphAdapter`` for the given
        tenant. Same contract as ``GraphPool.graph``.

        Iter 28 follow-up 2: returns a
        ``GraphAdapter`` (the framework's current
        shape). The adapter runs the underlying sync
        ``falkordblite`` query in a worker thread.
        """
        if self._db is None:
            self.connect()
        sync_graph = self._db.select_graph(graph_name_for_tenant(tenant_id))
        return LiteGraphAdapter(sync_graph)

    def close(self) -> None:
        """
        Close the embedded server. Idempotent. The
        OS-level process is terminated by
        ``falkordblite``; on-disk files are preserved
        unless ``db_path`` was ``None`` (in which case
        they live in ``tempfile.gettempdir()`` and may
        be cleaned up by the OS on reboot).
        """
        if self._db is None:
            return
        try:
            self._db.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            # The falkordblite shim may emit a warning
            # about an unauthenticated shutdown coroutine.
            # Swallow -- the underlying server has already
            # torn down. We log at DEBUG for diagnostics.
            logger.debug("falkordblite.close.swallowed", exc_info=True)
        self._db = None
        logger.info("falkordblite.closed", db_path=self._db_path)
