# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Connection-lifecycle contract against a real Redis server.

The unit tests in ``test_connection_lifecycle.py`` pin the
close contract without a server. These confirm the same
contract end-to-end against a live Redis: a connection that is
released is really gone from the server's client list.

The server-side check is the decisive one — the framework can
close its own bookkeeping correctly while the socket stays
open, and only ``CLIENT LIST`` tells the truth.
"""

from __future__ import annotations

import asyncio

import pytest
import redis.asyncio as redis_async

pytestmark = pytest.mark.asyncio

REDIS_URL = "redis://:redispassword@localhost:6379"


async def _client_count(redis_client) -> int:
    """Number of clients currently connected to the server."""
    info = await redis_client.info("clients")
    return int(info["connected_clients"])


async def test_aclose_releases_the_socket_to_the_server(clean_redis):
    """After ``RedisPool.aclose()`` the server must see one
    fewer connection.

    This is the assertion the framework's own bookkeeping
    cannot fake: the socket has to actually close.
    """
    from kntgraph.infra.config import fresh_settings
    from kntgraph.infra.redis import RedisPool

    settings = fresh_settings()
    settings.redis_url = REDIS_URL

    pool = RedisPool.from_settings(settings)
    client = pool.client

    await client.ping()
    before = await _client_count(clean_redis)
    assert before > 0

    await pool.aclose()

    # Give the server a moment to reap the socket.
    after = before
    for _ in range(20):
        after = await _client_count(clean_redis)
        if after < before:
            break
        await asyncio.sleep(0.05)

    assert after < before, (
        f"the server still reports {after} connected clients after "
        f"RedisPool.aclose() (was {before}) — the socket leaked"
    )


async def test_shared_pool_survives_one_adapter_close(clean_redis):
    """One adapter closing its transport must not disconnect
    the others sharing the pool.

    The ADR-082 reconnect path is per-adapter; the pool is
    shared. Closing the pool would kill every sibling adapter.
    """
    from kntgraph.infra.redis._event_log._adapter import RedisEventLogAdapter

    conn_pool = redis_async.ConnectionPool.from_url(REDIS_URL, max_connections=10)
    client = redis_async.Redis(connection_pool=conn_pool)

    a1 = RedisEventLogAdapter(client=client, key_prefix="svc:")
    # The sibling adapter that shares the pool; it exists to make
    # the sharing explicit, not to be called.
    _a2 = RedisEventLogAdapter(client=client, key_prefix="svc:")

    await a1.close_transport()

    # The sibling must still be able to talk to the server.
    assert await client.ping() is True
