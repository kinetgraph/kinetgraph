# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Integration test for the ADR-076 prefix plumbing.

Two adapters with different ``key_prefix`` values on the
same Redis MUST NOT see each other's data. Without this
layer, two services sharing one Redis would cross-talk
at the storage boundary (the pre-fix gap that ADR-076
§1.1 describes).

Coverage:

  - ``ToolRouter`` + ``WorkerManager`` (DEBT §2.35)
  - ``RedisAPIKeyStorage`` (DEBT §2.35 follow-up #2)

The test runs against a live Redis via the
``clean_redis`` fixture (``tests/integration/conftest.py``).
It is part of the ``integration`` CI step
(``uv run scripts/ci.py --only integration``) and is
NOT collected by the default ``tests`` step (the live
Redis container is not available there).
"""

from __future__ import annotations

import pytest
import redis.asyncio as aioredis

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.stream.event_log.store import EventLog
from kntgraph.infra.redis._auth import RedisAPIKeyStorage
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter


pytestmark = pytest.mark.asyncio


async def _publish_via_router(
    redis_client: aioredis.Redis,
    agent_id: str,
    tool_name: str,
    key_prefix: str,
) -> str:
    """Publish a single ``tool.<name>.requested`` event
    through a fresh :class:`ToolRouter` and return the
    composed stream key the router wrote to.
    """
    router = ToolRouter(redis_client, key_prefix=key_prefix)
    event = Event.create(
        event_type=f"tool.{tool_name}.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"params": {"text": f"hello from {key_prefix or '<no-prefix>'}"}},
        correlation=CorrelationContext.new(),
    )
    await router.route_batch([event])
    return router._stream_key(tool_name)


class TestToolDispatcherKeyPrefix:
    """End-to-end isolation between two services sharing
    one Redis with different prefixes. The router writes
    to the prefixed stream key; ``xrange`` confirms the
    entry is at the prefixed key and absent from the
    other prefix's key.
    """

    async def test_router_writes_to_prefixed_stream_key(self, clean_redis) -> None:
        """A ``ToolRouter`` with ``key_prefix="acme:"``
        publishes to ``acme:knt:tools:echo:queue``. ``XRANGE``
        confirms the entry is at the prefixed key.
        """
        stream_key = await _publish_via_router(
            clean_redis, agent_id="agent-1", tool_name="echo", key_prefix="acme:"
        )
        assert stream_key == "acme:knt:tools:echo:queue"

        entries = await clean_redis.xrange(stream_key)
        assert len(entries) == 1
        _msg_id, fields = entries[0]
        # The payload is the JSON-serialised event; the
        # ``event_type`` round-trips through.
        payload = fields[b"payload"]
        assert isinstance(payload, bytes)
        assert b"tool.echo.requested" in payload

    async def test_two_prefixes_do_not_cross_talk(self, clean_redis) -> None:
        """Two ``ToolRouter`` instances with different
        prefixes on the same Redis MUST NOT see each
        other's tool messages. Service A writes to
        ``acme:knt:tools:echo:queue``; service B writes
        to ``crm:knt:tools:echo:queue``; ``xrange`` on
        the A stream does NOT see B's message and
        vice-versa. This is the canonical multi-service
        use case from ADR-076 §1.1 / DEBT §2.35.
        """
        acme_key = await _publish_via_router(
            clean_redis, agent_id="agent-acme", tool_name="echo", key_prefix="acme:"
        )
        crm_key = await _publish_via_router(
            clean_redis, agent_id="agent-crm", tool_name="echo", key_prefix="crm:"
        )

        # Each prefix wrote to its own stream key.
        assert acme_key == "acme:knt:tools:echo:queue"
        assert crm_key == "crm:knt:tools:echo:queue"
        assert acme_key != crm_key

        # Each prefix's stream has exactly one entry (the
        # other prefix's message is NOT visible).
        acme_entries = await clean_redis.xrange(acme_key)
        crm_entries = await clean_redis.xrange(crm_key)
        assert len(acme_entries) == 1
        assert len(crm_entries) == 1

        # And the unprefixed key is empty -- no service
        # accidentally wrote to the legacy wire format.
        legacy_entries = await clean_redis.xrange("knt:tools:echo:queue")
        assert legacy_entries == []

    async def test_prefixed_worker_manager_creates_group_on_prefixed_stream(
        self, clean_redis
    ) -> None:
        """A ``WorkerManager`` with ``key_prefix="acme:"``
        creates its consumer group on
        ``acme:knt:tools:echo:queue``. ``XINFO GROUPS``
        confirms the group exists at the prefixed key,
        not at the legacy unprefixed key.
        """
        # Build an EventLog so ``WorkerManager.__init__``
        # is happy (the manager does not read from the
        # EventLog on ``start`` but the type signature
        # requires one).
        log = EventLog(RedisEventLogAdapter(client=clean_redis, key_prefix="acme:"))
        manager = WorkerManager(clean_redis, log, key_prefix="acme:")
        manager._tools["echo"] = type(  # type: ignore[assignment]
            "EchoStub", (), {"name": "echo"}
        )

        await manager.start()
        try:
            groups = await clean_redis.xinfo_groups("acme:knt:tools:echo:queue")

            # ``XINFO GROUPS`` returns ``str`` keys even with
            # ``decode_responses=False`` (Redis 7 behaviour),
            # so we look up both shapes to be robust across
            # client versions.
            def _group_name(g: dict) -> str | bytes | None:
                return g.get("name", g.get(b"name"))

            assert any(
                _group_name(g) in (b"fmh_tool_workers", "fmh_tool_workers")
                for g in groups
            ), f"Consumer group not found on prefixed stream; groups={groups!r}"
        finally:
            await manager.stop()


class TestAPIKeyStorageKeyPrefix:
    """``RedisAPIKeyStorage`` namespaces every binding
    key the storage reads or writes (DEBT §2.35
    follow-up #2). Two services sharing one Redis with
    different prefixes MUST NOT see each other's
    bindings.
    """

    async def test_two_prefixes_store_at_distinct_keys(
        self, clean_redis: aioredis.Redis
    ) -> None:
        """Two ``RedisAPIKeyStorage`` instances with
        different prefixes on the same Redis MUST
        write to disjoint keys.

        Without the prefix plumbing the storage would
        write to the unprefixed ``knt:api:keys:<digest>``
        on both services -- the canonical §1.1 failure
        mode for two services sharing one Redis.
        """
        acme_storage = RedisAPIKeyStorage(client=clean_redis, key_prefix="acme:")
        crm_storage = RedisAPIKeyStorage(client=clean_redis, key_prefix="crm:")

        # Same digest on both prefixes; the bytes are
        # different per service. Without prefix plumbing
        # the second ``store`` would overwrite the first.
        await acme_storage.store("shared-digest", b"acme-payload")
        await crm_storage.store("shared-digest", b"crm-payload")

        acme_hit = await acme_storage.lookup("shared-digest")
        crm_hit = await crm_storage.lookup("shared-digest")

        assert acme_hit.is_ok() and acme_hit.ok_value() == b"acme-payload"
        assert crm_hit.is_ok() and crm_hit.ok_value() == b"crm-payload"

        # And the unprefixed legacy key is empty -- no
        # service accidentally wrote to the pre-076
        # wire format.
        legacy_raw = await clean_redis.get("knt:api:keys:shared-digest")
        assert legacy_raw is None

    async def test_prefixed_storage_round_trip(
        self, clean_redis: aioredis.Redis
    ) -> None:
        """A prefixed storage ``store`` followed by
        ``lookup`` returns the same bytes; ``delete``
        removes the binding; a follow-up ``lookup``
        returns ``Ok(None)``.

        Pin the symmetric ``store`` / ``lookup`` /
        ``delete`` wiring so a future refactor does
        not silently drop the prefix on one of the
        three paths.
        """
        storage = RedisAPIKeyStorage(client=clean_redis, key_prefix="acme-billing:")

        await storage.store("digest-xyz", b'{"role": "agent"}')
        hit = await storage.lookup("digest-xyz")
        assert hit.is_ok() and hit.ok_value() == b'{"role": "agent"}'

        await storage.delete("digest-xyz")
        miss = await storage.lookup("digest-xyz")
        assert miss.is_ok() and miss.ok_value() is None
