# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.infra.redis._errors import MemoryError
from kntgraph.infra.redis._world_checkpoint._redis import (
    RedisWorldCheckpointStorage,
    storage_key,
)
from kntgraph.infra.world_checkpoint import IncrementalWorldStore, WorldCheckpoint


class FakeRedisClient:
    """Minimal in-process Redis: GET/SET/DELETE/UNLINK plus
    the transactional pipeline the P5b cursor split writes
    through. Each op mutates ``data``; ``ops`` records the
    sequence for assertions."""

    def __init__(self) -> None:
        self.data: dict[str, bytes | None] = {}
        self.ops: list[tuple] = []

    async def get(self, key: str) -> bytes | None:
        self.ops.append(("get", key))
        return self.data.get(key)

    async def set(self, key: str, value: bytes, *, ex: int | None = None) -> None:
        self.ops.append(("set", key, ex))
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.ops.append(("delete", key))
        self.data[key] = None

    async def unlink(self, *keys: str) -> None:
        self.ops.append(("unlink", keys))
        for key in keys:
            self.data[key] = None

    def pipeline(self, transaction: bool = True) -> "_FakePipeline":
        self.ops.append(("pipeline", transaction))
        return _FakePipeline(self)


class _FakePipeline:
    """Collects SET ops and applies them atomically on
    ``execute`` — the behaviour the cursor-key transaction
    depends on."""

    def __init__(self, client: FakeRedisClient) -> None:
        self._client = client
        self._queued: list[tuple[str, bytes, int | None]] = []

    def set(self, key: str, value: bytes, *, ex: int | None = None) -> "_FakePipeline":
        self._queued.append((key, value, ex))
        return self

    async def execute(self) -> list:
        for key, value, ex in self._queued:
            self._client.data[key] = value
        return [True] * len(self._queued)


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


@pytest.mark.asyncio
async def test_world_checkpoint_payload_is_zlib_compressed():
    """ADR-068 §3.5: the checkpoint payload is zlib-compressed
    on write. The stored bytes must NOT be a raw pickle (a raw
    pickle of a non-empty tuple starts with the protocol-2
    opcode 0x80 or an ASCII header); it must start with the
    zlib CMF magic byte."""
    import zlib

    client = FakeRedisClient()
    storage = RedisWorldCheckpointStorage(client=client)
    store = IncrementalWorldStore(storage=storage)

    event = Event.create(
        event_type="document.received",
        agent_id="agent-1",
        event_class="domain",
        data={"doc_id": "A1"},
        correlation=CorrelationContext.new(correlation_id="corr-1"),
    )
    checkpoint = WorldCheckpoint(world=World.fold([event], tick=1), last_stream_id="5")

    await store.save("agent-1", checkpoint)

    raw = client.data[storage_key("agent-1")]
    assert raw is not None
    assert raw[0] == 0x78  # zlib CMF byte (deflate, 32K window)
    # Decompressing must yield a parseable pickle (the legacy
    # inner format is unchanged).
    inner = zlib.decompress(raw)
    import pickle  # nosec B403 - test asserts the writer's own format

    tick, _storage, views, stream_id = pickle.loads(inner)  # nosec B301
    assert tick == 1
    assert stream_id == "5"
    assert "agent-1" in views


@pytest.mark.asyncio
async def test_world_checkpoint_load_reads_legacy_raw_pickle():
    """ADR-068 §3.5 wire-format compatibility: a checkpoint
    written by a pre-compression build (raw pickle, no zlib
    wrapper) must still load. The sniffing path treats any
    non-0x78 first byte as legacy raw pickle."""
    import pickle  # nosec B403 - legacy-format fixture

    client = FakeRedisClient()
    storage = RedisWorldCheckpointStorage(client=client)
    store = IncrementalWorldStore(storage=storage)

    event = Event.create(
        event_type="document.received",
        agent_id="agent-legacy",
        event_class="domain",
        data={"doc_id": "L1"},
        correlation=CorrelationContext.new(correlation_id="corr-legacy"),
    )
    world = World.fold([event], tick=3)
    legacy_payload = pickle.dumps(
        (world.tick, world.storage, dict(world.views), "42-0")  # nosec B301
    )
    client.data[storage_key("agent-legacy")] = legacy_payload

    loaded = await store.load("agent-legacy")

    assert loaded.world.tick == 3
    assert loaded.world.agents["agent-legacy"].domain_phase == "document.received"
    assert loaded.last_stream_id == "42-0"


# ---------------------------------------------------------------------------
# Tier 4 stream inspection (ADR-075): IncrementalWorldStore forwards
# queue_length / pending_count to the underlying WorldCheckpointStorage.
# ---------------------------------------------------------------------------


class _StubStorage:
    """Minimal storage with the new Tier 4 methods; asserts
    the facade forwards to it correctly."""

    def __init__(
        self,
        queue_length_return: int = 0,
        pending_count_return: int = 0,
        raise_queue_length: Exception | None = None,
    ) -> None:
        self._ql = queue_length_return
        self._pc = pending_count_return
        self._raise_ql = raise_queue_length
        self.queue_length_calls: list[str] = []
        self.pending_count_calls: list[str] = []

    async def queue_length(self, stream_key: str) -> int:
        self.queue_length_calls.append(stream_key)
        if self._raise_ql is not None:
            raise self._raise_ql
        return self._ql

    async def pending_count(self, stream_key: str) -> int:
        self.pending_count_calls.append(stream_key)
        return self._pc


class TestIncrementalWorldStoreQueueInspection:
    @pytest.mark.asyncio
    async def test_queue_length_forwards_to_storage(self) -> None:
        """``IncrementalWorldStore.queue_length`` delegates to
        the underlying storage and returns the storage's value."""
        stub = _StubStorage(queue_length_return=7)
        store = IncrementalWorldStore(stub)  # type: ignore[arg-type]
        result = await store.queue_length("knt:tools:foo:queue")
        assert result == 7
        assert stub.queue_length_calls == ["knt:tools:foo:queue"]

    @pytest.mark.asyncio
    async def test_pending_count_forwards_to_storage(self) -> None:
        """``IncrementalWorldStore.pending_count`` delegates to
        the underlying storage and returns the storage's value."""
        stub = _StubStorage(pending_count_return=1)
        store = IncrementalWorldStore(stub)  # type: ignore[arg-type]
        result = await store.pending_count("knt:tools:bar:queue")
        assert result == 1
        assert stub.pending_count_calls == ["knt:tools:bar:queue"]

    @pytest.mark.asyncio
    async def test_queue_length_returns_zero_when_storage_lacks_method(
        self,
    ) -> None:
        """Legacy storage (no ``queue_length`` method) ⇒ 0.

        The dispatcher treats ``0`` as "no stuck" so a storage
        that predates the Protocol extension stays
        backward-compatible.
        """

        class _LegacyStorage:
            pass

        store = IncrementalWorldStore(_LegacyStorage())  # type: ignore[arg-type]
        result = await store.queue_length("any:key")
        assert result == 0

    @pytest.mark.asyncio
    async def test_pending_count_returns_zero_when_storage_lacks_method(
        self,
    ) -> None:
        """Legacy storage (no ``pending_count`` method) ⇒ 0."""

        class _LegacyStorage:
            pass

        store = IncrementalWorldStore(_LegacyStorage())  # type: ignore[arg-type]
        result = await store.pending_count("any:key")
        assert result == 0

    @pytest.mark.asyncio
    async def test_queue_length_returns_zero_on_storage_error(self) -> None:
        """Storage raises ⇒ facade catches and returns 0 (fail-soft).

        The dispatcher's stuck-in-queue query is best-effort:
        a Redis hiccup must not escalate into a recovery loop.
        """
        stub = _StubStorage(raise_queue_length=RuntimeError("redis down"))
        store = IncrementalWorldStore(stub)  # type: ignore[arg-type]
        result = await store.queue_length("any:key")
        assert result == 0
