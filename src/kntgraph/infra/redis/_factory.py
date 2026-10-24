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
  - :func:`create_solution_storage`      — Solution tier (Hash, ADR-010 / ADR-049)
  - :func:`create_dlq_storage`           — Dead-letter queue storage
  - :func:`create_api_key_storage`       — API key storage (auth layer)
  - :func:`create_tool_router`           — ToolRouter (ADR-036 / ADR-083)
  - :func:`create_worker_manager`        — WorkerManager (ADR-036)

When ``client=`` is passed, factories do not touch
``Settings`` at all. Useful for tests and for callers that
manage the connection lifecycle externally.

Namespace prefix (ADR-076)
--------------------------

Each factory accepts an explicit ``key_prefix=`` override.
When omitted, the prefix is read from
``Settings.redis_key_prefix``. When ``client=`` is passed,
the prefix MUST be passed too (the factory cannot infer
it from a raw client). This keeps the seam honest: the
caller owns the prefix decision, not the adapter.

The tool-dispatcher factories (:func:`create_tool_router`,
:func:`create_worker_manager`) close the DEBT §2.35
follow-up #3 gap: callers that set ``KNT_REDIS_KEY_PREFIX``
saw storage keys move to the prefixed namespace but tool
queues still landed at the unprefixed ``knt:tools:*:queue``
root because ``ToolRouter`` and ``WorkerManager`` had no
factory and defaulted ``key_prefix=""``.
"""

from __future__ import annotations

from kntgraph.infra.config import Settings, fresh_settings
from kntgraph.stream.event_log.store import EventLog
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter

from ._auth import APIKeyStorage, RedisAPIKeyStorage
from ._client import RedisLike
from ._dlq import MAXLEN_DEFAULT as DLQ_MAXLEN_DEFAULT
from ._dlq import RedisDLQStorage
from ._event_log import MAXLEN_DEFAULT, EventLogStorage, RedisEventLogAdapter
from ._memory import (
    RedisContinuityStorage,
    RedisProfileStorage,
    RedisSessionStorage,
    RedisSolutionStore,
    ShortMemoryStorage,
)


def _resolve_client(
    settings: Settings | None,
    client: RedisLike | None,
) -> RedisLike:
    """Return the caller-owned Redis client (ADR-084 §3.2)."""
    if client is None:
        from ._errors import RedisClientRequiredError

        raise RedisClientRequiredError(
            "a Redis client is required: create one with "
            "create_redis_pool(settings) and pass client=pool.client. "
            "The framework will not build a connection pool it cannot "
            "close (ADR-084 §3.2)."
        )
    return client


def _resolve_key_prefix(
    settings: Settings | None,
    key_prefix: str | None,
) -> str:
    """Pick the namespace prefix for the adapter.

    Precedence (highest first):

      1. ``key_prefix`` argument (the caller explicitly
         picked a namespace; overrides everything).
      2. ``settings.redis_key_prefix`` (the operator set
         ``KNT_REDIS_KEY_PREFIX``).
      3. Empty string (default; pre-ADR-076 wire format,
         byte-for-byte).
    """
    if key_prefix is not None:
        return key_prefix
    if settings is not None:
        return settings.redis_key_prefix
    return ""


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
        settings = fresh_settings()
    value = getattr(settings, "global_stream_maxlen", None)
    if value is None or value <= 0:
        return default
    return value


def create_event_log_storage(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
    key_prefix: str | None = None,
) -> EventLogStorage:
    """Build the EventLog storage adapter.

    ``maxlen`` defaults to ``Settings.stream_maxlen`` (per-tenant
    cap). Falls back to ``MAXLEN_DEFAULT`` (100k) when the
    setting is missing or non-positive, so test contexts
    without a ``Settings`` instance keep working.

    ``key_prefix`` -- see ADR-076. Falls back to
    ``settings.redis_key_prefix`` when omitted.
    """
    return RedisEventLogAdapter(
        client=_resolve_client(settings, client),
        maxlen=_resolve_stream_maxlen(settings, default=MAXLEN_DEFAULT),
        key_prefix=_resolve_key_prefix(settings, key_prefix),
    )


def create_session_storage(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
    ttl_seconds: int | None = None,
    key_prefix: str | None = None,
) -> ShortMemoryStorage:
    """Build the Session tier storage (JSON cache).

    ``ttl_seconds`` defaults to
    ``Settings.session_ttl_seconds`` (24h).
    """
    if ttl_seconds is None:
        if settings is None:
            settings = fresh_settings()
        ttl_seconds = settings.session_ttl_seconds
    return RedisSessionStorage(
        client=_resolve_client(settings, client),
        ttl_seconds=ttl_seconds,
        key_prefix=_resolve_key_prefix(settings, key_prefix),
    )


def create_profile_storage(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
    ttl_seconds: int | None = None,
    key_prefix: str | None = None,
) -> ShortMemoryStorage:
    """Build the Profile tier storage (Hash cache).

    ``ttl_seconds`` defaults to
    ``Settings.profile_ttl_seconds`` (None = no TTL).
    """
    if ttl_seconds is None:
        if settings is None:
            settings = fresh_settings()
        ttl_seconds = settings.profile_ttl_seconds
    return RedisProfileStorage(
        client=_resolve_client(settings, client),
        ttl_seconds=ttl_seconds,
        key_prefix=_resolve_key_prefix(settings, key_prefix),
    )


def create_continuity_storage(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
    ttl_seconds: int | None = None,
    key_prefix: str | None = None,
) -> ShortMemoryStorage:
    """Build the Continuity tier storage (Hash cache, sliding TTL).

    ``ttl_seconds`` defaults to
    ``Settings.continuity_ttl_seconds`` (90 days, sliding).
    """
    if ttl_seconds is None:
        if settings is None:
            settings = fresh_settings()
        ttl_seconds = settings.continuity_ttl_seconds
    return RedisContinuityStorage(
        client=_resolve_client(settings, client),
        ttl_seconds=ttl_seconds,
        key_prefix=_resolve_key_prefix(settings, key_prefix),
    )


def create_dlq_storage(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
    key_prefix: str | None = None,
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
        key_prefix=_resolve_key_prefix(settings, key_prefix),
    )


def create_solution_storage(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
    ttl_seconds: int | None = None,
    key_prefix: str | None = None,
) -> RedisSolutionStore:
    """Build the Solution tier storage (Hash cache, ADR-010 / ADR-049).

    ``ttl_seconds`` defaults to
    ``Settings.solution_ttl_seconds`` (None = no TTL;
    Solutions are explicitly invalidated by the operator).
    """
    if ttl_seconds is None:
        if settings is None:
            settings = fresh_settings()
        ttl_seconds = settings.solution_ttl_seconds
    return RedisSolutionStore(
        client=_resolve_client(settings, client),
        ttl_seconds=ttl_seconds,
        key_prefix=_resolve_key_prefix(settings, key_prefix),
    )


def create_api_key_storage(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
    key_prefix: str | None = None,
) -> APIKeyStorage:
    """Build the API key storage adapter (auth layer, ADR-019).

    Empty ``client=`` falls back to the
    ``Settings.redis_url``-derived pool. Empty
    ``key_prefix=`` falls back to
    ``settings.redis_key_prefix`` -- the operator's
    ``KNT_REDIS_KEY_PREFIX`` env var. The composed key is
    ``<prefix>knt:api:keys:<digest>`` (the framework's
    reserved ``knt:`` literal is appended by the storage,
    matching the ADR-076 §1.3 contract; see
    ``RedisAPIKeyStorage.storage_key``).

    The factory returns the ``APIKeyStorage`` Protocol
    rather than the concrete ``RedisAPIKeyStorage`` so the
    cache (``APIKeyCacheAdapter``) and the verifier
    (``RedisAPIKeyVerifier``) compose without taking a
    dependency on the Redis impl.
    """
    return RedisAPIKeyStorage(
        client=_resolve_client(settings, client),
        key_prefix=_resolve_key_prefix(settings, key_prefix),
    )


def create_tool_router(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
    key_prefix: str | None = None,
    dedup: bool = False,
    dedup_ttl_seconds: int | None = None,
) -> ToolRouter:
    """Build the :class:`ToolRouter` (ADR-036 / ADR-083).

    Closes the DEBT §2.35 follow-up #3 gap: callers that
    set ``KNT_REDIS_KEY_PREFIX`` saw storage keys move to
    the prefixed namespace but tool queues still landed
    at the unprefixed ``knt:tools:<name>:queue`` root
    because ``ToolRouter`` had no factory and defaulted
    ``key_prefix=""``.

    ``key_prefix`` falls back to
    ``settings.redis_key_prefix`` when omitted -- the
    operator's ``KNT_REDIS_KEY_PREFIX`` env var. The
    composed stream key is
    ``<prefix>knt:tools:<name>:queue`` and the composed
    dedup-index key is
    ``<prefix>knt:tools:<name>:idem:<event_id>``
    (ADR-076 §1.3 contract, ADR-083 §3.1).

    ``dedup`` (ADR-083) opts in to per-``event_id``
    dedup at the ``XADD`` boundary. The default
    ``False`` keeps the pre-ADR-083 wire format
    byte-for-byte. ``dedup_ttl_seconds`` bounds the
    index size; ``None`` reads
    :data:`kntgraph.infra.redis._tools.DEDUP_TTL_SECONDS_DEFAULT`
    (1h).

    ``client=`` is required (ADR-084 §3.2): the framework will
    not build a connection pool the caller cannot close. Create
    the pool with :func:`create_redis_pool` and pass its client.
    """
    from kntgraph.infra.redis._tools import DEDUP_TTL_SECONDS_DEFAULT

    if dedup_ttl_seconds is None:
        dedup_ttl_seconds = DEDUP_TTL_SECONDS_DEFAULT
    return ToolRouter(
        redis=_resolve_client(settings, client),
        key_prefix=_resolve_key_prefix(settings, key_prefix),
        dedup=dedup,
        dedup_ttl_seconds=dedup_ttl_seconds,
    )


def create_worker_manager(
    settings: Settings | None = None,
    *,
    redis: RedisLike | None = None,
    event_log: EventLog,
    group_name: str = "fmh_tool_workers",
    consumer_name: str = "worker-1",
    reaper_interval: float = 60.0,
    reaper_idle_time: float = 300.0,
    heartbeat_interval_seconds: float = 30.0,
    key_prefix: str | None = None,
    max_pool_workers: int | None = None,
) -> WorkerManager:
    """Build the :class:`WorkerManager` (ADR-036).

    Mirror of :func:`create_tool_router` for the worker
    side: ``key_prefix`` falls back to
    ``settings.redis_key_prefix`` so an operator who sets
    ``KNT_REDIS_KEY_PREFIX=acme-billing:`` gets
    ``acme-billing:knt:tools:<name>:queue`` on both the
    consumer-group creation in ``start()``, the consume
    loop, and the reaper loop. Without this factory a
    caller that relied on the env var would still see
    the WorkerManager open its consumer group on the
    unprefixed root (DEBT §2.35 follow-up #3).

    ``event_log`` is required (the manager constructor
    takes it positionally). The other keyword arguments
    mirror :class:`WorkerManager.__init__` 1:1 so a
    caller that already knows the manager's signature
    can pass the same values here.

    ``redis=`` is required (ADR-084 §3.2): the framework will
    not build a connection pool the caller cannot close. Create
    the pool with :func:`create_redis_pool` and pass its client.
    """
    return WorkerManager(
        redis=_resolve_client(settings, redis),
        event_log=event_log,
        group_name=group_name,
        consumer_name=consumer_name,
        reaper_interval=reaper_interval,
        reaper_idle_time=reaper_idle_time,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        key_prefix=_resolve_key_prefix(settings, key_prefix),
        max_pool_workers=max_pool_workers,
    )


__all__ = [
    "create_api_key_storage",
    "create_continuity_storage",
    "create_dlq_storage",
    "create_event_log_storage",
    "create_profile_storage",
    "create_session_storage",
    "create_solution_storage",
    "create_tool_router",
    "create_worker_manager",
]
