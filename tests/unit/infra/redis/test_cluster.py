# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the cluster seam (``infra.redis._cluster``).

The seam is what tells a standalone client from a cluster
client, what keeps a multi-key command inside one hash slot,
and what drops the sockets of a cluster client so the next
command reconnects.

The tests run without a cluster: the cluster driver connects
lazily, and its slot arithmetic is pure CRC16 over the key.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.asyncio as redis_async

pytestmark = pytest.mark.asyncio


def _standalone_client() -> redis_async.Redis:
    """A standalone client over a pool that never connects."""
    return redis_async.Redis(
        connection_pool=redis_async.ConnectionPool.from_url("redis://localhost:6379")
    )


def _cluster_client() -> redis_async.RedisCluster:
    """A cluster driver that has not talked to any node yet."""
    return redis_async.RedisCluster(host="localhost", port=6379)


class _StubWriter:
    """Minimal stand-in for an ``asyncio`` transport writer.

    ``Connection.disconnect`` calls ``_writer.close()`` and then
    ``await _writer.wait_closed()``. The tests must run without
    a live server, but the stub has to be honest about closing,
    otherwise the assertion "the socket was closed" would be
    vacuously true.
    """

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        return None


class TestGroupKeysBySlot:
    async def test_standalone_keys_stay_in_one_group(self):
        from kntgraph.infra.redis._cluster import group_keys_by_slot

        keys = ["knt:agents:a-1:events", "knt:agents:a-2:events"]
        assert group_keys_by_slot(_standalone_client(), keys) == [keys]

    async def test_test_double_is_not_mistaken_for_a_cluster_client(self):
        """A ``MagicMock`` answers every attribute lookup.

        A duck-typed probe would classify every test double as a
        cluster client and split every fan-in it sees.
        """
        from kntgraph.infra.redis._cluster import group_keys_by_slot

        keys = ["knt:agents:a-1:events", "knt:agents:a-2:events"]
        assert group_keys_by_slot(MagicMock(), keys) == [keys]

    async def test_empty_input_yields_no_group(self):
        from kntgraph.infra.redis._cluster import group_keys_by_slot

        assert group_keys_by_slot(_standalone_client(), []) == []
        assert group_keys_by_slot(_cluster_client(), []) == []

    async def test_cluster_keys_are_grouped_by_slot(self):
        """The per-agent streams do not share a slot, so the
        fan-in is split into one group per stream."""
        from kntgraph.infra.redis._cluster import group_keys_by_slot

        client = _cluster_client()
        keys = ["knt:agents:a-1:events", "knt:agents:a-2:events"]

        groups = group_keys_by_slot(client, keys)

        assert len(groups) == 2
        assert sorted(group[0] for group in groups) == sorted(keys)

    async def test_cluster_keys_of_one_slot_stay_together(self):
        """Two keys with the same hash tag share a slot, so one
        command may span them."""
        from kntgraph.infra.redis._cluster import group_keys_by_slot

        client = _cluster_client()
        keys = ["knt:{a-1}:events", "knt:{a-1}:cursor"]

        groups = group_keys_by_slot(client, keys)

        assert groups == [keys]

    async def test_groups_keep_the_order_of_the_input(self):
        from kntgraph.infra.redis._cluster import group_keys_by_slot

        client = _cluster_client()
        keys = ["knt:agents:a-1:events", "knt:agents:a-2:events"]

        groups = group_keys_by_slot(client, keys)

        assert [group[0] for group in groups] == keys


class TestDisconnectTransport:
    async def test_standalone_client_closes_the_pool_sockets(self):
        """The reconnect path must close the socket while
        leaving the pool usable for the sibling adapters
        (ADR-084 §3.3)."""
        from kntgraph.infra.redis._cluster import disconnect_transport

        conn_pool = redis_async.ConnectionPool.from_url("redis://localhost:6379")
        client = redis_async.Redis(connection_pool=conn_pool)
        conn = redis_async.Connection()
        conn._reader = object()  # type: ignore[assignment]
        conn._writer = _StubWriter()  # type: ignore[assignment]
        conn_pool._available_connections.append(conn)

        await disconnect_transport(client)

        assert conn._reader is None and conn._writer is None

    async def test_cluster_client_marks_its_node_connections(self):
        """A cluster client keeps its sockets in one pool per
        node: the idle ones are closed and the in-flight ones
        are marked to reconnect when their command finishes."""
        from kntgraph.infra.redis._cluster import disconnect_transport

        node = MagicMock()
        node.disconnect_free_connections = AsyncMock()
        client = _cluster_client()

        with patch.object(redis_async.RedisCluster, "get_nodes", return_value=[node]):
            await disconnect_transport(client)

        node.update_active_connections_for_reconnect.assert_called_once()
        node.disconnect_free_connections.assert_awaited_once()

    async def test_unknown_client_is_a_no_op(self):
        """A client that is neither driver has no known socket
        layout; the call must not raise."""
        from kntgraph.infra.redis._cluster import disconnect_transport

        await disconnect_transport(MagicMock())


class _Settings:
    """The settings the driver construction reads."""

    def __init__(self, cluster: bool = False) -> None:
        self.redis_url = "redis://localhost:6379"
        self.redis_cluster = cluster
        self.redis_max_connections = 17
        self.redis_socket_timeout = 15.0
        self.redis_health_check_interval = 15.0


class TestBuildClient:
    async def test_standalone_driver_by_default(self):
        from kntgraph.infra.redis._cluster import build_client

        with patch("redis.asyncio.connection.ConnectionPool.from_url") as mock_pool:
            client = build_client(_Settings())

        mock_pool.assert_called_once()
        assert isinstance(client, redis_async.Redis)
        assert not isinstance(client, redis_async.RedisCluster)

    async def test_cluster_driver_when_the_setting_is_on(self):
        from kntgraph.infra.redis._cluster import build_client

        cluster_client = _cluster_client()
        with patch("redis.asyncio.RedisCluster.from_url") as mock_cluster:
            mock_cluster.return_value = cluster_client
            client = build_client(_Settings(cluster=True))

        mock_cluster.assert_called_once()
        args, kwargs = mock_cluster.call_args
        assert "redis://localhost:6379" in (args or ())
        assert kwargs["max_connections"] == 17
        assert kwargs["decode_responses"] is False
        assert client is cluster_client


class TestCloseClient:
    async def test_standalone_client_closes_the_pool_it_owns(self):
        """redis-py closes the pool only when the client owns
        it; the framework built the pool, so it closes it
        (ADR-084 §1.2)."""
        from kntgraph.infra.redis._cluster import close_client

        conn_pool = redis_async.ConnectionPool.from_url("redis://localhost:6379")
        client = redis_async.Redis(connection_pool=conn_pool)
        conn = redis_async.Connection()
        conn._reader = object()  # type: ignore[assignment]
        conn._writer = _StubWriter()  # type: ignore[assignment]
        conn_pool._available_connections.append(conn)

        await close_client(client)

        assert conn._reader is None and conn._writer is None

    async def test_cluster_client_closes_every_node(self):
        from kntgraph.infra.redis._cluster import close_client

        client = _cluster_client()
        with patch.object(
            redis_async.RedisCluster, "aclose", new=AsyncMock()
        ) as aclose:
            await close_client(client)

        aclose.assert_awaited_once()
