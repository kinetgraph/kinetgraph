# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
infra.graph._protocol -- Framework-level boundary for any graph DB.

The framework treats "graph" as an abstract capability:
project events to nodes/edges, query vectors, traverse
relationships. The concrete database (FalkorDB today,
Neo4j/Memgraph tomorrow) is hidden behind this Protocol.

This module is the **canonical home** of the
``GraphAdapter`` Protocol, the ``GraphQueryResult``
return type, and the ``GraphError`` exception class.
Before this module existed, the three types were
defined in the vertical
(``src/kntgraph/knowledge/graph/_protocol.py``) and
imported by the framework's adapter layer
(``src/kntgraph/infra/graph/_adapter.py``,
``_pool.py``, ``_lite_pool.py``) -- a one-way vertical
leak that violated the dependency rule (AGENTS.md
§1.2: framework never imports from vertical). The
audit (Prioridade 1, C.5-C.7) listed the three sites
as structural debt.

The vertical re-exports the three types so existing
callers do not need to update their imports. The same
pattern as the ``CachedSolution`` move
(``core/components/solution.py``) and the ``DeadLetterEvent``
move (``runner/_dlq_protocol.py``): canonical home is
the framework; vertical re-exports for back-compat.

The single ``query`` method is enough for every
framework operation (project events, traverse
relationships, run vector search) because Cypher
itself is a complete query language. Sub-adapters
(``GraphAgentAdapter``, ``GraphDocumentAdapter`` ...)
compose a ``GraphAdapter`` and call ``query`` with
their own Cypher templates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "GraphAdapter",
    "GraphError",
    "GraphQueryResult",
]


@dataclass(frozen=True, slots=True)
class GraphQueryResult:
    """
    The framework-level representation of a graph query result.

    The native ``falkordb.query_result.QueryResult`` carries
    a ``result_set`` (list of tuples) and an optional
    ``headers`` field. We carry the same shape here so the
    conversion at the adapter boundary is mechanical.

    Why ``tuple`` instead of ``list``: a tuple is immutable,
    safe to share across coroutines without copy. The
    ``frozen=True, slots=True`` dataclass makes the same
    guarantee at the object level.

    ``headers`` is optional because some queries (e.g.
    vector search) return anonymous columns; the caller
    resolves them by position.
    """

    result_set: tuple = ()
    headers: tuple = ()


class GraphError(Exception):
    """
    Concrete error type for graph adapter failures.

    Carries a ``kind`` discriminator so callers can branch
    on the failure mode (``connection_lost``,
    ``query_failed``, ``schema_mismatch``) without parsing
    the message string.

    ``cause`` holds the original native exception
    (``falkordb.exceptions.ResponseError``, connection
    error, etc.) for diagnostics. ``None`` when the
    failure originates inside the framework.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "graph_error",
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.cause = cause


@runtime_checkable
class GraphAdapter(Protocol):
    """
    The framework-level boundary for any graph database.

    The single ``query`` method is enough for every
    framework operation (project events, traverse
    relationships, run vector search) because Cypher
    itself is a complete query language. Sub-adapters
    (``GraphAgentAdapter``, ``GraphDocumentAdapter`` ...)
    compose a ``GraphAdapter`` and call ``query`` with
    their own Cypher templates.

    The Protocol is ``runtime_checkable`` so factories and
    tests can use ``isinstance(obj, GraphAdapter)`` for
    defensive type checks.

    Iter 10 (ADR-019 epílogo) -- ``GraphAdapter`` is
    async-only. The framework does NOT support sync
    ``Graph`` (FalkorDB <1.6) anymore; see
    ``FalkorDBGraphAdapter`` for the only supported
    impl.
    """

    async def query(
        self,
        cypher: str,
        *,
        params: dict | None = None,
    ) -> GraphQueryResult:
        """
        Execute a Cypher query and return its rows.

        The adapter is responsible for:

          - opening/closing the underlying connection
          - translating ``params`` to the backend format
          - converting the native result to
            ``GraphQueryResult`` (always returns rows,
            never raises)
          - wrapping native exceptions in ``GraphError``

        Returns an empty ``GraphQueryResult`` when the
        graph does not exist or has no data.
        """
        ...
