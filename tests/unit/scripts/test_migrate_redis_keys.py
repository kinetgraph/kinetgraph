# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for ``scripts/migrate_redis_keys.py`` (ADR-076 §3.3).

The script wraps three small functions; we test them in
isolation against ``fakeredis.aioredis`` so the suite
runs without a real Redis.

Coverage:

  - dry-run enumerates every key and prints the target
    count + a sample; never calls ``RENAME``.
  - --commit actually ``RENAME``s each key, preserving
    TTL on the way.
  - Target-key-exists collision: the second run on
    the same data skips with ``skipped`` (idempotent).
  - Empty ``to_prefix`` is a no-op (the operator set
    neither env var).
  - from_prefix == to_prefix is a no-op (defensive).
  - Errors are caught and counted (the script does NOT
    abort on the first failure -- the operator wants to
    see the whole damage picture).

Sync-test pattern (matches ``test_migrate_principals.py``):
the async fixture populates the fake Redis once via
``asyncio.run``; test functions are sync and ``await``
Redis calls inline. This sidesteps the ``pytest-asyncio``
strict-mode ceremony for a small migration-script test
suite.
"""

from __future__ import annotations

import asyncio

import fakeredis
import fakeredis.aioredis
import pytest

import migrate_redis_keys  # noqa: E402  # pyright: ignore[reportMissingImports]


SEED_KEYS = (
    "knt:agents:agent-1:events",
    "knt:eventids:00000000-0000-0000-0000-000000000001",
    "knt:dlq:events",
    "knt:dlq:by_event_id",
    "knt:tools:weather:queue",
    "other_namespace:keep",
)


@pytest.fixture
def fake_redis():
    """An aioredis fake pre-populated with the canonical
    pre-076 wire shape.

    The keys mirror what the framework writes today
    (``infra.redis._event_log._keys``,
    ``infra.redis._dlq``, ``infra.redis._memory._solution``,
    ``tools.worker`` ``tool_stream_prefix``):

      - one agent's EventLog Stream
      - the idempotency index entry
      - the DLQ Stream
      - the DLQ event index (Hash)
      - a tool queue Stream
      - one unrelated ``other_namespace:keep`` that the
        script MUST ignore (prefix mismatch).
    """
    server = fakeredis.FakeServer()
    server.connected = True
    redis_client = fakeredis.aioredis.FakeRedis(server=server)

    async def _populate():
        await redis_client.xadd(
            "knt:agents:agent-1:events",
            {"event_id": "00000000-0000-0000-0000-000000000001"},
        )
        await redis_client.xadd(
            "knt:agents:agent-1:events",
            {"event_id": "00000000-0000-0000-0000-000000000002"},
        )
        # Idempotency index (24h TTL, ADR-019).
        await redis_client.set(
            "knt:eventids:00000000-0000-0000-0000-000000000001",
            b"1-0",
            ex=86_400,
        )
        # DLQ stream + event index.
        await redis_client.xadd(
            "knt:dlq:events",
            {"event_id": "00000000-0000-0000-0000-000000000099"},
        )
        await redis_client.hset(
            "knt:dlq:by_event_id",
            "00000000-0000-0000-0000-000000000099:timeout",
            "1-0",
        )
        # Tool queue.
        await redis_client.xadd(
            "knt:tools:weather:queue",
            {"payload": "{}"},
        )
        # Unrelated key that the script MUST NOT touch.
        await redis_client.set("other_namespace:keep", b"x")

    asyncio.run(_populate())
    return redis_client


# ---------------------------------------------------------------------------
# Migrate (happy paths)
# ---------------------------------------------------------------------------


class TestMigrateDryRun:
    """Dry-run path: enumerate + sample, never call RENAME."""

    def test_enumerates_every_knt_key(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """The SCAN visits all 5 ``knt:*`` keys (not the
        unrelated ``other_namespace:keep``)."""

        async def _run():
            return await migrate_redis_keys._migrate_with_client(
                client=fake_redis,
                from_prefix="knt:",
                to_prefix="acme-billing:knt:",
                dry_run=True,
            )

        report = asyncio.run(_run())
        assert report.scanned == 5
        assert report.renamed == 0  # dry-run never renames
        assert report.errors == 0

    def test_samples_first_log_every_pairs(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """Dry-run samples the first ``LOG_EVERY``
        ``(from, to)`` pairs for the operator to eyeball."""

        async def _run():
            return await migrate_redis_keys._migrate_with_client(
                client=fake_redis,
                from_prefix="knt:",
                to_prefix="acme-billing:knt:",
                dry_run=True,
            )

        report = asyncio.run(_run())
        # 5 scanned, 5 sampled (under the LOG_EVERY cap).
        assert len(report.samples) == 5
        # Every sample is a strict prefix substitution.
        for frm, to in report.samples:
            assert frm.startswith("knt:")
            assert to.startswith("acme-billing:knt:")
            assert frm[len("knt:") :] == to[len("acme-billing:knt:") :]

    def test_does_not_mutate_redis(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """After dry-run, every original key is still at
        the bare prefix; the to-prefix does not exist yet."""

        async def _run():
            await migrate_redis_keys._migrate_with_client(
                client=fake_redis,
                from_prefix="knt:",
                to_prefix="acme-billing:knt:",
                dry_run=True,
            )

        asyncio.run(_run())

        async def _check():
            for key in SEED_KEYS[:5]:
                assert await fake_redis.exists(key), key
            # None of the to-prefix keys exist yet.
            assert not await fake_redis.exists("acme-billing:knt:agents:agent-1:events")

        asyncio.run(_check())


class TestMigrateCommit:
    """Commit path: actually ``RENAME`` every key."""

    def test_renames_every_key(self, fake_redis: fakeredis.aioredis.FakeRedis) -> None:
        async def _run():
            return await migrate_redis_keys._migrate_with_client(
                client=fake_redis,
                from_prefix="knt:",
                to_prefix="acme-billing:knt:",
                dry_run=False,
            )

        report = asyncio.run(_run())
        assert report.scanned == 5
        assert report.renamed == 5
        assert report.skipped == 0
        assert report.errors == 0

        # All keys moved; nothing at the bare prefix anymore.
        async def _check():
            for frm in SEED_KEYS[:5]:
                assert not await fake_redis.exists(frm), frm
            for to in (
                "acme-billing:knt:agents:agent-1:events",
                "acme-billing:knt:eventids:00000000-0000-0000-0000-000000000001",
                "acme-billing:knt:dlq:events",
                "acme-billing:knt:dlq:by_event_id",
                "acme-billing:knt:tools:weather:queue",
            ):
                assert await fake_redis.exists(to), to

        asyncio.run(_check())

    def test_preserves_ttl_on_idempotency_index(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """The idempotency index has a 24h TTL (ADR-019).
        ``RENAME`` preserves it; the moved key still has a
        TTL after the migration.
        """

        async def _run():
            before_ttl = await fake_redis.ttl(
                "knt:eventids:00000000-0000-0000-0000-000000000001"
            )
            assert before_ttl > 0  # the fixture set a TTL

            await migrate_redis_keys._migrate_with_client(
                client=fake_redis,
                from_prefix="knt:",
                to_prefix="acme-billing:knt:",
                dry_run=False,
            )

            after_ttl = await fake_redis.ttl(
                "acme-billing:knt:eventids:00000000-0000-0000-0000-000000000001"
            )
            # ``RENAME`` preserves the TTL; the new key inherits
            # the 86 400-second budget rather than starting
            # fresh at "no expiry".
            assert after_ttl > 0
            assert after_ttl >= before_ttl - 5

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Idempotency (the re-run case)
# ---------------------------------------------------------------------------


class TestMigrateIdempotent:
    """Re-running on already-migrated data is safe."""

    def test_second_run_skips_existing_targets(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """After the first --commit, the second run finds
        every key already at the target; ``RENAME`` raises
        ``already exists``; the script counts those as
        ``skipped`` (NOT as errors).
        """

        async def _run():
            # First run: commit.
            first = await migrate_redis_keys._migrate_with_client(
                client=fake_redis,
                from_prefix="knt:",
                to_prefix="acme-billing:knt:",
                dry_run=False,
            )
            assert first.renamed == 5
            assert first.errors == 0

            # Second run on the same Redis: every target already
            # exists, so every rename is a no-op collision.
            second = await migrate_redis_keys._migrate_with_client(
                client=fake_redis,
                from_prefix="knt:",
                to_prefix="acme-billing:knt:",
                dry_run=False,
            )
            assert second.scanned == 0  # no keys at the bare prefix
            assert second.renamed == 0
            assert second.skipped == 0
            assert second.errors == 0

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------


class TestMigrateNoOp:
    """The script must short-circuit when there is nothing
    to migrate, not iterate over an empty Redis."""

    def test_empty_to_prefix_is_no_op(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """The default ``KNT_REDIS_KEY_PREFIX=""`` means
        there is no namespace to adopt. The script must
        not run a useless SCAN."""

        async def _run():
            report = await migrate_redis_keys._migrate_with_client(
                client=fake_redis,
                from_prefix="knt:",
                to_prefix="",
                dry_run=False,
            )
            assert report.scanned == 0
            assert report.renamed == 0
            # The bare-prefix keys are untouched.
            assert await fake_redis.exists("knt:agents:agent-1:events")

        asyncio.run(_run())

    def test_same_prefix_is_no_op(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """Defensive: from == to is a no-op (not a rename loop)."""

        async def _run():
            report = await migrate_redis_keys._migrate_with_client(
                client=fake_redis,
                from_prefix="knt:",
                to_prefix="knt:",
                dry_run=False,
            )
            assert report.scanned == 0

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


class TestReportRender:
    """The dry-run report is human-readable."""

    def test_render_includes_counters(self) -> None:
        report = migrate_redis_keys.MigrationReport(
            from_prefix="knt:",
            to_prefix="acme-billing:knt:",
            scanned=42,
            renamed=42,
            skipped=0,
            errors=0,
            samples=[("knt:a:1", "acme-billing:knt:a:1")],
        )
        text = report.render(dry_run=False)
        assert "COMMIT" in text
        assert "'knt:'" in text
        assert "'acme-billing:knt:'" in text
        assert "scanned:      42" in text
        assert "renamed:      42" in text
        # Commit mode does NOT show the sample (operator
        # does not need to re-eyeball what already happened).
        assert "sample of" not in text

    def test_render_includes_sample_in_dry_run(self) -> None:
        report = migrate_redis_keys.MigrationReport(
            from_prefix="knt:",
            to_prefix="acme-billing:knt:",
            scanned=10,
            renamed=0,
            skipped=0,
            errors=0,
            samples=[
                ("knt:agents:a:events", "acme-billing:knt:agents:a:events"),
                ("knt:dlq:events", "acme-billing:knt:dlq:events"),
            ],
        )
        text = report.render(dry_run=True)
        assert "DRY-RUN" in text
        assert "sample of (from, to) pairs" in text
        # The renderer uses ``!r`` on both sides so the
        # sample line is wrapped in single quotes; pin the
        # shape so a future format tweak is caught here.
        assert "'knt:agents:a:events'  ->  'acme-billing:knt:agents:a:events'" in text
        assert "'knt:dlq:events'  ->  'acme-billing:knt:dlq:events'" in text


# ---------------------------------------------------------------------------
# _iter_keys (the SCAN wrapper)
# ---------------------------------------------------------------------------


class TestIterKeys:
    """The SCAN wrapper yields every matching key exactly once.

    ``fakeredis`` honours the ``SCAN`` cursor protocol, so
    the wrapper's iteration semantics can be exercised
    end-to-end without a real Redis.
    """

    def test_yields_every_matching_key(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        async def _collect():
            out = []
            async for key in migrate_redis_keys._iter_keys(fake_redis, "knt:"):
                out.append(key)
            return out

        collected = asyncio.run(_collect())
        # Sorted for assertion stability.
        collected.sort()
        assert collected == [
            "knt:agents:agent-1:events",
            "knt:dlq:by_event_id",
            "knt:dlq:events",
            "knt:eventids:00000000-0000-0000-0000-000000000001",
            "knt:tools:weather:queue",
        ]
        # The unrelated key is NOT yielded (prefix mismatch).
        assert "other_namespace:keep" not in collected


# ---------------------------------------------------------------------------
# _rename
# ---------------------------------------------------------------------------


class TestRename:
    """The atomic RENAME wrapper distinguishes the
    "target exists" collision from other RedisErrors.
    """

    def test_returns_true_on_success(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        async def _run():
            await fake_redis.set("knt:a", b"x")
            ok = await migrate_redis_keys._rename(fake_redis, "knt:a", "knt:b")
            assert ok is True
            assert not await fake_redis.exists("knt:a")
            assert await fake_redis.exists("knt:b")

        asyncio.run(_run())

    def test_returns_false_on_target_exists(
        self,
        fake_redis: fakeredis.aioredis.FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``fakeredis`` does not model the "target key
        exists" branch of ``RENAME`` (it silently
        overwrites). We patch the underlying ``client.rename``
        to raise the real-Redis ``ResponseError`` so the
        script's ``_rename`` catches it and returns
        ``False``.

        Without the patch the test would pass for the
        wrong reason (fakeredis would return ``True`` on a
        no-op collision); with the patch the script's
        ``except ResponseError`` branch fires and we
        verify ``_rename`` returns ``False``.
        """
        from redis.exceptions import ResponseError
        import migrate_redis_keys as mrk_module

        async def _run():
            await fake_redis.set("knt:a", b"x")
            await fake_redis.set("knt:b", b"y")

            # Patch the underlying ``rename`` call so the
            # target-exists branch fires (fakeredis would
            # not raise on its own). The script's
            # ``_rename`` wraps ``rename`` in a
            # ``try/except ResponseError``; the patch makes
            # that except branch fire.
            real_rename = fake_redis.rename

            async def fake_redis_rename(frm, to):
                if frm == "knt:a" and to == "knt:b":
                    raise ResponseError("ERR target key name already exists")
                return await real_rename(frm, to)

            monkeypatch.setattr(fake_redis, "rename", fake_redis_rename)

            ok = await mrk_module._rename(fake_redis, "knt:a", "knt:b")
            # The script's ``_rename`` caught the
            # ``ResponseError`` and returned ``False``.
            assert ok is False
            # The pre-existing target value is untouched.
            assert await fake_redis.get("knt:b") == b"y"
            # The source still exists (the rename was rejected).
            assert await fake_redis.exists("knt:a")

        asyncio.run(_run())
