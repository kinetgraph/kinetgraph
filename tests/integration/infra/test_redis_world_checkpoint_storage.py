# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Integration tests for ``RedisWorldCheckpointStorage`` Tier 4
methods (``queue_length`` and ``pending_count``).

The methods are the Redis-side implementation of the
``WorldCheckpointStorage`` Protocol extensions added in
ADR-075 Tier 4. They use only methods declared on
``RedisLike`` (``xinfo_stream`` and ``xpending_range``) so
the storage stays within the typed adapter boundary.

These tests run against a live Redis (the
``clean_redis`` fixture flushes the database before each
test). They cover:

  - missing stream / missing consumer group
  - existing stream with / without pending entries
  - tool-group name override
"""

from __future__ import annotations

import pytest

from kntgraph.infra.redis._world_checkpoint import RedisWorldCheckpointStorage


pytestmark = pytest.mark.asyncio


class TestRedisWorldCheckpointStorageQueueInspection:
    async def test_queue_length_returns_zero_for_missing_stream(
        self, clean_redis
    ) -> None:
        """``XINFO STREAM`` on a missing key fails; we return 0."""
        storage = RedisWorldCheckpointStorage(client=clean_redis)
        result = await storage.queue_length("knt:tools:nonexistent:queue")
        assert result == 0

    async def test_pending_count_returns_zero_for_missing_stream(
        self, clean_redis
    ) -> None:
        """``XPENDING`` on a missing group fails; we return 0."""
        storage = RedisWorldCheckpointStorage(client=clean_redis)
        result = await storage.pending_count("knt:tools:nonexistent:queue")
        assert result == 0

    async def test_queue_length_returns_count_for_existing_stream(
        self, clean_redis
    ) -> None:
        """An existing stream with N entries reports N."""
        stream_key = "knt:tools:echo:queue"
        for _ in range(4):
            await clean_redis.xadd(stream_key, {"payload": "{}"})
        storage = RedisWorldCheckpointStorage(client=clean_redis)
        result = await storage.queue_length(stream_key)
        assert result == 4

    async def test_pending_count_returns_zero_for_empty_pel(self, clean_redis) -> None:
        """Stream exists but consumer group has no entries ⇒ 0."""
        stream_key = "knt:tools:echo:queue"
        group_name = "fmh_tool_workers"
        # Create the stream + group.
        await clean_redis.xadd(stream_key, {"payload": "{}"})
        await clean_redis.xgroup_create(stream_key, group_name, id="0", mkstream=True)
        storage = RedisWorldCheckpointStorage(
            client=clean_redis, tool_group_name=group_name
        )
        result = await storage.pending_count(stream_key)
        assert result == 0

    async def test_pending_count_returns_positive_when_consumer_holds_messages(
        self, clean_redis
    ) -> None:
        """A consumer that read but didn't ack leaves entries in PEL.

        ``pending_count`` returns a positive value (``1``) when
        any entry is in the PEL; the exact count is intentionally
        NOT reported (see Protocol docstring — the dispatcher's
        stuck detection is binary).
        """
        stream_key = "knt:tools:echo:queue"
        group_name = "fmh_tool_workers"
        await clean_redis.xadd(stream_key, {"payload": "a"})
        await clean_redis.xadd(stream_key, {"payload": "b"})
        await clean_redis.xgroup_create(stream_key, group_name, id="0", mkstream=True)
        # Read without ack so the messages stay in the PEL.
        await clean_redis.xreadgroup(
            groupname=group_name,
            consumername="worker-1",
            streams={stream_key: ">"},
            count=2,
        )
        storage = RedisWorldCheckpointStorage(
            client=clean_redis, tool_group_name=group_name
        )
        result = await storage.pending_count(stream_key)
        assert result > 0

    async def test_pending_count_uses_default_group_name(self, clean_redis) -> None:
        """Default ``tool_group_name`` is ``"fmh_tool_workers"``."""
        storage = RedisWorldCheckpointStorage(client=clean_redis)
        assert storage._tool_group_name == "fmh_tool_workers"
        assert storage.DEFAULT_TOOL_GROUP == "fmh_tool_workers"

    async def test_pending_count_respects_custom_group_name(self, clean_redis) -> None:
        """Custom ``tool_group_name`` is used for the probe."""
        stream_key = "knt:tools:custom:queue"
        custom_group = "my_custom_group"
        await clean_redis.xadd(stream_key, {"payload": "{}"})
        await clean_redis.xgroup_create(stream_key, custom_group, id="0", mkstream=True)
        await clean_redis.xreadgroup(
            groupname=custom_group,
            consumername="c1",
            streams={stream_key: ">"},
            count=1,
        )
        storage = RedisWorldCheckpointStorage(
            client=clean_redis, tool_group_name=custom_group
        )
        result = await storage.pending_count(stream_key)
        assert result > 0

    async def test_queue_length_does_not_raise_on_redis_error(
        self, clean_redis
    ) -> None:
        """Storage returns 0 on backend failure (fail-soft).

        The dispatcher treats ``0`` as "no stuck this tick" and
        reruns the query next tick — a Redis hiccup never
        escalates into a recovery loop.
        """

        # Inject a fake client whose ``xinfo_stream`` raises.
        class _BrokenClient:
            async def xinfo_stream(self, key):  # pragma: no cover
                raise RuntimeError("redis down")

        storage = RedisWorldCheckpointStorage(
            client=_BrokenClient()  # type: ignore[arg-type]
        )
        result = await storage.queue_length("any:key")
        assert result == 0

    async def test_pending_count_does_not_raise_on_redis_error(
        self, clean_redis
    ) -> None:
        """Storage returns 0 on backend failure (fail-soft)."""

        class _BrokenClient:
            async def xpending_range(self, **kwargs):  # pragma: no cover
                raise RuntimeError("redis down")

        storage = RedisWorldCheckpointStorage(
            client=_BrokenClient()  # type: ignore[arg-type]
        )
        result = await storage.pending_count("any:key")
        assert result == 0
