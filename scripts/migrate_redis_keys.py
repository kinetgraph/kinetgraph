#!/usr/bin/env python

# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
migrate_redis_keys -- one-shot prefix migration for ADR-076.

Background
----------
The framework writes every Redis key under the
``KNT_REDIS_KEY_PREFIX`` namespace (ADR-076). Existing
deployments that pre-date the ADR have data at the bare
prefix (``knt:agents:...``, ``knt:dlq:events``, etc.). This
script is the one-shot ``SCAN`` + ``RENAME`` operator runs
when adopting the prefix:

    # Eyeball what would change.
    python scripts/migrate_redis_keys.py

    # Apply.
    python scripts/migrate_redis_keys.py --commit

    # Custom source / target prefix + Redis URL.
    python scripts/migrate_redis_keys.py \\
        --from-prefix "knt:" \\
        --to-prefix   "acme-billing:knt:" \\
        --commit
    python scripts/migrate_redis_keys.py \\
        --redis-url redis://:redispassword@host:6379/0 \\
        --commit

Mechanics
---------

  1. ``SCAN MATCH <from-prefix>* COUNT 500`` (matches the
     cost model in ADR-068 §1.2 -- 500 keys / cursor step
     keeps the round-trip steady-state at one per few
     hundred keys, not one per key).
  2. For each key found, ``RENAME <from> <to>`` -- ``RENAME``
     is atomic and preserves TTL, which is critical for the
     idempotency index (``knt:eventids:<id>``, 24 h TTL,
     ADR-019) and the DLQ event index.
  3. If ``<to>`` already exists (idempotent re-run),
     ``RENAME`` raises ``ResponseError``; the script logs
     and skips (continues with the next key).
  4. Per-key progress output (every 500 keys) and a final
     summary table: ``scanned``, ``renamed``, ``skipped
   (target exists)``, ``errors``.

Why dry-run is the default
-------------------------
Operators with production data run ``--dry-run`` (default)
first, eyeball the count and the per-key sample, then run
``--commit``. This is the standard safe default for every
other migration script in ``scripts/``; this one follows
the precedent. The dry-run path does NOT call ``RENAME``;
it composes the new key and prints a sample.

What this script does NOT do
----------------------------
- It does NOT enumerate every ``knt:*`` suffix. The
  framework owns the suffix templates
  (``infra.redis._event_log._keys``,
  ``infra.redis._dlq``, ``infra.redis._memory._solution``,
  ``tools.worker`` ``tool_stream_prefix``). Any new
  adapter that joins after ADR-076 inherits the prefix
  automatically -- no script update required.
- It does NOT migrate keys outside the configured
  ``from-prefix``. If the operator picked a non-default
  ``from-prefix`` (e.g. they already moved to ``prod:knt:``
  years ago), they pass ``--from-prefix`` explicitly.
- It does NOT touch the EventLog's cursor / dedup keys
  (``knt:eventids:*``); those are migrated by the same
  ``RENAME`` since they match the prefix glob.

Assumptions
-----------
- The new prefix is NOT a prefix of the old one (no
  re-RENAME chain). ``acme-billing:knt:`` after a script
  pointed at ``from-prefix=knt:`` and ``to-prefix=acme-billing:knt:``
  is the canonical pattern.
- Operators have enough Redis connection budget for the
  ``RENAME`` spike (one per matched key). The default
  pool of 50 covers a SCAN loop; a busy production
  fleet should consider raising ``KNT_REDIS_MAX_CONNECTIONS``
  during the cutover window.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import redis.asyncio as aioredis
import redis.exceptions
import structlog

from kntgraph.infra.config import settings


# Default source / target prefixes. ``from_prefix`` is the
# bare ``"knt:"`` (the pre-076 wire format); ``to_prefix``
# is the ``KNT_REDIS_KEY_PREFIX`` value (defaults to ``""``,
# which means the script is a no-op -- which is the safe
# fallback for operators that set neither env var).
DEFAULT_FROM_PREFIX = "knt:"
SCAN_COUNT = 500  # see ADR-068 §1.2 for the rationale.
LOG_EVERY = 500  # per-key progress log cadence (less spam than 1/1).


logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MigrationReport:
    """Per-run summary table.

    Counters are additive across the SCAN; the operator
    sees the totals at the end. ``renamed`` is the count
    of successful ``RENAME`` calls; ``skipped`` covers
    the case where the target key already exists (the
    previous successful run, or another operator manually
    moved the data); ``errors`` is the count of
    unexpected ``ResponseError`` (anything that is not
    the "target exists" case).

    Not frozen: the migration loop mutates the counters
    directly. The dataclass is short-lived (one per
    ``migrate()`` call) and never crosses a trust boundary.
    """

    from_prefix: str
    to_prefix: str
    scanned: int = 0
    renamed: int = 0
    skipped: int = 0
    errors: int = 0
    samples: list[tuple[str, str]] = field(default_factory=list)
    """First ``LOG_EVERY`` ``(from, to)`` pairs, for the
    operator to eyeball in the dry-run summary. Empty
    after ``--commit`` (no need to show what already
    happened)."""

    def render(self, dry_run: bool) -> str:
        """Human-readable report."""
        mode = "DRY-RUN" if dry_run else "COMMIT"
        lines: list[str] = [
            f"=== Migration report ({mode}) ===",
            f"  from_prefix:  {self.from_prefix!r}",
            f"  to_prefix:    {self.to_prefix!r}",
            f"  scanned:      {self.scanned}",
            f"  renamed:      {self.renamed}",
            f"  skipped:      {self.skipped}  (target already exists)",
            f"  errors:       {self.errors}",
        ]
        if self.samples and dry_run:
            lines.append("")
            lines.append(
                "  sample of (from, to) pairs (first {}):".format(len(self.samples))
            )
            for frm, to in self.samples[:10]:
                lines.append(f"    {frm!r}  ->  {to!r}")
            if len(self.samples) > 10:
                lines.append(f"    ... ({len(self.samples) - 10} more)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------


async def _iter_keys(client: aioredis.Redis, from_prefix: str) -> AsyncIterator[str]:
    """Yield every Redis key under ``from_prefix``.

    Uses ``SCAN`` with ``COUNT=SCAN_COUNT``; the cursor
    contract guarantees the iteration visits every key
    exactly once even if the keyspace mutates during the
    scan. New keys created mid-scan are picked up;
    deleted keys may or may not appear.
    """
    cursor = 0
    while True:
        cursor, keys = await client.scan(
            cursor=cursor,
            match=f"{from_prefix}*",
            count=SCAN_COUNT,
        )
        for raw in keys:
            yield raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if cursor == 0:
            break


async def _rename(client: aioredis.Redis, frm: str, to: str) -> bool:
    """Atomic ``RENAME <frm> <to>``.

    Returns:
        ``True`` on success.
        ``False`` when the target key already exists
        (idempotent re-run or pre-existing operator data
        -- safe to skip, the data is already in place).
    Raises:
        Any other ``redis.exceptions.RedisError`` (network
        failure, permission denied, etc.) bubbles up so
        the caller can record it as an error.
    """
    try:
        await client.rename(frm, to)
        return True
    except redis.exceptions.ResponseError as exc:
        # ``ResponseError`` from ``RENAME`` is the
        # "target key exists" branch; the exact message
        # is version-dependent ("ERR target key name
        # already exists"), so we match by exception
        # class and let the caller log the message.
        if "already exists" in str(exc):
            return False
        raise


async def migrate(
    redis_url: str,
    from_prefix: str,
    to_prefix: str,
    *,
    dry_run: bool = True,
) -> MigrationReport:
    """The one-shot migration -- CLI entry point.

    Opens a Redis client from ``redis_url``, runs the
    migration, then closes the client. Tests inject the
    client directly via :func:`_migrate_with_client`.
    """
    client = aioredis.from_url(redis_url)
    try:
        return await _migrate_with_client(
            client, from_prefix=from_prefix, to_prefix=to_prefix, dry_run=dry_run
        )
    finally:
        await client.aclose()


async def _migrate_with_client(
    client: aioredis.Redis,
    *,
    from_prefix: str,
    to_prefix: str,
    dry_run: bool,
) -> MigrationReport:
    """The core loop. Split from ``migrate`` so tests can
    inject a ``fakeredis`` client.

    Empty ``to_prefix`` is a no-op: the script logs a
    warning and returns an empty report. This matches
    the ``KNT_REDIS_KEY_PREFIX=""`` default -- operators
    that did not opt in to the namespace scope have no
    data to migrate.
    """
    report = MigrationReport(from_prefix=from_prefix, to_prefix=to_prefix)

    if not to_prefix:
        logger.warning(
            "migrate_redis_keys.no_op",
            reason="empty to_prefix (operators that did not set "
            "KNT_REDIS_KEY_PREFIX have no data to migrate)",
        )
        return report

    if to_prefix == from_prefix:
        logger.warning(
            "migrate_redis_keys.no_op",
            reason="from_prefix == to_prefix; nothing to do",
        )
        return report

    async for frm in _iter_keys(client, from_prefix):
        to = f"{to_prefix}{frm[len(from_prefix) :]}"
        report.scanned += 1

        # Always sample the first LOG_EVERY keys for
        # the dry-run report (the operator wants to
        # eyeball what the migration would do). For
        # ``--commit`` we skip sampling -- the script
        # progress output is enough.
        if dry_run and len(report.samples) < LOG_EVERY:
            report.samples.append((frm, to))

        if dry_run:
            if report.scanned % LOG_EVERY == 0:
                logger.info(
                    "migrate_redis_keys.dry_run_progress",
                    scanned=report.scanned,
                )
            continue

        try:
            renamed = await _rename(client, frm, to)
        except redis.exceptions.RedisError as exc:
            logger.warning(
                "migrate_redis_keys.rename_failed",
                frm=frm,
                to=to,
                error=str(exc),
            )
            report.errors += 1
            continue
        if renamed:
            report.renamed += 1
            if report.renamed % LOG_EVERY == 0:
                logger.info(
                    "migrate_redis_keys.progress",
                    renamed=report.renamed,
                )
        else:
            report.skipped += 1

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="migrate_redis_keys",
        description=(
            "One-shot ``SCAN`` + ``RENAME`` for adopting "
            "``KNT_REDIS_KEY_PREFIX`` (ADR-076). Dry-run by default."
        ),
    )
    parser.add_argument(
        "--from-prefix",
        default=DEFAULT_FROM_PREFIX,
        help=(
            "Source namespace to migrate. Defaults to "
            "``knt:`` (the pre-076 wire format). Override "
            "if the operator's data lives at a non-default "
            "prefix already."
        ),
    )
    parser.add_argument(
        "--to-prefix",
        default=settings.redis_key_prefix,
        help=(
            "Target namespace. Defaults to "
            "``$KNT_REDIS_KEY_PREFIX`` (or ``"
            "`` when the "
            "env var is unset, in which case the script is "
            "a no-op)."
        ),
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "Actually call ``RENAME``. Without this flag "
            "the script only prints what it would do (dry-run)."
        ),
    )
    parser.add_argument(
        "--redis-url",
        default=settings.redis_url,
        help=("Redis URL (default: ``$KNT_REDIS_URL`` or ``redis://localhost:6379``)."),
    )
    return parser.parse_args(argv)


async def amain(argv: list[str]) -> int:
    args = parse_args(argv)
    logger.info(
        "migrate_redis_keys.start",
        from_prefix=args.from_prefix,
        to_prefix=args.to_prefix,
        dry_run=not args.commit,
    )
    report = await migrate(
        redis_url=args.redis_url,
        from_prefix=args.from_prefix,
        to_prefix=args.to_prefix,
        dry_run=not args.commit,
    )
    print(report.render(dry_run=not args.commit))
    return 1 if report.errors else 0


def main() -> None:
    sys.exit(asyncio.run(amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
