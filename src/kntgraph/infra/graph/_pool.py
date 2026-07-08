# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
infra.graph._pool -- ``GraphPool`` facade (composition).

``GraphPool`` owns the lifecycle of a single graph
database connection per process. It composes a
``GraphAdapter`` (typically ``FalkorDBGraphAdapter``)
and exposes a ``graph(tenant_id)`` method that returns
the adapter bound to a tenant-scoped view.

Why a facade:

  - Sub-adapters (``GraphAgentAdapter``,
    ``GraphDocumentAdapter`` ...) need a
    ``GraphAdapter`` to compose. The facade is the
    single source of truth for "which tenant's graph am
    I querying?".
  - Connection management (open/close/reconnect) lives
    here, not in the adapter. The adapter is stateless
    beyond the ``AsyncGraph`` it wraps.

Iter 10 (ADR-019 epílogo) — async-only. The facade does
NOT inspect ``iscoroutinefunction`` at call time; the
``AsyncGraph`` is async by construction.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

import structlog

from kntgraph.knowledge.graph._protocol import GraphAdapter
from ._adapter import FalkorDBGraphAdapter


if TYPE_CHECKING:
    from falkordb.asyncio import FalkorDB


logger = structlog.get_logger()


# Tenant graph name convention. CNPJ may have characters
# that are not valid in graph names; we sanitise.
GRAPH_NAME_PREFIX = "fmh_tenant_"


def graph_name_for_tenant(tenant_id: str) -> str:
    """
    Returns the graph name for a tenant id.

    CNPJ-style strings ("12.345.678/0001-90") contain
    characters that graph names may not allow; we replace
    non-alphanumeric with underscores.
    """
    safe = "".join(c if c.isalnum() else "_" for c in tenant_id)
    return f"{GRAPH_NAME_PREFIX}{safe}"


class GraphPool:
    """
    Multi-tenant graph database connection pool.

    Holds a single underlying connection (one per process
    is typical) and exposes a per-tenant ``GraphAdapter``.

    Parameters
    ----------
    host:
        FalkorDB host. Defaults to ``"localhost"``.
    port:
        FalkorDB port. Defaults to ``16379``.
    password:
        Explicit password. If ``None``, the client reads
        ``Settings.falkordb_password`` (or the
        ``FMH_FALKORDB_PASSWORD`` env var as a fallback).
        An unset password opens an unauthenticated
        connection — explicit operator decision.

    The client is lazy: it does NOT open the connection
    on construction. ``connect()`` is called on first
    ``graph()`` access. Tests that never call ``graph()``
    do not require ``falkordb`` to be importable.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 16379,
        *,
        password: Optional[str] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._db: Optional["FalkorDB"] = None

    def connect(self) -> None:
        """
        Establish the underlying connection. Idempotent.

        The ``falkordb`` import is local so a process that
        never calls ``connect()`` does NOT require the
        package to be installed. Tests can stub the import
        without monkey-patching at module level.
        """
        if self._db is not None:
            return
        try:
            from falkordb.asyncio import FalkorDB
        except ImportError as e:
            raise ImportError(
                "falkordb is not installed. Run `uv pip install falkordb` "
                "or skip this step. FalkorDB is an OPTIONAL projection "
                "in FMH (ADR-004); the EventLog is the source of truth."
            ) from e
        resolved_password = self._resolve_password()
        if resolved_password is not None:
            self._db = FalkorDB(
                host=self._host,
                port=self._port,
                password=resolved_password,
            )
        else:
            self._db = FalkorDB(host=self._host, port=self._port)

    def _resolve_password(self) -> Optional[str]:
        """
        Resolve the password used at ``connect()`` time.

        Resolution order:
          1. Explicit ``password`` passed to ``__init__``.
          2. ``Settings.falkordb_password``.
          3. ``FMH_FALKORDB_PASSWORD`` env var.
        """
        if self._password is not None:
            return self._password
        try:
            from kntgraph.infra.config import settings

            if settings.falkordb_password:
                return settings.falkordb_password
        except Exception:
            # ``settings`` may not be importable in some
            # test or embed scenarios (e.g. when this
            # module is loaded without the rest of the
            # framework). Fall through to the env var.
            logger.debug(
                "graph_pool.password.settings_unavailable",
                exc_info=True,
            )
        env_pw = os.environ.get("FMH_FALKORDB_PASSWORD")
        return env_pw if env_pw else None

    def graph(self, tenant_id: str) -> GraphAdapter:
        """
        Returns the ``GraphAdapter`` for the given tenant.

        The returned adapter is bound to a tenant-scoped
        ``AsyncGraph`` (FalkorDB 1.6+). The caller is
        responsible for Cypher queries.

        Returns ``FalkorDBGraphAdapter`` (the only
        concrete adapter shipped today). Future backends
        (Neo4j, Memgraph) would add new factory methods
        rather than branch on tenant_id.
        """
        if self._db is None:
            self.connect()
        async_graph = self._db.select_graph(graph_name_for_tenant(tenant_id))
        return FalkorDBGraphAdapter(async_graph)

    def close(self) -> None:
        """Closes the connection. Idempotent."""
        self._db = None


__all__ = ["GRAPH_NAME_PREFIX", "GraphPool", "graph_name_for_tenant"]
