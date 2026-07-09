# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.graph._adapter -- ``FalkorDBGraphAdapter`` (reference impl).

The only concrete ``GraphAdapter`` shipped with the
framework today. Wraps a FalkorDB ``AsyncGraph``
(FalkorDB 1.6+) and exposes the single ``query`` method
required by the ``GraphAdapter`` Protocol.

Why one adapter, not two (sync + async):

  - The framework is async-first (AGENTS.md §4). Every
    I/O path is ``await``-based; adding a sync escape
    hatch would complicate the runtime model.
  - FalkorDB 1.6+ exposes ``AsyncGraph`` natively; the
    sync API is legacy and not supported.
  - Tests can swap this adapter for any mock that
    satisfies ``GraphAdapter``.

The adapter is the ONLY module in the framework that
imports ``falkordb`` at runtime. The lazy import pattern
mirrors the Redis shards: a process that never calls
``query`` does not require ``falkordb`` installed.

Wire format conversion:

  - ``falkordb.query_result.QueryResult`` → ``GraphQueryResult``
    (native ``result_set`` is a list of tuples; we
    freeze to ``tuple`` for safety).
  - Native exceptions
    (``falkordb.exceptions.ResponseError``, connection
    errors) → ``GraphError``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Optional

from kntgraph.knowledge.graph._protocol import GraphError, GraphQueryResult


if TYPE_CHECKING:
    from falkordb.asyncio import AsyncGraph


class FalkorDBGraphAdapter:
    """
    Reference ``GraphAdapter`` implementation backed by
    FalkorDB 1.6+ ``AsyncGraph``.

    The adapter holds a reference to an ``AsyncGraph``
    instance. Construction is the caller's responsibility:
    the framework does not own connection lifecycle
    (that's ``GraphPool``'s job; see ``_pool.py``).

    Parameters
    ----------
    graph:
        A FalkorDB ``AsyncGraph`` instance. The caller
        (typically ``GraphPool``) is responsible for
        ensuring the connection is open.

    The adapter does NOT call ``graph.connect()`` or
    similar — it assumes the graph is ready to accept
    queries. This keeps the adapter deterministic and
    testable: a test passes a mock ``AsyncGraph``
    directly.
    """

    def __init__(self, graph: "AsyncGraph") -> None:
        self._graph = graph

    async def query(
        self,
        cypher: str,
        *,
        params: Optional[Mapping[str, object]] = None,
    ) -> GraphQueryResult:
        """
        Execute a Cypher query against the wrapped
        ``AsyncGraph`` and convert the result.

        Iter 10 (ADR-019 epílogo) — async-only. The
        adapter does NOT inspect ``iscoroutinefunction``
        at call time; the ``AsyncGraph`` is guaranteed
        async by construction.

        Native exceptions are caught at the boundary and
        re-raised as ``GraphError`` so the rest of the
        framework only deals with framework types.

        The native ``falkordb.query_result.QueryResult``
        has a ``result_set`` list of tuples and an
        optional ``headers`` attribute. We freeze the
        list into a tuple for ``GraphQueryResult``.
        """
        try:
            native = await self._graph.query(
                cypher,
                params=dict(params) if params else None,
            )
        except Exception as e:
            raise GraphError(
                f"falkordb query failed: {e}",
                kind="query_failed",
                cause=e,
            ) from e
        if native is None:
            return GraphQueryResult(result_set=(), headers=())
        result_set = tuple(tuple(row) for row in native.result_set)
        headers = tuple(getattr(native, "headers", ()) or ())
        return GraphQueryResult(
            result_set=result_set,
            headers=headers,
        )


__all__ = ["FalkorDBGraphAdapter"]
