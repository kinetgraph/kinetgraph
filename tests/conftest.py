# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Pytest configuration and fixtures.
"""

import asyncio
from typing import Generator
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio


# Mock Redis for unit tests
class MockRedis:
    """Mock Redis client for unit tests."""

    def __init__(self):
        self.data = {}
        self.streams = {}
        self.pubsub_channels = {}

    async def xadd(self, key: str, fields: dict, maxlen: int | None = None) -> bytes:
        if key not in self.streams:
            self.streams[key] = []

        event_id = f"{len(self.streams[key]) + 1}-0"
        # Store fields as-is (mock doesn't encode)
        self.streams[key].append((event_id.encode(), fields))

        if maxlen and len(self.streams[key]) > maxlen:
            self.streams[key] = self.streams[key][-maxlen:]

        return event_id.encode()

    async def xrange(
        self, key: str, min: str = "-", max: str = "+", count: int | None = None
    ):
        if key not in self.streams:
            return []

        results = self.streams[key]
        if count:
            results = results[:count]

        # Return in format expected by EventStore
        return results

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict,
        count: int | None = None,
        block: int | None = None,
    ):
        # Mock implementation
        return []

    async def xack(self, key: str, groupname: str, message_id: str):
        pass

    async def xinfo_stream(self, key: str) -> dict:
        if key not in self.streams:
            return {"length": 0, "last-entry": (b"0", {})}  # type: ignore[dict-item]

        last_entry = self.streams[key][-1] if self.streams[key] else (b"0", {})
        return {
            "length": len(self.streams[key]),
            "last-entry": last_entry,
        }

    async def scan_iter(self, pattern: str, count: int = 100):
        # Convert pattern to simple glob matching
        pattern_prefix = pattern.replace("*", "")
        for key in self.streams.keys():
            if pattern_prefix in key or key.startswith(pattern_prefix):
                yield key.encode()

    async def hset(self, key: str, mapping: dict | None = None, **kwargs):
        if key not in self.data:
            self.data[key] = {}
        if mapping:
            self.data[key].update(
                {
                    k.encode() if isinstance(k, str) else k: (
                        v.encode() if isinstance(v, str) else v
                    )
                    for k, v in mapping.items()
                }
            )

    async def hgetall(self, key: str) -> dict:
        return self.data.get(key, {})

    async def hincrby(self, key: str, field: str, amount: int):
        if key not in self.data:
            self.data[key] = {}
        current = int(self.data[key].get(field.encode(), 0))
        self.data[key][field.encode()] = str(current + amount).encode()
        return current + amount

    async def expire(self, key: str, seconds: int):
        pass

    async def publish(self, channel: str, message: str):
        if channel not in self.pubsub_channels:
            self.pubsub_channels[channel] = []
        self.pubsub_channels[channel].append(message)

    def pubsub(self):
        return MockPubSub(self)

    def pipeline(self, transaction: bool = False) -> "MockPipeline":
        return MockPipeline(self)

    async def close(self):
        pass


class MockPubSub:
    """Mock Redis PubSub."""

    def __init__(self, redis: MockRedis):
        self.redis = redis
        self.subscribed = []

    async def subscribe(self, *channels):
        self.subscribed.extend(channels)

    async def listen(self):
        while True:
            for channel in self.subscribed:
                if channel in self.redis.pubsub_channels:
                    for message in self.redis.pubsub_channels[channel]:
                        yield {"type": "message", "data": message}
            await asyncio.sleep(0.1)


class MockPipeline:
    """Mock Redis Pipeline."""

    def __init__(self, redis: MockRedis):
        self.redis = redis
        self.commands = []

    def multi(self):
        pass

    async def execute(self):
        results = []
        for cmd, args, kwargs in self.commands:
            result = await cmd(*args, **kwargs)
            results.append(result)
        return results

    def xadd(self, key: str, fields: dict, maxlen: int | None = None) -> "MockPipeline":
        self.commands.append((self.redis.xadd, [key, fields], {"maxlen": maxlen}))
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest_asyncio.fixture
async def mock_redis() -> MockRedis:
    """Fixture for mock Redis client."""
    return MockRedis()


@pytest_asyncio.fixture
async def mock_redis_client() -> AsyncMock:
    """Fixture for AsyncMock Redis client."""
    client = AsyncMock()
    client.xadd = AsyncMock(return_value=b"1-0")
    client.xrange = AsyncMock(return_value=[])
    client.xreadgroup = AsyncMock(return_value=[])
    client.xack = AsyncMock()
    client.xinfo_stream = AsyncMock(return_value={"length": 0})
    client.scan_iter = AsyncMock(return_value=[])
    client.hset = AsyncMock()
    client.hgetall = AsyncMock(return_value={})
    client.hincrby = AsyncMock(return_value=1)
    client.expire = AsyncMock()
    client.publish = AsyncMock()
    client.pubsub = MagicMock(return_value=MockPubSub(MockRedis()))
    client.pipeline = MagicMock(return_value=MockPipeline(MockRedis()))
    client.close = AsyncMock()
    return client


@pytest_asyncio.fixture(autouse=True)
async def reset_correlation_context():
    """Reset correlation context between tests.

    Sets a default CorrelationContext for the test body so
    call sites that read ``correlation_middleware.current()``
    (memory/profile, memory/session, memory/continuity/manager,
    memory/continuity/recorders/*) get a non-None context under
    ADR-037. Tests that want to assert on a specific flow id
    should use the ``sample_correlation_context`` fixture or
    call ``correlation_middleware.scope(...)`` directly.
    """
    from kntgraph.core.event import CorrelationContext, correlation_middleware
    from kntgraph.core.event.correlation import _correlation_context

    ctx = CorrelationContext.new(correlation_id=uuid4())
    _correlation_context.set(ctx)
    yield
    correlation_middleware.clear()


@pytest.fixture(autouse=True)
def reset_settings_cache():
    """
    Drop the `fresh_settings` lru_cache between tests so a
    `monkeypatch.setenv(...)` in test N does not leak into
    test N+1 via the cached singleton.
    """
    from kntgraph.infra.config import fresh_settings

    fresh_settings.cache_clear()
    yield
    fresh_settings.cache_clear()


@pytest.fixture
def sample_correlation_context():
    """Sample correlation context for tests."""
    from uuid import UUID

    from kntgraph.core.event import CorrelationContext

    return CorrelationContext(
        correlation_id=UUID("00000000-0000-0000-0000-000000000001"),
        causation_id=None,
        span_id=UUID("00000000-0000-0000-0000-000000000002"),
        metadata={"tenant_id": "test-tenant", "test": True},
    )


@pytest.fixture
def sample_agent_id() -> str:
    """Sample agent ID for tests."""
    return "agent-test-123"


@pytest.fixture
def sample_tenant_id() -> str:
    """Sample tenant ID for tests."""
    return "tenant-test-123"


@pytest.fixture
def event_loop() -> Generator:
    """Create event loop for tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# Real Redis fixture for integration tests
try:
    import redis.asyncio as aioredis

    @pytest_asyncio.fixture
    async def real_redis():
        """Real Redis client for integration tests."""
        import os

        redis_password = os.environ.get("KNT_REDIS_PASSWORD", "redispassword")
        client = aioredis.Redis(
            host="localhost",
            port=6379,
            db=0,
            password=redis_password,
            decode_responses=False,
        )
        # Test connection
        await client.ping()
        yield client
        # Cleanup
        await client.flushdb()
        await client.close()

except Exception:
    pass
