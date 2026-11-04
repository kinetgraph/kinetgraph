# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
RedisPool — connection pool + factory.

This module and :mod:`kntgraph.infra.redis._cluster` are the
only places in the framework that import ``redis.asyncio``
directly (besides ``_client.py``'s ``TYPE_CHECKING`` block).
Construction is the single point that touches ``from_url`` /
``ConnectionPool``; consumers receive a ``RedisLike``-typed
view, not the concrete class.

Two entry points:

  - ``RedisPool.from_settings(settings)`` — build from a
    ``Settings`` instance.
  - ``create_redis_pool(settings=None)`` — convenience
    factory used by ``scripts/ci.py`` and tests.

``Settings.redis_cluster`` (env: ``KNT_REDIS_CLUSTER``)
switches the driver to the cluster client. The seam that owns
the difference between the two drivers lives in
:mod:`kntgraph.infra.redis._cluster`.

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

import structlog
from redis import exceptions as redis_exceptions

from kntgraph.infra.config import Settings, fresh_settings

from ._client import RedisLike
from ._cluster import RedisDriver, build_client, close_client

logger = structlog.get_logger()


@dataclass(frozen=True)
class RedisPool:
    """Connection pool wrapper. Exposes a ``RedisLike`` view
    and the namespace prefix (ADR-076).
    """

    _client: RedisDriver
    _key_prefix: str = ""

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> RedisPool:
        """Build a pool from ``Settings`` (or ``fresh_settings()`` if None)."""
        settings = settings or fresh_settings()
        client = build_client(settings)
        logger.info(
            "redis_pool.created",
            url=settings.redis_url,
            cluster=settings.redis_cluster,
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
        """Close the pool and every connection it holds. Idempotent.

        redis-py only closes the pool when the client owns it; this wrapper
        owns it (ADR-084 §1.2).

        Per ADR-077 the catch is narrow and ``CancelledError`` propagates.
        The close is best-effort: a transport error is logged, not raised.
        """
        try:
            await close_client(self._client)
        except (
            redis_exceptions.RedisError,
            ConnectionError,
            TimeoutError,
            OSError,
        ) as exc:  # pragma: no cover
            logger.warning("redis_pool.aclose.failed", error=str(exc))


def create_redis_pool(settings: Settings | None = None) -> RedisPool:
    """Convenience factory. ``settings=None`` reads ``fresh_settings()``."""
    return RedisPool.from_settings(settings)


__all__ = ["RedisPool", "create_redis_pool"]
