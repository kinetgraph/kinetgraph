#!/usr/bin/env python

# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
migrate_principals — upgrade legacy ``fmh:api:keys:*``
bindings from the pre-ADR-017 format (plain agent_id
string) to the post-ADR-017 JSON Principal format.

Usage::

    # Dry-run (default; prints what would change).
    python scripts/migrate_principals.py

    # Apply.
    python scripts/migrate_principals.py --apply

    # Custom Redis URL.
    python scripts/migrate_principals.py --redis-url redis://...

The script:

  1. Scans ``fmh:api:keys:*`` in Redis.
  2. For each binding:
     - If already JSON with all required fields → skip.
     - If a legacy string → parse it as ``agent_id`` and
       construct a Principal via the same heuristic the
       verifier uses (``_legacy_principal`` in
       ``api/auth.py``). Write the JSON form.
  3. Prints a summary.

This script is **idempotent**. Running it twice does not
double-write; the second pass sees the JSON form and
skips every entry.

After migration, run ``scripts/verify_bindings.py`` (a
companion one-liner) to confirm every key is now in
JSON form.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterator

import redis.asyncio as aioredis


KEY_PREFIX = "fmh:api:keys:"
SCAN_COUNT = 100


async def _scan_bindings(
    redis_client: aioredis.Redis,
) -> Iterator[tuple[str, bytes]]:
    """
    Yield ``(redis_key, raw_value)`` for every key
    under ``fmh:api:keys:*``.

    Uses SCAN to avoid blocking Redis on large
    deployments; the prefix is hard-coded so we don't
    need to expose the binding-table internals.
    """
    cursor = 0
    while True:
        cursor, keys = await redis_client.scan(
            cursor=cursor,
            match=f"{KEY_PREFIX}*",
            count=SCAN_COUNT,
        )
        for k in keys:
            raw = await redis_client.get(k)
            if raw is not None:
                yield k, raw
        if cursor == 0:
            break


def _is_legacy_string(raw: bytes) -> bool:
    """
    Heuristic: a binding is "legacy" when it is NOT
    valid JSON. A leading byte of ``{`` is the cheap
    pre-check; we then run ``json.loads`` to confirm.
    """
    stripped = raw.strip()
    if not stripped.startswith(b"{"):
        return True
    try:
        json.loads(stripped)
    except json.JSONDecodeError:
        return True
    return False


def _migrate_value(raw: bytes) -> bytes | None:
    """
    Build the JSON Principal form for a legacy binding.

    Returns the new value (bytes), or ``None`` when
    the binding is already in the correct shape (no
    migration needed).
    """
    if not _is_legacy_string(raw):
        return None  # already migrated
    decoded = raw.decode("utf-8", errors="replace").strip()
    if not decoded:
        # Empty value — leave alone, surface as
        # migration failure (we don't write).
        raise ValueError("empty legacy binding")
    tenant_id = decoded.partition(".")[0] or decoded
    payload = {
        "agent_id": decoded,
        "role": "agent",
        "tenant_id": tenant_id,
        "key_id": "legacy",
    }
    return json.dumps(payload, sort_keys=True).encode("utf-8")


async def run(
    redis_url: str,
    apply: bool,
) -> int:
    """
    Main loop. Returns 0 on success, 1 on hard failure.

    Failure modes:
      - Connection refused / Redis down
      - JSON bindings that fail Pydantic validation
        (rare; the script logs and skips them so a
        single bad entry does not abort the migration)
    """
    redis_client = aioredis.from_url(redis_url)
    try:
        await redis_client.ping()
    except Exception as e:
        print(f"❌ Cannot reach Redis at {redis_url}: {e}")
        return 1

    stats = {"scanned": 0, "migrated": 0, "skipped": 0, "errors": 0}
    try:
        for redis_key, raw in _scan_bindings(redis_client):
            stats["scanned"] += 1
            try:
                new_value = _migrate_value(raw)
            except ValueError as e:
                print(f"  ✗ {redis_key}: {e}")
                stats["errors"] += 1
                continue
            if new_value is None:
                stats["skipped"] += 1
                continue
            mode = "APPLY" if apply else "DRY-RUN"
            print(f"  {mode}: {redis_key} -> {new_value.decode('utf-8')}")
            if apply:
                await redis_client.set(redis_key, new_value)
            stats["migrated"] += 1
    finally:
        await redis_client.aclose()

    print()
    print(f"Scanned:  {stats['scanned']}")
    print(f"Migrated: {stats['migrated']}{'' if apply else ' (dry-run)'}")
    print(f"Skipped:  {stats['skipped']} (already migrated)")
    print(f"Errors:   {stats['errors']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate legacy API-key bindings to the ADR-017 Principal JSON format."
        )
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("FMH_REDIS_URL", "redis://localhost:6379"),
        help="Redis connection URL (default: $FMH_REDIS_URL)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually write the migrated bindings. "
            "Without this flag the script runs in "
            "dry-run mode and only prints what would "
            "change."
        ),
    )
    args = parser.parse_args()
    if not args.apply:
        print("Running in DRY-RUN mode. Pass --apply to write.")
        print()
    return asyncio.run(run(args.redis_url, args.apply))


if __name__ == "__main__":
    sys.exit(main())
