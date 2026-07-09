# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
High-level factories for Redis adapters.

Settings-driven. The framework's recommended entry points:

  - :func:`create_redis_pool`            — connection pool
  - :func:`create_event_log_storage`     — EventLog storage
  - :func:`create_session_storage`       — Session tier (JSON)
  - :func:`create_profile_storage`       — Profile tier (Hash)
  - :func:`create_continuity_storage`    — Continuity tier (Hash, sliding TTL)
  - :func:`create_dlq_storage`           — Dead-letter queue storage

When ``client=`` is passed, factories do not touch
``Settings`` at all. Useful for tests and for callers that
manage the connection lifecycle externally.
"""

from __future__ import annotations

from kntgraph.infra.config import Settings

from ._client import RedisLike
from ._dlq import MAXLEN_DEFAULT as DLQ_MAXLEN_DEFAULT
from ._dlq import RedisDLQStorage
from ._event_log import EventLogStorage, MAXLEN_DEFAULT, RedisEventLogAdapter
from ._memory import (
    RedisContinuityStorage,
    RedisProfileStorage,
    RedisSessionStorage,
    ShortMemoryStorage,
)
from ._pool import RedisPool


def _resolve_client(
    settings: Settings | None,
    client: RedisLike | None,
) -> RedisLike:
    if client is None:
        pool = RedisPool.from_settings(settings)
        return pool.client
    return client


def _resolve_stream_maxlen(
    settings: Settings | None,
    *,
    default: int,
) -> int:
    """Read ``Settings.stream_maxlen`` lazily.

    ``Settings`` is the canonical source for the per-tenant
    EventLog trim threshold. When the caller doesn't pass
    ``settings=`` (or the field is unset), we fall back to
    the module-level ``MAXLEN_DEFAULT`` so the factory still
    works in test contexts that don't construct a
    ``Settings`` instance.
    """
    if settings is None:
        from kntgraph.infra.config import fresh_settings

        settings = fresh_settings()
    value = getattr(settings, "stream_maxlen", None)
    if value is None or value <= 0:
        return default
    return value


def _resolve_global_maxlen(
    settings: Settings | None,
    *,
    default: int,
) -> int:
    """Read ``Settings.global_stream_maxlen`` lazily.

    The DLQ is a global stream (one for the whole
    deployment, not per-tenant), so it uses
    ``global_stream_maxlen``. Same fallback rules as
    :func:`_resolve_stream_maxlen`.
    """
    if settings is None:
        from kntgraph.infra.config import fresh_settings

        settings = fresh_settings()
    value = getattr(settings, "global_stream_maxlen", None)
    if value is None or value <= 0:
        return default
    return value


def create_event_log_storage(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
) -> EventLogStorage:
    """Build the EventLog storage adapter.

    ``maxlen`` defaults to ``Settings.stream_maxlen`` (per-tenant
    cap). Falls back to ``MAXLEN_DEFAULT`` (100k) when the
    setting is missing or non-positive, so test contexts
    without a ``Settings`` instance keep working.
    """
    return RedisEventLogAdapter(
        client=_resolve_client(settings, client),
        maxlen=_resolve_stream_maxlen(settings, default=MAXLEN_DEFAULT),
    )


def create_session_storage(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
    ttl_seconds: int | None = None,
) -> ShortMemoryStorage:
    """Build the Session tier storage (JSON cache).

    ``ttl_seconds`` defaults to
    ``Settings.session_ttl_seconds`` (24h).
    """
    if ttl_seconds is None:
        if settings is None:
            from kntgraph.infra.config import fresh_settings

            settings = fresh_settings()
        ttl_seconds = settings.session_ttl_seconds
    return RedisSessionStorage(
        client=_resolve_client(settings, client),
        ttl_seconds=ttl_seconds,
    )


def create_profile_storage(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
    ttl_seconds: int | None = None,
) -> ShortMemoryStorage:
    """Build the Profile tier storage (Hash cache).

    ``ttl_seconds`` defaults to
    ``Settings.profile_ttl_seconds`` (None = no TTL).
    """
    if ttl_seconds is None:
        if settings is None:
            from kntgraph.infra.config import fresh_settings

            settings = fresh_settings()
        ttl_seconds = settings.profile_ttl_seconds
    return RedisProfileStorage(
        client=_resolve_client(settings, client),
        ttl_seconds=ttl_seconds,
    )


def create_continuity_storage(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
    ttl_seconds: int | None = None,
) -> ShortMemoryStorage:
    """Build the Continuity tier storage (Hash cache, sliding TTL).

    ``ttl_seconds`` defaults to
    ``Settings.continuity_ttl_seconds`` (90 days, sliding).
    """
    if ttl_seconds is None:
        if settings is None:
            from kntgraph.infra.config import fresh_settings

            settings = fresh_settings()
        ttl_seconds = settings.continuity_ttl_seconds
    return RedisContinuityStorage(
        client=_resolve_client(settings, client),
        ttl_seconds=ttl_seconds,
    )


def create_dlq_storage(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
) -> RedisDLQStorage:
    """Build the DLQ storage adapter.

    The DLQ is a global stream (one per deployment, not
    per-tenant), so ``maxlen`` defaults to
    ``Settings.global_stream_maxlen`` (1M). Falls back to
    ``MAXLEN_DEFAULT`` (1M) when the setting is missing.
    """
    return RedisDLQStorage(
        client=_resolve_client(settings, client),
        maxlen=_resolve_global_maxlen(settings, default=DLQ_MAXLEN_DEFAULT),
    )


__all__ = [
    "create_continuity_storage",
    "create_dlq_storage",
    "create_event_log_storage",
    "create_profile_storage",
    "create_session_storage",
]
