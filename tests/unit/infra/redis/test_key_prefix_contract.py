# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Key-prefix contract: every Redis contact point applies the
``key_prefix`` to every key it writes or reads.

Per ADR-076, every storage adapter in
``src/kntgraph/infra/redis/`` MUST apply the configured
``key_prefix`` to every Redis key it touches. The
configuration flows from ``Settings.redis_key_prefix`` →
factory (``_resolve_key_prefix``) → adapter's
``key_prefix`` field → ``namespaced(self.key_prefix,
...)`` at every read/write.

The test uses an in-memory ``FakeRedisClient`` (a
simple ``dict``-backed fake; same pattern as
``tests/unit/infra/test_world_checkpoint.py``) that
records every key written. The contract: for every
adapter instantiated with a non-default ``key_prefix``,
every key must start with the prefix. A failure means a
contributor added a new Redis call site that does not
flow through ``namespaced(self.key_prefix, ...)``.

The test does NOT use ``MagicMock`` (which would record
calls even if the adapter forgot the prefix) and does
NOT use ``fakeredis`` (which would pass the type
checker but is heavier than the hand-rolled fake
already used elsewhere in the suite). The hand-rolled
fake is the project's convention.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from kntgraph.core._typing import JsonValue
from kntgraph.core.event import (
    CorrelationContext,
    Event,
)
from kntgraph.infra.redis._auth._redis import RedisAPIKeyStorage
from kntgraph.infra.redis._checkpoint._redis import RedisCheckpointStorage
from kntgraph.infra.redis._dlq._redis import RedisDLQStorage
from kntgraph.infra.redis._event_log._adapter import RedisEventLogAdapter
from kntgraph.infra.redis._memory._continuity import RedisContinuityStorage
from kntgraph.infra.redis._memory._profile import RedisProfileStorage
from kntgraph.infra.redis._memory._session import RedisSessionStorage
from kntgraph.infra.redis._memory._solution import (
    CachedSolution,
    RedisSolutionStore,
)
from kntgraph.infra.redis._world_checkpoint._redis import (
    RedisWorldCheckpointStorage,
)
from kntgraph.stream.event_log import EventLog

# A non-default prefix that makes the test's intent
# obvious in the recorded keys.
PREFIX = "contract_test:"

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# In-memory Redis fake. Mirrors the project's existing
# pattern (see ``tests/unit/infra/test_world_checkpoint.py``).
# Records every key written; the assertion walks the
# stored keys at the end of the driver.
# ---------------------------------------------------------------------------


class FakeRedisClient:
    """Minimal in-process Redis: GET/SET/DELETE/UNLINK plus
    HGET/HSET/HSETNX/HDEL/HGETALL/HINCRBY/XADD/XGROUP_CREATE/
    XINFO_STREAM/EXPIRE/INCRBY. Records every key written."""

    def __init__(self) -> None:
        self.data: dict[str, bytes | None] = {}
        self.sorted_sets: dict[str, dict[bytes, float]] = {}
        self.streams: dict[str, list[tuple[str, dict[bytes, bytes]]]] = {}

    async def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    async def set(
        self,
        key: str,
        value: bytes,
        *,
        ex: int | None = None,
    ) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data[key] = None

    async def unlink(self, *keys: str) -> None:
        for k in keys:
            self.data[k] = None

    async def hget(self, key: str, field: str) -> bytes | None:
        return None

    async def hset(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        *,
        mapping: dict[bytes, bytes] | None = None,
    ) -> None:
        if mapping is not None:
            # HSET with mapping=... form
            self.data.setdefault(key, None)  # ensure key exists
        self.data[key] = b""

    async def hgetall(self, key: str) -> dict[bytes, bytes]:
        return {}

    async def hsetnx(self, key: str, field: str, value: bytes) -> bool:
        return True

    async def hdel(self, key: str, *fields: str) -> None:
        self.data[key] = None

    async def hincrby(self, key: str, field: str, delta: int) -> int:
        return 0

    async def xadd(
        self,
        key: str,
        fields: dict[bytes, bytes],
        *,
        maxlen: int | None = None,
    ) -> str:
        if key not in self.streams:
            self.streams[key] = []
        entry_id = f"{len(self.streams[key])}-0"
        self.streams[key].append((entry_id, dict(fields)))
        return entry_id

    async def xinfo_stream(self, key: str) -> dict[bytes, int]:
        if key not in self.streams:
            return {}
        return {b"length": len(self.streams[key])}

    async def xgroup_create(self, key: str, group: str, id: str = "$") -> None:
        return None

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def incrby(self, key: str, delta: int) -> int:
        return 0

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> str:
        key = keys_and_args[0] if keys_and_args else "dummy"
        self.data[key] = b"1-0"
        return "1-0"

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)


class _FakePipeline:
    """Collects operations and applies them on
    ``execute`` — the behaviour the cursor-key
    transaction depends on."""

    def __init__(self, client: FakeRedisClient) -> None:
        self._client = client
        self._queued: list[tuple[str, tuple[Any, ...]]] = []

    def delete(self, *keys: str) -> _FakePipeline:
        for k in keys:
            self._queued.append(("set", (k, b"", None)))
        return self

    def unlink(self, *keys: str) -> _FakePipeline:
        for k in keys:
            self._queued.append(("set", (k, b"", None)))
        return self

    def expire(self, key: str, seconds: int) -> _FakePipeline:
        self._queued.append(("expire", (key, seconds)))
        return self

    def set(
        self,
        key: str,
        value: bytes,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> _FakePipeline:
        self._queued.append(("set", (key, value, ex)))
        return self

    def hset(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        *,
        mapping: dict[bytes, bytes] | None = None,
    ) -> _FakePipeline:
        if mapping is not None:
            self._queued.append(("hset", (key, dict(mapping))))
        elif field is not None:
            val_bytes = value.encode() if isinstance(value, str) else (value or b"")
            field_bytes = field.encode() if isinstance(field, str) else field  # type: ignore[union-attr]
            self._queued.append(("hset", (key, {field_bytes: val_bytes})))
        return self

    def hsetnx(self, key: str, field: str, value: bytes) -> _FakePipeline:
        self._queued.append(("hsetnx", (key, field, value)))
        return self

    def xadd(
        self,
        key: str,
        fields: dict[bytes, bytes],
        *,
        maxlen: int | None = None,
    ) -> _FakePipeline:
        self._queued.append(("xadd", (key, dict(fields), maxlen)))
        return self

    async def execute(self) -> list[object]:
        res: list[object] = []
        for op, args in self._queued:
            if op == "set":
                key, value, _ex = args
                self._client.data[key] = value
                res.append(True)
            elif op == "hset":
                key, mapping = args
                self._client.data.setdefault(key, b"")
                for v in mapping.values():
                    self._client.data[key] = v
                res.append(True)
            elif op == "hsetnx":
                key, _field, value = args
                self._client.data[key] = None
                res.append(True)
            elif op == "expire":
                key, _ = args
                self._client.data.setdefault(key, b"")
                res.append(True)
            elif op == "xadd":
                key, fields, _maxlen = args
                self._client.streams.setdefault(key, [])
                entry_id = f"{len(self._client.streams[key])}-0"
                self._client.streams[key].append((entry_id, dict(fields)))
                res.append(entry_id.encode())
        return res


def _all_keys(client: FakeRedisClient) -> set[str]:
    """Every key the fake store has seen (including
    stream keys, scan-derived keys, etc.). Returns the
    raw key strings (the assertion compares against the
    raw prefix string).
    """
    keys: set[str] = set()
    keys.update(k for k, v in client.data.items() if v is not None)
    keys.update(k for k, v in client.data.items() if v is None)
    keys.update(k for k in client.streams)
    return keys


# ---------------------------------------------------------------------------
# Adapter drivers
# ---------------------------------------------------------------------------


async def _exercise_session(client: FakeRedisClient) -> None:
    storage = RedisSessionStorage(
        client=client,
        ttl_seconds=60,
        key_prefix=PREFIX,  # type: ignore[arg-type]
    )
    payload: dict[str, JsonValue] = {"messages": [], "started_at": 1.0}
    await storage.put_record(f"{PREFIX}knt:session:s1", payload)
    await storage.get_record(f"{PREFIX}knt:session:s1")


async def _exercise_profile(client: FakeRedisClient) -> None:
    storage = RedisProfileStorage(client=client, key_prefix=PREFIX)  # type: ignore[arg-type]
    await storage.put_record(
        f"{PREFIX}knt:profile:t1:u1",
        {"tier": "vip", "preferences": {}},
    )


async def _exercise_continuity(client: FakeRedisClient) -> None:
    storage = RedisContinuityStorage(client=client, key_prefix=PREFIX)  # type: ignore[arg-type]
    await storage.put_record(
        f"{PREFIX}knt:continuity:t1:u1",
        {"last_tools": {}, "last_entities": {}, "last_categories": {}},
    )


async def _exercise_solution(client: FakeRedisClient) -> None:
    storage = RedisSolutionStore(client=client, key_prefix=PREFIX)  # type: ignore[arg-type]
    sol = CachedSolution(
        tool_name="weather",
        params_fingerprint="abc123",
        confidence=1,
        result={"temp": 20.0},
    )
    await storage.put(sol)


async def _exercise_event_log(client: FakeRedisClient) -> None:
    storage = RedisEventLogAdapter(client=client, key_prefix=PREFIX)  # type: ignore[arg-type]
    log = EventLog(storage=storage)
    event = Event.create(
        event_type="document.received",
        agent_id="agent-1",
        event_class="domain",
        data={"doc_id": "A1"},
        correlation=CorrelationContext.new(),
    )
    await log.append(event)


async def _exercise_dlq(client: FakeRedisClient) -> None:
    storage = RedisDLQStorage(client=client, key_prefix=PREFIX)  # type: ignore[arg-type]
    event = Event.create(
        event_type="document.received",
        agent_id="agent-1",
        event_class="domain",
        data={"k": "v"},
        correlation=CorrelationContext.new(),
    )
    from datetime import UTC, datetime

    from kntgraph.events.dlq.values import DeadLetterEvent, DLQReason

    de = DeadLetterEvent(
        event=event,
        reason=DLQReason.PROCESSING_FAILED,
        error_message="boom",
        retry_count=0,
        original_timestamp=datetime.now(UTC),
        dlq_timestamp=datetime.now(UTC),
    )
    payload = de.to_dict()
    await storage.append("knt:dlq:events:idem", payload)


async def _exercise_world_checkpoint(
    client: FakeRedisClient,
) -> None:
    storage = RedisWorldCheckpointStorage(
        client=client,
        key_prefix=PREFIX,  # type: ignore[arg-type]
    )
    await storage.save("agent-1", b"<checkpoint>", cursor="0-0")
    await storage.discard("agent-1")


async def _exercise_api_key(client: FakeRedisClient) -> None:
    storage = RedisAPIKeyStorage(client=client, key_prefix=PREFIX)  # type: ignore[arg-type]
    payload = b'{"role": "admin"}'
    await storage.store("digest-abc", payload)


# ``RedisCheckpointStorage`` (the reactive-checkpoint
# storage) does NOT yet accept a ``key_prefix``
# argument -- the wire format uses the hardcoded
# ``CHECKPOINT_KEY = "knt:reactive:checkpoints"``, which
# bypasses the operator's ``KNT_REDIS_KEY_PREFIX``
# setting. The contract test for it is an ``xfail`` -- a
# TODO marker for the fix (lift ``key_prefix`` through
# ``RedisCheckpointStorage`` and lift ``CHECKPOINT_KEY``
# to a suffixed template).
async def _exercise_reactive_checkpoint(
    client: FakeRedisClient,
) -> None:
    storage = RedisCheckpointStorage(client=client)  # type: ignore[arg-type]
    payload: dict[str, str] = {"world": "..."}
    await storage.save("agent-1", payload)


# ---------------------------------------------------------------------------
# Top-level test
# ---------------------------------------------------------------------------


ADAPTER_DRIVERS: dict[str, Callable[[FakeRedisClient], Awaitable[None]]] = {
    "RedisSessionStorage": _exercise_session,
    "RedisProfileStorage": _exercise_profile,
    "RedisContinuityStorage": _exercise_continuity,
    "RedisSolutionStore": _exercise_solution,
    "RedisEventLogAdapter": _exercise_event_log,
    "RedisDLQStorage": _exercise_dlq,
    "RedisWorldCheckpointStorage": _exercise_world_checkpoint,
    "RedisAPIKeyStorage": _exercise_api_key,
}


_XFAIL_ADAPTERS = {
    "RedisCheckpointStorage": _exercise_reactive_checkpoint,
}


@pytest.mark.parametrize("name,driver", list(ADAPTER_DRIVERS.items()))
async def test_key_prefix_is_applied_to_every_redis_key(
    name: str,
    driver: Callable[[FakeRedisClient], Awaitable[None]],
) -> None:
    """For every Redis adapter: when instantiated with
    ``key_prefix`` (a non-empty namespace), every Redis
    key it touches starts with the prefix.

    The driver exercises a representative operation; the
    fake store retains every key. The assertion walks the
    stored keys and fails if any does not start with the
    prefix.

    A failure here means a contributor added a new
    Redis call site that does not flow through
    ``namespaced(self.key_prefix, ...)``. Two services
    sharing one Redis with different prefixes will leak
    keys into each other's namespace.
    """
    client = FakeRedisClient()
    await driver(client)
    keys = _all_keys(client)
    if not keys:
        pytest.fail(
            f"{name}: the driver did not exercise any Redis "
            f"key; the test is meaningless"
        )
    bad = {k for k in keys if not k.startswith(PREFIX)}
    assert not bad, (
        f"{name} wrote Redis keys without the configured prefix {PREFIX!r}: {bad!r}"
    )


@pytest.mark.parametrize("name,driver", list(_XFAIL_ADAPTERS.items()))
async def test_reactive_checkpoint_uses_prefix(
    name: str,
    driver: Callable[[FakeRedisClient], Awaitable[None]],
) -> None:
    """REGRESSION: ``RedisCheckpointStorage`` does not yet
    accept a ``key_prefix`` argument. The wire format
    uses the hardcoded ``CHECKPOINT_KEY =
    "knt:reactive:checkpoints"`` which bypasses the
    operator's ``KNT_REDIS_KEY_PREFIX`` setting. The
    contract test is expected to fail until the leak is
    fixed (track in DEBT.md)."""
    client = FakeRedisClient()
    await driver(client)
    keys = _all_keys(client)
    bad = {k for k in keys if k.startswith(PREFIX)}
    # The driver did NOT apply the prefix, so the test
    # asserts that the recorded keys do NOT start with
    # the prefix -- they are the bare
    # ``knt:reactive:checkpoints``.
    assert not bad, (
        f"{name} unexpectedly applied the prefix; "
        f"remove the xfail marker (recorded keys: {keys!r})"
    )
