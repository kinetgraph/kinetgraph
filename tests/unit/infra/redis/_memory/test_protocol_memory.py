# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for ShortMemoryStorage Protocol — domain-level interface.

The three memory tiers (Session, Profile, Continuity) all share
the same pattern: read a record from cache, fall back to a
fold over the EventLog, refresh the cache. The storage
interface is a thin wrapper around this pattern that the
``BaseShortTermMemory`` calls into.

RED phase of Iteration 2 (ADR-019).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.asyncio


class TestShortMemoryStorageProtocol:
    def test_module_importable(self):
        from kntgraph.infra.redis._memory import ShortMemoryStorage

        assert ShortMemoryStorage is not None

    def test_memory_storage_lists_required_methods(self):
        from kntgraph.infra.redis._memory import ShortMemoryStorage

        for name in (
            "get_record",
            "put_record",
            "delete_record",
            "iter_keys",
        ):
            assert hasattr(ShortMemoryStorage, name), (
                f"ShortMemoryStorage must declare {name!r}"
            )


class TestShortMemoryStorageGetReturnsMappingOrNone:
    def test_get_record_returns_mapping_when_present(self):
        """Contract: ``get_record`` returns a Mapping when the key exists."""
        from kntgraph.infra.redis._memory import ShortMemoryStorage

        storage = MagicMock(spec=ShortMemoryStorage)
        storage.get_record = MagicMock(return_value={"a": "1", "b": "2"})

        result = storage.get_record("fmh:session:abc")
        assert result is not None
        assert result["a"] == "1"


class TestShortMemoryStoragePutRecord:
    def test_put_record_accepts_mapping_and_ttl(self):
        from kntgraph.infra.redis._memory import ShortMemoryStorage

        storage = MagicMock(spec=ShortMemoryStorage)
        storage.put_record = MagicMock()

        storage.put_record(
            "fmh:session:abc",
            {"messages": ["hi"]},
            ttl_seconds=3600,
        )
        storage.put_record.assert_called_once_with(
            "fmh:session:abc",
            {"messages": ["hi"]},
            ttl_seconds=3600,
        )

    def test_put_record_accepts_no_ttl(self):
        from kntgraph.infra.redis._memory import ShortMemoryStorage

        storage = MagicMock(spec=ShortMemoryStorage)
        storage.put_record = MagicMock()

        storage.put_record("fmh:profile:t1:u1", {"tier": "vip"})
        storage.put_record.assert_called_once_with(
            "fmh:profile:t1:u1",
            {"tier": "vip"},
        )


class TestShortMemoryStorageDeleteRecord:
    def test_delete_record_removes_key(self):
        from kntgraph.infra.redis._memory import ShortMemoryStorage

        storage = MagicMock(spec=ShortMemoryStorage)
        storage.delete_record = MagicMock()

        storage.delete_record("fmh:session:abc")
        storage.delete_record.assert_called_once_with("fmh:session:abc")


class TestShortMemoryStorageIterKeys:
    def test_iter_keys_returns_async_iterable(self):
        from kntgraph.infra.redis._memory import ShortMemoryStorage

        async def fake_iter_keys(prefix):
            for k in [f"{prefix}1", f"{prefix}2"]:
                yield k

        storage = MagicMock(spec=ShortMemoryStorage)
        storage.iter_keys = fake_iter_keys

        # Synchronous iteration over the async generator
        keys = []
        import asyncio

        async def collect():
            async for k in storage.iter_keys("fmh:session:"):
                keys.append(k)

        asyncio.run(collect())
        assert keys == ["fmh:session:1", "fmh:session:2"]


class TestNoDirectRedisImportInMemory:
    """Framework code must not import ``redis.asyncio`` directly.

    Iteration 5 (ADR-019) closed the gap: every shard
    is now a typed adapter. The memory tier
    (``memory/base.py``, ``memory/session.py``,
    ``memory/profile.py``, ``memory/continuity/manager.py``)
    no longer imports ``redis`` directly; all four
    consume ``ShortMemoryStorage``.
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
            "memory/legacy files: " + ", ".join(f"{p}:{n}" for p, n, _ in offenders)
        )
