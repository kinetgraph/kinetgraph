# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Integration test for the tool-dispatcher prefix plumbing via
the settings-driven factories (DEBT §2.35 follow-up #3).

The storage adapters close the prefix gap through
``create_event_log_storage`` / ``create_dlq_storage`` / etc.
(``src/kntgraph/infra/redis/_factory.py``) — they read
``Settings.redis_key_prefix`` when the caller omits
``key_prefix=``. ``ToolRouter`` and ``WorkerManager``
historically did NOT have factories, so a caller who
relied on ``KNT_REDIS_KEY_PREFIX`` for storage would
still see tool queues land at the unprefixed
``knt:tools:<name>:queue`` (the "tools still at the root"
failure mode).

This test exercises the new settings-driven factories so a
regression that drops ``_resolve_key_prefix`` from the
factory path is caught here.

Coverage:

  - ``create_tool_router(settings=...)`` honours
    ``settings.redis_key_prefix``.
  - ``create_worker_manager(settings=...)`` honours
    ``settings.redis_key_prefix``.
  - The factory-built ToolRouter writes to the prefixed
    stream key; ``xrange`` confirms the entry is at the
    prefixed key and absent from the legacy unprefixed key.
  - The factory-built WorkerManager creates its consumer
    group on the prefixed stream key.

Runs against a live Redis via the ``clean_redis`` fixture.
"""

from __future__ import annotations

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.infra.config import Settings
from kntgraph.infra.redis import RedisEventLogAdapter
from kntgraph.infra.redis._factory import create_tool_router, create_worker_manager
from kntgraph.stream.event_log.store import EventLog
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter
from kntgraph.tools.worker import tool_worker

pytestmark = pytest.mark.asyncio

PREFIX: str = "acme-billing:"


@tool_worker(name="echo", description="echo back the params")
class _EchoStub:
    async def invoke(
        self, *, idempotency_key: str | None = None, **kwargs: object
    ) -> str:
        return "ok"


def _settings_with_prefix() -> Settings:
    """Build a Settings instance with ``redis_key_prefix=PREFIX``.

    Uses ``KNT_REDIS_URL`` (env) so the live Redis used by the
    integration step is reachable; only ``redis_key_prefix`` is
    overridden for the assertion.
    """
    return Settings(redis_key_prefix=PREFIX)


class TestFactoryToolRouterHonoursSettingsPrefix:
    """``create_tool_router(settings=...)`` threads the
    operator's ``KNT_REDIS_KEY_PREFIX`` through to the
    underlying ``ToolRouter`` so tool messages land in the
    prefixed namespace, not at the legacy ``knt:tools:*:queue``.
    """

    async def test_factory_tool_router_writes_to_prefixed_stream(
        self, clean_redis
    ) -> None:
        """A ``ToolRouter`` built via ``create_tool_router``
        with ``Settings.redis_key_prefix="acme-billing:"``
        publishes to ``acme-billing:knt:tools:echo:queue``.
        The legacy unprefixed key MUST stay empty.
        """
        settings = _settings_with_prefix()
        assert settings.redis_key_prefix == PREFIX

        router: ToolRouter = create_tool_router(settings=settings, client=clean_redis)
        assert router._key_prefix == PREFIX

        event = Event.create(
            event_type="tool.echo.requested",
            agent_id="agent-1",
            event_class="domain",
            data={"params": {"text": "hello from factory"}},
            correlation=CorrelationContext.new(),
        )
        await router.route_batch([event])

        prefixed_entries = await clean_redis.xrange("acme-billing:knt:tools:echo:queue")
        assert len(prefixed_entries) == 1, (
            "factory-built ToolRouter did not honour settings.redis_key_prefix; "
            "the entry landed outside the prefixed namespace."
        )

        legacy_entries = await clean_redis.xrange("knt:tools:echo:queue")
        assert legacy_entries == [], (
            "factory-built ToolRouter wrote to the unprefixed root "
            "even though settings.redis_key_prefix was set."
        )


class TestFactoryWorkerManagerHonoursSettingsPrefix:
    """``create_worker_manager(settings=...)`` threads the
    operator's ``KNT_REDIS_KEY_PREFIX`` through to the
    underlying ``WorkerManager`` so the consumer group is
    created on the prefixed stream key.
    """

    async def test_factory_worker_manager_creates_group_on_prefixed_stream(
        self, clean_redis
    ) -> None:
        """A ``WorkerManager`` built via ``create_worker_manager``
        creates its consumer group on
        ``acme-billing:knt:tools:echo:queue`` when
        ``Settings.redis_key_prefix="acme-billing:"``.
        ``XINFO GROUPS`` confirms the group exists at the
        prefixed key and not at the legacy unprefixed key.
        """
        settings = _settings_with_prefix()
        log = EventLog(RedisEventLogAdapter(client=clean_redis, key_prefix=PREFIX))

        manager: WorkerManager = create_worker_manager(
            settings=settings,
            redis=clean_redis,
            event_log=log,
        )
        assert manager._key_prefix == PREFIX
        manager.register(_EchoStub, acl=None)

        await manager.start()
        try:
            groups = await clean_redis.xinfo_groups("acme-billing:knt:tools:echo:queue")

            def _group_name(g: dict) -> str | bytes | None:
                return g.get("name", g.get(b"name"))

            assert any(
                _group_name(g) in (b"fmh_tool_workers", "fmh_tool_workers")
                for g in groups
            ), (
                "factory-built WorkerManager did not honour "
                "settings.redis_key_prefix; consumer group landed "
                "outside the prefixed namespace."
            )

            # The legacy unprefixed stream MUST NOT exist --
            # XINFO GROUPS raises ``ResponseError`` on a missing
            # key, which is the GOOD case here (the factory
            # routed everything through the prefixed namespace).
            from redis import exceptions as redis_exceptions

            with pytest.raises(redis_exceptions.ResponseError):
                await clean_redis.xinfo_groups("knt:tools:echo:queue")
        finally:
            await manager.stop()


class TestFactoryExplicitPrefixOverridesSettings:
    """Explicit ``key_prefix=`` on the factory call wins
    over ``settings.redis_key_prefix`` — same precedence
    rule as the storage factories (``_resolve_key_prefix``
    in ``infra/redis/_factory.py``).
    """

    async def test_explicit_key_prefix_beats_settings(self, clean_redis) -> None:
        """``create_tool_router(settings=prefix_a, key_prefix=prefix_b)``
        uses ``prefix_b`` for the stream key — the explicit
        override wins.
        """
        settings = Settings(redis_key_prefix="prefix_a:")
        router = create_tool_router(
            settings=settings, client=clean_redis, key_prefix="prefix_b:"
        )
        assert router._key_prefix == "prefix_b:"
