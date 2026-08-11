<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-056: Postgres as a single-backend replacement for Redis in single-tenant deployments — keep Redis as the default; offer Postgres + Debezium CDC as an opt-in alternative

**Status:** Proposed
**Date:** 2026-08-11
**Related:** [ADR-019](./ADR-019-Redis-Adapter-Typing.md) (the Protocol-driven adapter convention that makes this migration tractable), [ADR-005](./ADR-005-Checkpoints-Idempotency.md) (the 3-phase idempotency claim), [ADR-036](./ADR-036-Tool-Worker-Pattern.md) (the Tool Worker Pattern that relies on Redis Consumer Groups), [ADR-049](./ADR-049-Zero-Token-Architecture.md) (ZTA — the post-script that already lists Postgres as an explicit shipping target), [ADR-054](./ADR-054-WorkerManager-Transport-Evaluation.md) (the parallel "decision not to migrate" ADR this one mirrors in structure)

> **Scope.** This ADR records a **decision to add, not to
> replace**. Redis remains the default backend. Postgres
> becomes a second supported backend for the three storage
> surfaces where the migration is clean (EventLog, the three
> short-memory tiers, DLQ, API keys) and where the operator
> needs SQL introspection, multi-key transactions, or
> change-data-capture for downstream consumers. The Tool
> Worker Pattern stays on Redis in V1; the migration of the
> CG-based queue is a separate, riskier ADR (§4).

## 1. Context

The kntgraph framework's storage layer is, today,
**unconditionally Redis**. The relationship is documented
across the codebase:

- `pyproject.toml` — `redis>=5.0` in `[project].dependencies`
  (not in an extra, not optional).
- `README.md:9` — "A pure, event-sourced agent framework
  over **Redis Streams**."
- `docs/quickstart.md` — the first install step is
  `docker run -d -p 6379:6379 --name kntgraph-redis redis:latest`.
- `ADRs/ADR-049-Zero-Token-Architecture.md` §6 — ZTA already
  lists Postgres as a target for the Solution tier's
  post-script work.

A proposal was raised to make Postgres + SQLAlchemy +
Debezium CDC the **sole** storage backend, eliminating
Redis from the deployment entirely. The motivation was
threefold:

1. **Operational consolidation** — many operators already
   run Postgres for OLTP workloads; one database instead
   of two reduces the topology.
2. **Change-Data-Capture** — Debezium turning the
   `event_log` table into a Kafka topic gives the
   framework a first-class CDC stream without a custom
   outbox protocol. Redis has no native CDC.
3. **SQL introspection** — `pg_stat_*`, ad-hoc queries,
   `psql` REPL, BI tools. Redis introspection is `INFO`
   + `SCAN`, with no schema or relational semantics.

The evaluation asked: does Postgres, behind a SQLAlchemy
2.0 async adapter and a Debezium outbox table, replicate
the **functional** contract of every Redis surface the
framework depends on, at a cost that justifies the swap?
The answer (per the analysis in §2) is **yes for the
seven K/V + Stream + Hash surfaces, with caveats; and
deferred for the Tool Worker Pattern (Consumer Groups)**.

This ADR records the **proposed split**: keep Redis as the
default, add Postgres as a second supported backend for
the seven K/V + Stream + Hash surfaces, and gate the
Tool Worker Pattern behind a follow-up ADR once the
first seven are battle-tested.

### 1.1 What the framework already has

The 2026-Q2 refactor recorded in **ADR-019** isolated
`redis.asyncio` behind typed Protocols:

| Protocol | File | Surface |
|---|---|---|
| `RedisLike` | `infra/redis/_client.py:78` | 22-method opaque client (the boundary) |
| `PipelineLike` | `infra/redis/_client.py:35` | 6-method pipeline subset |
| `EventLogStorage` | `infra/redis/_event_log/_adapter.py:69` | `append`, `read`, `read_with_cursor`, `read_latest`, `stream_len`, `list_agents`, `delete` |
| `ShortMemoryStorage` | `infra/redis/_memory/_adapter.py:83` | `get_record`, `put_record`, `delete_record`, `iter_keys` |
| `CheckpointStorage` | `infra/redis/_checkpoint/_adapter.py:54` | `load`, `save`, `load_all`, `clear`, `clear_all` |
| `WorldCheckpointStorage` | `infra/redis/_world_checkpoint/_adapter.py:38` | `load`, `save (ttl)`, `discard` |
| `DLQStorage` | `infra/redis/_dlq/_adapter.py:28` | 11 methods covering stream + 3 hash indexes |
| `APIKeyStorage` | `infra/redis/_auth/_adapter.py:54` | `lookup`, `store`, `delete` |

**18 source files** consume the Protocols (not the
`redis.asyncio.Redis` client). The single direct
`redis.asyncio` import in production code is
`infra/redis/_pool.py:50-51` — the legitimate
boundary, with two `TYPE_CHECKING` exceptions for
exception types.

The architectural groundwork is in place. What is
missing is the second set of implementations
(`Postgres*Storage`) and the configuration surface
that selects between them.

### 1.2 What Postgres changes for the framework

Seven surfaces are migration candidates in V1:

| Surface | Redis primitive | Postgres equivalent |
|---|---|---|
| EventLog | Stream (`XADD MAXLEN`, `XRANGE`, `XREVRANGE`, `XINFO`) | `event_log` table partitioned by `tick` + retention job |
| EventLog idempotency | String KV (`GET`, `SET NX`, `SET`) | `event_id_index` table with `UNIQUE(event_id)` + `INSERT ... ON CONFLICT DO NOTHING` |
| DLQ stream | Stream (`XADD MAXLEN`, `XRANGE`, `XINFO`, `XDEL`) | `dlq_events` table (partitioned) |
| DLQ indexes (3) | Hash (`HGET`, `HSET`, `HSETNX`, `HINCRBY`, `HSCAN_ITER`) | 3 normalised tables + indices |
| Session cache | String KV with `EX` | `session` table with `expires_at` |
| Profile cache | Hash (via pipeline) | `profile` table with `JSONB` payload + `expires_at` |
| Continuity cache | Hash with sliding `EX` | `continuity` table with `JSONB` payload + sliding `expires_at` |
| Reactive checkpoint | Hash (one field per agent) | `reactive_checkpoint` table (one row per agent) |
| World checkpoint | String KV with `EX` (pickled) | `world_checkpoint` table with `BYTEA` payload + `expires_at` |
| API keys | String KV | `api_keys` table (UNIQUE on digest) |
| Solution store | Hash (one per tool) | `solution` table with `(tool_name, params_fingerprint)` PK |

**Out of scope for V1** (deferred to a follow-up ADR):

| Surface | Why deferred |
|---|---|
| **Tool Worker queue** (`knt:tools:<name>:queue`) | Depends on **Consumer Groups**: `XREADGROUP ... BLOCK`, `XACK`, `XAUTOCLAIM`, `XPENDING`. Postgres equivalent is `SELECT ... FOR UPDATE SKIP LOCKED` + a `claimed_by/claimed_at` column. The PEL model and the auto-reclaim semantics are different enough to deserve their own ADR and a dedicated migration. The proposal in §4.1 is to land this in a follow-up. |
| **Tool worker idempotency** (`knt:tool:<id>:idempotency`) | Same reason — coupled to the worker queue. |

## 2. Decision

### 2.1 The split

| Concern | Redis (default, unchanged) | Postgres + Debezium (new, opt-in) |
|---|---|---|
| Default for new deployments | ✅ | — |
| Default for tests (`pytest -m unit`) | ✅ (`fakeredis`) | — |
| Default for tests (`pytest -m integration`) | ✅ (real Redis) | ✅ if `KNT_STORAGE_BACKEND=postgres` (testcontainers) |
| EventLog | ✅ | ✅ |
| DLQ | ✅ | ✅ |
| Session / Profile / Continuity | ✅ | ✅ |
| Reactive checkpoint | ✅ | ✅ |
| World checkpoint | ✅ | ✅ |
| API keys | ✅ | ✅ |
| Solution store | ✅ | ✅ |
| **Tool Worker queue** | ✅ | **deferred** (follow-up ADR) |
| **Tool idempotency** | ✅ | **deferred** (follow-up ADR) |
| CDC to downstream (Kafka) | not supported | ✅ via Debezium connector (opt-in) |

The decision is **additive**: nothing existing changes.
Operators that don't set `KNT_STORAGE_BACKEND=postgres`
keep the current behaviour, byte for byte.

### 2.2 New package: `infra/postgres/`

A new sub-package, mirror of `infra/redis/`:

```
infra/postgres/
├── __init__.py            # public API: factories, errors, re-exports
├── _client.py             # Protocol PostgresLike (the boundary)
├── _pool.py               # async SQLAlchemy 2.0 engine + session factory
├── _codec.py              # bytes / dict / JSONB codec (mirrors _codec)
├── _errors.py             # PostgresAdapterError, IdempotencyConflict, etc.
├── _factory.py            # create_event_log_storage(settings) -> EventLogStorage
│                          #   (chooses Redis or Postgres impl from settings)
└── _event_log/            # sub-adapter (mirrors infra/redis/_event_log/)
    ├── __init__.py
    ├── _adapter.py        # PostgresEventLogAdapter
    ├── _keys.py           # table/column name constants
    ├── _idempotency.py    # 3-phase claim via UNIQUE + ON CONFLICT
    ├── _partitioning.py   # tick-based partitioning + retention job
    └── _cdc.py            # Debezium outbox table + helpers
```

The remaining sub-adapters (`_memory/`, `_dlq/`,
`_checkpoint/`, `_world_checkpoint/`, `_auth/`,
`_solution/`) follow the same shape, one PR per
sub-adapter. Each PR lands a Protocol implementation +
unit tests + an integration test using
`testcontainers[postgresql]`.

### 2.3 The `PostgresLike` Protocol

A new Protocol in `infra/postgres/_client.py`, **not**
mirroring every method of `RedisLike` — that would
be a thin shim and would not buy anything. Instead,
`PostgresLike` exposes the higher-level operations
the new adapters need:

```python
@runtime_checkable
class PostgresLike(Protocol):
    """Opaque async SQLAlchemy view. The framework never
    imports ``sqlalchemy`` outside this package (mirrors
    the ``redis.asyncio`` boundary in ``infra/redis``)."""

    @property
    def engine(self) -> AsyncEngine: ...

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Yields a session inside ``BEGIN`` / ``COMMIT`` /
        ``ROLLBACK``. Adapters call this for multi-statement
        writes (the 3-phase idempotency claim, the
        checkpoint write-through)."""

    async def execute(
        self, stmt: Executable, *,
        params: Mapping[str, Any] | None = None,
    ) -> CursorResult[Any]: ...

    async def fetch_one(
        self, stmt: Executable, *,
        params: Mapping[str, Any] | None = None,
    ) -> Row[Any] | None: ...

    async def fetch_all(
        self, stmt: Executable, *,
        params: Mapping[str, Any] | None = None,
    ) -> Sequence[Row[Any]]: ...
```

The `RedisLike` Protocol is **not** deprecated. The
`EventLog` orchestrator consumes `EventLogStorage`
(a higher-level Protocol); the new
`PostgresEventLogAdapter` implements that Protocol.
Nothing in `EventLog` (or the rest of the framework)
sees `PostgresLike` or `RedisLike` directly.

### 2.4 Idempotency claim in Postgres

The current 3-phase claim in
`infra/redis/_event_log/_idempotency.py` uses `SET NX`
for atomicity. The Postgres equivalent is two statements
inside one transaction:

```sql
-- _check_phase: read existing
SELECT stream_id, status FROM event_id_index
  WHERE event_id = :event_id
  FOR UPDATE;          -- row lock for the duration of the tx

-- _claim_phase: insert the placeholder (or hit a conflict)
INSERT INTO event_id_index (event_id, status, stream_id, claimed_at)
  VALUES (:event_id, 'placeholder', NULL, now())
  ON CONFLICT (event_id) DO NOTHING
  RETURNING event_id;

-- _finalize_phase: write the stream_id and the event row
INSERT INTO event_log (stream_id, agent_id, event_id, payload, ts)
  VALUES (:stream_id, :agent_id, :event_id, :payload, now());
UPDATE event_id_index
  SET status = 'finalized', stream_id = :stream_id
  WHERE event_id = :event_id;
```

The `claimed_at` timestamp is the equivalent of Redis's
PLACEHOLDER recovery: a sweeper can later
`UPDATE ... SET status = 'abandoned' WHERE status = 'placeholder' AND claimed_at < now() - interval '5 minutes'`
to release claims orphaned by a crashed writer. The
interval is a new setting (§2.6).

This pattern is **functionally equivalent** to the Redis
3-phase claim, with one extra column (`claimed_at`) and
one extra sweeper job. The complexity is comparable.

### 2.5 The Debezium outbox table

A new table `event_outbox` is written in the **same
transaction** as the `event_log` insert:

```sql
CREATE TABLE event_outbox (
  id            BIGSERIAL PRIMARY KEY,
  event_id      UUID NOT NULL UNIQUE,
  agent_id      TEXT NOT NULL,
  payload       JSONB NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at  TIMESTAMPTZ
);
CREATE INDEX event_outbox_unpublished_idx
  ON event_outbox (created_at)
  WHERE published_at IS NULL;
```

The Debezium Postgres connector is configured with
`table.include.list=public.event_outbox` and the
default logical decoding plugin (`pgoutput`).
The `event_outbox` table is the **only** table the
connector reads; the connector publishes to a Kafka
topic named `kntgraph.events.<agent_id>` (configurable
via the connector's `transforms.route.topic.regex`).

The outbox table is **appended in the same tx** as
`event_log`, so consumers see exactly-once-ish
delivery: if the application tx commits, the row
becomes visible to Debezium; if the tx aborts, it
doesn't. The framework does **not** run a sweeper to
clean up the outbox — Debezium's `tombstones.on.delete`
handles deletion via the connector's retention config.

This is the path that unlocks the use cases Redis
does not serve natively: Kafka-fed analytics, the
FalkorDB projection getting its events from a
topic instead of polling, audit replay, multi-tenant
materialised views.

### 2.6 Configuration

A new `StorageSettingsMixin` (in
`infra/config/_storage.py`, env prefix `KNT_`)
adds three fields:

```python
class StorageSettingsMixin(BaseSettings):
    storage_backend: Literal["redis", "postgres"] = Field(
        default="redis",
    )
    postgres_url: str = Field(
        default="postgresql+asyncpg://localhost:5432/kntgraph",
    )
    postgres_pool_size: int = Field(default=10)
    idempotency_claim_timeout_s: int = Field(default=300)
```

`Settings` adds this mixin to its existing 12. Existing
deployments default to `redis`; no change.

`RedisSettingsMixin` is **not** removed. The
`redis_url` field is still required when
`storage_backend=redis`. The migration path is
opt-in: a deployment that wants Postgres sets
`KNT_STORAGE_BACKEND=postgres` and `KNT_POSTGRES_URL=...`
and the factories in `infra/redis/_factory.py` (now
renamed `infra/storage/_factory.py` — see §4.2) read
the setting and return the right implementation.

The `_factory.py` functions keep their existing
signatures: `(settings: Settings | None = None, *,
client: RedisLike | None = None) -> EventLogStorage`.
The new entry point is `(settings, *, client=None,
pg_client=None) -> EventLogStorage`. The current
`create_*_storage` callers are unchanged; the switch
is automatic from the `Settings.storage_backend` value.

### 2.7 Dependency change (non-breaking)

`pyproject.toml` moves `redis>=5.0` from
`[project].dependencies` to a new extra:

```toml
[project]
dependencies = [
    # ... (unchanged)
    # "redis" removed from here
]

[project.optional-dependencies]
redis = ["redis>=5.0"]
postgres = [
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.30",
    "alembic>=1.13",   # for the partitioning migrations
]
```

The default install (`pip install kntgraph`) becomes
**storage-less**: the framework's `__init__.py` reads
`KNT_STORAGE_BACKEND` and raises a clear
`StorageBackendNotConfigured` error at first I/O if
no backend extra is installed. This is the explicit
breaking change; the migration guide in §3.6 documents
it.

The two extras compose: `pip install "kntgraph[redis]"`
keeps today's behaviour; `pip install
"kntgraph[postgres]"` enables the new backend;
`pip install "kntgraph[redis,postgres]"` enables both
(useful for staged migrations).

### 2.8 Debezium is **not** a framework dependency

Debezium is an **operator concern**, not a framework
one. The framework ships:

- the `event_outbox` table and the write-side helper
  (`infra/postgres/_event_log/_cdc.py`), and
- a `docs/operations/debezium.md` page with the
  connector config template.

The Debezium **connector itself** runs in the
operator's Kafka Connect cluster. The framework does
not start it, does not depend on it, and does not
fail if it is absent. An operator that wants
CDC runs the connector; one that doesn't
(Redis-only deployment) doesn't install it.

This is the explicit boundary: the framework's
contract ends at "the row is in `event_outbox`". The
delivery to Kafka is Debezium's contract.

### 2.9 Tests

| Layer | Backend | Test approach |
|---|---|---|
| Unit | both | `RedisLike` mocks (existing) + new `PostgresLike` mocks |
| Unit | both | Protocol conformance suites for `EventLogStorage`, `ShortMemoryStorage`, etc. — run against both backends |
| Integration | Redis | Real `localhost:6379` (existing, unchanged) |
| Integration | Postgres | `testcontainers[postgresql]` (new) — one container per session, fresh schema per test |
| CDC | Postgres | A **manual** smoke test in `tests/manual/cdc_smoke.py` that requires a running Debezium + Kafka; **not** part of the automated gate (gated on `RUN_CDC_SMOKE=1`) |

The conformance test approach is the load-bearing
piece: the same `EventLogStorage` Protocol is
exercised by the same `EventLog` orchestrator against
two implementations, and a single test file drives
both. This is how we keep the two backends from
drifting.

## 3. Consequences

### 3.1 What the operator gains

- **One database instead of two** for single-tenant
  deployments that already run Postgres.
- **Native CDC** for downstream consumers (Kafka,
  ClickHouse, the FalkorDB projection, BI tools)
  via the Debezium outbox — a path the Redis
  deployment does not have.
- **SQL introspection** of the EventLog, the three
  memory tiers, the DLQ, the API keys, and the
  checkpoints. Ad-hoc queries for incident
  response, schema migrations via Alembic, BI
  integrations via the same `psql`/`pgwire` they
  already use.
- **Multi-key transactions** in MVCC semantics. The
  current `MULTI/EXEC` pattern (used by the
  Profile / Continuity write-through) is replaced
  by `BEGIN` / `COMMIT` with proper isolation
  level semantics.
- **Cleaner TTL** model. `expires_at TIMESTAMPTZ`
  + a background sweeper is easier to reason
  about (and to operate) than `EXPIRE` plus
  Redis's lazy expiration. Sliding TTL on
  Continuity is a trigger or a `UPDATE` in the
  same statement, not a `EXPIRE` per write.

### 3.2 What the operator loses

- **Latency p99.** Redis on localhost: ~0.1–1 ms for
  KV/Stream. Postgres with `synchronous_commit=on`:
  ~1–5 ms for the same workload. With
  `synchronous_commit=off` (the recommended setting
  for the event log), the gap closes to ~0.5–2 ms
  but durability changes (the framework documents
  this as a per-tenant choice). The `dispatch_*_call`
  resilience layer (§3.3) absorbs the difference.
- **Tool Worker Pattern** stays on Redis in V1
  (§4.1). A deployment that needs the worker queue
  on Postgres waits for the follow-up ADR.
- **Operational simplicity** of Redis (one binary,
  one config, `INFO`/`SCAN` for diagnostics) is
  replaced by Postgres (tuning `postgresql.conf`,
  vacuum, autovacuum, connection pooling,
  `pg_stat_*` for observability). For teams that
  already operate Postgres, this is a wash; for
  teams that don't, it's a net cost.
- **The `fakeredis` ergonomic for unit tests** is
  replaced by `testcontainers[postgresql]`. The
  test suite becomes ~3× slower on the integration
  side (container spin-up) but the unit side stays
  fast (Postgres is mocked via `PostgresLike`).

### 3.3 Resilience: the existing `dispatch_*_call` layer

The `stream/event_log/dispatch.py` module already
implements the `circuit breaker → retry-with-backoff
→ direct-with-timeout` pipeline for Redis calls. The
Postgres adapters plug into the same
`dispatch_redis_call` orchestrator via a new
`dispatch_pg_call` sibling. Both call sites have
the same shape:

```python
async def append(self, event) -> Result[str, ...]:
    return await dispatch_pg_call(
        lambda: self._insert_with_outbox(event),
        circuit_breaker=self._breaker,
        backoff=self._backoff,
        timeout_s=self._timeout,
    )
```

The Postgres version adds a `deadline_s` argument
(default 5.0 s, the same as the Redis default) and
reuses the existing `BackoffPolicy` (3 attempts,
50 ms base, 1 s max, 10 s budget). No new resilience
knobs are introduced.

### 3.4 Configuration: the operator surface is small

Six new env vars (`KNT_STORAGE_BACKEND`,
`KNT_POSTGRES_URL`, `KNT_POSTGRES_POOL_SIZE`,
`KNT_IDEMPOTENCY_CLAIM_TIMEOUT_S`,
`KNT_DEBEZIUM_TOPIC_PREFIX`,
`KNT_DEBEZIUM_CONNECT_URL`) replace zero. The
existing `KNT_REDIS_URL` is unchanged. The
`Settings` class grows by one mixin (`StorageSettingsMixin`).

### 3.5 Test impact

| File class | Lines to add | Lines to remove |
|---|---|---|
| Unit tests for the 8 Postgres adapters | ~3,500 | 0 |
| Protocol conformance suites (run against both backends) | ~800 | 0 |
| `testcontainers[postgresql]` conftest | ~150 | 0 |
| `tests/manual/cdc_smoke.py` | ~120 | 0 |
| Mock updates in existing fakeredis tests | 0 | 0 (the tests are unchanged) |
| **Total** | **~4,500 LOC new** | **0 LOC removed** |

The existing `tests/unit/infra/redis/` suite is
**not touched**. The new `tests/unit/infra/postgres/`
suite mirrors it one-for-one.

### 3.6 Migration guide (operator-facing)

| Step | Action |
|---|---|
| 1 | `pip install "kntgraph[postgres]"` (or both extras during the transition). |
| 2 | Run `alembic upgrade head` against the target Postgres database. |
| 3 | Set `KNT_STORAGE_BACKEND=postgres` and `KNT_POSTGRES_URL=...` in the deployment. |
| 4 | Replay the EventLog from Redis into Postgres (a one-shot CLI command: `python -m kntgraph.cli.storage migrate --from redis --to postgres --agent *`). |
| 5 | Restart the framework processes. The new factories read `KNT_STORAGE_BACKEND=postgres` and return the Postgres adapters. |
| 6 | (Optional) Deploy the Debezium connector pointing at the new `event_outbox` table. |
| 7 | Decommission the Redis cluster once the framework has been on Postgres for one full business cycle. |

Steps 4 and 7 are the only ones with operational
risk; both are gated by the framework's existing
**DR drill** (the disaster-recovery walkthrough in
`docs/operations/dr_drill.md`).

### 3.7 What the framework does **not** do

- **The framework does not run Debezium.** §2.8.
- **The framework does not own the Alembic
  migrations** as a separate CLI. The `alembic`
  dependency is the migration tool, the
  `migrations/` directory is shipped in the
  framework, and `python -m kntgraph.cli.storage
  migrate` is the entry point that wraps
  `alembic upgrade head`. We do not re-invent
  Alembic.
- **The framework does not promise
  Postgres-specific features** (logical
  replication slots, `LISTEN/NOTIFY`,
  `pgvector`) as part of this ADR. They are
  separate concerns for separate ADRs.
- **The framework does not back-port the
  Postgres adapters to < Python 3.12.** The
  `sqlalchemy[asyncio]>=2.0` dependency requires
  the same Python floor the rest of the framework
  already requires (3.12+).

### 3.8 What this ADR does **not** change

- `redis.asyncio` is still imported in
  `infra/redis/_pool.py:50-51` (the boundary).
- The 8 `infra/redis/*` Protocols are unchanged.
- The `EventLog`, `DeadLetterQueue`,
  `IncrementalWorldStore`, `CheckpointStore`,
  `APIKeyVerifier`, and the three `*Manager`
  classes are unchanged. They consume Protocols.
- The `BaseShortTermMemory` cache orchestration
  is unchanged. The `ShortMemoryStorage` Protocol
  is the abstraction.
- The `RedisLike` Protocol is unchanged.
- The 12 existing `Settings` mixins are unchanged
  (one new mixin is added).
- The `RedisSettingsMixin` is unchanged
  (it stays a first-class setting for
  `KNT_STORAGE_BACKEND=redis` deployments).
- The `cli/templates/main.py.jinja` and
  `cli/templates/consumer.py.jinja` scaffolds
  are **not** Postgres-aware. They get a new
  `cli/templates/postgres_main.py.jinja` /
  `cli/templates/postgres_consumer.py.jinja`
  pair (separate PR, not part of this ADR).

## 4. Pending (out of scope for this ADR)

### 4.1 Tool Worker Pattern migration (the deferred bet)

The Tool Worker queue (`knt:tools:<name>:queue`) and
its consumer-group semantics (`XREADGROUP`, `XACK`,
`XAUTOCLAIM`, `XPENDING`) are the single hardest
piece to migrate. The proposed path is:

```sql
CREATE TABLE tool_queue (
  tool_name    TEXT NOT NULL,
  stream_id    BIGSERIAL,
  event_id     UUID NOT NULL,
  payload      JSONB NOT NULL,
  claimed_by   TEXT,
  claimed_at   TIMESTAMPTZ,
  delivery_count INT NOT NULL DEFAULT 0,
  PRIMARY KEY (tool_name, stream_id)
);
CREATE INDEX tool_queue_unclaimed_idx
  ON tool_queue (tool_name, stream_id)
  WHERE claimed_by IS NULL;
```

```sql
-- The WorkerManager poll loop
SELECT stream_id, event_id, payload
  FROM tool_queue
  WHERE tool_name = :tool
    AND claimed_by IS NULL
    AND (claimed_at IS NULL OR claimed_at < now() - interval '5 minutes')
  ORDER BY stream_id
  FOR UPDATE SKIP LOCKED
  LIMIT 1;
```

The "PEL" becomes a `claimed_by IS NOT NULL` filter.
The auto-reclaim (`XAUTOCLAIM`) becomes the
`claimed_at < now() - interval '5 minutes'` predicate
in the same `SELECT`. The DLQ threshold
(`__tool_worker_retries__`) becomes a `delivery_count`
column. The shape is recognisable; the semantics are
close but not identical (the Redis PEL holds
acknowledged messages until `XACK`; the Postgres
queue deletes the row on successful `UPDATE`).

A follow-up ADR will land this in isolation, with
its own test plan and a side-by-side benchmark
against the Redis implementation. The benchmark
target is **within 2× of the Redis CG loop on the
same hardware** for the 1k events/s / 10 tools /
4 workers profile; if the Postgres path misses
the target, the V1 default stays Redis for the
worker queue and the Postgres deployment ships
with a documented "the worker queue still talks
to Redis" caveat.

### 4.2 The factory package rename

The current `infra/redis/_factory.py` becomes
`infra/storage/_factory.py`. The Redis-specific
factories are re-exported from `infra/redis/` for
back-compat. The Postgres factories are added
alongside. The rename is mechanical but touches
the `infra/redis/__init__.py` re-export list.

This is its own PR, not part of the per-adapter
PRs. It's also a chance to remove the three
back-compat shims that ADR-019 §2.4 flagged for
removal — the same release can retire the
`infra/redis.py`, `infra/redis_codec.py`, and
`infra/idempotency.py` shims.

### 4.3 Alembic migrations vs the framework's own schema versioning

`kntgraph` already has a schema for the
`Settings` pydantic model but does not have a
DB-migration story. This ADR introduces one
(via `alembic`). The decision **where** the
migrations live (in `kntgraph` itself, in a
sibling package, or in a separately-versioned
schema repository) deserves its own ADR and is
flagged here.

### 4.4 The pickle in `World` checkpoint

`WorldCheckpointStorage.save` writes a Python
`pickle.dumps(...)` of the `World` tuple. This
fragility (cross-process, cross-version) was
flagged in ADR-018 §4. The Postgres migration
adds the second process boundary; the
follow-up ADR that converts the pickle to
Pydantic + JSON should land before any
production deployment of the Postgres
backend that crosses a Python version.

### 4.5 Debezium connector management

The framework does not run the connector, but
the operator does. A follow-up ADR should
record:

- The exact connector config template (a
  checked-in JSON in `docs/operations/debezium.md`).
- The retention / compaction policy on the
  resulting Kafka topic.
- The failure mode when Debezium is down
  (the outbox table grows; the framework
  does not block on it; an alert is needed).
- The schema evolution story
  (`event_outbox` payload format changes
  require a Debezium SMT or a topic-versioned
  schema registry).

## 5. Related decisions

- **ADR-019 (Adapter Redis — encapsulamento
  tipado).** The Protocol convention this ADR
  inherits. The `Postgres*Storage` classes
  follow the same shape (lowercase `Adapter`
  suffix optional, Protocol-driven injection,
  typed errors). ADR-019 §2.4 already
  anticipated the multi-backend future with
  the 3-form `EventLog.__init__` heuristic.
- **ADR-005 (Checkpoints & Idempotency).** The
  3-phase claim this ADR adapts. The Postgres
  version is a `SELECT ... FOR UPDATE` +
  `INSERT ... ON CONFLICT` in a transaction;
  the same three phases (`_check_phase`,
  `_claim_phase`, `_finalize_phase`) map
  one-to-one. The `claim_event_id_slot`
  orchestrator is the same function, with
  different primitives underneath.
- **ADR-036 (Tool Worker Pattern).** The
  Consumer-Group dependency this ADR
  defers to a follow-up. The
  `FOR UPDATE SKIP LOCKED` pattern in
  §4.1 is the closest Postgres equivalent
  to the Redis CG semantics; the follow-up
  ADR is the place to validate it
  empirically.
- **ADR-049 (Zero-Token Architecture).** §6
  already lists Postgres as a target for the
  Solution tier post-script work. This
  ADR generalises that signal: it makes
  Postgres a first-class backend for the
  whole storage layer (not just the
  Solution tier).
- **ADR-054 (WorkerManager transport
  evaluation).** The "decision not to
  migrate" ADR that this one mirrors in
  structure. Where ADR-054 says "keep
  `ProcessPoolExecutor` + Redis Streams",
  this ADR says "keep Redis as the default,
  add Postgres as an opt-in alternative,
  defer the Worker queue migration".
  The two together form the framework's
  record of why the current storage
  architecture is what it is.

## 6. References

- `src/kntgraph/infra/redis/` — the 8 sub-adapters
  the new `infra/postgres/` mirrors. ~3,500 LOC
  of reference implementation to read before
  writing the new code.
- `src/kntgraph/infra/redis/_client.py:78` — the
  `RedisLike` Protocol this ADR's `PostgresLike`
  is the sibling of.
- `src/kntgraph/infra/redis/_event_log/_idempotency.py` —
  the 3-phase claim the Postgres version adapts
  (§2.4).
- `src/kntgraph/stream/event_log/dispatch.py` —
  the `dispatch_redis_call` orchestrator the
  Postgres adapters reuse (with a new
  `dispatch_pg_call` sibling).
- `src/kntgraph/infra/config/_redis.py:31` —
  the `RedisSettingsMixin` this ADR keeps
  (and augments with `StorageSettingsMixin`).
- `pyproject.toml` — the dependency block this
  ADR splits into `redis` and `postgres`
  extras (§2.7).
- `docs/quickstart.md` — the install page that
  will gain a `KNT_STORAGE_BACKEND=postgres`
  quickstart alongside the existing Redis one.
- `docs/operations/dr_drill.md` — the
  disaster-recovery walkthrough that gates the
  Redis → Postgres data replay (§3.6 step 4).
- `tests/integration/conftest.py:33` —
  `KNT_REDIS_PASSWORD` env var; not part of
  this ADR but worth flagging that the
  Postgres equivalent
  (`KNT_POSTGRES_PASSWORD`) lands in the
  same conftest.
- Debezium Postgres connector docs —
  the operator-side `pgoutput` config
  template that lives in
  `docs/operations/debezium.md` (not yet
  written; §4.5).
- SQLAlchemy 2.0 async ORM docs — the
  `AsyncEngine` / `AsyncSession` / `Executable`
  API this ADR's `PostgresLike` Protocol
  wraps.
- ADR-051 (Release Versioning via Git Tags)
  + ADR-052 (PyPI Publishing) — the release
  pipeline that will publish
  `kntgraph[redis]` and `kntgraph[postgres]`
  as the first multi-extra release.
