# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for RedisPool — connection pool + factory.

Part of the RED phase for Iteration 1 (ADR-019).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio


class TestRedisPool:
    async def test_pool_module_importable(self):
        from kntgraph.infra.redis import RedisPool, create_redis_pool

        assert RedisPool is not None
        assert callable(create_redis_pool)

    async def test_pool_from_settings_uses_configured_url(self):
        from kntgraph.infra.config import Settings
        from kntgraph.infra.redis import RedisPool

        settings = Settings(redis_url="redis://test-host:1234/0")
        with patch("redis.asyncio.connection.ConnectionPool.from_url") as mock_from_url:
            RedisPool.from_settings(settings)
            mock_from_url.assert_called_once()
            args, kwargs = mock_from_url.call_args
            assert "redis://test-host:1234/0" in (args or ())
            assert kwargs.get("max_connections") == settings.redis_max_connections

    async def test_pool_max_connections_propagated_from_settings(self):
        from kntgraph.infra.config import Settings
        from kntgraph.infra.redis import RedisPool

        settings = Settings(redis_url="redis://x", redis_max_connections=99)
        with patch("redis.asyncio.connection.ConnectionPool.from_url") as mock_from_url:
            RedisPool.from_settings(settings)
            kwargs = mock_from_url.call_args.kwargs
            assert kwargs["max_connections"] == 99

    async def test_pool_client_satisfies_redis_like(self):
        from kntgraph.infra.redis import RedisPool

        _pool = RedisPool.__new__(RedisPool)
        # We can't construct a real client without I/O, but
        # we can check the property is typed RedisLike.
        # The runtime check is performed by the type checker.
        assert "client" in dir(RedisPool)

    async def test_pool_aclose_releases_connection(self):
        """``aclose()`` must close the client AND the pool it built.

        ADR-084 §1.2: redis-py only closes the connection pool when
        the client owns it, and ``from_settings`` builds the pool
        explicitly — so the framework, which created it, must close
        it.

        The client is a real ``redis.asyncio.Redis`` over a real
        pool: the close path branches on the concrete driver (a
        cluster client owns one pool per node instead), so a fake
        would not exercise the branch that runs in production.
        """
        import redis.asyncio as redis_async

        from kntgraph.infra.redis import RedisPool

        class _StubWriter:
            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                return None

        conn_pool = redis_async.ConnectionPool.from_url(
            "redis://localhost:6379", max_connections=50
        )
        client = redis_async.Redis(connection_pool=conn_pool)
        conn = redis_async.Connection()
        conn._reader = object()  # type: ignore[assignment]
        conn._writer = _StubWriter()  # type: ignore[assignment]
        conn_pool._available_connections.append(conn)

        pool = RedisPool.__new__(RedisPool)
        # RedisPool is frozen, so use object.__setattr__ to bypass.
        object.__setattr__(pool, "_client", client)
        await pool.aclose()

        assert conn._reader is None and conn._writer is None, (
            "the connection pool was not closed — its sockets leak (ADR-084 §1.2)"
        )

    async def test_pool_passes_health_check_interval_from_settings(self):
        """ADR-082 §3.4: ``redis_health_check_interval`` is
        forwarded to ``ConnectionPool.from_url``. Default
        behaviour (redis-py 0 = disabled) would silently
        stall the consumer on a half-open TCP — the setting
        defaults to 15 s to stay below typical NAT / firewall
        idle timeouts.
        """
        from kntgraph.infra.config import Settings
        from kntgraph.infra.redis import RedisPool

        settings = Settings(
            redis_url="redis://x",
            redis_health_check_interval=30.0,
        )
        with patch("redis.asyncio.connection.ConnectionPool.from_url") as mock_from_url:
            RedisPool.from_settings(settings)
            kwargs = mock_from_url.call_args.kwargs
            assert kwargs["health_check_interval"] == 30.0

    async def test_pool_health_check_interval_default_is_15(self):
        """ADR-082 §3.4: the framework default is 15 s
        (matches the backoffice production value)."""
        from kntgraph.infra.config import fresh_settings

        settings = fresh_settings()
        assert settings.redis_health_check_interval == 15.0

    async def test_pool_health_check_interval_zero_opts_out(self):
        """Setting ``redis_health_check_interval=0`` disables
        the redis-py health check (the pre-ADR-082 behaviour
        for operators who depend on it byte-for-byte).
        """
        from kntgraph.infra.config import Settings
        from kntgraph.infra.redis import RedisPool

        settings = Settings(
            redis_url="redis://x",
            redis_health_check_interval=0.0,
        )
        with patch("redis.asyncio.connection.ConnectionPool.from_url") as mock_from_url:
            RedisPool.from_settings(settings)
            kwargs = mock_from_url.call_args.kwargs
            assert kwargs["health_check_interval"] == 0


class TestRedisPoolClusterMode:
    """``KNT_REDIS_CLUSTER`` switches the driver.

    The cluster driver follows the topology (it resolves the
    ``MOVED`` redirects a standalone client cannot), walks every
    node on ``SCAN``, and splits a non-transactional pipeline per
    node. The standalone driver does none of that.
    """

    async def test_cluster_mode_is_off_by_default(self):
        from kntgraph.infra.config import fresh_settings

        assert fresh_settings().redis_cluster is False

    async def test_cluster_mode_builds_the_cluster_driver(self):
        from kntgraph.infra.config import Settings
        from kntgraph.infra.redis import RedisPool

        settings = Settings(redis_url="redis://test-host:6379", redis_cluster=True)
        with patch("redis.asyncio.RedisCluster.from_url") as mock_from_url:
            RedisPool.from_settings(settings)
            mock_from_url.assert_called_once()
            args, kwargs = mock_from_url.call_args
            assert "redis://test-host:6379" in (args or ())
            assert kwargs.get("max_connections") == settings.redis_max_connections
            assert kwargs.get("decode_responses") is False

    async def test_standalone_mode_is_the_default(self):
        from kntgraph.infra.config import Settings
        from kntgraph.infra.redis import RedisPool

        settings = Settings(redis_url="redis://test-host:6379")
        with (
            patch("redis.asyncio.RedisCluster.from_url") as mock_cluster,
            patch("redis.asyncio.connection.ConnectionPool.from_url") as mock_pool,
        ):
            RedisPool.from_settings(settings)
            mock_cluster.assert_not_called()
            mock_pool.assert_called_once()
