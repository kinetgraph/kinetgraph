# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for WorldCheckpointStorage Protocol.

Iteration 5 (ADR-019). The protocol abstracts the
``IncrementalWorldStore`` Redis I/O. The facade
(``IncrementalWorldStore``) becomes a thin composition.

The wire format is intentionally kept pickle-based for
now (ADR-018 §5). A future iteration may swap to msgpack
+ JSON; the Protocol does not change.
"""

from __future__ import annotations

from typing import Any

import pytest


pytestmark = pytest.mark.asyncio


class TestWorldCheckpointStorageProtocol:
    def test_module_importable(self):
        from kntgraph.infra.redis._world_checkpoint import (
            WorldCheckpointStorage,
        )

        assert WorldCheckpointStorage is not None

    def test_storage_lists_required_methods(self):
        from kntgraph.infra.redis._world_checkpoint import (
            WorldCheckpointStorage,
        )

        for name in ("load", "save", "discard"):
            assert hasattr(WorldCheckpointStorage, name), (
                f"WorldCheckpointStorage must declare {name!r}"
            )


class TestNoDirectRedisImportInWorldCheckpoint:
    """All 5 shards + world_checkpoint are typed adapters.

    Iteration 5 (ADR-019) closed the last gap:
    ``infra/world_checkpoint.py`` no longer imports
    ``redis`` directly; it consumes
    ``WorldCheckpointStorage``.
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
            "Direct `redis` imports outside kntgraph/infra/redis/: "
            + ", ".join(f"{p}:{n}" for p, n, _ in offenders)
        )
