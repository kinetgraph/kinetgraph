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

Namespace prefix (ADR-076)
--------------------------

``key_prefix`` is read once at construction (from
``Settings.redis_key_prefix``) and exposed via the
``key_prefix`` property. The pool itself does not compose
keys; the per-feature adapters read ``pool.key_prefix``
at every ``_k(suffix)`` call so a single ``RedisPool``
instance can be shared across adapters that all see the
same prefix.

The prefix is validated once at ``Settings`` construction
(see :class:`RedisSettingsMixin._check_key_prefix`); the
pool re-runs the validator defensively at the seam so a
manually-constructed pool (tests, examples) cannot
introduce a malformed prefix.
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
    """Connection pool wrapper. Exposes a ``RedisLike`` view
    and the namespace prefix (ADR-076).
    """

    _client: "redis_async.Redis"
    _key_prefix: str = ""

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
            key_prefix=settings.redis_key_prefix,
        )
        return cls(
            _client=client,
            _key_prefix=settings.redis_key_prefix,
        )

    @property
    def client(self) -> RedisLike:
        """Return the underlying client. Typed as RedisLike at the boundary."""
        return self._client  # type: ignore[return-value]

    @property
    def key_prefix(self) -> str:
        """Return the namespace prefix for every key this
        pool's adapters write/read.

        Empty string preserves the pre-ADR-076 wire
        format byte-for-byte. Adapters compose keys via
        :func:`infra.redis._prefix.namespaced` which
        short-circuits on the empty case.
        """
        return self._key_prefix

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
