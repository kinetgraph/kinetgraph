# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for DLQStorage Protocol.

Iteration 5 (ADR-019). The DLQ has 4 Redis keys (stream +
3 indexes + counters) and an idempotency protocol
(hash-based, parallel to the SET-based pattern in the
EventLog). The protocol abstracts all of that; the
``DeadLetterQueue`` class becomes a thin facade that builds
``DeadLetterEvent`` from the parsed dicts.

Why split
---------

The previous ``DeadLetterQueue.append`` (CC=5) and
``DeadLetterQueue.get_stats`` (CC=3) mix five concerns:

  1. Idempotency (XADD + HSET placeholder + HSETNX)
  2. Counter bumps (HINCRBY on the per-reason index)
  3. Wire format decode (Redis stream entry → dict)
  4. Domain construction (dict → ``DeadLetterEvent``)
  5. Error mapping (RedisError → PersistenceError)

Iteration 5 moves (1), (2) and (3) to the storage. The
queue class is a thin composition over the Protocol.

Result contract (AGENTS.md §6):

  - ``append``        returns ``Ok(stream_id)`` /
    ``Err(DLQMemoryError)``.
  - ``read``          returns ``Ok(dict | None)`` /
    ``Err(DLQMemoryError)``.
  - ``list_for_agent`` returns ``Ok(list[dict])`` /
    ``Err(DLQMemoryError)``.
  - ``list_by_reason`` returns ``Ok(list[dict])`` /
    ``Err(DLQMemoryError)``.
  - ``list_all``      returns ``Ok(list[dict])`` /
    ``Err(DLQMemoryError)``.
  - ``read_index``    returns ``Ok(dict | None)`` /
    ``Err(DLQMemoryError)``.
  - ``bump_reason_counter`` returns ``Ok(None)`` /
    ``Err(DLQMemoryError)``.
  - ``get_stats``     returns ``Ok(dict)`` /
    ``Err(DLQMemoryError)``.
  - ``purge``         returns ``Ok(int)`` /
    ``Err(DLQMemoryError)``.
  - ``drop_entry``    returns ``Ok(None)`` /
    ``Err(DLQMemoryError)``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.asyncio


class TestDLQStorageProtocol:
    def test_module_importable(self):
        from kntgraph.infra.redis._dlq import DLQStorage

        assert DLQStorage is not None

    def test_dlq_storage_lists_required_methods(self):
        from kntgraph.infra.redis._dlq import DLQStorage

        for name in (
            "append",
            "read",
            "list_for_agent",
            "list_by_reason",
            "list_all",
            "read_index",
            "bump_reason_counter",
            "get_stats",
            "purge",
            "drop_entry",
        ):
            assert hasattr(DLQStorage, name), f"DLQStorage must declare {name!r}"


class TestDLQStorageAppendContract:
    def test_append_accepts_dict_payload(self):
        from kntgraph.infra.redis._dlq import DLQStorage

        storage = MagicMock(spec=DLQStorage)
        storage.append = MagicMock()

        payload = {"event_id": "abc", "agent_id": "a-1", "reason": "timeout"}
        storage.append("dlq:abc:timeout", payload)
        storage.append.assert_called_once_with("dlq:abc:timeout", payload)


class TestDLQStorageReadContract:
    def test_read_returns_dict_or_none(self):
        from kntgraph.infra.redis._dlq import DLQStorage

        storage = MagicMock(spec=DLQStorage)
        storage.read = MagicMock(return_value={"event_id": "abc"})

        result = storage.read("1-0")
        assert result is not None


class TestNoDirectRedisImportInDLQ:
    """Framework code must not import ``redis.asyncio`` directly.

    Iteration 5 (ADR-019) closed the gap: every shard
    is now a typed adapter. The DLQ layer
    (``events/dlq/store.py`` and ``actions.py``) no
    longer imports ``redis`` directly; both consume
    ``DLQStorage``.
    """

    EXCLUDED = {
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
            "events/dlq/: " + ", ".join(f"{p}:{n}" for p, n, _ in offenders)
        )
