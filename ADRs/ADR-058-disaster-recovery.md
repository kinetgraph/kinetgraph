<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-058: Disaster Recovery — restore procedures, retention policy, and the WAIT budget

- **Status:** Implemented (rev. 4 — read-only thin cold tier)
- **Date:** 2026-08-15 (rev. 2: 2026-08-15; rev. 3: 2026-08-15; rev. 4: 2026-08-15)
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-057](./ADR-057-durabilidade-dos-dados.md) — Data Storage (sibling; historical events, schema, offload, maintenance)
  - [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md) — World and incremental fold (now also owns the `WorldSnapshot` cost/value question, §3.6)
  - [ADR-005](./ADR-005-Checkpoints-Idempotency.md) — checkpoints and idempotency
  - [ADR-002](./ADR-002-Replay-Puro.md) — canonical replay
  - [ADR-045](./ADR-045-Tool-Call-Request-TTL.md) — ToolCallRequest TTL
  - [ADR-037](./ADR-037-Mandatory-Correlation-Propagation.md) — correlation propagation (referenced in §2.1 step 3, §5.1)

> **Scope.** This ADR isolates **disaster recovery** from
> the **data layer** covered by ADR-057. It commits the
> operational runbooks (restore AOF, replay `EventLog`,
> reconstruct `World`, **restore from Iceberg when
> enabled**), the **WAIT-budget** for `WAIT`-protected
> flows, the **retention policy** that bounds the blast
> radius of any single failure, the **tag-naming
> contract** that the cold tier uses to mark restore
> points, and the **read-only `knt dr` CLI** for DR state
> inspection.
>
> **Revision 4 — read-only thin cold tier.** Earlier
> revisions of this ADR conflated the cold-tier **read**
> (DR) with the cold-tier **write** (offload, schema,
> partition, warehouse config). **v4** splits along the
> axis of who writes and who reads:
>
> - This ADR (058) commits: `IcebergRestore` (read-only
>   wrapper), the hybrid restore algorithm, the promote
>   step (cold→hot), the `ice_drill` agent, the source-
>   selection logic that decides where a re-fold reads
>   from, the drill cadence, the WAIT budget, **and the
>   tag-naming contract** (`make_restore_point_tag`).
> - [ADR-057](./ADR-057-durabilidade-dos-dados.md)
>   commits: the Iceberg schema, the partition spec, the
>   sort order, the warehouse and catalog configuration,
>   the `ice_offload` agent (the **writer** of cold-tier
>   data), the `ice_maintenance` agent, the
>   `ice_offload_tick` tool, and the retention defaults.
>
> The 058 **does not** decide what the Iceberg schema is,
> how the table is partitioned, or how the offload is
> orchestrated. The 057 **does not** decide what a
> restore point is, when the cold tier is consulted for
> a re-fold, or how the drill cadence is set. The two
> ADRs meet at the **tag**: the 058 owns the tag
> convention, the 057's `ice_offload_tick` tool creates
> the tag using the 058's exported function.
>
> **PostgreSQL is an architectural error for this use
> case.** v1 demoted Postgres; v2 demoted Postgres; v3
> names the demotion explicitly; v4 keeps the demotion.
> The relational tier was duplicating state (the EventLog
> is already the source of truth) and adding a second
> stateful service whose only job was to project events
> into rows. The framework does not require it, and any
> deployment that reintroduces it carries its own
> recovery obligation outside this ADR.

## 1. Context

ADR-057 commits the **data shape** and **durability stance**
of the kntgraph data layer (Redis AOF + RDB, Iceberg offload,
`WAIT` for critical flows). It does **not** commit:

1. The **restore procedure** in operational terms — who
   triggers restore, from which artefact, against which SLO,
   and how the system is verified afterwards.
2. The **WAIT budget** — what is the measured latency cost of
   `WAIT numreplicas 1` per write, and what percentile is the
   intake allowed to add to its critical-path latency before
   the trade-off flips.
3. The **retention policy** — how long backups, AOF rewrites,
   and Iceberg snapshots are kept, and at which point the
   restore test is invalidated by staleness.
4. **The role of Iceberg in recovery** — ADR-057 §2.3
   commits Iceberg as a **read-only analytics layer** with
   `snapshot_retention_days=7` and S3 versioning for 365
   days. v2 of this ADR promoted Iceberg to a recovery
   tier; **v3 makes the cold tier optional** via
   `KNT_ICEBERG_ENABLED`. When the flag is off, the
   framework's recovery contract is Redis alone.
5. **Why PostgreSQL is the wrong shape here.** Adding a
   relational store to back projections of an EventLog
   that already has a stream-based primary is duplicate
   state. The second service costs more than it saves:
   a DBA rotation, PITR drill, base-backup schedule, and
   WAL archive are not justified by the projection-only
   workload. The framework does not require it, and any
   deployment that reintroduces it carries its own
   recovery obligation outside this ADR.

This ADR closes those five gaps. The recovery contract is
**always** the hot tier (Redis). The cold tier (Iceberg) is
a deployment option, not a framework commitment.

## 2. Decision

The recovery model is **one mandatory tier, one optional
tier**:

- **Hot tier — Redis (mandatory).** Primary source of
  truth for the `EventLog` (ADR-002, ADR-057 §4.1). Restore
  target for the common case (single-node failure, AOF
  replay, replica rebuild). The restore procedure is
  identical to v1 of this ADR. **Every deployment has
  this tier.**
- **Cold tier — Iceberg (optional).** Enabled via
  `KNT_ICEBERG_ENABLED=true` at project creation. When
  enabled, Iceberg is the authoritative offload of the
  EventLog (ADR-057 §4.9) and the restore source when
  Redis is lost beyond AOF retention. When disabled, the
  recovery contract ends at §2.1; §2.2 is **not
  applicable**. There is no need to opt out at runtime
  — the flag is a project-creation decision.

There is no relational tier. PostgreSQL, if deployed, is a
projection sink with no recovery obligation under this
ADR.

### 2.1 Restore procedure — hot tier (Redis)

The Redis restore is the same three ordered steps as v1.
Each step has an owner and a verification step before the
next.

#### Step 1 — Restore the EventLog from AOF/RDB

- **Artefact:** the most recent AOF rewrite, plus the RDB
  snapshot taken immediately before the rewrite (mixed mode,
  `aof-use-rdb-preamble yes`).
- **Owner:** SRE on call.
- **Action:** boot a fresh Redis replica from the AOF + RDB;
  verify the replica reaches the same `last_stream_id` as the
  one reported in the most recent `WAIT` ack from the intake.
- **Verification:** `INFO replication` shows the new replica
  fully caught up; `XLEN knt:agent:<id>:events` matches the
  pre-failure count recorded in the intake's last health
  snapshot.
- **SLO:** **RTO ≤ 30 min** for the Redis cluster to be back
  online accepting writes. **RPO ≤ 1 s** for flows protected
  by `WAIT`; **RPO ≤ N** for flows without `WAIT`, where N
  is the `XADD ... MAXLEN` cap of the affected stream.
- **Failure mode that escalates to §2.2:** AOF/RDB artefacts
  are missing, corrupted, or older than the retention
  window in §2.3. In that case, the EventLog is rebuilt
  from the Iceberg cold tier.

#### Step 2 — Replay the EventLog into a fresh `World`

- **Artefact:** the restored EventLog (either from §2.1
  step 1, or from §2.2 if Redis was lost beyond AOF).
- **Owner:** framework bootstrap (automated).
- **Action:** cold-start each tracked agent via
  `World.fold(events_from_xrange_minus_to_plus,
  projection=project_default)`. The result is a fresh
  `World` whose `last_stream_id` matches the cursor the
  `IncrementalWorldStore` is about to write.
- **Verification:** `World.fold` is deterministic; running it
  twice against the same input produces the same
  `state_hash`. The bootstrap records both hashes and fails
  loud if they diverge.
- **SLO:** **≤ 5 min for 100 k events / agent**, measured at
  the median. This is the number that bounds the cluster
  rejoin time after Redis is back.

#### Step 3 — Replay `correlation_id` integrity

- **Artefact:** the restored `World` and the restored
  `EventLog`.
- **Owner:** framework bootstrap (automated).
- **Action:** for every agent, walk the EventLog and assert
  that every `ToolCallRequest` carries the same
  `correlation_id` as the matching `ToolCallCompletion`
    (or a `NoOpCompletion`). Divergences are
    logged and surfaced to SRE.
- **Verification:** invariant assertion fails the bootstrap
  if any divergence is detected.
- **SLO:** **must complete before the dispatcher accepts
  new intents**; failure means manual intervention.

### 2.2 Restore procedure — cold tier (Iceberg, **conditional on `KNT_ICEBERG_ENABLED=true`**)

When the cold tier is enabled, the cold tier is the
**historical baseline** that backs the §2.1 restore when
AOF/RDB are insufficient. This ADR is the **read-only
consumer** of the cold tier; the **writer** is owned by
[ADR-057 §4.11](./ADR-057-durabilidade-dos-dados.md). The
two ADRs meet at the **tag** (§2.2.1.5): the 058 owns the
naming convention, the 057's `ice_offload_tick` tool
creates the tag using the 058's exported function.

When `KNT_ICEBERG_ENABLED=false`, **this entire section is
not applicable.** The recovery contract is §2.1 alone.

#### 2.2.1 Read-only entry point — `IcebergRestore`

This ADR ships **one** class. It is read-only; the writer
is `ice_offload_tick` in
[ADR-057 §4.11](./ADR-057-durabilidade-dos-dados.md). No
custom adapter Protocol, no `LocalFilesystemAdapter` or
`S3Adapter` (those were deleted in v3) — the deployment
configures a warehouse URI and a catalog URI once at
project bootstrap, and both `IcebergRestore` (here) and
`ice_offload_tick` (in 057) read the same configuration.

| Flag | Owner | Default | Purpose |
|---|---|---|---|
| `KNT_ICEBERG_ENABLED` | 058 | `false` | Master switch for the cold tier |
| `KNT_ICEBERG_WAREHOUSE` | 057 | `file:///var/lib/kntgraph/iceberg/warehouse` | Warehouse URI; PyIceberg accepts `file://`, `s3://`, `s3a://`, `gs://`, `abfs://` |
| `KNT_ICEBERG_CATALOG_URI` | 057 | `sqlite:////var/lib/kntgraph/iceberg/catalog.db` | Catalog URI; `SqlCatalog` over SQLite is the dev/CI default |
| `KNT_ICEBERG_SNAPSHOT_INTERVAL_S` | 057 | `3600` | How often the offload tool commits a new snapshot |
| `KNT_ICEBERG_OFFLOAD_BATCH` | 057 | `10000` | Events per offload batch |
| `KNT_ICEBERG_TABLE_NAME` | 057 | `knt_events` | Iceberg table identifier |
| `KNT_RETENTION_ICEBERG_SNAPSHOTS_DAYS` | 057 (default) / 058 (consumer) | `7` | Snapshot retention; consumed by `ice_maintenance` for `expire_snapshots` |
| `KNT_RETENTION_EVENT_CLASS_<CLASS>__TTL_S` | 057 (default) / 058 (consumer) | per [ADR-057 §4.11.6.4](./ADR-057-durabilidade-dos-dados.md) | Per-`event_class` Redis retention; consumed by `ice_offload_tick` for the per-class XTRIM |

The 058 **does not** commit defaults for the warehouse,
catalog, snapshot interval, batch size, or table name —
those are data-layer decisions and live in 057. The 058
**does** commit the `KNT_ICEBERG_ENABLED` flag and the
SLOs that govern the cold tier as a **restore source**.

```python
class IcebergRestore:
    """Read-only access to the cold tier for restore.

    The writer is ADR-057 §4.11 `ice_offload_tick`. This
    class never mutates the catalog.
    """

    def __init__(self) -> None:
        # Reads KNT_ICEBERG_WAREHOUSE and KNT_ICEBERG_CATALOG_URI
        # from the 057 settings; the 058 does not own those
        # flags.
        ...

    def list_snapshots(
        self, table: str, *, since: datetime | None = None
    ) -> list[IcebergSnapshotRef]: ...

    def read_events(
        self,
        table: str,
        *,
        snapshot_id: int | None = None,
        as_of: datetime | None = None,
        agent_id: str | None = None,
        since_stream_id: str | None = None,
        until_stream_id: str | None = None,
    ) -> Iterator[pa.RecordBatch]: ...

    def manifest(self) -> IcebergManifest: ...
```

`IcebergRestore` is a **thin wrapper** over a PyIceberg
catalog. `read_events` returns Arrow `RecordBatch` streams
(native PyArrow, the same format PyIceberg reads from
Parquet) — the framework's `World.fold` consumes them
without a Python-level materialisation step. `manifest()`
returns the resolved warehouse, the resolved catalog URI
and class, the table schema (per
[ADR-057 §4.5](./ADR-057-durabilidade-dos-dados.md)), the
list of `restore_point_*` tags, the `current_snapshot_id`,
**and `last_stream_id_per_class: dict[str, str]` per
agent** (the per-class sub-cursors from
[ADR-057 §4.11.6](./ADR-057-durabilidade-dos-dados.md)) —
these are the cursors that bound the per-class `tail` in
the hybrid restore (§2.2.2). It is the first thing the
runbook prints; if it fails, restore is aborted and SRE
is paged.

#### 2.2.1.5 Tag-naming contract (owned by 058)

The cold tier marks each commit with a **tag** so that
`IcebergRestore.manifest()` can identify a stable
"restore point" without scanning the entire snapshot
history. The naming convention is **owned by this ADR**;
the `ice_offload_tick` tool in
[ADR-057 §4.11](./ADR-057-durabilidade-dos-dados.md)
imports the function from here and uses it.

```python
# Defined in this ADR's module; exported.
RESTORE_POINT_TAG_PREFIX = "restore_point_snap_"

def make_restore_point_tag(snapshot_id: int) -> str:
    """Return the canonical restore-point tag name for a
    given Iceberg snapshot ID. The 057 `ice_offload_tick`
    tool calls this after each `table.append`."""
    return f"{RESTORE_POINT_TAG_PREFIX}{snapshot_id}"
```

**Why this is the 058's contract.** The tag is a
**restore primitive** — it exists so the cold-tier restore
can find a stable point in time. The 057's `ice_offload`
does not need the tag for its own correctness; it creates
the tag as a side-effect of the offload, on behalf of the
058. The naming format, the prefix, and the `snapshot_id`
suffix are 058 decisions.

**Idempotency.** `manage_snapshots().create_tag` is
idempotent on (snapshot_id, tag_name) — re-creating the
same tag on the same snapshot is a no-op. The 057's tool
relies on this for retry semantics (see
[ADR-057 §4.11.4](./ADR-057-durabilidade-dos-dados.md)).

#### 2.2.2 Hybrid restore algorithm

The algorithm is identical regardless of warehouse URI or
catalog class; only the catalog and storage differ, and
that is PyIceberg's job.

With the per-`event_class` sub-cursors committed by
[ADR-057 §4.11.6](./ADR-057-durabilidade-dos-dados.md),
the hybrid restore reads **per-class tails** instead of a
single global tail. The `baseline` (the Iceberg snapshot)
is shared across classes — one snapshot, multiple
`event_class` rows — but the `tail` from Redis is read
once per class and folded independently. The bootstrap
cursor is `max(stream_id)` across all classes.

```
Inputs:
  - restore: IcebergRestore
  - agents: list[str]
  - last_known_stream_id_per_class: dict[str, str] | None
       # per ADR-057 §4.11.6; recovered from the last
       # WAIT ack or the intake's last health snapshot.
       # If None, use the baseline as the only source.
  - target_stream_id: str | None
       # optional cutoff (same for all classes)

For each agent in agents:
  1. manifest = restore.manifest()
     if manifest has no restore_point tag, abort
     with "cold tier has no restore points" — escalate
     to SRE. The 057 `ice_offload` may not have run yet
     in this deployment.
  2. baseline_snapshot = manifest.latest_restore_point.snapshot_id
     # Pick the most recent tag. The 057 §4.11.4 guarantees
     # that the snapshot tagged is the snapshot that
     # successfully committed and was followed by XTRIM
     # (per-class, per §4.11.6).
  3. baseline = restore.read_events(
       table=manifest.table_name,
       snapshot_id=baseline_snapshot,
       agent_id=agent,        # Parquet row-group filter
     )
     # baseline contains all classes below the snapshot
     # boundary; the schema carries `event_class` per row.
  4. tail_per_class: dict[str, list[Event]] = {}
     for event_class, last_id in last_known_stream_id_per_class.items():
       tail_per_class[event_class] = EventLog.xrange(
         key=knt:agent:<id>:events,
         min=last_id,          # the class's own sub-cursor
         max=target_stream_id,
       )   # may be empty per class if Redis was lost
           # OR if the class's retention has expired
           # (see ADR-057 §4.11.6.4 — e.g., "lifecycle" TTL 7d)
  5. fold(baseline + concat(tail_per_class.values()))
     → World for this agent
     # Fold is idempotent on stream_id (ADR-002, ADR-005),
     # so any overlap between baseline and tail is absorbed
     # and any class gap (expired retention) is filled by
     # the Iceberg snapshot.
  6. assert correlation_id integrity (§2.1 step 3)
  7. write bootstrap cursor = max stream_id seen across
     all classes; record last_stream_id_per_class for the
     next cold-tier restore.

Output:
  - one World per agent, fold-deterministic
  - bootstrap manifest: {
      baseline_snapshot,
      last_known_stream_id_per_class,
      target_stream_id,
      count_baseline_per_class, count_tail_per_class,
      fold_hash, duration_s,
      max_stream_id_per_class,  # for the next cursor
    }
```

The two interesting failure modes:

- **Gap between Iceberg and EventLog, per class.** The
  offload tool is eventually consistent per class; a window
  of duplicate events may exist for `event_class=C` if the
  last XTRIM for `C` has not yet fired. The fold is
  **idempotent on `stream_id`** (ADR-002, ADR-005), so
  duplicates are absorbed. The bootstrap records the count
  of events seen twice per class in the manifest (an
  observability signal, not a correctness requirement).
- **Iceberg ahead of the intake, per class.** If the
  offload tool raced the restore and the most recent tagged
  snapshot is newer than `last_known_stream_id_per_class[C]`
  for some class `C`, the tail for `C` is read up to the
  snapshot's `max_stream_id` instead, and `target_stream_id`
  is clamped at the snapshot boundary for `C` only. This is
  the **safe** direction; the inverse direction (EventLog
  ahead of Iceberg) is the **gap** mode above.
- **Class retention expired in Redis.** If
  `last_known_stream_id_per_class["lifecycle"]` is older
  than the Redis retention (per ADR-057 §4.11.6.4 default
  7 d), the tail is empty and the entire class history
  comes from the Iceberg snapshot. This is expected for
  high-volume classes — the snapshot is the durable
  source, Redis is the hot cache.

The data is **assumed to be intact**. The 057's
`ice_offload_tick` writes events exactly as the EventLog
delivered them; restoration reads them as-is. There is no
adapter-level dedup and no Bloom filter — the fold's
idempotence on `stream_id` is the dedup mechanism, and it
runs in O(N) once, which is the same cost the dispatcher
already pays for the hot tier.

#### 2.2.3 SLOs for the cold tier

| Metric | Target | How |
|---|---|---|
| `cold_restore_p50_s` | **≤ 60 s** for 100 k events / agent | histogram per restore run |
| `cold_restore_p99_s` | **≤ 5 min** for 100 k events / agent | histogram per restore run |
| `iceberg_read_throughput_mb_s` | **≥ 50 MB/s** sustained | PyIceberg-internal, exposed for SRE |
| `iceberg_snapshot_staleness_h` | **≤ 24 h** at p99 | measured as `now - max(committer_timestamp)` over all snapshots |

The cold tier is **not** a low-latency path. The SLOs above
are targets for a once-per-quarter drill, not for hot
recovery. If Redis is healthy, §2.1 is used; §2.2 is
fallback only.

#### 2.2.4 Promoting a cold-tier restore to the hot tier

After a cold-tier restore, the resulting EventLog is
written to a fresh Redis cluster as a series of
`XADD knt:agent:<id>:events * field value` calls
(preserving the original `stream_id` so cursor continuity
holds). The promotion step is:

1. Boot a fresh Redis cluster (no AOF to replay; it is
   clean).
2. For each agent, replay the hybrid result of §2.2.2
   into the cluster via `XADD` with explicit IDs. **The
   `payload` field is written as the raw bytes from the
   Iceberg `payload` column** (msgpack-encoded per
   [ADR-057 §4.5.1](./ADR-057-durabilidade-dos-dados.md));
   the `schema_version` column is preserved as a separate
   field so the downstream decoder can rehydrate the
   payload using the correct payload schema. **Do not
   decode-and-re-encode the payload during promotion** —
   re-encoding risks losing forward compatibility for
   payloads that the current process cannot decode (a
   future `schema_version` may exist that this build of
   kntgraph does not recognise).
3. Run §2.1 step 2 (fold to a fresh `World`) and §2.1
   step 3 (`correlation_id` integrity) on the promoted
   cluster.
4. Switch the intake's `redis_url` to the new cluster.
5. Resume `WAIT` flows.

The promotion is the **only** way the cold tier becomes
the hot tier; the cold tier is never written to directly
by the live intake. The framework enforces this by
separating the `IcebergRestore` class (read-only on the
cold tier, this ADR) from the `ice_offload_tick` tool
(write-only on the cold tier,
[ADR-057 §4.11](./ADR-057-durabilidade-dos-dados.md)).

#### 2.2.5 `ice_drill` — DR supervisor (cold tier)

The cold tier has two agents in this design:

- **`ice_offload`** in
  [ADR-057 §4.11](./ADR-057-durabilidade-dos-dados.md) —
  the **writer**, framework-owned, production-deployed.
- **`ice_drill`** — the **DR supervisor**, this ADR.

`ice_drill` is a cyclic `Runner` that, on each tick,
checks the cold tier's restore-point freshness and runs
the hybrid restore on a sandbox replica (per §2.1 SLOs
for the drill). The drill validates:

- The `IcebergRestore.manifest()` returns a valid
  `latest_restore_point` tag.
- The hybrid restore finishes inside the SLOs of §2.2.3.
- The `correlation_id` integrity assertion holds for the
  restored `World`.

The drill is **read-only** on the cold tier (it consumes
via `IcebergRestore`); the write side of the drill is the
sandbox Redis cluster, ephemeral and torn down per tick.
A failed drill blocks the next release per §2.1.

`ice_drill` is **separate from** `ice_maintenance` and
`ice_offload`. Three supervisors, three SLOs, three SRE
owners. They share the Iceberg table but not state;
PyIceberg serialises commits via the catalog.

### 2.3 Retention policy

The retention table is the contract for §2.1 step 1 and
§2.2.2 step 1: a restore test is invalid if the artefacts
it relied on have already been garbage-collected.

**Retention is configurable, not committed.** v2 of this
ADR fixed the numbers in the body. v3 publishes them as
**defaults** and lets the deployment override them via
flags. The ADR commits the *shape* of retention (which
artefacts are retained, who owns them); the *digits* are
deployment policy.

| Artefact | Flag | Default | Owner | Notes |
|---|---|---|---|---|
| AOF rewrites | `KNT_RETENTION_AOF_DAYS` | `7` | SRE | daily rewrite at 03:00 local |
| RDB snapshots | `KNT_RETENTION_RDB_DAYS` | `30` | SRE | daily at 03:30, weekly on Sundays |
| Per-`event_class` Redis retention | `KNT_RETENTION_EVENT_CLASS_<CLASS>__TTL_S` | per [ADR-057 §4.11.6.4](./ADR-057-durabilidade-dos-dados.md) (`lifecycle=7d`, `domain=90d`, `tool=30d`) | framework | defaults from 057; overrides here are deployment policy |
| Iceberg snapshots | `KNT_RETENTION_ICEBERG_SNAPSHOTS_DAYS` | `7` | data platform | only meaningful when `KNT_ICEBERG_ENABLED=true` |
| Iceberg metadata files | `KNT_RETENTION_ICEBERG_SNAPSHOTS_DAYS` | `7` | data platform | expire with parent snapshot |
| S3 versioning | `KNT_RETENTION_S3_VERSIONING_DAYS` | `365` | data platform | only meaningful when the warehouse URI is `s3://`; protects the cold tier against object-level corruption |
| Restore-test artefacts | (not configurable) | 1 year | SRE | last successful DR test result |
| DR drill sandbox | (not configurable) | ephemeral | SRE | created and torn down per drill |

The AOF/RDB/Iceberg flags are Pydantic Settings with `gt=0`.
A value of `0` is rejected (it would mean "no retention",
which is self-contradictory for an artefact the restore
contract relies on). **Exception:**
`KNT_RETENTION_EVENT_CLASS_<CLASS>__TTL_S=0` is **valid**
— it means "never trim" for that class (see
[ADR-057 §4.11.6.4](./ADR-057-durabilidade-dos-dados.md)).
The Pydantic Settings validator permits `0` only for the
per-class Redis retention flags; other retention flags
remain strict `gt=0`. Setting
`KNT_RETENTION_ICEBERG_SNAPSHOTS_DAYS` when
`KNT_ICEBERG_ENABLED=false` is a **warning, not an
error**: the value is recorded but has no effect, so a
project that later flips the cold tier on does not have to
re-tune retention.

**Removed in v3 (vs. v2 of this ADR):**

| Artefact | Reason for removal |
|---|---|
| LocalFilesystem warehouse row | No longer a separate concept; the warehouse is just a URI under `KNT_ICEBERG_WAREHOUSE` |
| `KNT_ICEBERG_ADAPTER` flag | Replaced by the warehouse/catalog URIs; PyIceberg handles the backend |

**Removed in v2 (vs. v1 of this ADR):**

| Artefact | Reason for removal |
|---|---|
| PostgreSQL WAL archive | v2 has no relational recovery obligation |
| PostgreSQL base backups | same |

If a deployment continues to run PostgreSQL for
projections, its retention is **out of scope** for this
ADR. The deployment team owns it.

### 2.4 WAIT budget — measure before committing

`WAIT numreplicas 1 timeout_ms <T>` is on the intake's
critical path for flows classified as critical (e.g.
financial closing, ADR-057 §3.1). The cost of `WAIT` is a
**round-trip to the replica per write**, on top of the
local fsync. The `timeout_ms` parameter is **mandatory**
(see §5.1); `<T>` is set per route to the `wait_p99_ms`
target so a slow replica cannot stall the intake.

This section commits the **budget** (the measured envelope
the intake must stay inside) and the **metric series** the
framework exposes. The **policy** — who is allowed to use
`WAIT`, on which flows, and which anti-patterns are
forbidden — lives in §5.1 (contractual) and is enforced by
the framework. The framework commits to **measuring**, not
guessing, the budget:

| Metric | Target | How |
|---|---|---|
| `wait_p50_ms` | **≤ 5 ms** | histogram per intake route; sampled, not full |
| `wait_p99_ms` | **≤ 25 ms** | histogram per intake route; **also the cap on `timeout_ms`** |
| `wait_timeout_rate` | **< 0.1 %** over a 24h window | counter; per-route alert at > 0.5 % |
| `wait_cost_overhead_pct` | **≤ 15 %** of total intake latency | ratio, sampled |

If any of these targets is exceeded for 7 consecutive days,
the on-call rotation is required to **re-classify** the flow
out of "critical" (downgrade to AOF-only) or **add
replicas** to bring `WAIT` cost back inside the budget. The
default is `WAIT numreplicas 1`; flows that require RPO = 0
must use `WAIT numreplicas <N>` where N matches the replica
count the deployment committed to (no over-promising).

The metrics above are recorded by the intake itself and
emitted as `intake.wait.{route}.{quantile}` series. They are
not framework metrics (the framework is transport-agnostic);
they are deployment metrics (see §2.6).

> **Note — `WorldSnapshot` (a.k.a. `WorldCheckpoint`)
> considerations moved out.** Earlier revisions of this
> ADR included a §2.5 "Reconsidering the value of
> `WorldSnapshot`" that proposed a default-flip on
> `KNT_WORLD_CHECKPOINT_ENABLED` and a deletion of the
> legacy `CheckpointStore`. That is **not** a DR concern;
> it is a question about the cost/value of a derived cache
> relative to the EventLog. The investigation lives in
> [ADR-018 §3.6](./ADR-018-WorldIncremental-WorldSystem.md)
> (companion to §3.5 "O que NÃO muda"). The legacy
> `CheckpointStore` deletion and the `docs/checkpoints.md`
> rewrite are tracked in DEBT.md. **No DR contract
> depends on this outcome.**

### 2.5 Deployment-side observability — DR drill only

The DR drill (§2.1 / §2.2) is a **quarterly exercise**,
owned by SRE. The drill measures RTO end-to-end and is
recorded against the retention policy in §2.3. A failed
drill blocks the next release. The framework exposes the
following hooks for SRE instrumentation:

| Hook | Purpose |
|---|---|
| `ice_drill.tick_requested` | Drill tick fired |
| `ice_drill.restore_completed` | Drill run finished; payload: `{baseline_snapshot, fold_hash, duration_s}` |
| `ice_drill.drift_detected` | Fold hash diverged between drill runs |
| `intake.correlation_integrity.failed` | Bootstrap integrity check failed (see §2.1 step 3, §2.2.2 step 6) |

WAIT metrics (`intake.wait.*`) are **not** in this list;
they live in the deployment's intake observability stack
and are out of scope for the DR drill dashboard. SRE owns
both Grafana boards.

### 2.6 `knt dr` — read-only CLI for DR state

The `knt` CLI (ADR-050) grows a `dr` sub-Typer for
**read-only inspection** of DR state. SRE uses it during
incident triage, drill audits, and pre-release checks.
The sub-Typer is **strictly read-only**: no command in
`knt dr` mutates Redis, Iceberg, or any service state.
Drill triggering stays on the `ice_drill` agent (§2.2.5);
restore triggering stays in the SRE runbook.

The shape follows the existing CLI conventions (ADR-050 §1,
§2): every top-level command is a sub-Typer with
declarative Typer options. The `dr` namespace is opt-in
via the `[iceberg]` extra (same as `KntIcebergSettings`),
so projects without the cold tier do not pay for the
imports.

#### 2.6.1 Commands

```bash
# Resolved retention table (per §2.3 + ADR-057 §4.11.6).
# Prints the merged view: framework defaults, deployment
# overrides, Pydantic-validated values.
knt dr retention show
knt dr retention show --as-json    # machine-readable

# Cold-tier manifest (per §2.2.1).
# Requires KNT_ICEBERG_ENABLED=true; otherwise exits 2
# with a clear message ("cold tier disabled").
knt dr manifest
knt dr manifest --agent nf-001     # per-agent sub-cursor

# Per-class retention report (per ADR-057 §4.11.6.4).
# Lists the effective TTL per event_class for the current
# agent scope (defaults to all tracked agents).
knt dr retention show --per-class
#   lifecycle  7d     (KNT_RETENTION_EVENT_CLASS_LIFECYCLE__TTL_S=604800)
#   domain     90d    (default; override = none)
#   tool       30d    (default; override = none)

# DR drill status (last N runs, per §2.2.5).
knt dr drill status
knt dr drill status --last 10
knt dr drill status --since 30d

# WAIT budget snapshot (per §2.4).
# Reads the deployment's intake metrics endpoint
# (deployment-side; framework only formats).
knt dr wait-budget show
knt dr wait-budget show --route /intents/nf-001
```

#### 2.6.2 Why read-only

A DR CLI that mutates state has **two failure modes** the
read-only form does not:

1. **Operator foot-gun.** A typo
   (`knt dr drill run --on prod`) can drop a production
   cluster. The `ice_drill` agent already runs the drill
   on its own sandbox; the CLI never needs to trigger it.
2. **Drift between CLI and agent.** If the CLI triggers a
   drill with one set of flags and the agent uses another,
   the audit trail (the drill history) becomes
   incomprehensible. The CLI **reads** what the agent
   wrote; it does not **write** anything the agent will
   later disagree with.

If a future revision needs to trigger a drill from the CLI
(e.g. for on-demand pre-release drills), the command
**must** go through the `ice_drill` agent's intake event
(per ADR-037 correlation propagation), not directly
mutate state. Tracked as a follow-up if/when needed.

#### 2.6.3 Anti-patterns (specific to `knt dr`)

- **Any write/mutate/delete command in `knt dr`** —
  **forbidden**. The sub-Typer is strictly read-only.
  Triggering a drill, promoting cold → hot, or running
  `XTRIM` are owned by the `ice_drill` agent (§2.2.5),
  the runbook (§2.2.4), and the `ice_offload_tick` tool
  (ADR-057 §4.11), respectively.
- **Reading cold-tier state via `knt dr` when
  `KNT_ICEBERG_ENABLED=false`** — exits 2 with a clear
  message. The CLI does not silently fall back to
  "no manifest found"; that would hide a configuration
  mistake.
- **Bypassing `IcebergRestore.manifest()` with ad-hoc
  PyIceberg reads in `knt dr`** — **forbidden**. The
  audit trail lives in `manifest()`. Direct reads bypass
  it (same anti-pattern as §5.3).

#### 2.6.4 What this CLI is **not**

- **Not a replacement for the SRE runbook.** The runbook
  is the source of truth for the step-by-step restore
  procedure. The CLI is the **inspector**; the runbook
  is the **operator**.
- **Not a dashboard.** Rich output to the terminal is
  nice for triage but does not replace the Grafana board
  (§2.5). Dashboards aggregate; the CLI queries.
- **Not a framework primitive.** `knt dr` lives in
  `src/kntgraph/cli/commands/dr.py` like every other
  command; it does not introduce a new core type or
  Protocol.

## 3. Consequences

- **Operational cost.** SRE owns the Redis restore runbook,
  the hybrid restore runbook (when the cold tier is
  enabled), and the DR drill (run by the `ice_drill`
  agent per §2.2.5). Data platform owns the warehouse
  and the catalog. The **offload job** is owned by
  [ADR-057 §4.11](./ADR-057-durabilidade-dos-dados.md) —
  the 058 is a **read-only** consumer; the 058 does not
  own the offload. **DBA cost is removed** in v2;
  PostgreSQL has no recovery obligation under this ADR.
  The dev/CI cost of the cold tier, when enabled, is
  borne by the framework team (compose file with a local
  `SqlCatalog`).
- **Latency cost on the intake.** `WAIT` adds one round-trip
  per write for critical flows. The budget (§2.4) is
  measured, not assumed; if it is exceeded, flows are
  re-classified.
- **Storage cost.** Defaults in §2.3 imply ~30 days of
  AOF/RDB. With the cold tier enabled, +7 days of Iceberg
  snapshots and +365 days of S3 versioning on the
  warehouse. Both numbers are deployment-overridable via
  `KNT_RETENTION_*_DAYS`. The v1 PostgreSQL WAL/base-backup
  cost is removed. Net effect versus v1: lower recurring
  cloud bill, lower operational toil.
- **Code cost.** `IcebergRestore` wrapper
  (`src/kntgraph/infra/iceberg/restore.py`) is a thin
  PyIceberg wrapper. The **`ice_offload` agent and
  `ice_offload_tick` tool are owned by
  [ADR-057 §4.11](./ADR-057-durabilidade-dos-dados.md)** —
  this ADR does not ship a writer for the cold tier.
  The `WorldCheckpoint` cost/value question (and the
  legacy `CheckpointStore` deletion) is owned by
  [ADR-018 §3.6](./ADR-018-WorldIncremental-WorldSystem.md)
  and DEBT.md, not by this ADR.
- **Documentation cost.** When the cold tier is enabled,
  a new `docs/dr-iceberg-restore.md` runbook is required.
  `docs/dr-iceberg-restore.md` documents the **read** path
  (this ADR); the **write** path is documented in
  `docs/iceberg-offload.md` (owned by 057). The
  `docs/checkpoints.md` rewrite is owned by
  [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md).

## 4. Granularity decisions (v3)

- **`WAIT numreplicas`:** always `1` in v1. Flows that
  require RPO = 0 with N > 1 replicas must be added to the
  budget explicitly and re-measured before being marked
  critical.
- **Restore drill cadence:** **quarterly**, with the first
  drill scheduled for the calendar quarter following the
  adoption of this revision. The drill exercises **only
  the tiers the project has enabled**: §2.1 (hot) is
  always exercised. §2.2 (cold) is exercised **only when
  `KNT_ICEBERG_ENABLED=true`**. A project with the cold
  tier disabled is not required to drill it; a project
  with the cold tier enabled must drill both tiers.
- **Default `KNT_ICEBERG_ENABLED`:** **`false`.** A project
  that wants the cold tier opts in at creation time. The
  flag is **read-only at runtime**; flipping it requires
  re-bootstrapping. This is deliberate: the cold tier
  is a project-creation decision, not a knob.
- **Default `KNT_ICEBERG_WAREHOUSE`:** **owned by
  [ADR-057 §4.11](./ADR-057-durabilidade-dos-dados.md)**,
  default `file:///var/lib/kntgraph/iceberg/warehouse`
  (filesystem, dev/CI). Production deployments override
  to `s3://...` or equivalent.
- **Default `KNT_ICEBERG_CATALOG_URI`:** **owned by
  [ADR-057 §4.11](./ADR-057-durabilidade-dos-dados.md)**,
  default `sqlite:////var/lib/kntgraph/iceberg/catalog.db`
  (`SqlCatalog` over SQLite). Production deployments
  override to the appropriate `RestCatalog` /
  `GlueCatalog` / `NessieCatalog` URI.
- **Retention defaults:** see §2.3. Deployments override
  via `KNT_RETENTION_*_DAYS` flags. The framework
  publishes the defaults; it does not enforce them on
  third-party tooling (a DBA running a manual backup
  schedule is not constrained by these flags).
- **`correlation_id` integrity assertion:** always-on at
  bootstrap. Disabled only via explicit env var
  (`KNT_BOOTSTRAP_CORRELATION_CHECK=skip`) for green-field
  databases with no historical events. **In v3, this
  assertion is also run after a cold-tier restore**
  (§2.2.2 step 6) before promotion to the hot tier, when
  the cold tier is enabled.
- **Cold-tier promote step (§2.2.4):** the only path from
  the cold tier to the hot tier. Direct writes to the hot
  tier from the cold tier are forbidden (§5).
- **PostgreSQL:** **an architectural error for this use
  case** (see §1 item 6). Not a recovery target in v3.
  Deployments that run Postgres for projections do so
  under their own retention policy, out of scope for this
  ADR.

## 5. Anti-patterns

Anti-patterns are split into three categories, **not all
of which are obligations**. The distinction matters: an
obligation in framework code is enforced at runtime; a
recommendation is guidance for the deployment team.

### 5.1 Contractual — enforced in framework code

These are **forbidden in code**. The framework refuses to
boot, or refuses a specific call, when one of them is
violated.

- **`WAIT numreplicas 0`** — defeats the durability contract.
- **`WAIT numreplicas > replicas_available`** — silent
  degradation. Some Redis versions fall back to "wait for
  zero replicas" when the requested count is unreachable,
  which is indistinguishable from `WAIT 0`. The intake
  must use `WAIT numreplicas <N>` where `N` matches the
  committed replica count.
- **`WAIT` without `timeout_ms`** — blocks indefinitely on
  replica failure. Redis 7+ accepts
  `WAIT numreplicas timeout_ms`; the timeout is mandatory.
- **`WAIT` on a non-critical flow** — costs a replica
  round-trip without paying for RPO. The flow
  classification is owned by the intake config and audited
  against the budget in §2.4.
- **`appendfsync always` with `poll_interval < 1s`** —
  incompatible with the `ReactiveDispatcher`
  (`poll_interval=0.25s`); degrades tick latency to an
  unacceptable I/O cost. `everysec` is fixed; see ADR-057
  §3.1.
- **Disabling the `correlation_id` integrity check in
  production** — defeats §2.1 step 3 and §2.2.2 step 6.
- **Treating `WorldCheckpoint` as a source of truth in
  restore logic** — the EventLog is the source of truth
  (ADR-002). The checkpoint is derived; if it disagrees
  with the EventLog, the EventLog wins. The cost/value
  of the checkpoint as a cache is owned by
  [ADR-018 §3.6](./ADR-018-WorldIncremental-WorldSystem.md),
  but the **restore contract** is always: rebuild from
  EventLog, not trust the checkpoint.
- **Treating `WorldCheckpoint` as required for restore** —
  code must be cold-start-safe. A `load()` returning
  empty triggers a full re-fold (§2.1 step 2), not a
  failure. The restore contract never depends on a
  checkpoint being present.

### 5.2 Operational — recommendations for the deployment team

These are **recommendations, not enforced in code**. They
live in runbooks (`docs/dr-*.md`) and on the SRE wiki.
Violating them degrades the recovery story; the framework
will not catch it.

- **Restoring from a backup that has been garbage-collected**
  — restore is invalid if the artefact's age exceeds the
  retention window in §2.3.
- **Running DR drill on production** — drills run on a
  sandbox replica with the same configuration.
- **Skipping the DR drill because "we have backups"** — the
  drill is the verification; backups without verification
  are a liability.
- **AOF rewrite or RDB snapshot during peak traffic** —
  AOF rewrites are scheduled at 03:00 local and RDB
  snapshots at 03:30 (see §2.3); running them during
  business hours stalls the Redis primary on I/O and
  breaks the `everysec` latency budget.
- **AOF rewrite or RDB snapshot during peak traffic** —
  AOF rewrites are scheduled at 03:00 local and RDB
  snapshots at 03:30 (see §2.3); running them during
  business hours stalls the Redis primary on I/O and
  breaks the `everysec` latency budget.

### 5.3 Contextual — only apply when the feature is enabled

These are **mandatory within their scope, but the scope
itself is opt-in**. A project that has not enabled the
relevant feature is not bound by them.

**Cold-tier (Iceberg) anti-patterns — only when
`KNT_ICEBERG_ENABLED=true`:**

- **Writing the cold tier from this ADR** — the 058
  ships `IcebergRestore` (read-only) and `ice_drill`
  (read-only). The writer is the
  [`ice_offload_tick`](./ADR-057-durabilidade-dos-dados.md#4111-motivation-and-scope)
  tool, owned by
  [ADR-057 §4.11](./ADR-057-durabilidade-dos-dados.md).
  Any code path in the 058 that mutates the cold tier
  is **forbidden**.
- **Promoting the cold tier to the hot tier without
  running §2.1 step 2 and step 3** — every promotion
  must re-fold the EventLog and assert `correlation_id`
  integrity before the intake accepts new intents.
- **Reading the cold tier with a catalog that is not in
  the resolved `manifest()`** — restores must go
  through `IcebergRestore` so the manifest is recorded;
  ad-hoc `pyiceberg` reads bypass the audit trail.
- **Running a cold-tier restore on the live Redis
  cluster** — the cold-tier restore writes to a fresh
  cluster (§2.2.4 step 1); running it against the live
  cluster risks double-writes.
- **Tagging a snapshot with a name not derived from
  `make_restore_point_tag`** — the tag-naming contract
  is owned by §2.2.1.5 of this ADR. A diverging tag is
  invisible to `IcebergRestore.manifest()` and breaks
  the cold-tier restore.

**PostgreSQL anti-patterns — only when PostgreSQL is in
the deployment:**

- **Treating PostgreSQL as a recovery target under this
  ADR** — PostgreSQL is a projection sink without a
  recovery obligation under this ADR. Any deployment
  that wants Postgres-recoverable projections must own
  that contract locally and document it outside this
  ADR. The framework does not enforce this; it merely
  does not contract for it. **Rev. scope note:** with
  [ADR-057 §2.2 removed in rev. 4](./ADR-057-durabilidade-dos-dados.md),
  the framework no longer publishes a Postgres durability
  stance at all. The conditional anti-pattern here is
  kept only as a guardrail against re-introducing
  Postgres recovery obligations under this ADR.

### 5.4 Removed anti-patterns (vs. v2, v3)

The following anti-patterns were **deleted in v3 or v4**
as either obsolete (the feature was removed) or wrongly
classified (was an obligation, is actually a
recommendation):

| v2 anti-pattern | v4 status | Why |
|---|---|---|
| Configuring `KNT_ICEBERG_ADAPTER=local` in production | **Removed in v3** | The adapter concept is gone; the warehouse URI is configuration, and the SRE is responsible for pointing it at `s3://` in prod |
| `IcebergRestoreAdapter` Protocol contract | **Removed in v3** | Replaced by the simpler `IcebergRestore` class |
| Treating `KNT_ICEBERG_ADAPTER=local` as a guardrail | **Removed in v3** | Same as above |
| Offload orchestration as a separate concern of the 058 | **Removed in v4** | The offload is owned by ADR-057 §4.11; the 058 is read-only |
| Warehouse / catalog config defaults in §2.2.1 | **Removed in v4** | Owned by ADR-057 §4.11; the 058 references it |
| Schema and partition in §2.2.2 | **Removed in v4** | Owned by ADR-057 §4.5; the 058 references it |

## 6. Open questions

1. **DR drill scheduling tool.** Manual calendar entry or
   automated cron? Manual for v1; revisit if drills are
   missed. **In v4, the drill is run by the `ice_drill`
   agent (§2.2.5)**; the calendar entry becomes an audit
   artefact, not a schedule.
2. **Replica topology for `WAIT numreplicas 1` deployments.**
   Single replica is enough for `WAIT 1`, but the
   availability story degrades to a single point of failure
   during replica rebuild. Multi-AZ is the recommended
   baseline; this is a deployment concern, not a framework
   decision.
3. **Cross-region restore.** In scope for a future revision
   if business expansion requires it. v4 is single-region.
   **In a cross-region world, both a cross-region Redis
   replica and a cross-region warehouse bucket are
   required**; the promotion step (§2.2.4) needs to be
   re-validated in that topology.
4. **Encryption-at-rest for Iceberg and Redis.** Mentioned
   in the ADR-057 review but not yet committed. v4 ships
   without; a future revision commits. **When the
   warehouse URI is `s3://`, SSE-S3 is the expected
   default; SSE-KMS is a deployment choice.**
5. **Catalog class for production.** `SqlCatalog` is the
   dev/CI default. Production will use `RestCatalog`,
   `GlueCatalog`, or `NessieCatalog`; the choice is per
    deployment and is recorded in `manifest()`. The DR
    drill's first production run commits a default for
    that deployment.
6. **Retention CLI shape.** **Resolved in §2.6** — the
   `knt dr` sub-Typer is committed as **read-only**.
   `knt dr retention show [--per-class]` prints the
   resolved retention table; no `--set` flag (mutations
   stay in the deployment's config-management layer).
   Triggers (drill run, cold→hot promote, XTRIM) stay on
   the agent / runbook / tool side.
7. **Hybrid restore algorithm with per-`event_class`
   cursors.** **Resolved in §2.2.2** — the algorithm
   now consumes `last_known_stream_id_per_class:
   dict[str, str]`, reads `tail_per_class`, and emits
   `max_stream_id_per_class` for the next cursor. The
   v5 migration is **in this revision**, not deferred.
8. **Per-class retention integration in `manifest()`.**
   **Resolved in §2.2.1** — `IcebergRestore.manifest()`
   now exposes `last_stream_id_per_class: dict[str, str]`
   per agent. The bootstrap cursor derivation in §2.2.2
   step 7 reads from this field.
9. **Tag contract coupling with ADR-057.** This ADR
   exports `make_restore_point_tag(snapshot_id)`; the
   057 `ice_offload_tick` tool imports it. If the 058
   changes the tag format (prefix, suffix, or naming
   scheme), every deployed 057 must be updated. Is this
   coupling acceptable, or should the function live in
   a third "iceberg-contracts" module owned by neither
   ADR? Proposed for a future minor; v4 ships the
   direct import.
