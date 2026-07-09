# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for APIKeyStorage Protocol — domain interface.

Iteration 3 (ADR-019). The protocol is storage-format-agnostic;
the verifier (``RedisAPIKeyVerifier``) consumes it.

RED phase: tests first.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.asyncio


class TestAPIKeyStorageProtocol:
    def test_module_importable(self):
        from kntgraph.infra.redis._auth import APIKeyStorage

        assert APIKeyStorage is not None

    def test_api_key_storage_lists_required_methods(self):
        from kntgraph.infra.redis._auth import APIKeyStorage

        for name in ("lookup", "store", "delete"):
            assert hasattr(APIKeyStorage, name), f"APIKeyStorage must declare {name!r}"


class TestAPIKeyStorageLookupContract:
    def test_lookup_returns_raw_bytes_or_none(self):
        """Contract: ``lookup(digest)`` returns the raw value or None."""
        from kntgraph.infra.redis._auth import APIKeyStorage

        storage = MagicMock(spec=APIKeyStorage)
        storage.lookup = MagicMock(return_value=b'{"agent_id": "a-1"}')

        result = storage.lookup("abc123")
        assert result == b'{"agent_id": "a-1"}'


class TestAPIKeyStorageStoreContract:
    def test_store_accepts_raw_bytes(self):
        from kntgraph.infra.redis._auth import APIKeyStorage

        storage = MagicMock(spec=APIKeyStorage)
        storage.store = MagicMock()

        storage.store("abc123", b'{"agent_id": "a-1"}')
        storage.store.assert_called_once_with("abc123", b'{"agent_id": "a-1"}')


class TestAPIKeyStorageDeleteContract:
    def test_delete_removes_binding(self):
        from kntgraph.infra.redis._auth import APIKeyStorage

        storage = MagicMock(spec=APIKeyStorage)
        storage.delete = MagicMock()

        storage.delete("abc123")
        storage.delete.assert_called_once_with("abc123")


class TestNoDirectRedisImportInAuth:
    """Framework code must not import ``redis.asyncio`` directly.

    Iteration 5 (ADR-019) closed the gap: every shard
    (event_log, memory, auth, checkpoint, dlq,
    world_checkpoint) is now a typed adapter. The auth
    layer (``api/auth.py``) no longer imports ``redis``
    directly; it consumes ``APIKeyStorage``.
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
            "Direct `redis` imports outside kntgraph/infra/redis/: "
            + ", ".join(f"{p}:{n}" for p, n, _ in offenders)
        )
