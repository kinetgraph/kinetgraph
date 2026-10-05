<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-077: Exception translation lives at the storage adapter boundary

- **Status:** Accepted (proposed 2026-10-05)
- **Date:** 2026-10-05
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-019](./ADR-019-Redis-Adapter-Typing.md) — typed Redis adapters; the `infra/redis/` sub-adapter organisation this ADR reuses. ADR-019 established *what* the adapter layer is; this ADR establishes *which exceptions the layer raises*.
  - [ADR-076](./ADR-076-Service-Scoped-Redis-Key-Prefix.md) — namespace prefix plumbing; uses the same `Result` translation surface this ADR codifies.
  - [AGENTS.md](../AGENTS.md) §1.1 (no `Any` / no bare `object`), §1.2 (framework never imports from vertical), §6 (errors are typed; `Result[T, E]` and `*Error` exceptions), §11 (branch policy).
  - [`kntgraph-typed-errors`](../.agents/skills/kntgraph-typed-errors/SKILL.md) — the existing skill that mandates `Result` for mutating ops. This ADR extends the same principle to the exception layer: the *expected-failure path* is `Result`, the *unexpected crash path* is a typed `*Error` that the adapter Protocol documents.
  - [`kntgraph-type-discipline`](../.agents/skills/kntgraph-type-discipline/SKILL.md) — `JsonValue` discipline, `TYPE_CHECKING` for type-only imports.

---

## 1. Context

### 1.1 The problem: `except Exception` is everywhere

The framework's Railway Pattern contract says mutating ops return `Result[T, *Error]`. The typed-error hierarchy already exists:

```python
# src/kntgraph/core/result/errors.py
class RailwayError(Exception): ...
class ValidationError(RailwayError): ...
class PersistenceError(RailwayError): ...
class BusinessError(RailwayError): ...
class ToolError(RailwayError): ...

# src/kntgraph/infra/redis/_errors.py
class RedisAdapterError(Exception): ...
class RedisUnavailableError(RedisAdapterError): ...
class MemoryError(RedisAdapterError): ...
class MemoryDecodeError(MemoryError): ...
class MemorySerializationError(MemoryError): ...
class MemoryMiss(MemoryError): ...
class IdempotencyConflict(RedisAdapterError): ...
```

The hierarchy is there. The problem is that **call sites do not trust the Protocol**: every `BaseShortTermMemory` implementation, every facade in `events/dlq/`, and every consumer in the runner wraps storage calls in `except Exception` because the third-party `redis.asyncio` library can raise `RedisError`, `ConnectionError`, `TimeoutError`, `OSError`, `asyncio.CancelledError`, or anything else the transport layer decides.

Result: 6 `except Exception` clauses in framework code (`ruff check` reports 6 `BLE001` violations), each carrying a `# noqa: BLE001` to silence the lint. The `# noqa` is a *confession* that the contract is not enforced.

### 1.2 The specific offenders

| File | Line | Method | What it does |
|---|---:|---|---|
| `src/kntgraph/memory/base.py` | 467 | `_read_fold_cursor` | Wraps `ShortMemoryStorage.read_fold_cursor` in `try/except Exception`, returns `None` on failure (logged) |
| `src/kntgraph/memory/base.py` | 496 | `_write_fold_cursor` | Wraps `ShortMemoryStorage.write_fold_cursor` in `try/except Exception`, returns `Err(PersistenceError)` on failure |
| `src/kntgraph/memory/session.py` | 387 | `list_active` | Wraps `iter_keys` in `try/except Exception`, returns `Err(PersistenceError)` |
| `src/kntgraph/memory/profile.py` | 348 | `list_for_tenant` | Same shape as `list_active` |
| `src/kntgraph/memory/continuity/manager.py` | 471 | `list_for_tenant` | Same shape as `list_active` |
| `src/kntgraph/events/dlq/store.py` | 148 | `append` (agent-index HSETNX branch) | Wraps `client.hsetnx` in `try/except Exception`, logs and continues |

Each of these sites is an **admission that the storage Protocol's `Result[T, MemoryError]` return type is not actually a contract**: the call site cannot trust it because under the hood the adapter might raise. The `try/except` is defensive glue, but the glue itself is broken (catches everything, including `KeyboardInterrupt`).

### 1.3 The pattern the codebase already uses *elsewhere*

The graph adapter (`src/kntgraph/infra/graph/_adapter.py:43`) imports `GraphError` from the knowledge vertical. Per ADR-019, that import is a known vertical-leak (§1.2 of the audit). The **fix** for that leak is "declare the error type in `infra/graph/_protocol.py` and have the vertical re-export from there" — the error type belongs to the framework, not the vertical.

This ADR applies the same principle to all storage adapters: the **error type belongs to the adapter Protocol**; the **third-party exception** (e.g. `redis.exceptions.RedisError`) is the implementation detail that the adapter swallows once and for all.

### 1.4 What we want

A single principle, applied uniformly:

> **The storage Protocol documents the exception types it raises. Adapters translate third-party exceptions into those types at the boundary. Consumers trust the Protocol and never wrap storage calls in `try/except`.**

The principle generalises to every layer that touches an external system — `redis`, `httpx`, `pika`, `boto3`, the file system, the OS. Each of those layers gets a typed `*Error` family in `infra/<layer>/_errors.py`; each adapter raises only those; each Protocol declares the return as `Result[T, <Layer>Error]`.

---

## 2. Decision

### 2.1 The principle: translate at the adapter, trust at the call site

The framework's storage layers follow three layers, each with one job:

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 3: Call site (framework + verticals)                       │
│   - Trusts the Protocol; no `try/except`                        │
│   - Uses `Result.is_err() / .err_value() / .err_value_or_raise()`│
└─────────────────────────────────────────────────────────────────┘
                              ▲
                              │ `Result[T, <Layer>Error]`
                              │
┌─────────────────────────────────────────────────────────────────┐
│ Layer 2: Protocol (in `infra/<layer>/_protocol.py`)             │
│   - Declares the contract: every method returns `Result[T, E]`  │
│   - Documents the `*Error` family that wraps third-party raises │
│   - No third-party imports                                      │
└─────────────────────────────────────────────────────────────────┘
                              ▲
                              │ conforms to Protocol
                              │
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1: Adapter (in `infra/<layer>/_<sub>/`)                   │
│   - Translates third-party exceptions to `<Layer>Error`          │
│   - Translates the third-party's empty/None/raise to `Result`    │
│   - The ONLY place that imports the third-party library         │
└─────────────────────────────────────────────────────────────────┘
```

The single-source-of-truth for the `*Error` family is the Protocol module. Adapters import the `*Error` family from the Protocol and raise only those. Call sites import the `*Error` family from the Protocol and pattern-match on the concrete subclasses.

### 2.2 The `RedisAdapterError` family is the Redis layer's contract

`src/kntgraph/infra/redis/_errors.py` already defines the family. This ADR formalises it as the **complete set of exceptions the Redis adapters may raise**, and renames the module-private `_errors.py` → `_protocol.py` to make the family part of the public contract.

```python
# src/kntgraph/infra/redis/_errors.py — frozen for the lifetime of this ADR
class RedisAdapterError(Exception):
    """Base for every error the Redis adapter layer raises.

    Call sites that need to handle the entire failure surface
    of a Redis adapter (e.g. an operator-facing recovery loop)
    catch this. Call sites that need to distinguish miss from
    decode-error from transport-error catch the concrete
    subclasses below.
    """

class RedisUnavailableError(RedisAdapterError):
    """Connection lost, timeout, or pool exhausted.

    Translates redis.asyncio.TimeoutError, ConnectionError,
    OSError, and redis.exceptions.ConnectionError at the
    adapter boundary (see §2.3).
    """

class IdempotencyConflict(RedisAdapterError):
    """A concurrent writer holds the placeholder for this key."""

class MemoryError(RedisAdapterError):
    """Base for short-memory cache errors."""

class MemoryDecodeError(MemoryError):
    """The cached payload was malformed (corrupt JSON, etc.)."""

class MemorySerializationError(MemoryError):
    """The record could not be serialised to the cache wire format."""

class MemoryMiss(MemoryError):
    """The key was not present in the cache (clean miss)."""
```

The file is **not renamed** to `_protocol.py` because that would be a noisy import-path change. The family is *used as* a Protocol contract; its module name is just a private naming convention from ADR-019 that does not need to be revisited here. §3.3 documents this explicit decision.

### 2.3 Adapters translate third-party exceptions, in *one place each*

The translation table the Redis adapters commit to:

| Third-party exception | Translated to | Adapter site |
|---|---|---|
| `redis.exceptions.ConnectionError` | `RedisUnavailableError` | Every `await self.client.<op>` |
| `redis.exceptions.TimeoutError` | `RedisUnavailableError` | Every `await self.client.<op>` |
| `asyncio.TimeoutError` (the Python builtin raised by `wait_for`) | `RedisUnavailableError` | The `aclose` / `wait` paths |
| `ConnectionError` (Python builtin, OS-level) | `RedisUnavailableError` | Every `await self.client.<op>` |
| `OSError` (the broader network-failure category) | `RedisUnavailableError` | Every `await self.client.<op>` |
| `ValueError` / `TypeError` (from JSON / msgpack decode) | `MemoryDecodeError` | The codec boundaries |
| `KeyError` (from `dict` access on a payload that was None) | `MemoryDecodeError` | The codec boundaries |
| `asyncio.CancelledError` | **NOT caught** — propagates | (everywhere; see §2.5) |
| `KeyboardInterrupt` / `SystemExit` | **NOT caught** — propagates | (everywhere; see §2.5) |

The **shape** of the translation is always the same:

```python
async def get_record(self, key: str) -> Result[Mapping[str, JsonValue], MemoryError]:
    try:
        raw = await self.client.get(key)
    except (redis_exceptions.RedisError, ConnectionError, asyncio.TimeoutError, OSError) as exc:
        return Err(RedisUnavailableError(f"redis get({key!r}): {exc}"))
    except (ValueError, TypeError) as exc:
        return Err(MemoryDecodeError(f"decode {key!r}: {exc}", key=key))
    if raw is None:
        return Ok(None)  # clean miss
    return Ok(decode_value(raw))
```

This is the **one place per adapter** where the third-party exception is named. Every other call site in the framework sees `Result[Mapping[str, JsonValue], MemoryError]` and pattern-matches on `RedisUnavailableError` vs `MemoryDecodeError` vs `MemoryMiss` without an `except` clause.

### 2.4 Consumers trust the Protocol — `try/except` is deleted from layer 3

`BaseShortTermMemory._read_fold_cursor` becomes:

```python
# before (BLE001 + # noqa + log-and-swallow):
async def _read_fold_cursor(self, key: str) -> str | None:
    try:
        return await self._storage.read_fold_cursor(self._fold_cursor_key(key))
    except Exception as e:  # noqa: BLE001
        logger.warning("short_term.fold_cursor.read_failed", key=key, error=str(e))
        return None

# after (typed + trust the Protocol):
async def _read_fold_cursor(
    self, key: str
) -> Result[str | None, MemoryError]:
    return await self._storage.read_fold_cursor(self._fold_cursor_key(key))
```

The consumer's contract changes from `str | None` (with "None on transport error" semantics) to `Result[str | None, MemoryError]`. A transport error is now an `Err(RedisUnavailableError(...))`; a missing cursor is `Ok(None)`. The two cases are no longer collapsed by a silent log.

`BaseShortTermMemory._write_fold_cursor`, `SessionManager.list_active`, `ProfileManager.list_for_tenant`, `ContinuityManager.list_for_tenant`, and `DeadLetterQueue.append` (the agent-index branch) follow the same translation. Their current `try/except Exception` (logged + swallowed / typed `Err`) becomes **just the `Result`-checking branch**, with no `try`/`except` keyword at all.

### 2.5 The `asyncio.CancelledError` exception is explicitly NOT caught

`asyncio.CancelledError` inherits from `BaseException` in Python 3.8+, not from `Exception`, so the existing `except Exception` already lets it through. This ADR codifies that the **adapter translation table (§2.3) catches only `Exception` and its subclasses, never `BaseException`**. Catching `BaseException` would swallow `KeyboardInterrupt` and `SystemExit`, breaking operator-driven shutdown and any future cooperative-cancellation protocols (ADR-075 §1.3 row #4 documents the dispatcher's `asyncio.CancelledError` semantics; we must not break them).

The previous `except Exception` at the call sites implicitly honoured this rule. The translation table at the adapter layer makes it explicit.

### 2.6 `try/except` at the adapter layer is exhaustive — no `# noqa`

The adapter's `try/except (redis_exceptions.RedisError, ConnectionError, asyncio.TimeoutError, OSError) as exc:` block is the **only place in the framework that catches those third-party exceptions**. The exception tuple is exhaustive of every transport failure mode the framework needs to translate. `ruff`'s `BLE001` rule does not fire because the catch is *narrow*, not blind.

There is **no** `# noqa: BLE001` in any adapter after this ADR lands. The six suppressions listed in §1.2 are removed.

### 2.7 The `BaseShortTermMemory._write_fold_cursor` happy-path changes from `return Ok(None)` to `return Ok(...)` (no change in the call site)

The current implementation:

```python
async def _write_fold_cursor(
    self, key: str, cursor: str
) -> Result[None, PersistenceError]:
    cursor_key = self._fold_cursor_key(key)
    ttl = self._ttl if self._ttl and self._ttl > 0 else None
    try:
        await self._storage.write_fold_cursor(cursor_key, cursor, ttl_seconds=ttl)
        return Ok(None)
    except Exception as e:  # noqa: BLE001
        logger.warning(...)
        return Err(PersistenceError(...))
```

Becomes:

```python
async def _write_fold_cursor(
    self, key: str, cursor: str
) -> Result[None, MemoryError]:
    cursor_key = self._fold_cursor_key(key)
    ttl = self._ttl if self._ttl and self._ttl > 0 else None
    return await self._storage.write_fold_cursor(cursor_key, cursor, ttl_seconds=ttl)
```

The error type narrows from `PersistenceError` to `MemoryError` because the base class is now passing through the adapter's `Result` rather than translating a third-party exception itself. `PersistenceError` remains the right type for **EventLog** failures (it is the EventLog's own contract — see `src/kntgraph/stream/event_log/store.py`), and a future ADR may add an `EventLogError` for symmetry, but that is out of scope here.

### 2.8 `list_active` / `list_for_tenant` change from `try/except` over `iter_keys` to `Result`

The current implementations translate a third-party exception to `PersistenceError` after a `try/except Exception` over the storage's `iter_keys` call. The new implementation:

```python
async def list_active(
    self, tenant_id: str, limit: int = 100
) -> Result[list[SessionState], MemoryError]:
    out: list[SessionState] = []
    prefix = SESSION_KEY_PREFIX  # the per-tier key prefix
    async for key in self._storage.iter_keys(prefix):
        sid = key[len(prefix) :]
        cache_result = await self._read_cache(self.cache_key(sid), sid)
        if cache_result.is_err():
            continue
        state = cache_result.ok_value()
        if state and state.tenant_id == tenant_id and state.is_active():
            out.append(state)
        if len(out) >= limit:
            break
    return Ok(out)
```

No `try/except` at all. The transport error surfaces as `Err(MemoryError(...))` from `iter_keys` (or, if the Protocol ever drops the `Result` shape, from the `iter_keys` Protocol's call to `await self.client.scan_iter(...)` — the translation in §2.3 is the only place that catches).

The new error type is `MemoryError` (was `PersistenceError`) for the same reason as §2.7.

### 2.9 The `_drop_entry` translation widens to `Result`

Already compliant with the new contract (it already returns `Result[None, PersistenceError]`; the underlying `read_index` / `drop_entry` / `bump_reason_counter` are typed `Result[... , MemoryError]` and call sites propagate). This ADR does not change the actions.py surface; it removes the `except Exception` only where it currently exists in the DLQ facade (the agent-index HSETNX in `store.py:148`).

### 2.10 The `DLQException` carry-over: the agent-index HSETNX is best-effort by design

`DeadLetterQueue.append`'s agent-index HSETNX (`src/kntgraph/events/dlq/store.py:148`) is wrapped in `try/except Exception` because the **agent index is a hint, not a source of truth**: the DLQ stream entry and the per-event_id index are the durable data, the agent index is the lookup optimisation. A failure on the agent index must not abort the append.

After this ADR, the `try/except Exception` becomes `try/except (redis_exceptions.RedisError, ConnectionError, asyncio.TimeoutError, OSError) as exc:` — narrow, typed, no suppression. The translation is `Err(RedisUnavailableError(...))` and the facade logs and continues (because the per-entry index already succeeded; the agent-index failure is a cache-coherence issue, not a durability issue).

The same shape applies to the two other "best-effort hint" sites in `append`: the per-reason counter bump (line 130) and the inner `bump_reason_counter` (line 187 of actions.py). Both follow the same `try/except <narrow>` + log + continue pattern.

---

## 3. Consequences

### 3.1 Positive

- **`ruff check` is clean for the 6 sites in §1.2.** No more `# noqa: BLE001` in the memory/vertical, the DLQ facade, or the runner. The lint is a real signal again, not a confession.
- **Call sites get the right error type.** `list_active` returning `Result[list[SessionState], MemoryError]` (was `Result[..., PersistenceError]`) lets a `SessionManager`-aware caller distinguish a transport failure from a Session-state validation failure. Today both collapse to `PersistenceError`, which is too coarse for a dashboard that needs to alert on different signals.
- **The Protocol is a real contract.** `ShortMemoryStorage.get_record` documented as `Result[Mapping[str, JsonValue], MemoryError]` is enforced: pyright narrows the return, and the runtime guarantees it (because the adapter translates at the boundary). A new call site that does `try/except Exception` around a Protocol call is a code-review red flag, not a code-review no-op.
- **The agent index HSETNX failure mode is observable.** Today, `except Exception` + `logger.warning(...)` swallows the traceback; the metrics sink only sees "DLQ append was slow". After this ADR, the `Err(RedisUnavailableError(...))` is logged with the same `warning` level but with a structured payload that the dashboard can match against `redis_exceptions.RedisError` class names.
- **`asyncio.CancelledError` propagation is documented as a guarantee.** The translation table in §2.3 spells out the catch list, so future code reviewers do not "improve" the adapter by adding a blanket `except Exception` and accidentally swallowing cancellation.

### 3.2 Negative (and the explicit non-regression note)

- **The base class's `read` / `refresh_cache` / `_write_fold_cursor` return type narrows from `PersistenceError` to `MemoryError` (or to `MemoryError` / `RedisAdapterError` more broadly).** This is a contract change for any consumer that pattern-matches on the concrete error subclass. The audit (§4 of the typing audit) found exactly **zero** call sites in the framework that pattern-match on `PersistenceError` from these surfaces, so the change is safe in-tree. External consumers (downstream services that import `kntgraph.memory.base.BaseShortTermMemory`) **do** see a narrower contract; per AGENTS.md §2, this is acceptable because the framework does not promise API stability across minor versions.
- **The `try/except` is now *required* at the adapter layer.** A contributor who adds a new storage call to a Redis adapter without wrapping it in the translation table will see `ruff` flag the missing catch (BLE001) — but only if `ruff` can see the third-party import. The mitigation: every adapter file is small (<500 LOC, per ADR-019) and has a `_redis.py` review checklist. The CI gate does not yet enforce this; see §5 commit 3.
- **Some `except Exception` sites remain — by design.** `src/kntgraph/agents/role_systems/_rule_based.py` catches `Exception` because the YAML parser is a different layer's problem (the YAML file is the boundary, the third-party `yaml.safe_load` raises a YAML-specific family). `src/kntgraph/stream/event_log/codec.py` catches `Exception` around `json.dumps` for the same reason. These are **not** the framework's storage layer; they are language-runtime concerns. The same pattern applies: a narrow `except (json.JSONDecodeError, TypeError, ValueError)` is correct; the existing `except Exception` there is a pre-existing debt, tracked separately.
- **The `agent_index_failed` log entry in §2.10 keeps the legacy `warning` level.** Operators familiar with the existing log line do not need to re-tune their alert thresholds. The structured payload gains a `class` field; the human-readable `error` field keeps the same format.

### 3.3 `_errors.py` is renamed in *intent*, not in *path*

§2.2 mentions renaming `_errors.py` → `_protocol.py` to make the family part of the public contract. The rename is **rejected** for this ADR:

- The `_errors.py` module is referenced by ~30 sites across `core/`, `infra/`, `memory/`, `events/`, and tests. Renaming touches every one of them, with no behaviour change.
- The `_` prefix is a private-naming convention from ADR-019 §2.1; it is enforced at the import-graph level (the `infra/redis/_errors.py` file is not re-exported from `infra/redis/__init__.py`; downstream code imports the class objects directly via `from kntgraph.infra.redis._errors import MemoryError`).
- The rename is a separate concern from this ADR's content. A follow-up ADR (or a section in a future ADR-019 amendment) can rename the file when the next major-version migration is on the table.

The "Protocol-shaped" intent is documented in §2.1 and §2.2. Code that depends on the family uses it as a Protocol; the file's name is a historical artifact.

### 3.4 What this ADR does NOT cover

- **The FalkorDB graph adapter's exception type.** `src/kntgraph/infra/graph/_adapter.py` imports `GraphError` from the knowledge vertical. The same fix applies (declare the error type in `infra/graph/_protocol.py`; have the vertical re-export). It is a separate ADR-sized change because it touches the framework→vertical boundary, not the storage layer.
- **The `stream/event_log/codec.py` `except Exception`.** The codec is the wire-format boundary, not the storage boundary. The fix (narrow the catch to `json.JSONDecodeError, TypeError, ValueError`) is a 4-line change but is out of scope for this ADR.
- **The `tools/manager.py` worker consume loop `except Exception`.** The worker dispatcher is a different layer (the tool-execution event loop, not the storage layer). The same principle applies — a future ADR can document the dispatcher's catch list. Out of scope here.
- **Replacing `redis.asyncio.Redis` with a custom `RedisLike`.** ADR-019 already did this (the `RedisLike` Protocol is the storage boundary). This ADR is a vertical slice of the same principle, applied to the exception family instead of the API surface.

---

## 4. Alternatives considered

### 4.1 Catch `Exception` everywhere with `# noqa: BLE001` and document the rationale

Keep the current `except Exception` + suppression pattern. The lint's complaint is "you are catching too much"; the response is "yes, and we mean to".

Rejected:

- The codebase has the `*Error` hierarchy in place. Catching `Exception` means the hierarchy is decoration, not contract.
- The `# noqa` count grows with every new boundary. The `DEBT.md` would accumulate entries that the team never pays down.
- Operators lose the signal: the agent-index HSETNX failure (`store.py:148`) is logged at `warning` because the only alternative is `raise`, and the current implementation does not want to abort the append. With the typed contract, the same behaviour is achievable with a narrow `except (redis_exceptions.RedisError, ...)` — the operator gets the same `warning` log, the call site loses the suppression, and the lint stays clean.

### 4.2 Catch `BaseException` instead of `Exception`

Defensive: "catch literally everything, including cancellation, and log it".

Rejected:

- This is a *worse* anti-pattern. `BaseException` includes `KeyboardInterrupt`, `SystemExit`, and `asyncio.CancelledError`. Catching these at the adapter layer would break operator-driven shutdown and any cooperative-cancellation protocol (ADR-075 §1.3 row #4).
- It also does not solve the BLE001 lint (the rule fires on `Exception`, not on the breadth of the catch; `except BaseException` would still trigger the broader `BLE001` family if the rule is re-tuned, and would not trigger `BLE001` today but would silently swallow critical signals).

### 4.3 Define a `BroadException` mixin to mark "intentionally broad" catches

Add a marker class that the lint can detect:

```python
class BroadException(Exception):
    """Marker for catch sites that intentionally swallow
    transport errors. Lint-recognised; do NOT use outside
    the storage adapter layer."""

try:
    ...
except (redis_exceptions.RedisError, BroadException) as exc:
    ...
```

Rejected: introduces a new type for the sole purpose of teaching the lint about it. The cleaner answer is "make the catch narrow" — the `*Error` hierarchy already exists.

### 4.4 Drop the `Result` return on storage calls; raise `MemoryError` and let `try/except` filter at the call site

Revert the Result-on-Result design; raise typed exceptions; consumers catch.

Rejected: inverts the framework's Railway Pattern. The Result path is the *expected* failure; the exception path is the *unexpected* crash. A storage transport failure is expected (Redis goes down; the worker re-queues; the dispatcher recovers), so it belongs in `Result`, not in `raise`. ADR-005 and the `kntgraph-typed-errors` skill codify this principle; this ADR does not reopen it.

### 4.5 Use `ExceptionGroup` (Python 3.11+) to raise the typed family atomically

Adapter raises `ExceptionGroup(RedisUnavailableError(...), MemoryDecodeError(...))` on multi-step failures; consumers handle with `except*`.

Rejected: the adapters are not multi-step. `get_record` is one Redis call; it either succeeds or fails with one cause. The `ExceptionGroup` machinery is for the rare "two sub-operations both failed" case (e.g. the world-checkpoint XADD + the cursor HSET) and is not the common path. The right answer for the rare case is "the first Err short-circuits the second" — which is what the current `Result` chain already does. Adopt `ExceptionGroup` only when the framework has a concrete multi-failure code path; the current audit found none.

---

## 5. Migration sequencing

Four commits, gate green at each step. The branching policy (`AGENTS.md` §11, see also `kntgraph-branch-policy` skill) is followed by the human reviewer; the AI agent stages and proposes the changes.

### Commit 1 — `_errors.py` documents the contract; `# noqa` removed from the 6 sites

**Goal:** the exception family becomes the named contract; the storage Protocol gains `-> Result[..., MemoryError]` (or the appropriate family) for the methods that did not already have it; the call sites lose the suppression.

Scope:

- `src/kntgraph/infra/redis/_errors.py`: docstring on `RedisAdapterError` is updated to the §2.2 wording. No code change.
- `src/kntgraph/memory/base.py`:
  - `ShortMemoryStorage` Protocol (re-imported via `TYPE_CHECKING`) gains the typed `Result[...]` return for `read_fold_cursor` and `write_fold_cursor` (if not already).
  - `_read_fold_cursor` and `_write_fold_cursor` lose the `try/except`; the return type narrows from `str | None` / `None` to `Result[str | None, MemoryError]` / `Result[None, MemoryError]`.
  - Call sites in `refresh_cache` and `refresh_cache_incremental` translate the new `Err(MemoryError)` to the existing `Err(PersistenceError(...))` surface (the public API of `refresh_cache` keeps `Result[None, PersistenceError]`).
- `src/kntgraph/memory/{session,profile}.py`, `src/kntgraph/memory/continuity/manager.py`: `list_active` / `list_for_tenant` lose the `try/except`; the return type narrows to `Result[list[StateT], MemoryError]`. Call sites in `consolidation.py` and the runner do not need to change (they don't pattern-match on the concrete error).
- `src/kntgraph/events/dlq/store.py:148`: the `except Exception` becomes `except (redis_exceptions.RedisError, ConnectionError, asyncio.TimeoutError, OSError) as exc:` and the log emits `class=type(exc).__name__` alongside `error=str(exc)`.
- All six `# noqa: BLE001` are removed in the same commit.

Acceptance:

- `ruff check` reports zero `BLE001` on the touched files.
- `pytest tests/unit/memory tests/integration/memory tests/unit/events tests/integration/test_dlq.py tests/integration/test_dlq_writer_e2e.py` is green (≥837 tests, the same baseline that the wrap-in-Result PR landed).
- A new `tests/unit/memory/test_base_short_term.py` test pins the new contract: `_read_fold_cursor` returns `Ok(None)` for a missing cursor, `Err(MemoryError(...))` for a transport failure (mocked via a fake `ShortMemoryStorage` whose `read_fold_cursor` raises `ConnectionError`).

### Commit 2 — Adapter translation in the production code

**Goal:** the Redis adapter layer (the `infra/redis/_*/*.py` files) does the third-party→`MemoryError` translation in the methods that currently lack it.

Scope:

- `src/kntgraph/infra/redis/_memory/_session.py`, `_profile.py`, `_continuity.py`, `_solution.py`: every `await self.client.<op>` is wrapped in the §2.3 translation table. Existing `except` sites are unified to the narrow `redis_exceptions.RedisError, ConnectionError, asyncio.TimeoutError, OSError` family. The `asyncio` import is added to the few files that do not already have it.
- `src/kntgraph/infra/redis/_event_log/_adapter.py`: same shape (the `idempotency.py` helper already raises `IdempotencyConflict`; the only missing translation is the `redis.exceptions.RedisError` arm on the `XADD` / `XREADGROUP` calls).
- `src/kntgraph/infra/redis/_dlq/_redis.py`: same shape. The `HGET` / `HGETALL` / `XADD` / `XLEN` calls get the translation; the existing `XADD`-PLACEHOLDER handling stays.
- `src/kntgraph/infra/redis/_checkpoint/_redis.py` and `src/kntgraph/infra/redis/_world_checkpoint/_redis.py`: same shape (these adapters are smaller; most paths already raise through the typed `Result`).
- `src/kntgraph/infra/redis/_auth/_redis.py`: same shape.

Acceptance:

- `ruff check` is clean for `src/kntgraph/infra/redis/`.
- All existing tests green.
- A new `tests/unit/infra/redis/test_exception_translation.py` pins the contract: a fakeredis client that raises `redis.exceptions.ConnectionError` on `GET` results in `Err(RedisUnavailableError(...))` (not in a bare `RedisError` propagating up). Same for `ConnectionError` (Python builtin), `asyncio.TimeoutError`, and `OSError`. A fakeredis client that raises `KeyboardInterrupt` does NOT result in an `Err(...)`; the `KeyboardInterrupt` propagates and fails the test (the adapter does not catch it).

### Commit 3 — TRY004: TypeError vs ValueError + `# noqa: TRY004` removed

**Goal:** the two `raise ValueError("...is not a list/dict")` sites in `src/kntgraph/memory/session.py` are reverted to `TypeError` (Python convention; the lint knows this), and the two tests that pinned `ValueError` are updated.

Scope:

- `src/kntgraph/memory/session.py:637`: `raise ValueError("messages is not a list")` → `raise TypeError("messages is not a list")`. Remove the `# noqa: TRY004`.
- `src/kntgraph/memory/session.py:644`: `raise ValueError("context is not a dict")` → `raise TypeError("context is not a dict")`. Remove the `# noqa: TRY004`.
- `tests/unit/memory/test_managers_unit.py:570,581`: `pytest.raises(ValueError, ...)` → `pytest.raises(TypeError, ...)`. The docstrings ("raises ValueError when messages is not a list") are updated to the correct exception name.

Acceptance:

- `ruff check` is clean for `src/kntgraph/memory/session.py` and the two test functions.
- The two tests are green.

### Commit 4 — DEBT entry + CHANGELOG

**Goal:** the work is discoverable.

Scope:

- `DEBT.md`: a new entry under the open section documenting the migration. The entry cross-references ADR-077 and lists the 6 sites that were touched in commit 1 + the 2 sites in commit 3, with the test that pins the new contract.
- `CHANGELOG.md`: an `[Unreleased]` entry noting "storage adapters now translate third-party exceptions to the typed `RedisAdapterError` family at the boundary; consumers of the memory and DLQ facades see narrower error types in the `Result` channel". The entry cross-references ADR-077.
- `AGENTS.md` §6: the new §6.3 "Exception translation lives at the adapter boundary" is added. The text is the executive summary of this ADR (the principle, the catch list, the `asyncio.CancelledError` guarantee).

Acceptance:

- `DEBT.md` entry is added.
- `CHANGELOG.md` entry is added.
- `AGENTS.md §6.3` is added.
- The CI gate (ADR-019 §2.5) is green.

---

## 6. Test plan

### 6.1 New unit tests (fakeredis, `KNT_REDIS_FAKE=1`)

- `tests/unit/infra/redis/test_exception_translation.py` — the adapter translation contract (commit 2):
  - `RedisSessionStorage.get_record` returns `Err(RedisUnavailableError(...))` when the underlying `redis.asyncio.Redis.get` raises `redis.exceptions.ConnectionError` (mocked with a side-effect).
  - Same for `redis.exceptions.TimeoutError`, `ConnectionError` (Python builtin), `asyncio.TimeoutError`, `OSError`.
  - Same for `RedisSessionStorage.put_record` (write path).
  - `RedisSessionStorage.get_record` returns `Err(MemoryDecodeError(...))` when the underlying decode raises `ValueError` (e.g. corrupt JSON).
  - `RedisSessionStorage.get_record` does NOT catch `KeyboardInterrupt`: a mocked `KeyboardInterrupt` propagates (the test uses `pytest.raises(KeyboardInterrupt)`).
  - `RedisSessionStorage.get_record` does NOT catch `asyncio.CancelledError`: same shape.
- `tests/unit/memory/test_base_short_term.py` (extension, commit 1):
  - `_read_fold_cursor` returns `Ok(None)` for a missing cursor (clean miss).
  - `_read_fold_cursor` returns `Err(MemoryError(...))` when the storage raises `ConnectionError` (mocked via a fake `ShortMemoryStorage`).
  - `_write_fold_cursor` returns `Err(MemoryError(...))` on the same shape.
  - `BaseShortTermMemory.refresh_cache` propagates the new `Err(MemoryError)` as `Err(PersistenceError(...))` (the public API is unchanged; the translation is internal).

### 6.2 Coverage gate

Per `AGENTS.md` §7 (`kntgraph-testing` skill): every new public function (the three `RedisAdapterError` subclasses' docstring examples, the adapter translation wrappers) gets the happy-path-plus-one-failure-mode test. Branch coverage on `infra/redis/` remains above the per-vertical baseline (re-checked at commit 2; if it regresses, update `verticals-baseline` per the existing process).

### 6.3 Lint and type-check gate

- `ruff check src/kntgraph/` is clean.
- `pyright` (per `pyrightconfig.json`) is clean.
- The two `pytest.raises(ValueError, ...)` tests in `tests/unit/memory/test_managers_unit.py:570,581` are updated to `pytest.raises(TypeError, ...)`; the test docstrings are updated; the test names are unchanged (the *behaviour* is the same; the exception class is what changes).

---

## 7. References

- [ADR-019](./ADR-019-Redis-Adapter-Typing.md) — typed Redis adapters; the `infra/redis/` sub-adapter organisation this ADR reuses and extends. ADR-019 established *what* the adapter layer is; this ADR establishes *which exceptions the layer raises*.
- [ADR-076](./ADR-076-Service-Scoped-Redis-Key-Prefix.md) — namespace prefix plumbing; uses the same `Result` translation surface this ADR codifies.
- [AGENTS.md §1](../AGENTS.md) — type discipline; `JsonValue` is the framework's JSON-shaped union; `RedisAdapterError` is the storage layer's "all transport failures" base class.
- [AGENTS.md §6](../AGENTS.md) — errors are typed. §6.2 (mutating operations return `Result`) is the principle this ADR extends to the exception layer.
- [AGENTS.md §11](../AGENTS.md) — branch policy; the four-commit migration in §5 follows the AI-stages-human-reviews model.
- [`.agents/skills/kntgraph-typed-errors/SKILL.md`](../.agents/skills/kntgraph-typed-errors/SKILL.md) — the existing skill that mandates `Result` for mutating ops. This ADR extends the same principle to the exception layer.
- [`.agents/skills/kntgraph-type-discipline/SKILL.md`](../.agents/skills/kntgraph-type-discipline/SKILL.md) — `JsonValue` discipline, `TYPE_CHECKING` for type-only imports. The adapter Protocol in this ADR reuses the same `TYPE_CHECKING` pattern from §1.5 of the skill.
- [`.agents/skills/kntgraph-ci-gate/SKILL.md`](../.agents/skills/kntgraph-ci-gate/SKILL.md) — the CI gate this PR feeds into. The `ruff check` step is the first stage that catches the BLE001 violations this ADR removes.
- [Python docs — `Exception` hierarchy](https://docs.python.org/3/library/exceptions.html#exception-hierarchy) — `asyncio.CancelledError` inherits from `BaseException` in 3.8+; the `except Exception` idiom honours this implicitly; the translation table in §2.3 makes it explicit.
- [PEP 654 — Exception Groups](https://peps.python.org/pep-0654/) — considered in §4.5; rejected for this ADR because the storage adapters are single-step.
