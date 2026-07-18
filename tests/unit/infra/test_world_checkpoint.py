# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pickle

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.result import Err, Ok
from kntgraph.core.world import World
from kntgraph.infra.redis._errors import MemoryError
from kntgraph.infra.redis._world_checkpoint._redis import (
    RedisWorldCheckpointStorage,
    storage_key,
)
from kntgraph.infra.world_checkpoint import IncrementalWorldStore, WorldCheckpoint


class FakeRedisClient:
    def __init__(self) -> None:
        self.data: dict[str, bytes | None] = {}

    async def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    async def set(self, key: str, value: bytes, *, ex: int | None = None) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data[key] = None


class FailingRedisClient(FakeRedisClient):
    async def get(self, key: str) -> bytes | None:
        raise RuntimeError("boom")

    async def set(self, key: str, value: bytes, *, ex: int | None = None) -> None:
        raise RuntimeError("boom")

    async def delete(self, key: str) -> None:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_world_checkpoint_store_roundtrip_and_ttl_payload():
    client = FakeRedisClient()
    storage = RedisWorldCheckpointStorage(client=client)
    store = IncrementalWorldStore(storage=storage, ttl_s=123)

    event = Event.create(
        event_type="document.received",
        agent_id="agent-1",
        event_class="domain",
        data={"doc_id": "A1"},
        correlation=CorrelationContext.new(correlation_id="corr-1"),
    )
    world = World.fold([event], tick=1)
    checkpoint = WorldCheckpoint(world=world, last_stream_id="123")

    await store.save("agent-1", checkpoint)

    loaded = await store.load("agent-1")

    assert loaded.world.tick == 1
    assert loaded.world.agents["agent-1"].domain_phase == "document.received"
    assert loaded.last_stream_id == "123"
    assert client.data[storage_key("agent-1")] is not None


@pytest.mark.asyncio
async def test_world_checkpoint_store_returns_empty_checkpoint_on_miss():
    storage = RedisWorldCheckpointStorage(client=FakeRedisClient())
    store = IncrementalWorldStore(storage=storage)

    loaded = await store.load("missing-agent")

    assert loaded.world.tick == 0
    assert loaded.last_stream_id == "-"


@pytest.mark.asyncio
async def test_world_checkpoint_store_returns_empty_checkpoint_on_storage_error():
    storage = RedisWorldCheckpointStorage(client=FailingRedisClient())
    store = IncrementalWorldStore(storage=storage)

    loaded = await store.load("agent-1")

    assert loaded.world.tick == 0
    assert loaded.last_stream_id == "-"


@pytest.mark.asyncio
async def test_world_checkpoint_store_handles_save_and_discard_errors():
    storage = RedisWorldCheckpointStorage(client=FailingRedisClient())
    store = IncrementalWorldStore(storage=storage)
    checkpoint = WorldCheckpoint(world=World.empty(), last_stream_id="-")

    await store.save("agent-1", checkpoint)
    await store.discard("agent-1")


@pytest.mark.asyncio
async def test_world_checkpoint_storage_load_returns_error_for_unexpected_payload_type():
    client = FakeRedisClient()
    client.data[storage_key("agent-1")] = "not-bytes"
    storage = RedisWorldCheckpointStorage(client=client)

    result = await storage.load("agent-1")

    assert result.is_err()
    assert isinstance(result.err_value(), MemoryError)


@pytest.mark.asyncio
async def test_world_checkpoint_storage_save_and_discard_use_result_contract():
    client = FakeRedisClient()
    storage = RedisWorldCheckpointStorage(client=client)

    save_result = await storage.save("agent-1", b"payload")
    discard_result = await storage.discard("agent-1")

    assert save_result.is_ok()
    assert save_result.ok_value() is None
    assert discard_result.is_ok()
    assert discard_result.ok_value() is None


@pytest.mark.asyncio
async def test_world_checkpoint_storage_load_returns_ok_none_on_miss():
    client = FakeRedisClient()
    storage = RedisWorldCheckpointStorage(client=client)

    result = await storage.load("missing")

    assert result.is_ok()
    assert result.ok_value() is None
