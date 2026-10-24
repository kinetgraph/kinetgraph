# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``ConnectionPoolLike`` — framework-level Protocol for the pool
behind a Redis client.

Split out of :mod:`kntgraph.infra.redis._client` so the client
Protocol and the pool Protocol can evolve independently: the
client's surface is driven by the framework's Redis commands, the
pool's by the connection-lifecycle policy (ADR-084).

A ``RedisLike`` does not own its pool. The caller that created
the pool closes it (ADR-084 §3.2) — the framework will not build
a pool the caller cannot reach. This Protocol exists so the
reconnect path can close the *sockets* without tearing the pool
down and taking the sibling consumers' connections with it
(§3.3).
"""

from __future__ import annotations

from typing import Protocol


class ConnectionPoolLike(Protocol):
    """Subset of ``redis.asyncio.connection.ConnectionPool`` the
    framework relies on.

    ADR-084 §3.3: the reconnect path calls ``disconnect()`` to
    close the sockets while leaving the pool usable for the
    sibling consumers. ``aclose()`` tears the pool down entirely,
    which is what the framework must NOT do on a transport error.

    Only ``disconnect()`` is declared: it is the single method
    the framework calls through this Protocol. The pool's
    ``aclose()`` is reached through the concrete
    ``redis.asyncio.Redis`` client in ``RedisPool.aclose``.
    """

    async def disconnect(self, inuse_connections: bool = True) -> None: ...


__all__ = ["ConnectionPoolLike"]
