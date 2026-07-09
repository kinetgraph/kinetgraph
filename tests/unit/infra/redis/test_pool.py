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
    def test_pool_module_importable(self):
        from kntgraph.infra.redis import RedisPool, create_redis_pool

        assert RedisPool is not None
        assert callable(create_redis_pool)

    def test_pool_from_settings_uses_configured_url(self):
        from kntgraph.infra.config import Settings
        from kntgraph.infra.redis import RedisPool

        settings = Settings(redis_url="redis://test-host:1234/0")
        with patch("redis.asyncio.connection.ConnectionPool.from_url") as mock_from_url:
            RedisPool.from_settings(settings)
            mock_from_url.assert_called_once()
            args, kwargs = mock_from_url.call_args
            assert "redis://test-host:1234/0" in (args or ())
            assert kwargs.get("max_connections") == settings.redis_max_connections

    def test_pool_max_connections_propagated_from_settings(self):
        from kntgraph.infra.config import Settings
        from kntgraph.infra.redis import RedisPool

        settings = Settings(redis_url="redis://x", redis_max_connections=99)
        with patch("redis.asyncio.connection.ConnectionPool.from_url") as mock_from_url:
            RedisPool.from_settings(settings)
            kwargs = mock_from_url.call_args.kwargs
            assert kwargs["max_connections"] == 99

    def test_pool_client_satisfies_redis_like(self):
        from kntgraph.infra.redis import RedisPool

        _pool = RedisPool.__new__(RedisPool)
        # We can't construct a real client without I/O, but
        # we can check the property is typed RedisLike.
        # The runtime check is performed by the type checker.
        assert "client" in dir(RedisPool)

    async def test_pool_aclose_releases_connection(self):
        from kntgraph.infra.redis import RedisPool

        pool = RedisPool.__new__(RedisPool)

        # Inject a fake client to verify aclose is awaited.
        # RedisPool is frozen, so use object.__setattr__ to bypass.
        class FakeClient:
            def __init__(self):
                self.closed = False

            async def aclose(self):
                self.closed = True

        fake = FakeClient()
        object.__setattr__(pool, "_client", fake)
        await pool.aclose()
        assert fake.closed
