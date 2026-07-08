# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.graph._protocol -- the framework-level boundary for any graph DB.

The framework treats "graph" as an abstract capability:
project events to nodes/edges, query vectors, traverse
relationships. The concrete database (FalkorDB today,
Neo4j/Memgraph tomorrow) is hidden behind this Protocol.

Sub-adapters (Agent/Document/ToolCall/Solution in ``_sub/``)
compose a ``GraphAdapter`` rather than inheriting from it.
This keeps each sub-adapter testable with a mock graph
and keeps the base Protocol minimal: a single async
``query`` method.

Error model:

  - Native exceptions (Cypher parse errors, connection
    failures) are caught at the adapter boundary and
    re-raised as ``GraphError``. This is the
    Python-idiomatic async pattern (raise, not Result).
  - Sub-adapters and callers use ``try/except GraphError``
    at the boundary of the graph I/O.

``runtime_checkable`` enables ``isinstance`` in factories
and tests — the same pattern used for the Redis shards.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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

    Iter 10 (ADR-019 epílogo) — ``GraphAdapter`` is async-only.
    The framework does NOT support sync ``Graph`` (FalkorDB
    <1.6) anymore; see ``FalkorDBGraphAdapter`` for the
    only supported impl.
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


__all__ = [
    "GraphAdapter",
    "GraphError",
    "GraphQueryResult",
]
