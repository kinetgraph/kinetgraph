# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
RedisPool — connection pool + factory.

This module is the only place in the framework that imports
``redis.asyncio`` directly (besides ``_client.py``'s
``TYPE_CHECKING`` block). Construction is the single point
that touches ``from_url`` / ``ConnectionPool``; consumers
receive a ``RedisLike``-typed view, not the concrete class.

Two entry points:

  - ``RedisPool.from_settings(settings)`` — build from a
    ``Settings`` instance.
  - ``create_redis_pool(settings=None)`` — convenience
    factory used by ``scripts/ci.py`` and tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from kntgraph.infra.config import Settings, fresh_settings

from ._client import RedisLike

if TYPE_CHECKING:
    import redis.asyncio as redis_async


logger = structlog.get_logger()


@dataclass(frozen=True)
class RedisPool:
    """Connection pool wrapper. Exposes a ``RedisLike`` view."""

    _client: "redis_async.Redis"

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "RedisPool":
        """Build a pool from ``Settings`` (or ``fresh_settings()`` if None)."""
        settings = settings or fresh_settings()
        import redis.asyncio as redis_async
        from redis.asyncio.connection import ConnectionPool

        pool = ConnectionPool.from_url(
            settings.redis_url,
            max_connections=settings.redis_max_connections,
            decode_responses=False,
            socket_connect_timeout=5,
            socket_keepalive=True,
            retry_on_timeout=True,
        )
        client = redis_async.Redis(connection_pool=pool)
        logger.info(
            "redis_pool.created",
            url=settings.redis_url,
            max_connections=settings.redis_max_connections,
        )
        return cls(_client=client)

    @property
    def client(self) -> RedisLike:
        """Return the underlying client. Typed as RedisLike at the boundary."""
        return self._client  # type: ignore[return-value]

    async def aclose(self) -> None:
        """Close all connections in the pool. Idempotent."""
        try:
            await self._client.aclose()
        except Exception as e:  # pragma: no cover
            logger.warning("redis_pool.aclose.failed", error=str(e))


def create_redis_pool(settings: Settings | None = None) -> RedisPool:
    """Convenience factory. ``settings=None`` reads ``fresh_settings()``."""
    return RedisPool.from_settings(settings)


__all__ = ["RedisPool", "create_redis_pool"]
