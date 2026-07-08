#!/usr/bin/env python

# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
migrate_profile_to_continuity — one-shot migration for tenants
that pre-date ADR-014.

Background
----------
Before ADR-014, the ``profile`` tier carried two kinds of
state mixed together: static preferences (regime tributário,
tier SLA, e-mail de NF-e) AND recency state (last CFOP used,
last client seen, last category chosen). The ADR separates
them: static preferences stay in ``profile``; recency state
moves to ``continuity``.

This script scans every ``profile.preference_set`` event in
the EventLog, classifies each ``pref_key`` by name, and
emits the corresponding ``continuity.*`` event for keys
that match a recency pattern. Static keys are left alone.

Idempotency
-----------
The EventLog dedupes on ``event_id``, which is a uuid5 of
(agent_id, event_type, payload). Re-running the script
produces the same events; calling it twice does not
duplicate anything.

Dry-run by default
------------------
Without ``--commit``, the script only PRINTS what it
would do. With ``--commit``, it actually calls
``ContinuityManager.record_*`` and writes to the EventLog.
This is the standard safe default for migration scripts.

Output
------
Per-tenant, per-user report with three counters:
  - migrated  (preference_set events translated to continuity)
  - skipped   (preference_set events that were static)
  - errors    (record_* returned Err)

A final summary table is printed at the end.

Usage
-----
    python scripts/migrate_profile_to_continuity.py            # dry-run
    python scripts/migrate_profile_to_continuity.py --commit   # apply
    python scripts/migrate_profile_to_continuity.py --tenant 12.345.678/0001-90
    FMH_REDIS_FAKE=1 python scripts/migrate_profile_to_continuity.py

Heuristics (the classification table)
-------------------------------------
The classification lives in a single function,
:classify_pref_key`. It is conservative: when in doubt, the
key is treated as static and left in the profile. The table
is documented inline below.

  prefix/suffix              → continuity event kind
  ``last_cnpj_*``            → continuity.entity_seen(kind=cnpj, value_hash=...)
  ``last_cpf_*``             → continuity.entity_seen(kind=cpf, value_hash=...)
  ``last_email_*``           → continuity.entity_seen(kind=email, value_hash=...)
  ``last_phone_*``           → continuity.entity_seen(kind=phone, value_hash=...)
  ``last_pix_*``             → continuity.entity_seen(kind=pix, value_hash=...)
  ``last_chave_*``           → continuity.entity_seen(kind=chave_nfe, value_hash=...)
  ``last_*`` (other)         → continuity.category_chosen(slot=last_<key>, value=...)
  ``recent_*``               → continuity.category_chosen(slot=recent_<key>, value=...)
  ``previous_*``             → continuity.category_chosen(slot=previous_<key>, value=...)
  anything else              → SKIP (stays in profile)
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
from dataclasses import dataclass, field
from typing import Iterable, Optional

import redis.asyncio as aioredis
import structlog

from kntgraph.core.event import Event
from kntgraph.infra.config import settings
from kntgraph.memory.consolidation import parse_agent_id
from kntgraph.memory.continuity import (
    ContinuityManager,
)
from kntgraph.memory.profile import (
    ProfileEventType,
    ProfileManager,
)
from kntgraph.stream.event_log import EventLog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Classification table
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Classification:
    """Result of classifying a single preference key."""

    target: str  # "category" | "entity" | "skip"
    slot: str = ""
    entity_kind: str = ""

    @property
    def is_skip(self) -> bool:
        return self.target == "skip"


_ENTITY_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("last_cnpj_", "cnpj"),
    ("last_cpf_", "cpf"),
    ("last_email_", "email"),
    ("last_phone_", "phone"),
    ("last_telefone_", "phone"),
    ("last_pix_", "pix"),
    ("last_chave_", "chave_nfe"),
)


def classify_pref_key(key: str) -> Classification:
    """
    Map a ``pref_key`` (the ``key`` field of a
    ``profile.preference_set`` event) to a target in the
    continuity tier.

    Conservative by design: when in doubt, return
    ``Classification(target="skip")`` and leave the key in
    the profile. The heuristic is a single pass over a
    small table; if a tenant has a non-standard key naming
    convention, extend the table here (do NOT special-case
    in the migration loop).
    """
    # Entity (PII) categories: must be hashed.
    for prefix, kind in _ENTITY_KIND_PREFIXES:
        if key.startswith(prefix):
            # The remaining suffix becomes the slot name so
            # the agent can distinguish last_cnpj_client
            # from last_cnpj_supplier.
            slot = key[len(prefix) :]
            return Classification(target="entity", slot=slot, entity_kind=kind)

    # Non-PII recency slots.
    for prefix in ("last_", "recent_", "previous_"):
        if key.startswith(prefix):
            return Classification(target="category", slot=key)

    return Classification(target="skip")


# ---------------------------------------------------------------------------
# Migration report
# ---------------------------------------------------------------------------


@dataclass
class UserReport:
    tenant_id: str
    user_id: str
    migrated: int = 0
    skipped: int = 0
    errors: int = 0
    details: list[str] = field(default_factory=list)


@dataclass
class MigrationReport:
    tenant_filter: Optional[str]
    dry_run: bool
    user_reports: list[UserReport] = field(default_factory=list)

    @property
    def total_migrated(self) -> int:
        return sum(r.migrated for r in self.user_reports)

    @property
    def total_skipped(self) -> int:
        return sum(r.skipped for r in self.user_reports)

    @property
    def total_errors(self) -> int:
        return sum(r.errors for r in self.user_reports)


# ---------------------------------------------------------------------------
# Migration logic
# ---------------------------------------------------------------------------


def iter_preference_set_events(
    events: Iterable[Event],
) -> Iterable[Event]:
    """
    Yield only the events that matter for migration:
    ``profile.preference_set`` events with a non-empty
    ``key``. ``profile.created`` seeds the initial state
    but is folded into the current ``ProfileState``, so we
    read the final state and re-derive the latest values
    per key (see ``current_preference_set_events``).
    """
    for e in events:
        if e.event_type != ProfileEventType.PREFERENCE_SET:
            continue
        key = e.data.get("key")
        value = e.data.get("value")
        if not key or value is None:
            continue
        yield e


def current_preference_set_events(events: list[Event]) -> list[Event]:
    """
    Reduce the preference stream to the LATEST event per
    key. ``preference_set`` is an "override" semantic (the
    last value wins); ``preference_unset`` removes a key.

    This mirrors the fold in ``_fold_profile_events`` but
    is isolated here so the migration script does not
    depend on internal helpers.
    """
    latest: dict[str, Event] = {}
    for e in events:
        if e.event_type == ProfileEventType.PREFERENCE_SET:
            key = e.data.get("key")
            if key:
                latest[key] = e
        elif e.event_type == ProfileEventType.PREFERENCE_UNSET:
            key = e.data.get("key")
            if key in latest:
                # The unset wins, drop the latest set.
                del latest[key]
    return list(latest.values())


async def migrate_one_user(
    cm: ContinuityManager,
    pm: ProfileManager,
    tenant_id: str,
    user_id: str,
    dry_run: bool,
) -> UserReport:
    """
    Migrate a single (tenant_id, user_id) pair.

    Reads the EventLog for the profile agent, folds to the
    current preference set, classifies each key, and emits
    the corresponding continuity event. Reports counts and
    details; never raises on individual failures (collected
    in ``errors``).
    """
    report = UserReport(tenant_id=tenant_id, user_id=user_id)

    profile_agent_id = ProfileManager.agent_id_for(tenant_id, user_id)
    events = await pm._log.read(profile_agent_id)
    if not events:
        report.details.append(f"no profile events at {profile_agent_id}; skip")
        return report

    latest_events = current_preference_set_events(events)
    if not latest_events:
        report.details.append("profile has no preference_set events; skip")
        return report

    # The continuity fold returns ``None`` until a
    # ``continuity.created`` event exists. To make the
    # migration result immediately usable, emit ``create``
    # first (idempotent on (tenant, user) — see
    # ``ContinuityManager.create``).
    if dry_run:
        report.migrated += 1
        report.details.append("  DRY  continuity.create")
    else:
        cr = await cm.create(tenant_id=tenant_id, user_id=user_id)
        if cr.is_err():
            report.errors += 1
            report.details.append(f"  ERR  continuity.create failed: {cr.err_value()}")
            return report
        report.migrated += 1
        report.details.append("  OK   continuity.create")

    for e in latest_events:
        key = str(e.data["key"])
        value = str(e.data["value"])
        cls = classify_pref_key(key)

        if cls.is_skip:
            report.skipped += 1
            report.details.append(f"  SKIP {key} = {value!r}")
            continue

        # Surface the plan BEFORE the side-effecting call.
        action = f"  {key} = {value!r} → continuity.{cls.target}(slot={cls.slot!r}"
        if cls.target == "entity":
            action += f", kind={cls.entity_kind!r}, value_hash=<sha256:...>"
        action += ")"

        if dry_run:
            report.migrated += 1
            report.details.append(f"  DRY  {action}")
            continue

        # Real write.
        try:
            if cls.target == "category":
                r = await cm.record_category_chosen(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    slot=cls.slot,
                    value=value,
                )
            elif cls.target == "entity":
                r = await cm.record_entity_seen(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    kind=cls.entity_kind,
                    value_hash=cm.hash_value(value),
                    source="profile_migration",
                )
            else:  # pragma: no cover
                raise RuntimeError(f"unreachable target {cls.target!r}")
        except Exception as exc:  # noqa: BLE001
            report.errors += 1
            report.details.append(f"  ERR  {action}  ({type(exc).__name__}: {exc})")
            continue

        if r.is_ok():
            report.migrated += 1
            report.details.append(f"  OK   {action}")
        else:
            report.errors += 1
            err = r.err_value()
            report.details.append(f"  ERR  {action}  ({err})")

    return report


async def list_profile_agents(
    log: EventLog,
) -> list[tuple[str, str]]:
    """
    Return every (tenant_id, user_id) pair that has a
    profile in the EventLog.

    Iterates the EventLog's stream of agent_ids and keeps
    only those whose id parses as a profile MemoryAgent.
    """
    out: list[tuple[str, str]] = []
    for aid in await log._list_agent_ids():
        mem = parse_agent_id(aid)
        if mem is None or mem.kind != "profile":
            continue
        out.append((mem.id1, mem.id2))
    return out


async def run(
    redis_url: str,
    tenant_filter: Optional[str],
    dry_run: bool,
) -> MigrationReport:
    redis = aioredis.from_url(redis_url)
    try:
        await redis.ping()
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            f"Could not reach Redis at {redis_url}: {type(e).__name__}: {e}"
        )

    log = EventLog(redis)
    cm = ContinuityManager(event_log=log, redis_client=redis, ttl_seconds=60)
    pm = ProfileManager(event_log=log, redis_client=redis)

    pairs = await list_profile_agents(log)
    if tenant_filter is not None:
        pairs = [(t, u) for (t, u) in pairs if t == tenant_filter]

    report = MigrationReport(tenant_filter=tenant_filter, dry_run=dry_run)

    if not pairs:
        logger.warning(
            "migrate.no_profiles_found",
            tenant_filter=tenant_filter,
        )
        return report

    for tenant_id, user_id in pairs:
        ur = await migrate_one_user(cm, pm, tenant_id, user_id, dry_run=dry_run)
        report.user_reports.append(ur)

    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_report(report: MigrationReport) -> str:
    lines: list[str] = []
    mode = "DRY-RUN" if report.dry_run else "COMMIT"
    lines.append(f"=== Migration report ({mode}) ===")
    if report.tenant_filter:
        lines.append(f"  tenant filter: {report.tenant_filter}")
    lines.append(f"  profiles scanned: {len(report.user_reports)}")
    lines.append(f"  migrated:         {report.total_migrated}")
    lines.append(f"  skipped:          {report.total_skipped}")
    lines.append(f"  errors:           {report.total_errors}")
    lines.append("")
    for ur in report.user_reports:
        lines.append(
            f"  [{ur.tenant_id}:{ur.user_id}] "
            f"migrated={ur.migrated} "
            f"skipped={ur.skipped} "
            f"errors={ur.errors}"
        )
        if ur.details:
            for line in ur.details:
                lines.append(line)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="migrate_profile_to_continuity",
        description=(
            "Migrate recency state from ``profile`` to "
            "``continuity`` (ADR-014). Dry-run by default."
        ),
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help=(
            "Actually write to the EventLog. Without this "
            "flag the script only prints what it would do."
        ),
    )
    p.add_argument(
        "--tenant",
        default=None,
        help=(
            "Restrict to a single tenant_id "
            "(e.g. 12.345.678/0001-90). Default: all tenants."
        ),
    )
    p.add_argument(
        "--redis-url",
        default=settings.redis_url,
        help="Redis URL (default: $FMH_REDIS_URL or redis://localhost:6379).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-action detail lines.",
    )
    return p.parse_args(argv)


async def amain(argv: list[str]) -> int:
    args = parse_args(argv)
    report = await run(
        redis_url=args.redis_url,
        tenant_filter=args.tenant,
        dry_run=not args.commit,
    )
    if args.quiet:
        # Trim per-action lines, keep only the per-user roll-up.
        trimmed = dataclasses.replace(report)
        for ur in trimmed.user_reports:
            ur.details = []
        print(format_report(trimmed))
    else:
        print(format_report(report))
    return 1 if report.total_errors else 0


def main() -> None:
    sys.exit(asyncio.run(amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
