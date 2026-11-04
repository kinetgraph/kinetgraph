# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Cluster-mode seam for the Redis adapter.

Redis Cluster -- and the Valkey cluster mode -- enforce two
rules a standalone server does not:

  1. **A command that touches several keys must touch one
     hash slot.** Redis answers ``CROSSSLOT`` otherwise. The
     slot is the CRC16 of the key, so two keys share one only
     when they carry the same hash tag (``{...}``) or by
     accident. The framework's keys carry no hash tag, so a
     multi-key command over two framework keys is rejected.
  2. **The client must follow the topology.** A standalone
     client that meets a cluster node receives a ``MOVED``
     redirect it cannot resolve, and ``SCAN`` walks only the
     node it was sent to.

This module owns the difference between the two clients:
telling them apart, splitting keys into the groups a
multi-key command may span, and dropping the sockets of a
cluster client so the next command reconnects (ADR-082
§3.2). The rest of the adapter keeps speaking the
:class:`~kntgraph.infra.redis._client.RedisLike` Protocol.

The concrete ``redis.asyncio`` types appear here (and in
:mod:`kntgraph.infra.redis._pool`) because this module is
where the framework meets the driver.

What the framework gives up in cluster mode
-------------------------------------------

The EventLog keeps one stream per agent and the tool queues
keep one stream per tool, so the keys of a single logical
entity live in different slots. Two consequences:

  - A multi-key command (``MULTI/EXEC``, ``DEL`` with
    several keys, ``XREAD`` with several streams) cannot
    span them. The write paths therefore issue one command
    per key, or a non-transactional pipeline, and the
    EventLog fan-in is split per slot.
  - The fan-in over N agent streams degrades from one
    blocking read to one blocking read per slot. The
    framework's own keys share no slot, so in practice that
    is one read per agent; the block budget is divided
    between them so a single connection is held at a time.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Protocol

import redis.asyncio as redis_async

from ._client import RedisLike

type RedisDriver = redis_async.Redis | redis_async.RedisCluster
"""The two drivers the framework speaks: the standalone client
and the cluster client."""


class DriverSettings(Protocol):
    """The settings the driver construction reads.

    A Protocol instead of the concrete ``Settings`` class:
    the config package imports this package (for the key
    prefix), so importing the config the other way would
    close the cycle.
    """

    redis_url: str
    redis_cluster: bool
    redis_max_connections: int
    redis_socket_timeout: float
    redis_health_check_interval: float


def build_client(settings: DriverSettings) -> RedisDriver:
    """Build the driver ``settings`` asks for.

    The standalone driver owns one pool. The cluster driver
    owns one pool per node, follows the topology (it resolves
    the ``MOVED`` redirects) and walks every node on ``SCAN``.
    It refuses a numbered database -- Redis Cluster has none
    -- so the URL must not select one.
    """
    if settings.redis_cluster:
        return redis_async.RedisCluster.from_url(
            settings.redis_url,
            decode_responses=False,
            max_connections=settings.redis_max_connections,
            socket_connect_timeout=5,
            socket_timeout=settings.redis_socket_timeout,
            socket_keepalive=True,
            health_check_interval=settings.redis_health_check_interval,
        )

    from redis.asyncio.connection import ConnectionPool

    pool = ConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=settings.redis_socket_timeout,
        socket_keepalive=True,
        retry_on_timeout=True,
        health_check_interval=settings.redis_health_check_interval,
    )
    return redis_async.Redis(connection_pool=pool)


async def close_client(client: RedisDriver) -> None:
    """Close the client and the pool it owns.

    The standalone client does not close a pool it did not
    create (redis-py); the cluster client closes the pool of
    every node in its ``aclose()``.
    """
    await client.aclose()
    if isinstance(client, redis_async.Redis):
        await client.connection_pool.aclose()


def group_keys_by_slot(client: RedisLike, keys: Sequence[str]) -> list[list[str]]:
    """Split ``keys`` into the groups one multi-key command may span.

    A standalone server accepts any number of keys in a
    single command, so the whole sequence is one group and
    the caller's fan-in is unchanged. A cluster server
    accepts only keys from the same hash slot, so the keys
    are grouped by slot and the caller issues one command
    per group.

    The groups keep the order of ``keys``, so the caller's
    cursor bookkeeping stays stable across calls. An empty
    input yields an empty list -- there is no command to
    issue.

    The driver is told apart by its concrete type, not by
    duck typing: a ``MagicMock`` answers every attribute
    lookup, so a ``hasattr`` probe would classify every test
    double as a cluster client and split every fan-in it
    sees.
    """
    if not keys:
        return []
    # The check is inline so the type checker narrows the client
    # to the cluster driver; a predicate function cannot carry
    # the narrowing on its own.
    if not isinstance(client, redis_async.RedisCluster):
        return [list(keys)]
    groups: dict[int, list[str]] = {}
    for key in keys:
        groups.setdefault(client.keyslot(key), []).append(key)
    return list(groups.values())


async def disconnect_transport(client: RedisLike) -> None:
    """Close the sockets so the next command reconnects (ADR-082 §3.2).

    The pool itself stays usable: sibling adapters share it
    (ADR-084 §3.3). A cluster client keeps its sockets in the
    per-node pools, so the idle ones are closed and the
    in-flight ones are marked to reconnect once their command
    finishes.

    Any other client exposes its pool as ``connection_pool`` and
    the pool closes its sockets through ``disconnect``. A client
    with no known socket layout is a no-op rather than an error.
    """
    if isinstance(client, redis_async.RedisCluster):
        for node in client.get_nodes():
            node.update_active_connections_for_reconnect()
            await node.disconnect_free_connections()
        return
    pool = getattr(client, "connection_pool", None)
    if pool is None:
        return
    disconnect = getattr(pool, "disconnect", None)
    if disconnect is None:
        return
    result = disconnect(inuse_connections=True)
    if inspect.isawaitable(result):
        await result


__all__ = [
    "DriverSettings",
    "RedisDriver",
    "build_client",
    "close_client",
    "disconnect_transport",
    "group_keys_by_slot",
]
