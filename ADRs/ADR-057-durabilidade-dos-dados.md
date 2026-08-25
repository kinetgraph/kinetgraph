<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-057: Data Storage — Operational and Historical

- **Status:** Implemented (rev. 4 — historical-events-only)
- **Date:** 2026-08-14 (rev. 4: 2026-08-15)
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-058](./ADR-058-disaster-recovery.md) — Disaster Recovery (sibling; restore procedure, WAIT budget, retention of backups)
  - [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md) — World and incremental fold
  - [ADR-005](./ADR-005-Checkpoints-Idempotency.md) — checkpoints and idempotency
  - [ADR-002](./ADR-002-Replay-Puro.md) — canonical replay

> **Scope.** This ADR covers the **historical event
> management** of kntgraph end-to-end: the operational hot
> path (Redis Streams as the source of truth), projections in PostgreSQL and the historical events and  (Apache Iceberg
> as the offload of the EventLog, with maintenance of the
> Iceberg table). It commits the **data shape** and the
> **durability stance** for each path.
>
> **Revision 4 — historical-only scope.** Earlier revisions
> of this ADR conflated two concerns: historical event
> management and disaster recovery. **v4 splits the
> concerns along the axis of "what happens to events over
> time"**:
>
> - This ADR (057) commits: the shape of the EventLog, the
>   schema and partition of the Iceberg table, the offload
>   job (`ice_offload`), and the table-maintenance agent
>   (`ice_maintenance`).
> - [ADR-058](./ADR-058-disaster-recovery.md) commits: the
>   restore procedure, the `ice_drill` agent, the hybrid
>   restore algorithm, the source-selection logic that
>   decides where a re-fold reads from (Redis vs. Iceberg),
>   the drill cadence, and the WAIT budget.
>
> The 057 does **not** decide what a "cold start" is, when
> the cold tier should serve a re-fold, or what a "restore
> drill" is. Those are 058 concerns. What 057 does decide is
> that the data layer must be **tolerant to empty caches**
> and that the EventLog source for any re-fold is whatever
> the deployment exposes — the choice between sources is
> the 058's.

## 1. Context

The Soldi Hub backoffice runs on kntgraph and handles
autonomous decisions, reconciliation, monthly closing, and
third-party integrations. The architectural principle is
**zero event loss**: the loss of any event breaks the
event-sourced ECS reconstruction and the ability to audit
agent actions through `correlation_id`.

The data layer is split into two paths along the **time
axis**, not the **concern axis**:

- **Operational hot path.** `Redis Streams` is the source of
  truth (the Event Store). State that does not fit in the
  in-process memory managers (`Profile` / `Continuity` /
  `Session` and `Domain`) is kept as derived caches adjacent to the
  EventLog, never as a parallel source of truth.
- **Historical cold path.** `Apache Iceberg` local volume or on S3 (Parquet)
  holds the offloaded history of the EventLog. The
  framework owns two agents on this path: **`ice_offload`**
  (commits new data from Redis Streams to Iceberg, then
  trims Redis) and **`ice_maintenance`** (non-destructive
  compaction and snapshot expiry).

Disaster recovery — restore procedure, hybrid source
selection (Redis vs. Iceberg), drill cadence, WAIT budget,
retention of backups — lives in
[ADR-058](./ADR-058-disaster-recovery.md). This ADR does
not commit the restore runbook, the drill cadence, or the
source-selection logic; it commits the **shape** of the
data and the **durability stance** for each path.

## 2. Decisions

The data layer commits three sub-decisions: one for the
operational hot path, one for application-level guarantees,
and one for the analytical cold path.

### 2.1. Redis Streams durability (operational hot path)

`Redis Streams` is the single source of truth for events.
Redis is configured for durability first, latency second.

- **AOF:** `appendonly yes`.
- **Fsync policy (`appendfsync`):** `everysec` is the
  default; the worst-case loss on hardware crash is one
  second. The alternative `always` is rejected for v1: it
  degrades the `ReactiveDispatcher`'s `poll_interval=0.25s`
  to unacceptable I/O cost (see §3.1).
- **RDB snapshots:** regular snapshots (e.g. hourly or
  every 10 k changes) enabled alongside AOF to accelerate
  restart and recovery (mixed mode, default since Redis 4.0).
- **Replica + `WAIT`:** critical flows (`WAIT numreplicas 1`
  on intake) achieve RPO = 0; non-critical flows accept
  RPO ≤ 1 s. The `WAIT` policy and budget are owned by
  [ADR-058 §2.4](./ADR-058-disaster-recovery.md).
- **Storage volume (cloud):** when the Redis primary runs
  on a managed compute with a **replicated volume**
  (EBS gp3 in a Multi-AZ attach topology, EFS Standard
  / One Zone with replication, Azure Premium SSD v2 with
  zone-redundancy, GCE Persistent Disk regional, Filestore
  Enterprise with replication), the AOF file benefits
  from **cross-AZ durability at no extra Redis-level
  cost**. The volume is the silent replica; AOF
  `appendfsync everysec` writes are durably replicated
  before the next `WAIT ack` confirms the intake. This
  does **not** change the `WAIT numreplicas 1` policy —
  the Redis replica is a separate guarantee (RPO = 0 for
  critical flows) — but it **lowers the cost of the
  `ttl=None` retention setting** in §4.11.6.4 because
  losing the Redis primary no longer means losing the
  AOF.
- **Storage volume (bare metal / single-host dev):** the
  volume is local and a single point of failure. The
  AOF `everysec` policy exposes up to 1 s of loss on
  host crash; the Iceberg cold tier (per §2.3) is the
  recovery target. The §4.11.6 per-class retention
  defaults assume this is the **fallback case**; in cloud
  deployments they can be relaxed (see §4.11.6.4).

### 2.2. Application-level guarantees (at-least-once + idempotency)

Infrastructure durability is complemented by code-level
guarantees:

- **Outbox pattern.** When an integration mutates
  PostgreSQL **before** publishing to kntgraph, the publish
  to `Redis Streams` **must** go through an Outbox inside
  the relational transaction to guarantee atomicity and
  avoid the dual-write problem. The Outbox applies only to
  backend integrations (e.g. NestJS) → kntgraph via Redis
  Stream; it does **not** apply inside the kntgraph core
  ECS, where events are born in the Event Log and there is
  no second transaction to coordinate.
- **Tool idempotency.** Every `Tool` receives an
  `idempotency_key`; retry supervisors re-pin `correlation_id`
  from the `ToolCallRequest` (per the correlation-propagation
  skill). Duplicate delivery during infrastructure failover
  is a no-op.

### 2.3. Apache Iceberg on S3 (historical cold path)

Redis is not suited for unbounded storage (RAM cost and
limits). The cold path uses `Apache Iceberg` on S3 (Parquet)
for long-horizon history of the EventLog:

- **Iceberg tables.** Events exported from `Redis Streams`
  land in Iceberg tables (Parquet). Iceberg gives schema
  evolution, time-travel, and columnar compression.
- **Offload process.** The framework owns the **`ice_offload`**
  agent (§4.10): a cyclic `Runner` that emits a ticket per
  tick, executed by the `WorkerManager` as the
  `ice_offload_tick` `@tool_worker`. The tool reads
  `XRANGE` from Redis, appends to Iceberg, and emits
  `XTRIM MINID < last_committed_id>` to prune the Redis
  stream.
- **Historical queries.** Trino / Athena / DuckDB queries
  against the Iceberg table read the full agent history
  (`correlation_id`, `stream_id`) without touching the
  operational Redis load. This is an **analytics** use
  case; the same table is also a **restore source** under
  the [ADR-058 §2.2](./ADR-058-disaster-recovery.md)
  contract, but the 058 is the owner of the restore
  procedure.

## 3. Consequences

- **Latency cost.** AOF plus Multi-AZ replication adds
  per-write latency to the kntgraph hot path. The trade is
  required for the durability stance; the WAIT budget and
  its acceptance thresholds are owned by
  [ADR-058 §2.4](./ADR-058-disaster-recovery.md).
- **Operational cost.** Multi-AZ Redis replicas and the
  Iceberg warehouse raise recurring cloud spend (compute
  and storage); trade-off accepted for the durability
  stance.
- **Operational complexity.** Monitoring and DR drills are
  required; the drill cadence and runbooks are owned by
  [ADR-058 §2.1](./ADR-058-disaster-recovery.md).

### 3.1. Granularity decisions (v1)

- **`appendfsync`:** fixed at `everysec`. The alternative
  `always` is rejected for v1 because it is incompatible
  with the `ReactiveDispatcher` (`poll_interval=0.25s`)
  and degrades tick latency to an unacceptable I/O cost.
- **RPO per flow:** granular, not global. Critical flows
  (e.g. financial closing) operate at RPO = 0 via
  synchronous `WAIT` on intake plus re-pinning of
  `correlation_id` from the `ToolCallRequest` in retry.
  Autocomplete flows accept RPO in minutes.
- **`WAIT` on non-critical flows** — **forbidden** as a
  policy rule owned by
  [ADR-058 §2.4](./ADR-058-disaster-recovery.md) and §5.1.
  Adds a replica round-trip to every write without paying
  for RPO. The flow classification is owned by the intake
  config and audited against the WAIT budget.
- **Outbox pattern:** applies **only** to backend
  integrations (e.g. NestJS) → kntgraph via Redis Stream.
  It does **not** apply inside the kntgraph core ECS:
  events are born in the Event Log and there is no second
  transaction to coordinate.
- **Offload tool:** a native Python consumer in a cyclic
  `Runner`, registered as a `@tool_worker` in the
  `WorkerManager` (§4.10). Spark Structured Streaming is
  rejected for v1 — the offload is small enough that a
  single Python process with PyArrow + PyIceberg covers
  the throughput, and reusing the `WorkerManager` keeps
  the observability and idempotency story uniform with
  the rest of the framework. **Not** Kafka Connect (Kafka
  is not in the current pipeline; ADR-036 / ADR-054).
- **Local disk in production:** anti-pattern. Loses
  durability against host failure and breaks HA multi-replica
  (`SqlCatalog` is single-writer). Local disk is allowed
  only in dev / CI / staging as Iceberg warehouse.

## 4. Iceberg Maintenance Layer (v1) — `ice_maintenance` agent

### 4.1. Motivation and scope

The **primary source of history is `Redis Streams`** (the
kntgraph framework contract). `IncrementalWorldStore` keeps
progressive `World` snapshots per agent in Redis pickle
(`knt:world:{agent_id}`, TTL 7 d). Redis expires entries by
an externally-configured retention policy (`XADD MAXLEN`
or `XTRIM MINID` coordinated with the offload job).

`ice_maintenance` is **not** responsible for:

- Long-horizon durability of history (the Redis contract).
- `XTRIM` of the Event Log (coordinated by the offload
  job, outside the framework).
- Destructive cleanup of Iceberg data (anti-pattern;
  see §4.7).

`ice_maintenance` is responsible **only** for:

- Non-destructive compaction of the Iceberg table
  (`rewrite_data_files`).
- Housekeeping of Iceberg manifest lists and metadata
  (`expire_snapshots`).

This is the only genuinely required Tool: it keeps the
analytical Iceberg projection performant without destroying
evidence.

### 4.2. Layered architecture

1. **Config (declarative):** `IcebergMaintenanceConfig`
   with `StorageLocation` (URI), `CatalogSpec`
   (`sql` / `glue` / `rest` / `unity`), `TableFormat`
   (`iceberg` / `delta`), `target_file_size_mb`,
   `retention_days`, `compaction_interval_seconds`.
2. **Adapter (Protocol):** `PyIcebergCheckpointStorage`
   implements `IcebergCheckpointStorage` (Protocol with
   `rewrite_data_files`, `expire_snapshots`,
   `current_snapshot_bytes`). It does **not** inherit from
   `WorldCheckpointStorage` — World checkpoints remain
   Redis pickle.
3. **Tools (WorkerManager):** **one** idempotent tool —
   `ice_compact` — with `idempotency_key` derived from
   `correlation_id + "compact"`. `ice_housekeeping`
   (`expire_snapshots`) may be added later if needed;
   `ice_xtrim` and destructive `ice_cleanup` are
   **forbidden**.
4. **Agent (bounded context):** `ice_maintenance` with
   **one** cyclic supervisor agent (`Runner`,
   `tick_interval=compaction_interval_seconds`). There is
   no reactive tick or analyst — the only action is to
   trigger compaction periodically.
5. **`IncrementalWorldStore`:** unchanged; remains Redis
   pickle. Memory pressure is relieved by Redis TTL, not
   by Iceberg.

### 4.3. Protocol and multi-backend

```python
@runtime_checkable
class IcebergCheckpointStorage(Protocol):
    """Non-destructive maintenance operations on Iceberg/Delta tables."""
    async def rewrite_data_files(self) -> Result[RewriteSummary, StorageError]: ...
    async def expire_snapshots(self, older_than_days: int) -> Result[int, StorageError]: ...
    async def current_snapshot_bytes(self) -> Result[int, StorageError]: ...
```

Supported URIs: `file://` (dev / CI), `s3://` / `s3a://`
(AWS production), `abfss://` (Azure Data Lake), `gs://`
(GCS), `oss://` (Aliyun). `azure://` is accepted as a
synonym for `abfss://`.

### 4.4. Pipeline shape

```
Runner (tick_interval = compaction_interval_seconds)
  │  correlation_id: X (generated by the supervisor)
  ▼
ice_maintenance.compaction_requested      {correlation_id: X, target_file_size_mb: 128}
  ▼
tool.ice_compact.requested                {correlation_id: X, attempt: 1}
  ▼  (WorkerManager)
tool.ice_compact.completed                {correlation_id: X}
```

No elaborate retry: compaction is **opportunistic**.
Failure → log + next tick retries. There is no
`correlation_id` recovery via Iceberg; that contract is the
Redis one.

### 4.5. Schema (framework default)

The framework ships a **versioned default schema** for the
EventLog Iceberg table. Projects can customise it (per
§4.5 table row in §4.6) but the default is the contract
that the **`ice_offload` tool writes against** and the
**[ADR-058 §2.2 `IcebergRestore`](./ADR-058-disaster-recovery.md)
reads from**. Both tools reference the same schema; any
schema evolution is a coordinated framework release.

#### 4.5.1. `ICE_EVENTS_SCHEMA_V1`

```python
import pyarrow as pa

ICE_EVENTS_SCHEMA_V1 = pa.schema(
    [
        pa.field("stream_id",       pa.large_string(), nullable=False),
        pa.field("agent_id",        pa.string(),       nullable=False),
        pa.field("correlation_id",  pa.string(),       nullable=False),
        pa.field("event_type",      pa.string(),       nullable=False),
        pa.field("payload",         pa.binary(),       nullable=False),
        pa.field("schema_version",  pa.int32(),        nullable=False),
        pa.field("committed_at_ms", pa.timestamp("ms"),nullable=False),
    ],
    metadata={
        "format_version": "1",
        "framework": "kntgraph",
        "module": "iceberg.events",
    },
)
```

Column rationale:

- **`stream_id`** — Redis Stream ID. The cursor for the
  hybrid restore algorithm. Preserved as-is in the Iceberg
  row so the cold-tier restore can resume the EventLog
  without re-numbering.
- **`agent_id`** — Tenant-scoped agent identifier. The
  natural key for restore per-agent and the sort key for
  Parquet row-group locality (§4.5.2).
- **`correlation_id`** — Join key for audit and DR
  verification (`correlation_id` integrity assertion per
  [ADR-058 §2.1 step 3](./ADR-058-disaster-recovery.md)).
- **`event_type`** — Discriminator (`ToolCallRequest`,
  `ToolCallCompletion`, `NoOpCompletion`, system events,
  etc.). Used as a Parquet row-group filter for analytical
  queries.
- **`payload`** — Binary encoding of the event body
  (msgpack). Schemaless at the Iceberg level; the
  `schema_version` column carries the payload schema
  version.

#### 4.5.1.1. Schema discipline — why `pickle` in one place, `binary` in another

Serialization is split by reader, not by convention. The
`World` snapshot uses **`pickle`** because the consumer is
the same Python process and the type graph (ECS
components, `@dataclass(frozen=True, slots=True)`,
cross-references, native `datetime` / `Decimal`) must
survive intact — see [ADR-018 §3.x](./ADR-018-WorldIncremental-WorldSystem.md).
Pickle preserves object identity, cycles, and native
types; msgpack + a JSON-Schema equivalent would force a
hand-rolled codec and lose the type.

The EventLog payload column on Iceberg is **`pa.binary()`**
because the cold-tier consumer is **schema-versioned and
opaque**: it decodes the bytes using `schema_version` at
read time, never trusting a specific Python shape across
the retention window. The Parquet row carries a primitive
that any reader (PyIceberg, Trino, DuckDB, Athena) can
read without binding to the Python type system.

The msgpack staging buffer in the `ice_offload_tick`
checkpoint (§4.11.4 Step 1) is an **internal staging
buffer**, not a contract: it exists only to absorb a
crash between the `XRANGE` read and the `table.append`,
and is discarded once the snapshot is committed. Its
format follows the same binary-opaque discipline as
`payload`, but it never reaches Iceberg.
- **`schema_version`** — Payload schema version (not the
  Iceberg schema version). Lets the cold-tier restore
  decode payloads that may have evolved over the
  retention window.
- **`committed_at_ms`** — Offload commit time. The partition
  key (§4.5.2). Differs from the original event timestamp;
  this is the time the offload job wrote the row.

The `partition_spec` and `sort_order` are framework
defaults; a project that needs different partitioning
overrides them at Iceberg-table creation time and documents
the override in the project's runbook.

#### 4.5.2. `PARTITION_SPEC_V1` and `SORT_ORDER_V1`

```python
from pyiceberg.partitioning import PartitionSpec, PartitionField
from pyiceberg.table.sorting import SortOrder, SortField

PARTITION_SPEC_V1 = PartitionSpec(
    PartitionField(
        source_id=7,  # committed_at_ms
        field_id=1000,
        transform="day",
        name="committed_day",
    ),
)

SORT_ORDER_V1 = SortOrder(
    SortField(source_id=2, transform="identity"),  # agent_id
)
```

The partition is `day(committed_at_ms)`. This gives
one partition per day of offload, aligned with the
`snapshot_retention_days` default in §4.9.5. The sort
order is `identity(agent_id)`, which gives Parquet row-group
locality for per-agent reads (the common case for both
analytics and restore).

#### 4.5.3. Tag contract (delegated)

The **`ice_offload` tool creates a tag on the Iceberg
snapshot after each commit**, naming it
`restore_point_snap_{snapshot_id}`. The naming convention
is **owned by
[ADR-058 §2.2.1.5](./ADR-058-disaster-recovery.md)**, not
by this ADR; the `ice_offload` tool imports the
`make_restore_point_tag(snapshot_id)` function from the
058's module. The 058 is the contract owner; the 057 is
the executor.

### 4.6. Architectural position — framework vs project (ice_maintenance)

| Layer | Framework (`kntgraph.features`) | Project |
|---|---|---|
| `IcebergMaintenanceConfig` | yes | parameterisation |
| `StorageLocation`, `CatalogSpec`, `TableFormat` | yes | no |
| Agent auto-wire | yes | no |
| `default_ice_maintenance_policy()` (CapabilityPolicy) | yes | imports |
| Adapter `PyIcebergCheckpointStorage` | yes | no |
| Delta alternative adapter | yes (install extra) | opt-in |
| Iceberg table schema (`ICE_EVENTS_SCHEMA_V1`, `PARTITION_SPEC_V1`, `SORT_ORDER_V1`) | yes (default) | can customise |
| Tag naming function `make_restore_point_tag` | **imported from ADR-058** | no |
| Events `ice_maintenance.*`, `ice_offload.*` | **no** (project namespace) | yes |
| Component `IceMaintenanceState` | **no** (domain) | yes |

### 4.7. CLI opt-in

```bash
uv run knt init my-project --with-iceberg-maintenance
uv run knt init my-project --with-iceberg-offload

# Runtime via env vars (12-factor)
KNT_ICE_MAINTENANCE__LOCATION_URI=s3://soldi-data-lake/iceberg
KNT_ICE_MAINTENANCE__CATALOG=glue
KNT_ICE_MAINTENANCE__TABLE_FORMAT=iceberg
KNT_ICE_MAINTENANCE__TARGET_FILE_SIZE_MB=128
KNT_ICE_MAINTENANCE__COMPACTION_INTERVAL_SECONDS=3600
KNT_ICE_MAINTENANCE__SNAPSHOT_RETENTION_DAYS=7

KNT_ICEBERG__WAREHOUSE=s3://soldi-data-lake/iceberg
KNT_ICEBERG__CATALOG_URI=glue://soldi-data-lake
KNT_ICEBERG__SNAPSHOT_INTERVAL_S=3600
KNT_ICEBERG__OFFLOAD_BATCH=10000
KNT_ICEBERG__MAX_CONCURRENCY=1
```

`KntIceMaintenanceSettings(BaseSettings)` and
`KntIcebergOffloadSettings(BaseSettings)` resolve via
Pydantic Settings. The `pyiceberg` dependency is an install
extra (`pip install kntgraph[iceberg]`). `delta-rs` lives
in a separate extra (`pip install kntgraph[iceberg-delta]`).

### 4.8. Constraints and anti-patterns

- **Tool as an async function** — forbidden; the Tool is a
  class decorated with `@tool_worker`.
- **Tool without `idempotency_key`** — the decorator
  refuses it; reinforced in review.
- **System with S3 I/O** — forbidden; the Tool performs
  `rewrite_data_files`.
- **`correlation_middleware.current()` in retry** — the
  supervisor re-pins from the `ToolCallRequest` (per the
  correlation-propagation skill).
- **`SqlCatalog` in multi-replica production** —
  `SqlCatalog` is single-writer; it breaks with ≥ 2 replicas
  of the `ReactiveDispatcher`. Production uses
  `GlueCatalog` + S3 or `RestCatalog` (Polaris / Lakekeeper).
- **Synchronous S3 PUT on the tick path** — compaction is
  cyclic (`Runner`), not on the `ReactiveDispatcher`.
- **`drop_orphans` / destructive `ice_cleanup`** —
  **forbidden**. Destroys audit evidence. Only
  non-destructive operations (`rewrite_data_files`,
  `expire_snapshots`) are allowed.
- **`ice_xtrim`** — **forbidden in `ice_maintenance`**.
  `XTRIM` of the Event Log is the responsibility of the
  `ice_offload` tool (§4.10), not the maintenance agent.
  The split is: `ice_maintenance` is **read-only on the
  Event Log** (it reads snapshots, files, manifests); the
  `ice_offload_tick` tool is the **only** writer that
  trims the Redis stream, and it does so only after the
  Iceberg commit is confirmed.
- **`expire_snapshots(older_than_days <= 0)`** —
  **forbidden**. Effectively deletes every snapshot, which
  is the destructive-cleanup anti-pattern above. The
  lower bound is `older_than_days >= snapshot_retention_days`.
- **Local disk in production** — breaks ADR-057 §1
  (durability against host failure).
- **Outbox pattern inside the kntgraph core ECS** —
  **forbidden**. Events are born in the Event Log and there
  is no second transaction to coordinate. The Outbox
  applies only to backend integrations (NestJS → kntgraph
  via Redis Stream) that mutate PostgreSQL **before**
  publishing.
- **`XTRIM MINID < last_committed_id`** before the Iceberg
  commit is confirmed — **forbidden** in any path that
  affects the Event Log. Trimming before the offload job
  has confirmed the Iceberg commit is irreversible event
  loss. Coordination is the offload job's contract; see
  §4.8.
- **Treating `WorldCheckpoint` as a contract** —
  **forbidden**. The checkpoint is a derived cache; code
  must be tolerant to an empty cache. A `load()` returning
  empty triggers a full re-fold from the available Event
  Log source, not a failure. The choice of Event Log
  source (Redis vs. Iceberg-restored) when the cache is
  empty is owned by
  [ADR-058 §2.2](./ADR-058-disaster-recovery.md).

### 4.9. Redis retention policy (complement to §2.1)

Redis memory pressure is relieved by **configurable TTL**,
not by external backup:

- **`XADD ... MAXLEN ~ N`**: approximate cap, performant,
  non-blocking for writers.
- **`XTRIM MINID < last_committed_id[event_class]`**:
  executed **only** by the `ice_offload_tick` tool
  (§4.11), inside its `invoke`, after the Iceberg commit
  is confirmed and the tag is in place. Monotonic per
  `event_class`, safe by construction. The per-class
  cursor contract is in §4.11.6.
- **`knt:world:{agent_id}` TTL**: default 7 d; can be
  shortened to hours for idle agents (config
  `INCREMENTAL_WORLD_CHECKPOINT_TTL_S`).
- `ice_maintenance` **does not** interfere with these
  policies. Its sole responsibility is to keep the Iceberg
  table performant.

### 4.10. Mandatory mitigations

1. **Small-file compaction:** `target_file_size_mb=128` and
   `rewrite_data_files` on the configured `tick_interval`
   (default 1 h), **off the `ReactiveDispatcher` tick**
   (the `Runner` owns it; `pump_once` does not block on S3
   PUT). The S3 PUT itself is **synchronous inside the
   `Runner`'s tick** — that is fine; the constraint is only
   that it must not run on the dispatcher's per-agent
   critical path.
2. **Sliding TTL in Redis:** `IncrementalWorldStore` TTL is
   configurable. When the TTL fires, the `World` cache is
   empty and the dispatcher must re-fold. The re-fold
   reads from the **available Event Log source** — in the
   common case that is the Redis Streams hot tier; in a
   deployment with `KNT_ICEBERG_ENABLED=true` whose Redis
   stream has been trimmed past the cursor, the cold tier
   can supply the historical baseline. The source-selection
   logic is owned by
   [ADR-058 §2.2](./ADR-058-disaster-recovery.md); this
   ADR only commits that the data layer is **tolerant to
   either path**.
3. **Rewrite serialisation:** `max_concurrent_rewrites=1`
   to avoid saturating S3 with parallel PUTs.
4. **Versioning + Object Lock on S3:** for financial flows,
   the bucket has `versioning` and `object-lock` (compliance
   mode) — immutability against privileged deletion.
5. **Auditable time-travel:** Iceberg snapshots with
   `snapshot_retention_days=7` (default) for historical
   queries and time-travel; the Parquet state is preserved
   per the S3 bucket policy. The retention default is
   overridable per deployment via
   `KNT_RETENTION_ICEBERG_SNAPSHOTS_DAYS`.

### 4.11. `ice_offload` — EventLog offload orchestration

The `ice_maintenance` agent (§4.2-§4.4) keeps the Iceberg
table **performant**. The `ice_offload` agent keeps the
**Redis→Iceberg pipeline flowing**. They are two
**independent supervisors**, two **independent tools**,
two **independent SLOs** — and they share the Iceberg
table without sharing state (PyIceberg serialises commits
via the catalog).

#### 4.11.1. Motivation and scope

`ice_offload` is responsible **only** for:

- Reading the EventLog from Redis Streams (per agent
  stream `knt:agent:<id>:events`).
- Appending batches to the Iceberg table.
- Creating a **tag** on each new snapshot, naming it
  per the [ADR-058 §2.2.1.5](./ADR-058-disaster-recovery.md)
  contract.
- Trimming the Redis stream per agent
  (`XTRIM MINID < last_committed_id_per_agent>`) **only
  after** the Iceberg commit is confirmed and the tag is
  in place.

`ice_offload` is **not** responsible for:

- Compaction or snapshot expiry — that is
  `ice_maintenance` (§4.2).
- Restore procedure, drill cadence, or source selection
  for re-fold — that is
  [ADR-058 §2.2](./ADR-058-disaster-recovery.md).
- The Iceberg schema or partition spec — that is
  §4.5 of this ADR.

#### 4.11.2. Layered architecture (parallel to §4.2)

1. **Config (declarative):** `KntIcebergOffloadSettings`
   with `WAREHOUSE` (URI), `CATALOG_URI`, `TABLE_NAME`
   (default `knt_events`), `SNAPSHOT_INTERVAL_S`
   (default `3600`), `OFFLOAD_BATCH` (default `10000`),
   `MAX_CONCURRENCY` (default `1`).
2. **No separate adapter.** The `ice_offload_tick` tool
   uses **PyIceberg directly** (no `Protocol` in
   between), because the operations it performs
   (`table.append`, `manage_snapshots().create_tag`,
   `XRANGE`, `XTRIM`) are PyIceberg / Redis primitives
   with no business logic to wrap. Compare with
   `ice_maintenance` (§4.3), which needs a Protocol
   because `rewrite_data_files` and `expire_snapshots`
   are framed as a maintenance contract.
3. **Tools (WorkerManager):** **one** idempotent tool —
   `ice_offload_tick` — with `idempotency_key` derived
   from the supervisor's `correlation_id` and
   `max_concurrency=1` (the offload sequence is serial by
   design: the next tick depends on the cursor of the
   previous one).
4. **Agent (bounded context):** `ice_offload` with
   **one** cyclic supervisor agent (`Runner`,
   `tick_interval=snapshot_interval_s`). It emits **one**
   `ice_offload.tick_requested` event per tick, with the
   last committed `stream_id` in the payload so the
   tool does not need to re-read the cursor.
5. **`IncrementalWorldStore`:** unchanged. Memory pressure
   is relieved by Redis TTL plus the offload's
   `XTRIM`, not by Iceberg.

#### 4.11.3. Pipeline shape (parallel to §4.4)

```
Runner (tick_interval = KNT_ICEBERG__SNAPSHOT_INTERVAL_S)
  │  correlation_id: X (generated by the supervisor)
  │  last_committed_stream_id: Y (read from Redis cursor)
  ▼
ice_offload.tick_requested                {correlation_id: X, last_committed: Y}
  ▼
tool.ice_offload_tick.requested           {correlation_id: X, attempt: 1}
  ▼  (WorkerManager, max_concurrency=1)
tool.ice_offload_tick.completed           {correlation_id: X, snapshot_id: Z, events_committed: N}
```

The four steps **inside** the `ice_offload_tick.invoke`
(see §4.11.4) are not separate tickets. They are internal
to one tool invocation, protected by a per-`correlation_id`
checkpoint in Redis. This keeps the EventLog contract
atomic: a reader of the EventLog sees **one** ticket per
offload tick, with the full sequence represented as
`tool.ice_offload_tick.completed` carrying
`snapshot_id` and `events_committed`.

#### 4.11.4. Internal sequence of `ice_offload_tick.invoke`

The tool performs four steps in order, with a checkpoint
written to Redis after each step. If the process crashes
mid-sequence, the `WorkerManager` retry re-enters the
`invoke` with the same `idempotency_key` (= `correlation_id`)
and the checkpoint logic resumes from the last completed
step.

```python
@tool_worker(name="ice_offload_tick", max_concurrency=1, retries=3)
class IceOffloadTick:
    async def invoke(self, idempotency_key: str) -> Result[OffloadResult, ToolError]:
        # idempotency_key == correlation_id from the supervisor
        cursor_key = f"knt:iceberg:offload:{idempotency_key}"

        if redis.get(cursor_key + ":completed"):
            return Ok(OffloadResult.already_done())

        # Step 1 — consume (idempotent on (cursor, batch_size))
        if not redis.get(cursor_key + ":consumed"):
            events = redis.xrange(
                "knt:agent:*:events",
                min=self._last_committed_stream_id,
                max="+",
                count=KNT_ICEBERG__OFFLOAD_BATCH,
            )
            if not events:
                return Ok(OffloadResult.empty())
            redis.set(cursor_key + ":consumed", "1")
            redis.set(cursor_key + ":events", msgpack.packb(events))
        else:
            events = msgpack.unpackb(redis.get(cursor_key + ":events"))

        # Step 2 — write (PyIceberg append is atomic)
        if not redis.get(cursor_key + ":snapshot"):
            arrow_table = build_arrow_table(events)  # per ICE_EVENTS_SCHEMA_V1
            table.append(arrow_table)
            snapshot_id = table.current_snapshot().snapshot_id
            redis.set(cursor_key + ":snapshot", str(snapshot_id))
        else:
            snapshot_id = int(redis.get(cursor_key + ":snapshot"))

        # Step 3 — tag (idempotent by name; name owned by ADR-058)
        if not redis.get(cursor_key + ":tagged"):
            tag_name = make_restore_point_tag(snapshot_id)  # imported from ADR-058
            table.manage_snapshots().create_tag(
                snapshot_id=snapshot_id,
                tag=tag_name,
            ).commit()
            redis.set(cursor_key + ":tagged", "1")

        # Step 4 — xtrim (only after steps 1-3 succeeded)
        for agent_id, last_stream_id in per_agent_max_stream_id(events).items():
            redis.xtrim(
                f"knt:agent:{agent_id}:events",
                minid=last_stream_id,
            )
        redis.set(cursor_key + ":completed", "1")

        return Ok(OffloadResult(
            snapshot_id=snapshot_id,
            events_committed=len(events),
        ))
```

**Why `XTRIM` is the last step.** The Redis stream is
trimmed only after the Iceberg commit and the tag are
both in place. If the process dies between steps, the
checkpoint absorbs the retry; the `XTRIM` is the
**irreversible** effect and is gated by the
`:completed` flag.

**Why per-agent `XTRIM` and not a global `XTRIM`.** Each
agent stream is trimmed only to the `stream_id` that
**that agent's events reached** in this batch. A
slow-producing agent whose `XRANGE` returned no events
is left untouched. This preserves the monotonicity of
the per-agent stream.

#### 4.11.5. Anti-patterns (specific to `ice_offload`)

- **Trimming before the Iceberg commit** — **forbidden**.
  The `:completed` flag is the gate; nothing trims
  without it.
- **Creating the tag before the snapshot** — **forbidden**.
  PyIceberg's `create_tag(snapshot_id=...)` raises if the
  snapshot does not exist. The order in §4.11.4 is
  load-bearing.
- **Concurrent `ice_offload_tick` invocations** — the
  `max_concurrency=1` is enforced by the `WorkerManager`
  and is non-negotiable. A second concurrent invocation
  would race on the `current_snapshot_id` read.
- **Bypassing the checkpoint** — **forbidden**. The
  checkpoint is the resumability contract; a "fast path"
  that skips it loses the retry semantics.
- **Using a tag name not derived from `make_restore_point_tag`**
  — **forbidden**. The naming convention is owned by
  [ADR-058 §2.2.1.5](./ADR-058-disaster-recovery.md);
  any divergence breaks the cold-tier restore.
- **Mixing `ice_offload` and `ice_maintenance` in the
  same supervisor agent** — **forbidden**. They are
  two concerns, two SLOs, two SRE owners.

#### 4.11.6. Per-`event_class` retention — `XTRIM` policy

##### 4.11.6.1. Motivation

The stream `knt:agent:{id}:events` carries two orthogonal
life-cycles in the same Redis Stream, discriminated by
`event_class`:

| `event_class` | Origin | Meaning | Reference |
|---|---|---|---|
| `"lifecycle"` | Framework | Operational phase (`agent.spawned`, `agent.idle`, …) | [ADR-003 §2.1](./ADR-003-Ciclo-Dual.md) |
| `"domain"` | Application | Domain facts materialised into ECS Components (Domain Memory) | [ADR-003 §2.2](./ADR-003-Ciclo-Dual.md), [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) |
| `"tool"` | Framework | Tool-call lifecycle (`tool.*.requested`, `tool.*.completed`) | [ADR-047](./ADR-047-Tool-Adapter-Pattern.md) |
| other | — | Project-defined | project namespace |

A single per-agent cursor (`last_committed_id`) lumps these
together: trimming below the cursor drops the entire
window, including the **Domain Facts** that ECS Components
depend on for rehydration. The current scheme (a single
`XTRIM MINID < last_committed_id`) cannot answer both
"keep recent lifecycle noise" and "preserve Domain Facts"
without one starving the other.

##### 4.11.6.2. Decision — per-class sub-cursors on a single stream

**Keep one stream per agent** (per
[ADR-003 §2.4](./ADR-003-Ciclo-Dual.md): "mesma stream, dois
eixos" — atomicity, single replay, single cursor is the
core invariant). **Refine the cursor** into a dict indexed
by `event_class`:

```python
last_committed_id: dict[str, str]  # per event_class, per agent
# Example for agent "nf-001":
# {
#   "lifecycle": "1715000000000-3",
#   "domain":    "1714900000000-7",   # older — domain is preserved longer
#   "tool":      "1715000000000-2",
# }
```

`last_committed_id["all"]` is **not** a thing. There is no
"global" cursor; only the per-class ones. The
`IncrementalWorldStore` carries the same dict as part of
its metadata (one extra field in the pickle; no schema
change).

##### 4.11.6.3. Invariants (the audit guarantee)

For every agent `a`, every `event_class` `C`, and every
`stream_id` `< last_committed_id[a][C]`:

1. **Iceberg commitment:** a row exists in the Iceberg
   table with that `stream_id`, in the snapshot tagged
   `restore_point_snap_{snapshot_id}` (per ADR-058 §2.2.1.5).
2. **Replay completeness:** `World.fold` from
   `(XRANGE - +)` on the surviving stream, plus
   `IcebergRestore.read_events` for `stream_id <
   last_committed_id[a][C]`, reconstructs the same World.

This is the same invariant as before, refined per class.
**No event is trimmed below a class cursor unless it is
durably in Iceberg.** That is the audit guarantee.

##### 4.11.6.4. Default retention classes

The framework ships three default policies. Deployments
override via `KNT_RETENTION_EVENT_CLASS_<CLASS>__TTL_S`
(env var, 12-factor) or `IcebergOffloadConfig.retention`
(declarative).

| `event_class` | Default retention (Redis) | Rationale |
|---|---|---|
| `"lifecycle"` | **7 d** | `OperationalPhase` is derived from the **last** lifecycle event; older events are noise. 7 d covers a weekend + AOF retention. |
| `"domain"` | **90 d** | Domain Facts materialise into ECS Components. The fold needs the full history for rehydration and audit (`correlation_id` walk). 90 d is the framework default; projects that need permanent domain history keep `"domain"` untrimmed (`ttl=None`) and rely on Iceberg as the long-horizon store. |
| `"tool"` | **30 d** | Tool calls are needed for replay and the `ToolCallRequest` TTL system (ADR-045), but a 30 d window covers any reasonable investigation horizon. |
| other | **30 d** (default) | Project-defined; sane default. |

`ttl=None` (or `ttl=0`) means **never trim** — the event
is kept in Redis as long as the AOF/RDB retention in
[ADR-058 §2.3](./ADR-058-disaster-recovery.md) allows, and
the cold tier is the long-horizon store. This is the
recommended setting for `"domain"` in projects where the
domain fact is also a legal/business record (financial
closing, regulatory filings).

**Cloud note (replicated volumes).** On a managed compute
with a replicated volume (per §2.1), the cost of
`ttl=None` for `"domain"` drops substantially: the AOF
file is itself durable across AZ failure, so keeping
Domain Facts in Redis no longer relies on the
single-host AOF. Cloud deployments commonly run with
`KNT_RETENTION_EVENT_CLASS_DOMAIN__TTL_S=0` (never trim)
and rely on Iceberg for cross-region durability, with
Redis as the hot cache for fast fold rehydration. The
cold tier is still the **legal** source of truth — the
volume replication is a **performance and availability**
multiplier, not an audit substitute.

##### 4.11.6.5. Examples

**Example 1 — agent with mixed workload.**

Agent `nf-001` processes a fiscal document. Its stream:

```
knt:agents:nf-001:events
  e1: 1714900000000-0  class=lifecycle  agent.spawned
  e2: 1714900001000-1  class=domain     document.received     (payload: {xml: "..."})
  e3: 1714900002000-2  class=tool       tool.parser.requested
  e4: 1714900003000-3  class=tool       tool.parser.completed
  e5: 1714900004000-4  class=domain     document.validated    (payload: {cnpj: "..."})
  e6: 1714900005000-5  class=lifecycle  agent.idle
```

After the first `ice_offload_tick` reads the full window
and commits snapshot `S1`:

```python
last_committed_id["nf-001"] = {
    "lifecycle": "1714900005000-5",
    "domain":    "1714900004000-4",
    "tool":      "1714900003000-3",
}
```

The tool then **issues three `XTRIM`s** — one per class —
each bounded by the class's TTL window, applied via
`MINID`:

```python
# XTRIM MINID cutoff is computed per-class from the TTL,
# not from the cursor directly. The cursor is the lower
# bound: we only trim events strictly below it.
trim_cutoff_lifecycle = now - 7d    # TTL(lifecycle) = 7d
trim_cutoff_domain    = now - 90d   # TTL(domain) = 90d
trim_cutoff_tool      = now - 30d   # TTL(tool) = 30d

# Per-class XTRIM (pseudocode)
for event_class, cutoff in (
    ("lifecycle", trim_cutoff_lifecycle),
    ("domain",    trim_cutoff_domain),
    ("tool",      trim_cutoff_tool),
):
    redis.xtrim(
        "knt:agents:nf-001:events",
        minid=stream_id_for_timestamp(cutoff),
    )
```

In a small workload where `now ≈ 1714900006`, the
`minid_for_timestamp(now - 90d)` is far older than any
event in the stream, so `e1..e6` are all preserved. The
cursor and the TTL cooperate: cursor gates **durability**
(Iceberg committed), TTL gates **retention** (Redis
memory). They are independent concerns.

**Example 2 — long-running agent, "domain" class set to
`ttl=None`.**

Agent `audit-007` accumulates 1 year of domain events
(`classification.applied`, `tax.regime.loaded`). The
project sets `KNT_RETENTION_EVENT_CLASS_DOMAIN__TTL_S=0`
(never trim). After 1 year, the stream has ~2 M events:

```python
last_committed_id["audit-007"] = {
    "lifecycle": "1714900005000-5",
    "domain":    "<never-trimmed>",
    "tool":      "1714900003000-3",
}
```

The `"domain"` key carries a sentinel `<never-trimmed>`
(not a `stream_id`); the `ice_offload_tick` skips the
`XTRIM` for that class. The `"lifecycle"` and `"tool"`
cursors continue to advance normally; their XTRIMs run as
usual.

The Iceberg table is the long-horizon store: a
`IcebergRestore.read_events(agent_id="audit-007",
stream_id__lt="1714900005000-5", event_class="domain")`
returns the full history. The Redis stream is the hot
cache; if it is lost, the cold tier rebuilds it via
`XADD` with explicit IDs (per ADR-058 §2.2.4 promotion
step).

**Example 3 — `WorldCheckpoint` integration.**

The `IncrementalWorldStore` stores the per-class cursor
alongside the World:

```python
# world_checkpoint.py (pseudocode)
checkpoint = WorldCheckpoint(
    world=pickled_world,
    last_stream_id=last_overall_stream_id,        # for the cold-start fallback
    last_stream_id_per_class={                     # NEW: per §4.11.6
        "lifecycle": "1714900005000-5",
        "domain":    "1714900004000-4",
        "tool":      "1714900003000-3",
    },
)
```

On restart:

- `World.fold(XRANGE - +)` covers the hot tier (Redis).
- `IcebergRestore.read_events(stream_id__lt=
  last_stream_id_per_class[C])` covers the cold tier
  for each class `C` where the Redis cursor is older
  than the cold-tier snapshot.
- The two halves are merged by `stream_id` (idempotent,
  per ADR-002, ADR-005).

The fold's idempotence on `stream_id` is unchanged; the
sub-cursor is metadata, not a new ordering. There is no
risk of double-application: a `stream_id` is unique
across the stream, regardless of `event_class`.

##### 4.11.6.6. Cost

- **Read cost:** unchanged. `ice_offload_tick` reads a
  single `XRANGE` window per tick.
- **Write cost:** the `ice_offload_tick` Step 4 changes
  from one `XTRIM` to **N `XTRIM`s** (one per class with
  events in the batch). N is bounded by the number of
  `event_class` values seen in the batch — typically
  ≤ 4. The total bytes trimmed are unchanged; the
  command count goes up by a constant factor.
- **Storage cost:** unchanged. Same events go to Iceberg;
  the Redis retention just becomes more granular.
  **Cloud caveat:** on a managed compute with a
  replicated volume (per §2.1), the storage cost of
  long Redis retention is **dominated by provisioned IOPS
  and replicated-GB pricing**, not by RAM. EBS gp3 charges
  per GB-month and per provisioned IOPS; EFS Standard
  charges per GB-class. A project that lifts `"domain"`
  from 90 d to `ttl=None` on a 2 GB stream pays an extra
  ~10× in volume storage (over 90 d) but **negligible**
  extra Redis CPU/RAM — Redis reads the AOF as a memory-
  mapped file after warm-up. On bare metal / single-host
  dev, the same lift is bounded by host disk size and is
  usually rejected at design review (see §4.8).
- **`WorldCheckpoint` cost:** one extra dict field per
  checkpoint; O(classes) memory and O(1) pickle cost.
- **Audit cost:** **lower**. The invariant in §4.11.6.3 is
  strictly stronger than the previous "all events below
  cursor are in Iceberg": it now states the same per
  class, which is what auditors actually need to verify
  Domain Fact preservation.

##### 4.11.6.7. Anti-patterns (specific to §4.11.6)

- **Trimming below a per-class cursor before the Iceberg
  commit for that class** — **forbidden**. The cursor is
  the durability gate, per §4.11.6.3.
- **Using a global `last_committed_id` (not per-class)**
  — **forbidden**. Forces a single retention policy on
  all classes; breaks the ADR-003 dual-cycle guarantee.
- **`ttl=0` (never trim) on `"lifecycle"` or `"tool"`** —
  allowed but discouraged: lifecycle/tool events age out
  of usefulness quickly, and the long-horizon store is
  Iceberg. Keeping them in Redis wastes RAM. The
  framework warns at boot when `ttl=0` is set on a
  high-volume class. **Cloud caveat:** on a managed
  compute with a replicated volume, the RAM pressure
  becomes a **storage-class** pressure (EBS gp3
  provisioned IOPS, EFS throughput class) rather than a
  host-RAM pressure, so the warning is downgraded from
  "waste" to "review the cost model". Projects with
  legitimate replay-window needs (audit, regulatory)
  may set `ttl=0` on `"tool"` and pay the storage bill;
  the framework does not refuse.
- **Per-event retention override** — **forbidden**. Mixing
  policies inside a class breaks the per-class invariant.
  The granularity is the `event_class`, not the event.
- **`WorldCheckpoint` with a global `last_stream_id` and
  per-class sub-cursors that disagree** — **forbidden**.
  The checkpoint's `last_overall_stream_id` must be
  `max(last_stream_id_per_class.values())`. A divergence
  is a checkpoint corruption; the bootstrap refuses to
  load and SRE is paged.

## 5. Open questions (for discussion)

1. ~~**RTO and RPO (Recovery Time / Point Objective):**~~
   **Resolved in §3.1** — `everysec` fixed; RPO
   granularised per flow (critical = 0 via `WAIT` plus
   correlation re-pin).
2. ~~**Lifecycle (offload to Iceberg):**~~ **Resolved in
   §3.1, §4.9, §4.11 and §4.11.6** — Redis expires via
   `XTRIM MINID` coordinated by the `ice_offload_tick`
   tool (§4.11), with **per-`event_class` sub-cursors**
   (§4.11.6) so Domain Facts (`event_class="domain"`) are
   preserved independently of lifecycle/tool churn. The
   `ice_maintenance` agent operates **only** non-destructive
   compaction (`rewrite_data_files`, `expire_snapshots`)
   with `compaction_interval_seconds=3600`. The two
   agents are independent supervisors in independent
   Runners; they share the Iceberg table but not state.
3. **Infrastructure environment:** managed Redis (AWS
   ElastiCache Multi-AZ / Azure Cache Premium / GCP
   Memorystore) or self-hosted? If self-hosted, confirm
   Sentinel plus synchronous replicas. **Decision pending
   the infra team.**
4. **Production Iceberg catalog:** Glue (AWS lock-in) vs.
   Polaris / Lakekeeper self-hosted (operational lock-in)?
   **Decision pending the platform team.**
5. **Local volume on Windows:** document the WSL2 / Linux
   container pre-condition; native Windows support is out
   of scope.
6. ~~**External offload job (responsibility):**~~ **Resolved
   in §4.11** — the `ice_offload` agent is framework-owned
   and deployed alongside the rest of the `ice_*` agents.
   SRE owns the operational health; data platform owns
   the warehouse and the catalog. No external job.
