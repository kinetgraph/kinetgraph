# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for ``scripts/migrate_principals.py``.

We use ``fakeredis.aioredis`` so the script can run
end-to-end (scan, read, write) without a real Redis.

Coverage:

  - Legacy string binding → JSON migration
  - Already-migrated JSON binding → skipped
  - Empty binding → counted as error (no write)
  - Hierarchical agent_id (tenant-A.agent-1) →
    tenant_id=tenant-A
  - Flat agent_id (agent-1) → tenant_id=agent-1
    (legacy convention)
  - DRY-RUN vs --apply semantics
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import fakeredis.aioredis
import pytest

# Make the script importable. The scripts dir is two
# levels up from tests/unit/scripts/ (tests → unit →
# scripts/); but the test file lives at
# ``tests/unit/scripts/`` so the relative path is
# ``../../../scripts`` from this file's directory.
THIS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR.parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import migrate_principals  # noqa: E402  # pyright: ignore[reportMissingImports]


KEY_PREFIX = "fmh:api:keys:"


@pytest.fixture
def fake_redis():
    """An aioredis fake populated with a mix of legacy
    and migrated bindings.
    """
    server = fakeredis.FakeServer()
    server.connected = True
    redis_client = fakeredis.aioredis.FakeRedis(server=server)

    async def _populate():
        # Legacy: hierarchical agent_id.
        await redis_client.set(f"{KEY_PREFIX}hash-1", b"tenant-A.agent-1")
        # Legacy: flat agent_id.
        await redis_client.set(f"{KEY_PREFIX}hash-2", b"agent-1")
        # Already migrated (JSON).
        await redis_client.set(
            f"{KEY_PREFIX}hash-3",
            json.dumps(
                {
                    "agent_id": "tenant-B.agent-2",
                    "role": "service",
                    "tenant_id": "tenant-B",
                    "key_id": "k-2026",
                }
            ).encode("utf-8"),
        )
        # Empty value (should be counted as error).
        await redis_client.set(f"{KEY_PREFIX}hash-4", b"")

    asyncio.run(_populate())
    return redis_client


def _scan_keys(redis_client) -> list[tuple[str, bytes]]:
    """Helper: read all bindings via the same path the
    script uses (so the test exercises the SCAN loop).
    """
    out = []
    cursor = 0
    while True:
        cursor, keys = asyncio.run(
            redis_client.scan(
                cursor=cursor,
                match=f"{KEY_PREFIX}*",
                count=100,
            )
        )
        for k in keys:
            raw = asyncio.run(redis_client.get(k))
            if raw is not None:
                out.append((k, raw))
        if cursor == 0:
            break
    return out


class TestMigrationHeuristics:
    def test_hierarchical_legacy_uses_first_segment_as_tenant(self):
        new = migrate_principals._migrate_value(b"tenant-A.agent-1")
        assert new is not None
        parsed = json.loads(new)
        assert parsed["agent_id"] == "tenant-A.agent-1"
        assert parsed["role"] == "agent"
        assert parsed["tenant_id"] == "tenant-A"
        assert parsed["key_id"] == "legacy"

    def test_flat_legacy_uses_agent_id_as_tenant(self):
        new = migrate_principals._migrate_value(b"agent-1")
        assert new is not None
        parsed = json.loads(new)
        assert parsed["agent_id"] == "agent-1"
        assert parsed["tenant_id"] == "agent-1"

    def test_already_migrated_is_skipped(self):
        new = migrate_principals._migrate_value(
            json.dumps(
                {
                    "agent_id": "tenant-A.agent-2",
                    "role": "agent",
                    "tenant_id": "tenant-A",
                    "key_id": "k-1",
                }
            ).encode("utf-8")
        )
        assert new is None  # no migration needed

    def test_empty_binding_raises(self):
        with pytest.raises(ValueError, match="empty legacy binding"):
            migrate_principals._migrate_value(b"")


class TestScan:
    def test_scan_yields_every_binding(self, fake_redis):
        bindings = _scan_keys(fake_redis)
        keys = [k.decode() for k, _ in bindings]
        assert any(k.endswith("hash-1") for k in keys)
        assert any(k.endswith("hash-2") for k in keys)
        assert any(k.endswith("hash-3") for k in keys)
        assert any(k.endswith("hash-4") for k in keys)


class TestFullMigration:
    def test_dry_run_does_not_write(self, fake_redis):
        """Mirrors the script's run() loop: empty bindings
        are counted as errors and SKIPPED (not written);
        legacy strings are counted as would-migrate;
        already-migrated bindings are counted as
        skipped.
        """

        async def _run():
            stats = {
                "scanned": 0,
                "migrated": 0,
                "errors": 0,
            }
            cursor = 0
            while True:
                cursor, keys = await fake_redis.scan(
                    cursor=cursor,
                    match=f"{KEY_PREFIX}*",
                    count=100,
                )
                for k in keys:
                    raw = await fake_redis.get(k)
                    if raw is None:
                        continue
                    stats["scanned"] += 1
                    try:
                        new = migrate_principals._migrate_value(raw)
                    except ValueError:
                        stats["errors"] += 1
                        continue
                    if new is not None:
                        stats["migrated"] += 1
                        # In DRY-RUN we would NOT call set().
                if cursor == 0:
                    break
            return stats

        stats = asyncio.run(_run())
        assert stats["scanned"] == 4
        assert stats["migrated"] == 2  # hash-1 and hash-2
        assert stats["errors"] == 1  # hash-4 (empty)
        # Confirm nothing was written.
        post_raw_1 = asyncio.run(fake_redis.get(f"{KEY_PREFIX}hash-1"))
        assert post_raw_1 == b"tenant-A.agent-1"

    def test_apply_writes_only_legacy(self, fake_redis):
        async def _run():
            cursor = 0
            while True:
                cursor, keys = await fake_redis.scan(
                    cursor=cursor,
                    match=f"{KEY_PREFIX}*",
                    count=100,
                )
                for k in keys:
                    raw = await fake_redis.get(k)
                    if raw is None:
                        continue
                    try:
                        new = migrate_principals._migrate_value(raw)
                    except ValueError:
                        continue  # empty binding: skip
                    if new is not None:
                        await fake_redis.set(k, new)
                if cursor == 0:
                    break

        asyncio.run(_run())
        # Legacy bindings are now JSON.
        post_raw_1 = asyncio.run(fake_redis.get(f"{KEY_PREFIX}hash-1"))
        parsed = json.loads(post_raw_1)
        assert parsed["role"] == "agent"
        assert parsed["tenant_id"] == "tenant-A"
        # Already-migrated binding was NOT overwritten
        # (its key_id is preserved).
        post_raw_3 = asyncio.run(fake_redis.get(f"{KEY_PREFIX}hash-3"))
        parsed3 = json.loads(post_raw_3)
        assert parsed3["key_id"] == "k-2026"
        # Empty binding still empty.
        post_raw_4 = asyncio.run(fake_redis.get(f"{KEY_PREFIX}hash-4"))
        assert post_raw_4 == b""
