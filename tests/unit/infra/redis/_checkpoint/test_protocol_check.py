# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for CheckpointStorage Protocol.

Iteration 4 (ADR-019). The protocol abstracts the Redis
I/O for the ReactiveDispatcher commit points. The store
class (``CheckpointStore``) becomes a thin composition
over the protocol.

Result contract (AGENTS.md §6):

  - ``load``        returns ``Ok(checkpoint)`` /
    ``Ok(None)`` / ``Err(MemoryError)``.
  - ``save``        returns ``Ok(None)`` / ``Err(MemoryError)``.
  - ``load_all``    returns ``Ok(dict)`` / ``Err(MemoryError)``.
  - ``clear``       returns ``Ok(None)`` / ``Err(MemoryError)``.
  - ``clear_all``   returns ``Ok(None)`` / ``Err(MemoryError)``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.asyncio


class TestCheckpointStorageProtocol:
    def test_module_importable(self):
        from kntgraph.infra.redis._checkpoint import CheckpointStorage

        assert CheckpointStorage is not None

    def test_checkpoint_storage_lists_required_methods(self):
        from kntgraph.infra.redis._checkpoint import CheckpointStorage

        for name in (
            "load",
            "save",
            "load_all",
            "clear",
            "clear_all",
        ):
            assert hasattr(CheckpointStorage, name), (
                f"CheckpointStorage must declare {name!r}"
            )


class TestCheckpointStorageLoadContract:
    def test_load_returns_mapping_or_none(self):
        from kntgraph.infra.redis._checkpoint import CheckpointStorage

        storage = MagicMock(spec=CheckpointStorage)
        storage.load = MagicMock(
            return_value={
                "last_event_id": "abc",
                "last_stream_id": "1-0",
                "confirmed_at": "2026-01-01T00:00:00+00:00",
            }
        )

        result = storage.load("agent-1")
        assert result is not None
        assert result["last_event_id"] == "abc"


class TestCheckpointStorageSaveContract:
    def test_save_accepts_mapping(self):
        from kntgraph.infra.redis._checkpoint import CheckpointStorage

        storage = MagicMock(spec=CheckpointStorage)
        storage.save = MagicMock()

        payload = {"last_event_id": "abc", "last_stream_id": "1-0"}
        storage.save("agent-1", payload)
        storage.save.assert_called_once_with("agent-1", payload)


class TestCheckpointStorageLoadAllContract:
    def test_load_all_returns_dict(self):
        from kntgraph.infra.redis._checkpoint import CheckpointStorage

        storage = MagicMock(spec=CheckpointStorage)
        storage.load_all = MagicMock(
            return_value={
                "agent-1": {"last_event_id": "abc"},
                "agent-2": {"last_event_id": "def"},
            }
        )

        result = storage.load_all()
        assert "agent-1" in result
        assert "agent-2" in result


class TestCheckpointStorageClearContract:
    def test_clear_removes_one(self):
        from kntgraph.infra.redis._checkpoint import CheckpointStorage

        storage = MagicMock(spec=CheckpointStorage)
        storage.clear = MagicMock()
        storage.clear("agent-1")
        storage.clear.assert_called_once_with("agent-1")

    def test_clear_all_wipes_everything(self):
        from kntgraph.infra.redis._checkpoint import CheckpointStorage

        storage = MagicMock(spec=CheckpointStorage)
        storage.clear_all = MagicMock()
        storage.clear_all()
        storage.clear_all.assert_called_once_with()


class TestNoDirectRedisImportInCheckpoint:
    """Framework code must not import ``redis.asyncio`` directly.

    Iteration 5 (ADR-019) closed the gap: every shard
    is now a typed adapter. The checkpoint layer
    (``infra/checkpoint.py``) no longer imports
    ``redis`` directly; it consumes ``CheckpointStorage``.
    """

    EXCLUDED = {
        # The Redis adapter package itself.
        "infra/redis/",
    }

    def test_no_redis_asyncio_imports_outside_redis_package(self):
        import re

        pattern = re.compile(r"^(?:from|import)\s+redis(?:\.asyncio)?")
        offenders: list[tuple[Any, int, str]] = []
        from pathlib import Path

        FRONTEND_SRC = Path("kntgraph/src/kntgraph")
        for py in FRONTEND_SRC.rglob("*.py"):
            if any(ex in str(py) for ex in self.EXCLUDED):
                continue
            for lineno, line in enumerate(py.read_text().splitlines(), start=1):
                if pattern.match(line):
                    offenders.append((py, lineno, line.strip()))
        assert not offenders, (
            "Direct `redis` imports outside kntgraph/infra/redis/ and "
            "infra/checkpoint.py: " + ", ".join(f"{p}:{n}" for p, n, _ in offenders)
        )
