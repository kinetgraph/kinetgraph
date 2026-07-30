# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``infra/checkpoint.py`` (``CheckpointStore``
and ``ReactiveCheckpoint``).

Closes the infra/checkpoint coverage gap (DEBT §3,
42% → 100%). The module has:

  - ``ReactiveCheckpoint`` — frozen dataclass with
    ``to_dict`` / ``from_dict`` round-trip.
  - ``CheckpointStore`` — thin facade over the
    ``CheckpointStorage`` Protocol, with 5 public
    methods (``load`` / ``save`` / ``clear`` /
    ``load_all`` / ``clear_all``). Every method logs
    the storage error and returns a safe default
    (``None`` / ``None`` / empty dict) so the dispatcher
    can continue past a transient I/O failure.
  - ``utcnow`` — the timestamp helper used by
    ``ReactiveCheckpoint.confirmed_at``.

The tests use ``AsyncMock`` for the storage Protocol
(no real Redis needed). The real ``RedisCheckpointStorage``
is exercised in
``tests/unit/infra/redis/_checkpoint/test_storage.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.infra.checkpoint import (
    CheckpointStore,
    ReactiveCheckpoint,
    utcnow,
)
from kntgraph.infra.redis._errors import MemoryError


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _capture_structlog_writes(caplog):
    """Reconfigure structlog so its output flows through
    the stdlib ``logging`` tree (which pytest's
    ``caplog`` captures). The fixture restores the
    original config on teardown.
    """
    import structlog

    caplog.set_level("WARNING")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
    )
    yield
    structlog.reset_defaults()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage_mock():
    storage = MagicMock()
    storage.load = AsyncMock()
    storage.save = AsyncMock()
    storage.clear = AsyncMock()
    storage.load_all = AsyncMock()
    storage.clear_all = AsyncMock()
    return storage


@pytest.fixture
def store(storage_mock) -> CheckpointStore:
    return CheckpointStore(storage_mock)


@pytest.fixture
def checkpoint() -> ReactiveCheckpoint:
    return ReactiveCheckpoint(
        agent_id="agent-1",
        last_event_id=uuid.uuid4(),
        last_stream_id="1234567890-0",
        confirmed_at=datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc),
        state_hash="sha256:abc",
    )


# ---------------------------------------------------------------------------
# ReactiveCheckpoint
# ---------------------------------------------------------------------------


class TestReactiveCheckpoint:
    def test_to_dict_round_trip(self, checkpoint: ReactiveCheckpoint) -> None:
        data = checkpoint.to_dict()
        assert data["last_event_id"] == str(checkpoint.last_event_id)
        assert data["last_stream_id"] == checkpoint.last_stream_id
        assert data["confirmed_at"] == checkpoint.confirmed_at.isoformat()
        assert data["state_hash"] == "sha256:abc"

    def test_from_dict_round_trip(self, checkpoint: ReactiveCheckpoint) -> None:
        data = checkpoint.to_dict()
        restored = ReactiveCheckpoint.from_dict(checkpoint.agent_id, data)
        assert restored.agent_id == checkpoint.agent_id
        assert restored.last_event_id == checkpoint.last_event_id
        assert restored.last_stream_id == checkpoint.last_stream_id
        assert restored.confirmed_at == checkpoint.confirmed_at
        assert restored.state_hash == checkpoint.state_hash

    def test_state_hash_optional(self) -> None:
        ck = ReactiveCheckpoint(
            agent_id="agent-1",
            last_event_id=uuid.uuid4(),
            last_stream_id="1-0",
            confirmed_at=datetime.now(timezone.utc),
            state_hash=None,
        )
        assert ck.state_hash is None
        restored = ReactiveCheckpoint.from_dict(ck.agent_id, ck.to_dict())
        assert restored.state_hash is None

    def test_is_frozen(self, checkpoint: ReactiveCheckpoint) -> None:
        with pytest.raises(Exception):
            checkpoint.agent_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CheckpointStore.load
# ---------------------------------------------------------------------------


class TestLoad:
    async def test_load_returns_none_when_storage_returns_none(
        self, store, storage_mock
    ) -> None:
        from kntgraph.core.result import Ok

        storage_mock.load = AsyncMock(return_value=Ok(None))
        assert await store.load("agent-1") is None

    async def test_load_returns_none_on_storage_error(
        self, store, storage_mock, caplog
    ) -> None:
        from kntgraph.core.result import Err

        storage_mock.load = AsyncMock(return_value=Err(MemoryError("redis down")))
        with caplog.at_level("WARNING", logger="kntgraph.infra.checkpoint"):
            assert await store.load("agent-1") is None

    async def test_load_returns_none_on_invalid_payload(
        self, store, storage_mock, caplog
    ) -> None:
        from kntgraph.core.result import Ok

        storage_mock.load = AsyncMock(return_value=Ok({"last_stream_id": "1-0"}))
        with caplog.at_level("WARNING", logger="kntgraph.infra.checkpoint"):
            assert await store.load("agent-1") is None

    async def test_load_round_trip(self, store, storage_mock, checkpoint) -> None:
        from kntgraph.core.result import Ok

        storage_mock.load = AsyncMock(return_value=Ok(checkpoint.to_dict()))
        loaded = await store.load("agent-1")
        assert loaded is not None
        assert loaded.last_event_id == checkpoint.last_event_id
        assert loaded.last_stream_id == checkpoint.last_stream_id
        assert loaded.state_hash == checkpoint.state_hash


# ---------------------------------------------------------------------------
# CheckpointStore.save
# ---------------------------------------------------------------------------


class TestSave:
    async def test_save_calls_storage(self, store, storage_mock, checkpoint) -> None:
        from kntgraph.core.result import Ok

        storage_mock.save = AsyncMock(return_value=Ok(None))
        await store.save(checkpoint)
        storage_mock.save.assert_awaited_once_with("agent-1", checkpoint.to_dict())

    async def test_save_logs_storage_error(
        self, store, storage_mock, checkpoint, caplog
    ) -> None:
        from kntgraph.core.result import Err

        storage_mock.save = AsyncMock(return_value=Err(MemoryError("redis down")))
        with caplog.at_level("WARNING", logger="kntgraph.infra.checkpoint"):
            await store.save(checkpoint)


# ---------------------------------------------------------------------------
# CheckpointStore.clear
# ---------------------------------------------------------------------------


class TestClear:
    async def test_clear_calls_storage(self, store, storage_mock) -> None:
        from kntgraph.core.result import Ok

        storage_mock.clear = AsyncMock(return_value=Ok(None))
        await store.clear("agent-1")
        storage_mock.clear.assert_awaited_once_with("agent-1")

    async def test_clear_logs_storage_error(self, store, storage_mock, caplog) -> None:
        from kntgraph.core.result import Err

        storage_mock.clear = AsyncMock(return_value=Err(MemoryError("redis down")))
        with caplog.at_level("WARNING", logger="kntgraph.infra.checkpoint"):
            await store.clear("agent-1")


# ---------------------------------------------------------------------------
# CheckpointStore.load_all
# ---------------------------------------------------------------------------


class TestLoadAll:
    async def test_load_all_empty(self, store, storage_mock) -> None:
        from kntgraph.core.result import Ok

        storage_mock.load_all = AsyncMock(return_value=Ok({}))
        result = await store.load_all()
        assert result == {}

    async def test_load_all_round_trip(self, store, storage_mock, checkpoint) -> None:
        from kntgraph.core.result import Ok

        storage_mock.load_all = AsyncMock(
            return_value=Ok({checkpoint.agent_id: checkpoint.to_dict()})
        )
        result = await store.load_all()
        assert "agent-1" in result
        assert result["agent-1"].last_event_id == checkpoint.last_event_id

    async def test_load_all_skips_invalid_entries(
        self, store, storage_mock, checkpoint, caplog
    ) -> None:
        from kntgraph.core.result import Ok

        storage_mock.load_all = AsyncMock(
            return_value=Ok(
                {
                    "agent-good": checkpoint.to_dict(),
                    "agent-bad": {"last_stream_id": "1-0"},
                }
            )
        )
        with caplog.at_level("WARNING", logger="kntgraph.infra.checkpoint"):
            result = await store.load_all()
        assert "agent-good" in result
        assert "agent-bad" not in result

    async def test_load_all_logs_storage_error(
        self, store, storage_mock, caplog
    ) -> None:
        from kntgraph.core.result import Err

        storage_mock.load_all = AsyncMock(return_value=Err(MemoryError("redis down")))
        with caplog.at_level("WARNING", logger="kntgraph.infra.checkpoint"):
            result = await store.load_all()
        assert result == {}


# ---------------------------------------------------------------------------
# CheckpointStore.clear_all
# ---------------------------------------------------------------------------


class TestClearAll:
    async def test_clear_all_calls_storage(self, store, storage_mock) -> None:
        from kntgraph.core.result import Ok

        storage_mock.clear_all = AsyncMock(return_value=Ok(None))
        await store.clear_all()
        storage_mock.clear_all.assert_awaited_once()

    async def test_clear_all_logs_storage_error(
        self, store, storage_mock, caplog
    ) -> None:
        from kntgraph.core.result import Err

        storage_mock.clear_all = AsyncMock(return_value=Err(MemoryError("redis down")))
        with caplog.at_level("WARNING", logger="kntgraph.infra.checkpoint"):
            await store.clear_all()


# ---------------------------------------------------------------------------
# utcnow
# ---------------------------------------------------------------------------


class TestUtcnow:
    def test_returns_aware_utc(self) -> None:
        now = utcnow()
        assert now.tzinfo is timezone.utc
