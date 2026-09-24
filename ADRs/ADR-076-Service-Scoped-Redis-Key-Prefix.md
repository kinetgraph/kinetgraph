<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-076: Service-scoped Redis key prefix via `KNT_REDIS_KEY_PREFIX`

- **Status:** Accepted (proposed 2026-09-24)
- **Date:** 2026-09-24
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-019](./ADR-019-Redis-Adapter-Typing.md) — typed Redis adapters; `RedisLike` Protocol. The plumbing this ADR introduces runs through the same `infra/redis/` sub-adapters that ADR-019 organised.
  - [ADR-041](./ADR-041-agents-roles-deprecation.md) — the `DeprecationWarning` → `git rm` lifecycle. NOT applied in this ADR (the DLQ key-constant duplication is killed as internal cleanup, no API change); reserved for the future relocation of `DLQReason` / `DeadLetterEvent`.
  - [ADR-057](./ADR-057-durabilidade-dos-dados.md) — per-class retention. The migration script introduced here MUST respect the same retention buckets when picking which keys to rename.
  - [ADR-068](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md) — `scan_iter` traffic model. The `SCAN_PATTERN` constant in `infra/redis/_event_log/_keys.py` becomes a function under this ADR; the cost model of §1.2 is unchanged because the prefix is a static prefix, not a wildcard.

---

## 1. Context

### 1.1 The problem: cross-talk when two services share a Redis

Every `knt:*` key the framework writes today is rooted at the top of the Redis keyspace. Two services (call them `acme-billing` and `acme-crm`) sharing a single Redis — same `KNT_REDIS_URL`, no per-service DB index — silently read each other's data:

| Caller | Reads / writes |
|---|---|
| `EventLog` (acme-billing) | `XRANGE knt:agents:<id>:events` — but `acme-crm` wrote there too |
| `WorkerManager` (acme-crm) | `XREADGROUP knt:tools:weather_api:queue` — but `acme-billing`'s dispatcher published there |
| `DeadLetterQueue` (acme-billing) | `XRANGE knt:dlq:events` — the DLQ is a **global** stream, no per-tenant partitioning at all |
| `RedisAPIKeyStorage` (acme-crm) | `GET knt:api:keys:<digest>` — digest collisions across services are rare but not impossible |

Cross-talk is silent — no exception, no log, just **wrong answers**. The framework treats `knt:*` as a private namespace; in reality it is a flat shared keyspace when two deployments coexist.

The only workaround today is **one Redis DB per service** (`?db=2`), which is a coarser separation: every operator has to reconfigure `KNT_REDIS_URL` per deploy, and two services on the same Redis require either separate Redis instances or a hand-managed DB index (no programmatic partition in code).

### 1.2 The 26-key footprint today

The `knt:` literal is hardcoded in **9 distinct modules** of the framework plus one legacy mirror, and in **~50 test sites**:

| Layer | File | Key |
|---|---|---|
| EventLog | `src/kntgraph/infra/redis/_event_log/_keys.py:24-30` | `AGENT_STREAM_KEY`, `EVENT_ID_INDEX`, `SCAN_PATTERN` |
| DLQ | `src/kntgraph/infra/redis/_dlq/_redis.py:45-48` | `dlq:events`, `dlq:reasons`, `dlq:by_agent`, `dlq:by_event_id` |
| **DLQ legacy (DUPLICATED)** | `src/kntgraph/events/dlq/values.py:45-48` | the same 4 keys |
| World checkpoint | `src/kntgraph/infra/redis/_world_checkpoint/_redis.py:29,36` | `world`, `world-cursor` |
| Solution tier | `src/kntgraph/infra/redis/_memory/_solution.py:93` | `solution` |
| Reactive checkpoint | `src/kntgraph/infra/redis/_checkpoint/_redis.py:43` | `reactive:checkpoints` |
| Auth | `src/kntgraph/infra/redis/_auth/_redis.py:33` | `api:keys` |
| Session tier | `src/kntgraph/memory/session.py:55` | `session` |
| Continuity tier | `src/kntgraph/memory/continuity/state.py:39` | `continuity` |
| Profile tier | `src/kntgraph/memory/profile.py:62` | `profile` |
| Tool router | `src/kntgraph/tools/manager.py:344,381,695`; `src/kntgraph/tools/router.py:57` | `tools:<name>:queue` |
| Reactive runner | `src/kntgraph/runner/reactive.py:205` | `tool_stream_prefix="knt:tools"` |
| Knowledge cfg | `src/kntgraph/infra/config/_knowledge.py:47` | `solutions:review` |

The `parse_agent_id_from_stream_key` helper in `_keys.py:57` also hardcodes `prefix = "knt:agents:"`; it cannot be tuned.

### 1.3 What we want

A **runtime-configurable namespace prefix** so each service scopes its keys to its own slice of the Redis keyspace, without changing the wire format of any existing data and without inventing per-service DB indexes. Set the prefix in the environment, and `acme-billing`'s EventLog writes `acme-billing:knt:agents:*:events` while `acme-crm`'s writes `acme-crm:knt:agents:*:events`. Same Redis, no overlap.

### 1.4 Non-goals

- **This is not a security boundary.** Redis has no per-prefix ACL; anyone who can `CONNECT` to the Redis can read every prefixed slice. If the requirement is *tenant isolation*, the answer is a different deployment topology (separate Redis instance per tenant), not a key prefix. The ADR declares this explicitly in §3.2 to avoid the misreading.
- **FalkorDB is out of scope.** FalkorDB's `graph_name` already provides the same partitioning knob at the graph level, and FalkorDB is on the decommission roadmap. No `KNT_FALKORDB_KEY_PREFIX` is introduced here.

---

## 2. Decision

### 2.1 The setting: `KNT_REDIS_KEY_PREFIX`

A new field on `RedisSettingsMixin`:

```python
# src/kntgraph/infra/config/_redis.py
class RedisSettingsMixin(BaseSettings):
    redis_url: str = Field(default="redis://localhost:6379")
    redis_max_connections: int = Field(default=50)
    redis_fake: bool = Field(default=False)

    # NEW: namespace prefix for every Redis key the framework
    # writes. Empty string (default) preserves the pre-ADR-076
    # behaviour byte-for-byte. Setting ``"acme-billing:"`` makes
    # the EventLog stream ``acme-billing:knt:agents:<id>:events``
    # instead of ``knt:agents:<id>:events``.
    redis_key_prefix: str = Field(default="")
```

Read via the existing `KNT_` env prefix: `KNT_REDIS_KEY_PREFIX=acme-billing:`. Validation is enforced by a custom validator on the field (see §2.4); empty string is always valid.

### 2.2 The helper: `infra/redis/_prefix.py`

A new module owning the prefix logic. Two functions, both pure:

```python
# src/kntgraph/infra/prefix.py (or infra/redis/_prefix.py)
from __future__ import annotations

import re

_PREFIX_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-:]*$")

def validate_prefix(prefix: str) -> None:
    """Raise ``ValueError`` if ``prefix`` is not a valid Redis
    key prefix. Empty string is always valid.

    Rules:
      - Allowed characters: ``[a-zA-Z0-9_.\\-:]``.
      - Must not contain ``*`` (breaks SCAN glob).
      - Must not contain ``{`` (breaks ``.format`` templates).
      - Must not be just ``:`` (looks like an oversight).
      - Trailing ``:`` is allowed but not required.
    """
    if prefix == "":
        return
    if not _PREFIX_PATTERN.match(prefix):
        raise ValueError(
            f"redis_key_prefix contains invalid characters: {prefix!r}. "
            f"Allowed: alphanumerics, ':', '_', '.', '-'."
        )
    if prefix == ":":
        raise ValueError("redis_key_prefix must not be just ':'.")
    if "*" in prefix or "{" in prefix or "}" in prefix:
        raise ValueError(
            f"redis_key_prefix contains glob/template syntax: {prefix!r}."
        )


def namespaced(prefix: str, key: str) -> str:
    """Return ``key`` prefixed with ``prefix`` if non-empty.

    Empty ``prefix`` returns ``key`` unchanged (zero-cost fast
    path; matches the pre-ADR-076 behaviour byte-for-byte).
    """
    validate_prefix(prefix)
    if not prefix:
        return key
    return f"{prefix}{key}"
```

Both functions are pure and side-effect-free. `validate_prefix` is called once at construction time of every adapter that holds a prefix, not on every call to `namespaced` (the cost of validation is amortised).

### 2.3 Plumbing: prefix flows `Settings → Pool → Factory → Adapter`

Three layers, each doing one thing:

#### Layer 1: `Settings` holds the prefix (declared above).

#### Layer 2: `RedisPool` accepts the prefix at construction time.

```python
# src/kntgraph/infra/redis/_pool.py (modified)
class RedisPool:
    def __init__(self, client: RedisLike, *, key_prefix: str = "") -> None:
        validate_prefix(key_prefix)
        self._client = client
        self._key_prefix = key_prefix

    @property
    def key_prefix(self) -> str:
        return self._key_prefix

    @classmethod
    def from_settings(cls, settings: Settings) -> "RedisPool":
        # ... existing pool construction ...
        return cls(client=client, key_prefix=settings.redis_key_prefix)
```

#### Layer 3: factories and adapters take `key_prefix`.

```python
# src/kntgraph/infra/redis/_factory.py (modified)
def create_event_log_storage(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
    key_prefix: str = "",   # NEW
) -> EventLogStorage:
    if key_prefix == "" and settings is not None:
        key_prefix = settings.redis_key_prefix
    return RedisEventLogAdapter(
        client=_resolve_client(settings, client),
        maxlen=_resolve_stream_maxlen(settings, default=MAXLEN_DEFAULT),
        key_prefix=key_prefix,   # NEW
    )
```

The pattern repeats for every factory (`create_dlq_storage`, `create_session_storage`, `create_profile_storage`, `create_continuity_storage`, `create_solution_storage`).

#### Layer 4: adapters apply the prefix at key-build time.

Each `Redis*Storage` gains a `key_prefix` constructor field. The existing string constants become **suffix templates**, and a thin `k(name)` private helper composes them:

```python
# src/kntgraph/infra/redis/_event_log/_adapter.py (modified)
class RedisEventLogAdapter:
    def __init__(
        self,
        client: RedisLike,
        *,
        maxlen: int = MAXLEN_DEFAULT,
        key_prefix: str = "",
    ) -> None:
        self._client = client
        self._maxlen = maxlen
        self._key_prefix = key_prefix

    def _k(self, suffix: str) -> str:
        """Compose a namespaced Redis key from a suffix template."""
        return namespaced(self._key_prefix, suffix)

    def stream_key_for_agent(self, agent_id: str) -> str:
        return self._k(AGENT_STREAM_KEY.format(agent_id=agent_id))
    # ... etc
```

`SCAN_PATTERN` becomes a function too, because the prefix is no longer constant:

```python
# src/kntgraph/infra/redis/_event_log/_keys.py (modified)
def scan_pattern(prefix: str = "") -> str:
    """Glob pattern for ``scan_iter`` when listing all agents."""
    return namespaced(prefix, "knt:agents:*:events")
```

The default-empty `prefix` keeps existing call sites compiling without change; the public `SCAN_PATTERN` constant is removed in favour of the function (and the `__init__.py` re-exporter is updated in the same commit; see §5).

### 2.4 Field validator on `Settings`

```python
# src/kntgraph/infra/config/_redis.py
from pydantic import field_validator

class RedisSettingsMixin(BaseSettings):
    redis_key_prefix: str = Field(default="")

    @field_validator("redis_key_prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        validate_prefix(v)
        return v
```

Validation runs at `Settings` construction (once, at process boot), not on every key build. The helper does the same check, so the field validator's role is to fail fast at boot rather than at first I/O.

### 2.5 DLQ key constants: kill the duplication now (no deprecation cycle)

`src/kntgraph/events/dlq/values.py:45-48` defines the same four DLQ keys as `src/kntgraph/infra/redis/_dlq/_redis.py:45-48`. The plumbing change in §2.3 turns this duplication into a **diverging source of truth**: the new prefix plumbing applies to the constants in `infra/redis/_dlq`, but the duplicate in `events/dlq/values.py` would keep emitting the unprefixed `knt:dlq:events` — half-prefixed Redis, the same cross-talk as §1.1.

Resolution: in the **same commit** as the prefix plumbing, replace the four legacy literals with re-exports from `infra.redis._dlq`:

```python
# src/kntgraph/events/dlq/values.py (modified — keys only)
from ..infra.redis._dlq import (
    DLQ_AGENT_INDEX,
    DLQ_EVENT_INDEX,
    DLQ_REASON_INDEX,
    DLQ_STREAM_KEY,
)
```

This is **internal cleanup**, not an API change: the import path (`kntgraph.events.dlq.values.DLQ_STREAM_KEY`) stays alive, the value stays the same. No `DeprecationWarning` is emitted; no minor-cycle delay is needed. The single source of truth becomes `infra.redis._dlq`, which now owns the prefixed layout end-to-end.

#### What this ADR does NOT touch

`events/dlq/values.py` also contains `DLQReason` (enum) and `DeadLetterEvent` (frozen dataclass + codec). These are **domain types**, not Redis-specific — they are not duplicated anywhere, and they are imported by four framework modules (`runner/_observability.py`, `runner/reactive.py`, `runner/tool_call_ttl_sweeper.py`, `runner/_dlq_writer.py`) and several tests. Removing the module would require first relocating these types to a new home, which is a **separate decision** (out of scope for ADR-076; tracked as a follow-up after the prefix plumbing lands and stabilises).

---

## 3. Consequences

### 3.1 Positive

- **Two services can share one Redis.** `KNT_REDIS_KEY_PREFIX=acme-billing:` vs `KNT_REDIS_KEY_PREFIX=acme-crm:` produces disjoint keyspaces; `XRANGE acme-billing:knt:agents:X:events` and `XRANGE acme-crm:knt:agents:X:events` are independent scans.
- **Zero behaviour change for the existing single-service deploy.** Default `redis_key_prefix=""` is byte-for-byte identical to pre-ADR-076. Operators opt in by setting the env var.
- **One source of truth for the prefix.** A single helper, called once per key build, instead of 26 hardcoded literals across 9 modules.
- **The DLQ key-constant duplication is killed.** The four `DLQ_*` keys in `events/dlq/values.py` are re-exported from `infra/redis/_dlq`, removing the diverging-source-of-truth risk that the prefix plumbing would otherwise expose. Same commit as the plumbing (§5 commit 1).
- **Test coverage is a free byproduct.** Migrating the ~50 hardcoded test sites to use the factories (which now carry the prefix) makes the test suite exercise the prefix path on every CI run, not just on the few tests that explicitly probe it.

### 3.2 Negative (and the explicit non-security note)

- **Not a security boundary.** Two operators sharing a Redis cannot be kept apart by a key prefix alone — both can `FLUSHDB`, both can `KEYS *`, both can `CONFIG GET`. Redis has no per-prefix ACL. The key prefix scopes *which keys the framework writes/reads*, not *who is allowed to*. If a deploy needs tenant isolation, the answer is a separate Redis instance (or Redis Enterprise / a managed service with per-tenant clusters). This is stated explicitly in §1.4 and in the operator-facing docstring of `KNT_REDIS_KEY_PREFIX` so it is not mis-sold as a security feature.
- **One indirection per key build.** `namespaced(prefix, key)` is one `if` + one f-string concat; cost is in the low microseconds at most. Negligible relative to the round-trip.
- **The `SCAN_PATTERN` constant becomes a function.** Three call sites in `infra/redis/__init__.py`, `infra/redis/_event_log/__init__.py`, and `infra/redis/_event_log/_adapter.py` change from `SCAN_PATTERN` (constant) to `scan_pattern(prefix)` (function). Mechanical, but visible in the diff.
- **Test fixtures must read the env var.** The current `clean_redis` fixture (in `tests/conftest.py` and integration `conftest.py` files) hardcodes `delete("knt:agents:*:events")` patterns. After this ADR it must read `KNT_REDIS_KEY_PREFIX` and apply it to every delete pattern. If a test forgets, the leak is silent — covered by the per-public-function test in §6.3.

### 3.3 Migration script: `scripts/migrate_redis_keys.py`

A new ops script in the same shape as `scripts/migrate_profile_to_continuity.py` and `scripts/migrate_principals.py`. **This is the "automatic migration" the operator runs once.** It is not run at framework boot (startup is the wrong place for a `SCAN` + `RENAME` over an entire Redis) — it is a dry-runnable, idempotent script the framework ships.

```
Usage:
    KNT_REDIS_KEY_PREFIX=acme-billing: \
        python scripts/migrate_redis_keys.py --dry-run

    KNT_REDIS_KEY_PREFIX=acme-billing: \
        python scripts/migrate_redis_keys.py --commit

    # explicit from/to (defaults to "knt:" from and $KNT_REDIS_KEY_PREFIX to):
    python scripts/migrate_redis_keys.py \
        --from-prefix "knt:" \
        --to-prefix   "acme-billing:knt:" \
        --commit
```

Mechanics:

1. `SCAN MATCH <from-prefix>* COUNT 500` (matches the cost model in ADR-068 §1.2).
2. For each key found, `RENAME <from> <to>` — `RENAME` is atomic and preserves TTL, which is critical for the idempotency index (`knt:eventids:<id>`, 24 h TTL, ADR-019).
3. If `<to>` already exists (idempotent re-run), `RENAME` raises; the script logs and skips (continues with the next key).
4. Per-key progress output (every 100 keys) and a final summary table: `renamed`, `skipped (target exists)`, `errors`.

**Why dry-run is the default.** Operators with production data run `--dry-run` first, eyeball the count, then run `--commit`. This is the standard safe default for every other migration script in `scripts/`; this one follows the precedent.

**Why no `--to-prefix` defaulting to `KNT_REDIS_KEY_PREFIX`.** It does default to that env var (for ergonomics), but explicit `--to-prefix` wins so an operator can review the value out-of-band (e.g. in a change-management ticket) before committing.

### 3.4 What the ADR does NOT cover

- **FalkorDB graph partitioning.** FalkorDB uses `graph_name` per query, and the framework already parameterises it. FalkorDB is on the decommission roadmap. Out of scope here.
- **Pub/Sub channels.** The framework uses Redis Streams (`XADD`/`XREADGROUP`), not Pub/Sub. The key prefix naturally applies to stream keys; there is no separate channel namespace to worry about.
- **Multi-region replication.** A Redis key prefix is a runtime configuration; replication topology is the operator's choice.
- **Dynamically changing the prefix at runtime.** The prefix is read once at boot from `Settings`. Changing it requires a restart. This is intentional — re-reading the prefix mid-process would require either a lock-protected reload or a coordinator process, neither of which the framework needs.

---

## 4. Alternatives considered

### 4.1 Redis DB index per service

Configure `KNT_REDIS_URL=redis://srv/0` for service A, `KNT_REDIS_URL=redis://srv/1` for service B. Standard Redis pattern.

Rejected:
- DB index is a per-connection property, not per-key. The framework would need to expose `KNT_REDIS_DB=N`, and operators would have to manage the index space themselves.
- Cross-service tooling (`redis-cli MONITOR`, `INFO KEYSPACE`) does not naturally aggregate across DBs.
- Migration from single-service to multi-service is the same operational headache (a `SELECT` round-trip per command, or explicit `KNT_REDIS_DB` per deploy).

The key prefix composes **with** the DB index (both can be set), so an operator who already uses DB indices keeps them; the prefix is the per-service knob on top.

### 4.2 Hardcode the prefix in code (`"knt:acme-billing:..."`)

Pre-compute the prefix per deployment by patching the constants.

Rejected: same shape of problem relocated. The operator still has to fork the constants per service; the framework cannot be shared across services without conflict.

### 4.3 Hardcoded prefix without env var (constant in `Settings`)

Add `redis_key_prefix: str = "knt:"` to `Settings` as a class constant.

Rejected: defeats the runtime configurability requirement. Operators on the same image cannot run different services.

### 4.4 Lazy migration at read time (transparent rewrite)

When an adapter sees a `knt:*` key, transparently rewrite to `<prefix>knt:*` on first access.

Rejected:
- Race conditions on concurrent reads/writes during the cutover.
- A mixed-state Redis (some keys at `knt:*`, some at `<prefix>knt:*`) is harder to reason about than a one-shot cutover.
- The framework would need to track "migrated" state per key, which is the kind of state the framework explicitly avoids (see ADR-002 — EventLog as the single source of truth).

A deterministic one-shot `RENAME` script (§3.3) is safer than an implicit migration path.

### 4.5 Dual-write during a cutover window

For N days, adapters write to both `knt:*` and `<prefix>knt:*`; reads prefer the prefixed path.

Rejected: doubles the I/O cost during the window, and the EventLog idempotency index (`knt:eventids:<id>`) would have to be deduped across the two keyspaces — adding complexity for a window that, in practice, no operator bothers to coordinate. The migration script in §3.3 is a one-shot op; it does not need a window.

### 4.6 Per-tenant ACL via Redis Cluster ACL

Redis has no per-prefix access control.

Not applicable. The "two services sharing a Redis" use case is **namespace scoping**, not access control. §1.4 / §3.2 make this explicit.

---

## 5. Migration sequencing

Three commits, gate green at each step. The branching policy (`AGENTS.md` §11, see also `kntgraph-branch-policy` skill) is followed by the human reviewer; the AI agent stages and proposes the changes.

### Commit 1 — Plumbing with default-empty prefix

**Goal:** zero behaviour change; gate green; `_prefix.py` lands and every adapter accepts `key_prefix` but defaults to `""`.

Scope:

- New file `src/kntgraph/infra/redis/_prefix.py` (the helper + validator).
- `RedisSettingsMixin.redis_key_prefix: str = ""` with `field_validator`.
- `RedisPool.__init__(client, *, key_prefix="")` + `key_prefix` property; `RedisPool.from_settings` reads from `Settings`.
- Every factory in `_factory.py` accepts `key_prefix=""` and forwards it.
- Every `Redis*Storage.__init__` accepts `key_prefix=""` and stores it.
- `parse_agent_id_from_stream_key` becomes `parse_agent_id_from_stream_key(key, prefix="")` (or stays as is and adds a sibling helper — to be decided at impl time).
- `SCAN_PATTERN` constant → `scan_pattern(prefix="")` function. All three call sites updated.
- DLQ keys: `events/dlq/values.py` re-exports the four `DLQ_*` key constants from `infra.redis._dlq` (no `DeprecationWarning`; see §2.5).

Acceptance:

- `KNT_REDIS_KEY_PREFIX=""` (the default): all 912+ existing tests green; no key the framework writes changes.
- `KNT_REDIS_KEY_PREFIX="test:"`: a single new integration test (`tests/integration/infra/test_redis_prefix.py`) confirms `XRANGE test:knt:agents:X:events` returns the events the framework wrote.
- `events/dlq/values.DLQ_STREAM_KEY` and the other three constants resolve to the same values as `infra.redis._dlq` (the duplication is dead); no test importing the constants from `events.dlq.values` breaks.

### Commit 2 — Consolidate the hardcoded literals

**Goal:** every `knt:*` literal in framework code moves into a constant or helper. No behaviour change.

Scope:

- `tools/manager.py` (3 sites): introduce `TOOL_QUEUE_KEY_TEMPLATE` in a new `infra/redis/_tools/_keys.py`; `WorkerManager` reads the prefix from `Settings`.
- `tools/router.py` (1 site): same.
- `runner/reactive.py`: the `tool_stream_prefix: str = "knt:tools"` parameter is kept for back-compat but the actual `scan_iter` pattern composes the prefix.
- `memory/session.py`, `memory/continuity/state.py`, `memory/profile.py`: their `*_KEY_PREFIX` constants stay (they are tenant tier names, fine as constants) but the storage adapter reads `Settings.redis_key_prefix` and applies it. The factory wiring is the single point of truth.
- `infra/config/_knowledge.py`: the `solutions_review_queue` default keeps `knt:` semantics; an optional `solutions_review_key_prefix` field composes with `redis_key_prefix`.

Acceptance:

- `grep -rn '"knt:' src/kntgraph --include='*.py'` returns **zero** hardcoded string literals (only composed patterns via `namespaced`).
- All tests green.

### Commit 3 — Test suite migration + migration script

**Goal:** tests stop hardcoding `knt:*`; the operator-facing migration script ships.

Scope:

- `tests/conftest.py` (and every integration `conftest.py`): the `clean_redis` fixture reads `KNT_REDIS_KEY_PREFIX` from env and composes the delete pattern. A single helper, `prefixed(pattern: str) -> str`, in a new `tests/_prefix.py`.
- All ~50 test sites that call `redis.delete("knt:...")`, `redis.xadd("knt:...", ...)`, etc. migrate to use the `prefixed` helper or the factory-provided adapter.
- New file `scripts/migrate_redis_keys.py` (the operator-facing script from §3.3).
- New file `tests/unit/scripts/test_migrate_redis_keys.py`: confirms `--dry-run` prints and exits 0 without writing; `--commit` performs a `RENAME` on a fakeredis instance; `--commit` against a pre-existing target is a no-op (the script logs and skips, does not raise).
- `CHANGELOG.md`: entry under `[Unreleased]` documenting the new env var, the helper, and the migration script.

Acceptance:

- All tests green; the test suite exercises both `redis_key_prefix=""` (default) and `redis_key_prefix="test:"` (new tests).
- `scripts/ci.py` gate (ADR-019 §2.5) is green.
- Manual smoke: `KNT_REDIS_FAKE=1 KNT_REDIS_KEY_PREFIX=demo: python scripts/migrate_redis_keys.py --commit` on a fakeredis-backed Redis behaves as documented (rename + skip-on-collision).

---

## 6. Test plan

### 6.1 Unit tests (fakeredis, `KNT_REDIS_FAKE=1`)

- `tests/unit/infra/redis/test_prefix.py` — `validate_prefix` rejects `":"`, `""` is allowed, `"acme:"` is allowed, `"acme:*"` is rejected, `"acme:{"` is rejected.
- `tests/unit/infra/redis/test_factory_prefix.py` — every factory function in `_factory.py` propagates `key_prefix` to the constructed adapter; default empty when neither `key_prefix` nor `settings.redis_key_prefix` is set.
- `tests/unit/infra/redis/_event_log/test_scan_pattern.py` — `scan_pattern("")` returns `"knt:agents:*:events"` (legacy contract); `scan_pattern("acme:")` returns `"acme:knt:agents:*:events"`.

### 6.2 Integration tests (real Redis)

- `tests/integration/infra/test_redis_prefix.py` — end-to-end: write events through `RedisEventLogAdapter(key_prefix="acme:")`, `XRANGE` shows them at `acme:knt:agents:*:events`; same adapter with `key_prefix="crm:"` writes to `crm:knt:agents:*:events`; `scan_iter(match=scan_pattern("acme:"))` returns only the `acme:` keys.
- `tests/integration/infra/test_redis_prefix_isolation.py` — two adapters with different prefixes on the same Redis instance never see each other's data (the §1.1 use case, made into a test).

### 6.3 Migration script tests

- `tests/unit/scripts/test_migrate_redis_keys.py` — fakeredis-backed:
  - `--dry-run` performs no writes (count of `keys()` unchanged before/after).
  - `--commit` renames every key matching the from-pattern; target keys exist with the same TTL (assert via `ttl`).
  - Re-running `--commit` after a successful run is a no-op (target exists → skip path).
  - Mismatched `--to-prefix` and `KNT_REDIS_KEY_PREFIX` raises (explicit flag wins, env var is fallback).

### 6.4 Coverage gate

Per `AGENTS.md` §7 (`kntgraph-testing` skill): every public function in `infra/redis/_prefix.py`, every factory in `infra/redis/_factory.py`, and `scripts/migrate_redis_keys.py` get the happy-path-plus-one-failure-mode test. Branch coverage on `infra/redis/` remains above the per-vertical baseline (re-checked at commit 3; if it regresses, update `verticals_baseline` per the existing process).

---

## 7. References

- [ADR-019](./ADR-019-Redis-Adapter-Typing.md) — typed Redis adapters; the `infra/redis/` sub-adapter organisation this ADR reuses.
- [ADR-041](./ADR-041-agents-roles-deprecation.md) — the `DeprecationWarning` → `git rm` lifecycle. NOT applied in this ADR; the DLQ key-constant duplication is killed as internal cleanup (no API change), not deprecated. Reserved for the future relocation of `DLQReason` / `DeadLetterEvent` if that decision goes the deprecation route.
- [ADR-057](./ADR-057-durabilidade-dos-dados.md) — per-class retention; the migration script must respect the same buckets when picking which keys to rename (the EventLog idempotency TTL, the DLQ retention, the world-checkpoint retention).
- [ADR-068](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md) — the `scan_iter` cost model; unchanged by this ADR because the prefix is static, not a wildcard.
- [AGENTS.md §1](../AGENTS.md) — type discipline; `redis_key_prefix: str` is the plain-string type, no `Any`.
- [AGENTS.md §2](../AGENTS.md) — no compat shims; the duplicated DLQ constants are re-exported (same path, new source) rather than shimmed behind a `DeprecationWarning`.
- [AGENTS.md §3](../AGENTS.md) — file layout; `_prefix.py` lives next to `_client.py`, `_codec.py`, etc. (~50 LOC, well under the 500-line guideline).
- [AGENTS.md §6](../AGENTS.md) — errors are typed; `validate_prefix` raises `ValueError`, not `Exception`.
- [AGENTS.md §7](../AGENTS.md) — testing; the per-public-function coverage gate.
- [AGENTS.md §11](../AGENTS.md) — branch policy; AI agent stages and proposes, human reviews and merges.
- [scripts/migrate_profile_to_continuity.py](../scripts/migrate_profile_to_continuity.py) — the shape `scripts/migrate_redis_keys.py` follows (dry-run default, `--commit` to apply, idempotent, summary table).
- [scripts/migrate_principals.py](../scripts/migrate_principals.py) — second precedent for the migration-script style.
