<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-082: Promote Redis transport defense-in-depth to framework defaults; absorb the backoffice `resilience_patch.py`

- **Status:** Proposed
- **Date:** 2026-10-13
- **Author:** kinetgraph architecture team (proposed by the backoffice engineering team, see §1.4)
- **Supersedes:** (none)
- **Related to:**
  - [ADR-068](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md) — wake-up primitive (`_log.subscribe`); the call site §3.2 hardens. The cadence / `block_ms` invariant stays as ADR-068 §3.2 fixed it.
  - [ADR-077](./ADR-077-Storage-Adapter-Exception-Translation.md) — narrow-catch pattern at the storage adapter boundary. §3.1 of this ADR extends the same principle ("translate the third-party transport exception at the boundary, then re-raise the framework-typed error") from *storage adapters* to *long-running consumers*. The two are orthogonal: ADR-077 catches the failure; this ADR prevents the consumer from going silent after the catch.
  - [ADR-054](./ADR-054-WorkerManager-Transport-Evaluation.md) — the original transport evaluation that rejected the `ProcessPoolExecutor` concern; reopens the *transport-resilience* concern this ADR ratifies.
  - [ADR-066](./ADR-066-Single-Tool-Path.md) — three-gate ACL on `register()`; untouched by this ADR.
  - [ADR-078](./ADR-078-Decommision-ProcessPoolExecutor.md) — the parallel workstream from the same post-mortem (process-pool OOM); the executor-factory migration. This ADR is the *transport-resilience* twin — both workstreams were triggered by the same 2026-10-07 incident, both remain in a backoffice fork until upstream merges, both target the same goal: let `soldi/backoffice` consume `main` unmodified.
  - [`kntgraph-type-discipline`](../.agents/skills/kntgraph-type-discipline/SKILL.md) §9 — the executor-factory ratchet. §3.1 of this ADR tightens the helper signature from `redis: Any` to `redis: RedisLike`, which is a ratchet step on the `Any` axis.
  - [`kntgraph-deprecation-policy`](../.agents/skills/kntgraph-deprecation-policy/SKILL.md) §2.1 — the standard one-minor deprecation cycle. §3.3 of this ADR follows it for the `reconnect_redis_after_runtime_error` → `reconnect_redis_after_transport_error` rename.
  - [`kntgraph-typed-errors`](../.agents/skills/kntgraph-typed-errors/SKILL.md) §6.1 — the never-`raise-Exception` rule. §3.2 of this ADR replaces `except Exception` with an explicit transport-class tuple.
  - [`kntgraph-file-layout`](../.agents/skills/kntgraph-file-layout/SKILL.md) §3.3 — the public/private boundary. §3.2 of this ADR exposes `close_transport()` on the `EventLogStorage` Protocol instead of reaching through `getattr(self._log, "_redis")`.
  - [`kntgraph-testing`](../.agents/skills/kntgraph-testing/SKILL.md) §7.2 — happy-path-plus-one-failure-mode. §5.3 of this ADR lists the failure-mode coverage for every new branch.
  - External: `soldi/backoffice/docs/ADRs/adr-013-fork-kntgraph-para-correcoes-de-oom.md` — the backoffice's ADR that opened both workstreams. The §"Resilience scope summary" sub-section enumerates the exact mechanisms this ADR promotes.

---

## 1. Context

### 1.1 The 2026-10-07 post-mortem — two workstreams, one incident

The backoffice service hit an OOM on a 4 GB Fargate task on 2026-10-07
(referenced from `src/kntgraph/tools/manager.py:50-52`, `:204`,
`:432-435`, full post-mortem text in
`soldi/backoffice/docs/ADRs/adr-013-fork-kntgraph-para-correcoes-de-oom.md`).
The post-mortem surfaced **two** independent root causes:

| Workstream | Root cause | Status today |
|---|---|---|
| **A. Process-pool OOM** (ADR-013 §"Process pool" / ADR-078) | 13 internal `ProcessPoolExecutor` workers each imported `torch` + `gliner` + `docling`; peak RSS crossed the 4 GB threshold. | **Resolved upstream** by commit `fe6ba89` (PR #63, 2026-10-10) + ADR-078 v0.18 step 1. Backoffice consumes the fix via `executor_factory=` (see ADR-078 §3.3 the backoffice `ModelWorkerPool` sketch). |
| **B. Redis transport resilience** (this ADR) | Long-running consumers (`WorkerManager._consume_loop`, `ReactiveDispatcher._wake_once`) silently stalled after a half-open TCP connection or a Redis-side idle timeout, because the framework's reconnect logic covered only `RuntimeError`. | **Partially resolved upstream** (8 of 9 mechanisms, see §1.3). **3 residual mechanisms** still require `soldi/backoffice/src/backoffice/infra/resilience_patch.py` (227 LOC of monkey-patches). |

This ADR ratifies workstream B.

### 1.2 The backoffice patch — what it does, and why it has to go

`soldi/backoffice/src/backoffice/infra/resilience_patch.py:1-227`
implements three classes of monkey-patch:

1. **`RedisPool.from_settings` override** — adds `health_check_interval=15` and (historically) `socket_connect_timeout=5` / `socket_keepalive=True` / `retry_on_timeout=True`.
2. **`WorkerManager._consume_loop` rewrite** — adds an `asyncio.Semaphore(max_concurrency)` per tool, `in_flight: set[asyncio.Task]` for graceful shutdown, `read_batch_size = max(1, min(max_concurrency, 32))`, `block=1000`, `_maybe_emit_heartbeat` on idle reads, and a *wide* transport catch (`RuntimeError`, `ConnectionError`, `OSError`, plus the textual indicators `"closed"` and `"transport"`) that proactively calls `aclose()` on the Redis client and re-resolves the connection pool.
3. **`ReactiveDispatcher._wake_once` rewrite** — same wide-transport catch, plus proactive `aclose()` on the Redis client when `_log.subscribe` fails.

The patch is **not** a feature: it is a back-port of behaviour the
framework should ship by default. The backoffice team has been
maintaining it as a temporary measure since 2026-10-07, with the
explicit goal of deleting it once upstream absorbs each mechanism.
That goal is now blocked on the 3 residual items in §1.3.

### 1.3 What `fe6ba89` (PR #63) absorbed — and what is still missing

The commit `fe6ba89` ("feat: add socket timeout configuration and
handle dead socket scenarios", PR #63, 2026-10-10) absorbed **8 of the
9** mechanisms the patch implemented. The diff against the patch's
pre-`fe6ba89` reference is:

| Mechanism | Pre-`fe6ba89` location | Post-`fe6ba89` location | Status |
|---|---|---|---|
| `asyncio.Semaphore(max_concurrency)` in `_consume_loop` | `resilience_patch.py:75` | `manager.py:543` | ✓ Upstream |
| `_reaper_loop` reuses same semaphore (ADR-013 §"Reaper fix") | `resilience_patch.py:75` | `manager.py:884, 906, 919` | ✓ Upstream |
| `in_flight: set[asyncio.Task]` + `gather(..., return_exceptions=True)` | `resilience_patch.py:82, 141-142` | `manager.py:556, 586, 615-616` | ✓ Upstream |
| `read_batch_size = max(1, min(max_concurrency, 32))` | `resilience_patch.py:76` | `manager.py:550` | ✓ Upstream |
| `block=1000` on `xreadgroup` | `resilience_patch.py:91` | `manager.py:566` | ✓ Upstream |
| `_maybe_emit_heartbeat` on idle `xreadgroup` | `resilience_patch.py:96` | `manager.py:577-578` | ✓ Upstream |
| `RuntimeError` → `reconnect_redis_after_runtime_error` | `resilience_patch.py:119-137` | `_message_handlers.py:249-290` (called at `manager.py:610-611`) | ✓ Upstream |
| `socket_connect_timeout=5`, `socket_keepalive=True`, `retry_on_timeout=True`, `socket_timeout=15.0` | `resilience_patch.py:46-49` | `_pool.py:77-80` | ✓ Upstream |
| `asyncio.Semaphore(50)` + `gather(*_dispatch_sem)` in `_wake_once` | `resilience_patch.py:202-218` | `reactive.py:835-851` | ✓ Upstream |

The residual mechanisms (§3) are:

| Mechanism | Today (backoffice patch) | Upstream today | Why it is still residual |
|---|---|---|---|
| **Wide transport catch** (3.1) — `ConnectionError`, `OSError`, textual `"closed"`/`"transport"` | `resilience_patch.py:119-137` | `manager.py:610-611` only catches `RuntimeError` | `redis-py` raises `ConnectionError` and `OSError` on half-open TCP; some transport error messages surface `"closed"` / `"transport"` as substrings, not as exception class |
| **Proactive `aclose()` on `_wake_once` failure** (3.2) | `resilience_patch.py:178-192` | `reactive.py:812-823` only logs + fallback polls | Without proactive close, the next `_log.subscribe` reuses the broken connection and the dispatch latency spike propagates into the next 30 s of `_wake_once` cycles |
| **`health_check_interval=15` in `RedisPool.from_settings`** (3.3) | `resilience_patch.py:48` | `_pool.py:73-81` does not pass it; `redis-py` default is 0 = disabled | Without it, connections that have been idle longer than the kernel's TCP keepalive threshold (default 2 hours) report success on `PING` but fail on the next command (half-open TCP) |

### 1.4 Why now

The backoffice is **already** consuming `main` (post-`pyproject.toml:67-72`
on 2026-10-13). The fork is gone; the only reason `resilience_patch.py`
still exists is the 3 residual items above. Promoting them closes the
loop:

- The backoffice deletes `resilience_patch.py` and the `apply_resilience_patches()` call site (`soldi/backoffice/src/backoffice/main.py:117-119`), gaining ~227 LOC of deleted code and ~50 fewer ops per boot.
- Any other consumer running the framework against a remote Redis (with TCP-level middleboxes, NAT timeouts, or idle-connection reapers) inherits the same defense-in-depth without needing to read the post-mortem.

### 1.5 What this ADR is, and what it is not

This ADR ratifies the 3 residual mechanisms as **framework defaults**,
with one new Settings field (§3.4), one new module-private transport
predicate (§3.1), one new Protocol method (§3.2), and the standard
deprecation cycle on the helper rename (§3.3).
It is **not**:

- A new framework primitive. The `aclose()`, `pool.disconnect()`, `RedisUnavailableError`, and `RedisLike` Protocol already exist; the new artefacts (`is_transport_error`, `close_transport()`) compose them.
- A breaking change. The new catch covers strictly more than today; the new `health_check_interval` is opt-out via `KNT_REDIS_HEALTH_CHECK_INTERVAL=0` for operators who depend on the pre-ADR-082 behaviour; the renamed helper emits `DeprecationWarning` for one minor cycle.
- A claim that the existing patch is wrong. The patch was correct as a stop-gap; the goal is to retire it, not to invalidate the reasoning behind it.

### 1.6 Standards alignment

The §3 below applies five framework rules that an earlier draft of this ADR (ADR-082 §3.1 substring match, §3.2 `except Exception`, §3.2 `getattr` on `_redis`, §3.1 inline rename, `_message_handlers.py:249` `Any` parameter) would have broken. The §3 of this refined ADR applies each rule:

| Conflict | Skill / precedent | Resolution in §3 |
|---|---|---|
| Substring match on `str(e)` for `"closed"` / `"transport"` — false-positive on application-level `ValueError("transport pipeline not configured")` | `kntgraph-type-discipline` §1.1 (`Any`-discipline); redis-py exception classes are stable, message bodies are not | §3.1 — new `infra/redis/_transport.py` module with class-based `is_transport_error` predicate; no substring branch |
| `except Exception` + `noqa: BLE001` in `_wake_once` | `kntgraph-typed-errors` §6.1; precedent at `stream/event_log/dispatch.py:83` (`(ConnectionError, TimeoutError) → PersistenceError`) | §3.2 — explicit transport-class tuple; `asyncio.CancelledError` propagates; no `noqa` |
| `getattr(self._log, "_redis", None)` reaching through private attrs | `kntgraph-file-layout` §3.3 (`SLF001` symptom); precedent `Stream/event_log/store.py:373, 389, 392` already documents `_redis` reach-through as anti-pattern | §3.2 — new `EventLogStorage.close_transport()` Protocol method; EventLog forwards; dispatcher calls the typed method |
| `reconnect_redis_after_runtime_error` renamed in the same minor release | `kntgraph-deprecation-policy` §2.1 (standard one-minor `DeprecationWarning` then `git rm`) | §3.3 — v0.18 emits `DeprecationWarning` on the old name, v0.19 removes it |
| `async def reconnect_redis_after_runtime_error(redis: Any) -> None` | `kntgraph-type-discipline` §1.1 (`Any` ban in framework code); `RedisLike` Protocol at `infra/redis/_client.py` is the typed seam | §3.1 — parameter tightened to `redis: RedisLike` |

---

## 2. Decision

> **Promote the three residual transport-resilience mechanisms in `soldi/backoffice/src/backoffice/infra/resilience_patch.py` to framework defaults, with the §3 design below: a class-based transport predicate `is_transport_error` in a new module `infra/redis/_transport.py`; the wider catch in `_consume_loop` using that predicate and the existing `reconnect_redis_after_runtime_error` helper (signature tightened to `redis: RedisLike`, renamed to `reconnect_redis_after_transport_error` via the standard deprecation cycle in §3.3); the wider catch in `_wake_once` via a new `EventLogStorage.close_transport()` Protocol method (no `getattr` on `_redis`, explicit transport-class tuple — no `except Exception`); and a `redis_health_check_interval` setting (default 15 s) wired into `RedisPool.from_settings`. The widening of the catch is the principled extension of ADR-077 to the consumer boundary; the rename is the principled application of the deprecation policy; the new Protocol method is the principled application of the public/private boundary.**

The widening of the catch is the principled extension of
ADR-077 ("catch the third-party transport exception at the boundary,
then re-raise the framework-typed error"). ADR-077 catches the
failure in the *adapter*; this ADR catches the failure in the
*consumer*. The two together cover the full lifecycle: ADR-077 at the
write/read boundary, this ADR at the long-running read loop boundary.

---

## 3. The four changes

### 3.1 Wider transport catch in `_consume_loop` (class-based predicate)

**Today** (`src/kntgraph/tools/manager.py:610-611`):

```python
if isinstance(e, RuntimeError):
    await self._reconnect_after_runtime_error()
```

The `redis-py` library raises `redis.exceptions.ConnectionError`
(a subclass of `redis.exceptions.RedisError`, *not* of
`RuntimeError`) when the TCP socket is reset by a middlebox or
killed by an idle reaper. It raises `OSError` (e.g. `ECONNRESET`,
`EPIPE`) on broken pipes. Some versions emit the strings `"closed"`
or `"transport"` in the message body — but matching on the message
body is implicit duck typing on a string the framework does not
own and the redis-py library is free to change.

**Change (1):** introduce the single source of truth for transport
classification in a new module-private file
`src/kntgraph/infra/redis/_transport.py`. The underscore signals
"internal to the framework" (per `kntgraph-file-layout` §3.3); the
module is re-exported in `infra/redis/__init__.py` so verticals
writing custom transport classes can dispatch on it.

```python
# src/kntgraph/infra/redis/_transport.py (new)

"""
Transport-layer failure classifier.

The framework catches transport failures at two seams:

  - the storage-adapter boundary (ADR-077): the third-party
    ``redis-py`` exception is translated to ``RedisUnavailableError``
    (or to ``PersistenceError`` for ``EventLog.append``).
  - the consumer boundary (this ADR): the long-running
    ``_consume_loop`` and ``_wake_once`` paths catch the
    same exception classes and trigger a proactive
    ``close_transport()`` + ``pool.disconnect()``.

The predicate is the single source of truth for "is this a
transport-class failure?". It is class-based, never
substring-based, because substring matching on exception
messages is implicit duck typing on a string the framework
does not own and the redis-py library is free to change.
"""

from __future__ import annotations

import redis.exceptions as redis_exceptions


_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    redis_exceptions.ConnectionError,
    redis_exceptions.TimeoutError,
    redis_exceptions.BusyLoadingError,  # Redis still loading RDB
    ConnectionError,
    TimeoutError,
    OSError,
)


def is_transport_error(e: BaseException) -> bool:
    """True when ``e`` is a transport-layer failure that
    requires proactive reconnect.

    The match is class-based; redis-py exception classes
    are stable across the 4.x and 5.x lines. Application-
    level ``RedisError`` subclasses (``WatchError``,
    ``ResponseError``, ``DataError``) are excluded —
    those indicate a wrong command, not a transport
    failure (ADR-077 §4.3).
    """
    return isinstance(e, _TRANSPORT_ERRORS)


__all__ = ["is_transport_error"]
```

The `BusyLoadingError` row covers the "Redis still loading the RDB
snapshot" case: the connection is alive but the server cannot serve
commands; this is transport-class and warrants a reconnect.

The tuple does **not** list `asyncio.TimeoutError` — under
`requires-python = ">=3.12"` (`pyproject.toml`) it is a
documented alias of the builtin `TimeoutError`, which is
already in the tuple. An earlier draft of this section
listed it explicitly; the alias was removed during the
implementation review (ruff `UP041`) to avoid the duplicate
entry. The consumer-side catches in §3.2 likewise use
`TimeoutError` (not `asyncio.TimeoutError`).

The tuple **does** list `RuntimeError`. The `redis-py`
library raises `RuntimeError("Transport is closed")` for
transport-class state changes; this is the exact pattern
the 2026-10-07 post-mortem identified (see
`tests/unit/tools/test_manager.py::TestConsumeLoop::
test_consume_loop_closes_dead_socket_on_runtime_error`,
which predates this ADR). `RuntimeError` is a generic
class, but the message body is the stable contract
`redis-py` ships; including the class catches every
variant `redis-py` emits without falling back to substring
matching (which the framework rejects as too brittle — see
§1.6 row 1). Application-level `RedisError` subclasses
(`WatchError`, `ResponseError`, `DataError`) remain
excluded — those indicate a wrong command, not a transport
failure (ADR-077 §4.3).

**Change (2):** wire the predicate into `_consume_loop`. The
helper at `manager.py:456` (`_reconnect_after_runtime_error`) is
renamed `_reconnect_after_transport_error` in the same commit; the
underlying free function (`_message_handlers.py:249`) follows the
deprecation cycle in §3.3:

```python
# src/kntgraph/tools/manager.py:610-611 (post-ADR-082)

if is_transport_error(e):
    await self._reconnect_after_transport_error()
```

with the helper signature tightened from `redis: Any` to
`redis: RedisLike` (closes the `Any` in
`_message_handlers.py:249` per `kntgraph-type-discipline` §1.1):

```python
# src/kntgraph/tools/_message_handlers.py (post-ADR-082)

async def reconnect_redis_after_transport_error(redis: RedisLike) -> None:
    """Tear down the current Redis client connection and
    force a reconnect on the next ``xreadgroup``.

    A transport-class failure (``is_transport_error(e)``,
    this ADR §3.1) on ``xreadgroup`` is the signal that
    the connection is in a broken state. Continuing to
    read on the same client returns the same error in a
    tight loop, so we close the client (best-effort) and
    disconnect the underlying connection pool. The next
    ``xreadgroup`` reconnects transparently.
    """
    close_fn = getattr(redis, "aclose", None) or getattr(redis, "close", None)
    if close_fn:
        try:
            res = close_fn()
            if asyncio.iscoroutine(res):
                await res
        except (
            redis_exceptions.RedisError,
            AttributeError,
            OSError,
            TimeoutError,
        ):
            pass
    pool = getattr(redis, "connection_pool", None)
    if pool and hasattr(pool, "disconnect"):
        try:
            dis_res = pool.disconnect()
            if asyncio.iscoroutine(dis_res):
                await dis_res
        except (
            redis_exceptions.RedisError,
            AttributeError,
            OSError,
            TimeoutError,
        ):
            pass
```

The helper's body — `aclose()` + `pool.disconnect()` — does not
change; the wider catch only widens the *trigger*, not the
*recovery*. The catch is **narrow on purpose** — `asyncio.CancelledError`
propagates, application-level `RedisError` subclasses (`WatchError`,
`ResponseError`, `DataError`) do **not** trigger a reconnect (those
indicate a wrong command, not a transport failure). ADR-077 §4.3
already established this discipline; this ADR follows it.

**Change (3):** export from the package init so verticals and
tests can import without reaching into the private module:

```python
# src/kntgraph/infra/redis/__init__.py — added row

from ._transport import is_transport_error

__all__ = [
    ...,
    "is_transport_error",
    ...,
]
```

### 3.2 Proactive `close_transport()` in `_wake_once` (typed Protocol method)

**Today** (`src/kntgraph/runner/reactive.py:812-823`):

```python
except Exception as e:  # noqa: BLE001 - top-level wake loop isolates transport crash
    logger.warning("reactive.wake.subscribe_failed", error=str(e))
    await self.dispatch_once()
    self._next_fallback_at = time.monotonic() + self._fallback_interval
    await asyncio.sleep(self._interval)
    return
```

Without proactive close, the next `_log.subscribe` reuses the same
broken client. `dispatch_once()` (the fallback poll) eventually
trips on the same broken client and the loop accumulates
`reactive.wake.subscribe_failed` warnings every `interval` seconds
until the kernel TCP retransmits time out and the underlying
socket finally closes — typically 30-60 s of degraded latency
before the framework recovers.

The current implementation has two structural problems this ADR
fixes:

1. `except Exception` violates `kntgraph-typed-errors` §6.1 and
   applies the `noqa: BLE001` blanket suppression. The narrow-catch
   precedent already exists at
   `stream/event_log/dispatch.py:83`
   (`(ConnectionError, TimeoutError) → PersistenceError`) and at
   `infra/redis/_pool.py:121-129`
   (`aclose` swallows `(RedisError, ConnectionError, TimeoutError, OSError)`).
2. Reaching through `self._log._redis` (or
   `self._log._storage.client`) is the anti-pattern the codebase
   already documents as wrong
   (`stream/event_log/store.py:373, 389, 392`).

**Change (1):** add a Protocol method on the storage boundary, not
on the EventLog. The EventLog forwards; the dispatcher calls the
typed method.

```python
# stream/event_log/store.py — Protocol addition

class EventLogStorage(Protocol):
    ...existing methods...

    async def close_transport(self) -> None:
        """Proactively close the underlying transport.

        Called by long-running consumers after a
        transport-class failure (this ADR §3.2) to
        force the next operation to reconnect instead
        of reusing the broken connection. Best-effort;
        implementation must catch and swallow transport
        exceptions on the close path (see
        ``RedisPool.aclose`` at
        ``infra/redis/_pool.py:111-129`` for the
        canonical swallow).
        """
```

```python
# infra/redis/_event_log/__init__.py — RedisEventLogAdapter

async def close_transport(self) -> None:
    """Forward to ``RedisPool.aclose`` if the adapter
    owns one. ``self._pool`` is the adapter's
    connection pool (ADR-076).
    """
    if self._pool is not None:
        await self._pool.aclose()
```

```python
# stream/event_log/store.py — EventLog forwarder

async def close_transport(self) -> None:
    """Forward to the storage adapter. See
    ``EventLogStorage.close_transport`` (this ADR).
    """
    if hasattr(self._storage, "close_transport"):
        await self._storage.close_transport()  # type: ignore[attr-defined]
```

The `hasattr` guard is the same pattern already in use at
`store.py:340, 403` for `latest_stream_id` and `read_with_cursor`.
The Protocol-level method replaces it once every adapter implements
the Protocol (acceptance criterion in §5.4).

**Change (2):** replace `except Exception` with an explicit
transport-class tuple that mirrors the predicate in §3.1. No
`noqa`, no catch-all:

```python
# src/kntgraph/runner/reactive.py:812-823 (post-ADR-082)

except asyncio.CancelledError:
    raise
except (
    ConnectionError,
    TimeoutError,
    OSError,
    redis_exceptions.ConnectionError,
    redis_exceptions.TimeoutError,
    redis_exceptions.BusyLoadingError,
) as e:
    # A transport-class wake-up failure: log, force a
    # reconnect via the typed Protocol method, then
    # fall back to the poll loop. The next
    # ``_log.subscribe`` resolves a fresh connection
    # instead of reusing the broken one.
    logger.warning("reactive.wake.subscribe_failed", error=str(e))
    await self._log.close_transport()
    await self.dispatch_once()
    self._next_fallback_at = time.monotonic() + self._fallback_interval
    await asyncio.sleep(self._interval)
    return
```

The narrow-catch pattern matches the `reconnect_redis_after_transport_error`
helper at `_message_handlers.py:271-277` and the
`RedisPool.aclose` pattern at `_pool.py:121-129` (ADR-077 §3.4).
`asyncio.CancelledError` propagates (line 812).

The `_wake_once` is the only call site that needs the change; the
`reactive.py:812-823` block is the single point of failure for the
push-first wake-up path documented in ADR-068 §3.2.

### 3.3 Rename `reconnect_redis_after_runtime_error` via the standard deprecation cycle

The free helper at `_message_handlers.py:249` carries the name
`reconnect_redis_after_runtime_error` — a misnomer post-`fe6ba89`,
since the catch was always about *transport*, not *runtime*
semantics. §3.1 introduces the canonical new name
`reconnect_redis_after_transport_error` and tightens the parameter
type from `Any` to `RedisLike`. The old name keeps working for one
minor cycle and is `git rm`'d in the next, per
`kntgraph-deprecation-policy` §2.1.

**v0.18 (this release):**

```python
# src/kntgraph/tools/_message_handlers.py (post-ADR-082)

import warnings

async def reconnect_redis_after_runtime_error(redis: RedisLike) -> None:
    """Deprecated. Use
    :func:`reconnect_redis_after_transport_error`.

    Removal target: v0.19.0. The name was a misnomer
    post-``fe6ba89`` (the catch was always about transport,
    not runtime semantics).
    """
    warnings.warn(
        "`reconnect_redis_after_runtime_error` is deprecated; "
        "use `reconnect_redis_after_transport_error` instead. "
        "Removal target: v0.19.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    await reconnect_redis_after_transport_error(redis)


async def reconnect_redis_after_transport_error(redis: RedisLike) -> None:
    """Tear down the current Redis client connection and
    force a reconnect on the next ``xreadgroup``.

    A transport-class failure (``is_transport_error(e)``,
    §3.1) on ``xreadgroup`` is the signal that the
    connection is in a broken state. Continuing to read
    on the same client returns the same error in a tight
    loop, so we close the client (best-effort) and
    disconnect the underlying connection pool. The next
    ``xreadgroup`` reconnects transparently.
    """
    close_fn = getattr(redis, "aclose", None) or getattr(redis, "close", None)
    if close_fn:
        try:
            res = close_fn()
            if asyncio.iscoroutine(res):
                await res
        except (
            redis_exceptions.RedisError,
            AttributeError,
            OSError,
            TimeoutError,
        ):
            pass
    pool = getattr(redis, "connection_pool", None)
    if pool and hasattr(pool, "disconnect"):
        try:
            dis_res = pool.disconnect()
            if asyncio.iscoroutine(dis_res):
                await dis_res
        except (
            redis_exceptions.RedisError,
            AttributeError,
            OSError,
            TimeoutError,
        ):
            pass
```

`WorkerManager._reconnect_after_runtime_error` is renamed
`_reconnect_after_transport_error` in the same commit. The method
is internal (not in `__all__`), so no deprecation wrapper is
needed; the call site at `manager.py:611` updates to the new name
and there is exactly one in-tree importer (`manager.py`).

**v0.19 (one minor later):** `reconnect_redis_after_runtime_error`
is `git rm`'d. The removal target is documented in the
`DeprecationWarning` message and committed in `CHANGELOG.md`. Per
`kntgraph-deprecation-policy` §2.2, "removal targets are not
optional" — once committed in `DeprecationWarning`, the helper
**must** be removed in v0.19.

### 3.4 `redis_health_check_interval` setting (default 15 s)

**Today** (`src/kntgraph/infra/redis/_pool.py:73-81`):

```python
pool = ConnectionPool.from_url(
    settings.redis_url,
    max_connections=settings.redis_max_connections,
    decode_responses=False,
    socket_connect_timeout=5,
    socket_timeout=settings.redis_socket_timeout,
    socket_keepalive=True,
    retry_on_timeout=True,
)
```

The `redis-py` library supports `health_check_interval=N` (seconds)
that triggers a `PING` before any connection that has been idle for
≥ N seconds is handed out. Default is 0 = disabled. Without it, an
idle connection that has been silently killed by an intermediate
firewall (typical NAT idle timeout: 60-300 s) returns success on
`PING` only because the local TCP buffer has not yet been notified,
and fails on the next command — a textbook half-open scenario.

**Change:** add the setting and wire it:

```python
# src/kntgraph/infra/config/_redis.py:44-58 — post-ADR-082

class RedisSettingsMixin(BaseSettings):
    redis_url: str = Field(default="redis://localhost:6379")
    redis_max_connections: int = Field(default=50)
    redis_socket_timeout: float = Field(default=15.0)
    # New in ADR-082: PING idle connections before reuse (seconds).
    # Set to 0 to disable (redis-py default; not recommended for
    # remote Redis with NAT / firewall idle reapers).
    redis_health_check_interval: float = Field(default=15.0)
    redis_fake: bool = Field(default=False)
    redis_key_prefix: str = Field(default="")
```

```python
# src/kntgraph/infra/redis/_pool.py:73-81 — post-ADR-082

pool = ConnectionPool.from_url(
    settings.redis_url,
    max_connections=settings.redis_max_connections,
    decode_responses=False,
    socket_connect_timeout=5,
    socket_timeout=settings.redis_socket_timeout,
    socket_keepalive=True,
    retry_on_timeout=True,
    health_check_interval=settings.redis_health_check_interval,
)
```

Default of 15 s matches the value the backoffice has been running
in production (`resilience_patch.py:48`) and is comfortably below
typical NAT idle timeouts (60 s for AWS NAT Gateway, 120 s for most
on-prem firewalls). Operators can opt out via
`KNT_REDIS_HEALTH_CHECK_INTERVAL=0` if they want the pre-ADR-082
behaviour byte-for-byte.

---

## 4. Configuration surface

### 4.1 New Settings field

| Field | Default | Env var | Rationale |
|---|---|---|---|
| `redis_health_check_interval` | `15.0` (seconds) | `KNT_REDIS_HEALTH_CHECK_INTERVAL` | PING idle connections before reuse; `0` opts out |

### 4.2 New module-private predicate (public via `__init__.py`)

| Symbol | Module | Public? | Rationale |
|---|---|---|---|
| `is_transport_error` | `infra/redis/_transport.py` | Re-exported in `infra/redis/__init__.py` | Single source of truth for transport-class detection; class-based, no substring matching |

### 4.3 New Protocol method

| Method | Protocol | Rationale |
|---|---|---|
| `EventLogStorage.close_transport() -> Awaitable[None]` | `EventLogStorage` (in `stream/event_log/store.py`) | Forces the next operation to reconnect after a transport-class failure; replaces `getattr(self._log, "_redis")` reach-through |

`EventLog.close_transport()` is a one-line method that forwards to
the storage adapter (with the `hasattr` guard already used at
`store.py:340, 403` for `latest_stream_id` and `read_with_cursor`).

### 4.4 Deprecation cycle (`kntgraph-deprecation-policy` §2.1)

| Symbol | v0.18 (this release) | v0.19 (next minor) |
|---|---|---|
| `reconnect_redis_after_runtime_error` | `DeprecationWarning`, forwards to `reconnect_redis_after_transport_error` | `git rm` |
| `_reconnect_after_runtime_error` (WorkerManager method) | Renamed in the same commit to `_reconnect_after_transport_error`; the old name is internal (not in `__all__`), so no deprecation cycle | n/a |

The helper is not in the public `kntgraph.tools` exports
(`__init__.py:135-162`), so the rename only affects deep forks and
in-tree call sites — there is exactly one in-tree caller
(`tools/manager.py:611`).

### 4.5 `WorkerManager.__init__` and `ReactiveDispatcher.__init__`

No signature change. The reconnect path is internal to the
consume loop; the dispatcher's wake loop is internal to `_wake_once`.
No new kwargs.

### 4.6 Logging taxonomy

No new log lines. The new path keeps the existing
`worker.consume_loop.error` (`manager.py:604-608`) and
`reactive.wake.subscribe_failed` (`reactive.py:819`) lines.

---

## 5. Migration plan

### 5.1 The migration is bit-for-bit backwards compatible

| Aspect | Before | After (v0.18) | After (v0.19) |
|---|---|---|---|
| `_consume_loop` catches `RuntimeError` only | ✓ | ✓ (still caught; `RuntimeError` is not transport-class in this design but the loop still completes the cycle) | ✓ |
| `_consume_loop` catches `redis.exceptions.ConnectionError` | ✗ | ✓ (new) | ✓ |
| `_consume_loop` catches `OSError` | ✗ | ✓ (new) | ✓ |
| `_consume_loop` catches `redis.exceptions.TimeoutError` | ✗ | ✓ (new) | ✓ |
| `_consume_loop` catches `redis.exceptions.BusyLoadingError` | ✗ | ✓ (new) | ✓ |
| `_consume_loop` reconnects on `"closed"` / `"transport"` substring | ✗ | ✗ (rejected — false-positive risk; ADR-082 §1.6 row 1) | ✗ |
| `_wake_once` reconnects proactively on `_log.subscribe` transport failure | ✗ | ✓ (new, via `EventLogStorage.close_transport()`) | ✓ |
| `_wake_once` uses `except Exception` + `noqa: BLE001` | ✓ | ✗ (replaced with explicit tuple) | ✗ |
| `_wake_once` reaches through `self._log._redis` | ✓ | ✗ (replaced with `self._log.close_transport()`) | ✗ |
| `ConnectionPool.health_check_interval` | 0 (disabled) | 15 (PING every 15 s) | 15 |
| Operators who need the pre-ADR-082 behaviour byte-for-byte | n/a | `KNT_REDIS_HEALTH_CHECK_INTERVAL=0` | `KNT_REDIS_HEALTH_CHECK_INTERVAL=0` |
| `reconnect_redis_after_runtime_error` importable | ✓ | ✓ (`DeprecationWarning`) | ✗ (removed) |
| `reconnect_redis_after_transport_error` importable | ✗ | ✓ | ✓ |

There is no behavioural change for `fakeredis` (no TCP transport);
existing integration tests pass without modification.

### 5.2 The shipping plan

1. **v0.18.x (next minor):** §3.1, §3.2, §3.4 ship together. §3.3 ships the new name with `DeprecationWarning` on the old. CHANGELOG entry: "ADR-082: Redis transport defense-in-depth. `_consume_loop` reconnects on `ConnectionError`/`OSError`/`redis.exceptions.TimeoutError`/`redis.exceptions.BusyLoadingError` via the new `is_transport_error` predicate in `infra/redis/_transport.py`. `_wake_once` proactively disconnects the Redis client via the new `EventLogStorage.close_transport()` Protocol method on transport-class `_log.subscribe` failures (replaces `except Exception` and `getattr` reach-through). `KNT_REDIS_HEALTH_CHECK_INTERVAL` defaults to 15 s. `reconnect_redis_after_runtime_error` is deprecated in favour of `reconnect_redis_after_transport_error`; removal in v0.19. Backwards compatible; set `KNT_REDIS_HEALTH_CHECK_INTERVAL=0` to opt out."
2. **Backoffice follow-up (soldi repo):** delete `soldi/backoffice/src/backoffice/infra/resilience_patch.py` and the `apply_resilience_patches()` call at `soldi/backoffice/src/backoffice/main.py:117-119`. Update `soldi/backoffice/docs/ADRs/adr-013-fork-kntgraph-para-correcoes-de-oom.md` status to "Resolvido (ADR-082 upstream)". Open a final acceptance PR to `CHANGELOG.md` confirming the deletion.
3. **v0.19.x (one minor later):** `git rm` `reconnect_redis_after_runtime_error` from `_message_handlers.py` per the deprecation target. CHANGELOG entry: "ADR-082 follow-up: remove deprecated `reconnect_redis_after_runtime_error`."

### 5.3 Test coverage

| Test | File | Asserts |
|---|---|---|
| `test_is_transport_error_class_match` | `tests/unit/infra/redis/test_transport.py` (NEW) | `is_transport_error(redis_exceptions.ConnectionError("x"))` is `True`; same for `OSError`, `TimeoutError`, `redis_exceptions.TimeoutError`, `redis_exceptions.BusyLoadingError`. |
| `test_is_transport_error_application_excluded` | same | `is_transport_error(redis_exceptions.ResponseError("WRONGTYPE"))` is `False`; same for `WatchError`, `DataError`, `ValueError("transport pipeline")`. |
| `test_is_transport_error_substring_does_not_match` | same | `is_transport_error(ValueError("Connection is closed"))` is `False` — protects against the substring-match regression. |
| `test_consume_loop_reconnects_on_redis_connection_error` | `tests/unit/tools/test_manager.py` | Mock the Redis client to raise `redis.exceptions.ConnectionError` on `xreadgroup`; assert `_reconnect_after_transport_error` is called. |
| `test_consume_loop_reconnects_on_os_error` | same | Mock `OSError("broken pipe")`; assert reconnect is called. |
| `test_consume_loop_reconnects_on_busy_loading` | same | Mock `redis.exceptions.BusyLoadingError`; assert reconnect is called. |
| `test_consume_loop_does_not_reconnect_on_data_error` | same | Mock `redis.exceptions.DataError`; assert reconnect is **not** called (transport-class only). |
| `test_consume_loop_does_not_reconnect_on_value_error_with_substring` | same (NEW) | Mock `ValueError("transport not configured")`; assert reconnect is **not** called — regression test for the ADR-082 substring-match footgun. |
| `test_wake_once_proactive_close_on_transport_failure` | `tests/unit/runner/test_reactive_wake_up.py` | Mock `_log.subscribe` to raise `redis.exceptions.ConnectionError`; assert `_log.close_transport()` is called before `dispatch_once`. |
| `test_wake_once_no_close_on_application_error` | same | Mock `_log.subscribe` to raise `ValueError`; assert `close_transport()` is **not** called. |
| `test_wake_once_no_close_on_cancelled` | same (NEW) | Mock `_log.subscribe` to raise `asyncio.CancelledError`; assert it propagates and `close_transport()` is **not** called. |
| `test_event_log_storage_close_transport_protocol` | `tests/unit/stream/event_log/test_store.py` | Construct a fake `EventLogStorage` that records `close_transport()`; call `event_log.close_transport()`; assert the fake recorded one call. |
| `test_reconnect_redis_after_runtime_error_warns` | `tests/unit/tools/test_message_handlers.py` | Call the old name with `pytest.warns(DeprecationWarning, match="v0.19")`; assert it forwards to the new helper. |
| `test_reconnect_redis_after_transport_error_signature` | same | Pass a fake object that is NOT a `RedisLike` (no `aclose`); assert the helper returns without raising (best-effort). |
| `test_reconnect_redis_after_transport_error_accepts_redis_like` | same | Pass a fake implementing `RedisLike` (`aclose`, `connection_pool.disconnect`); assert both are called. |
| `test_redis_pool_passes_health_check_interval` | `tests/unit/infra/redis/test_pool.py` | Construct a pool with `redis_health_check_interval=30`; assert `ConnectionPool._created_kwargs["health_check_interval"] == 30`. |
| `test_redis_pool_health_check_interval_default_is_15` | same | Assert `fresh_settings().redis_health_check_interval == 15.0`. |
| `test_redis_pool_health_check_interval_zero_opts_out` | same | Construct with `redis_health_check_interval=0`; assert `health_check_interval=0` (disabled). |

### 5.4 Acceptance criteria

- The shipped release exposes `Settings.redis_health_check_interval` with default `15.0` and respects `KNT_REDIS_HEALTH_CHECK_INTERVAL`.
- `is_transport_error` is the single source of truth for transport-class detection, class-based only; substring matching is rejected.
- `_consume_loop` reconnects on every class in `is_transport_error`'s tuple (via `_reconnect_after_transport_error`); does **not** reconnect on `ResponseError`, `WatchError`, `DataError`, `ValueError`, or any other non-transport exception class — including exceptions whose message contains `"closed"` or `"transport"`.
- `_wake_once` proactively calls `_log.close_transport()` when `_log.subscribe` fails with a transport-class error; does **not** call `close_transport()` on `asyncio.CancelledError`, `ResponseError`, `ValueError`, or any other non-transport exception class.
- `EventLogStorage.close_transport()` is in the Protocol; `RedisEventLogAdapter` implements it; fakeredis implements it as a no-op.
- `reconnect_redis_after_runtime_error` emits `DeprecationWarning` pointing to `reconnect_redis_after_transport_error`; removal target v0.19 is documented in the warning text and in `CHANGELOG.md`.
- All 17 tests in §5.3 pass.
- `soldi/backoffice` deletes `resilience_patch.py` and the `apply_resilience_patches()` call site within 1 minor release of upstream shipping.

---

## 6. Alternatives considered

| Alternative | Pros | Cons | Verdict |
|---|---|---|---|
| **A. Status quo (backoffice keeps the patch)** | Zero upstream work; immediate | Every new consumer reinvents the patch; the framework *encourages* the wrong pattern (monkey-patching consumers); backoffice accumulates ~227 LOC of fork debt that the team explicitly opened as a stop-gap | Rejected |
| **B. Document the patch as a "framework-recommended consumer pattern"** | Lets us avoid touching upstream | Monkey-patching a consumer's framework is not a pattern; it is a workaround. Documenting it would advertise an anti-pattern. | Rejected |
| **C. Promote only §3.4 (the new config knob)** | Smallest diff; one PR | Leaves §3.1 and §3.2 as silent monkey-patches; backoffice still needs `resilience_patch.py` for those two. Does not close the loop. | Partial — if §3.1 and §3.2 are blocked, fall back to this |
| **D. Promote only §3.1 + §3.2 (the wider catches) without `health_check_interval`** | Two of three items close the loop | Leaves §3.4 undone; backoffice still needs `resilience_patch.py:48`. Half-open TCP scenarios still silently stall. | Partial — if §3.4 is blocked, fall back to this |
| **E. Earlier draft of this ADR (substring match + `except Exception` + `getattr` reach-through + inline rename + `Any` parameter)** | One ADR, smallest diff to write | Five conflicts with framework standards (§1.6 table) — substring match is a real false-positive risk; `except Exception` violates typed-errors; `getattr` on private attrs violates file-layout; inline rename violates deprecation-policy; `Any` parameter violates type-discipline | Rejected — refined to this version |
| **G. (this ADR) Promote all three mechanisms with the §1.6 remediation** | Closes the loop AND aligns with the framework standards; adds a reusable `is_transport_error` primitive; introduces a Protocol method that other consumers can call without poking private attrs; follows the standard deprecation cycle | Larger diff than the earlier draft; one new module (`_transport.py`); one Protocol method; one deprecation cycle | **Recommended** |
| **H. Skip the rename and widen the catch on the old name** | No deprecation cycle | `runtime_error` is a misnomer post-`fe6ba89`; the rename is a one-line PR and the cycle is the framework's standard | Rejected — the deprecation cycle is cheaper than the permanent misnomer |
| **I. Reach through `EventLog` to the Redis client directly (the earlier §3.2 shape)** | No Protocol change | `SLF001` symptom on every consumer that needs the same hook; future framework modules must repeat the `getattr` chain; contradicts the anti-pattern already documented at `stream/event_log/store.py:373, 389, 392` | Rejected |

### 6.1 Why the catch widening is safe

The widening catches **strictly more** than the current code. The
only way the new behaviour can cause harm is if the wider catch
reconnects on an error that the operator wanted to bubble up. We
argue this cannot happen because:

1. **The predicate is class-based.** `is_transport_error` matches
   only classes in a stable tuple; it does not inspect the message
   body, so a redis-py wording change cannot flip the decision.
2. **The reconnect helper is idempotent and best-effort.**
   `_message_handlers.py` (post-rename: `_reconnect_after_transport_error`)
   catches `RedisError, AttributeError, OSError, TimeoutError` on
   both the `aclose()` and `pool.disconnect()` paths. A reconnect
   that fails to close simply skips the success tail and lets the
   next `xreadgroup` reconnect transparently.
3. **The narrow-catch pattern is established.** ADR-077 §4.3
   documents the discipline ("the third-party transport exception
   is caught at the boundary and translated into a
   framework-typed error"). §3.1 and §3.2 follow the same pattern.
4. **`fakeredis` does not exercise TCP transport.** Integration
   tests that use `KNT_REDIS_FAKE=1` (`config/_redis.py:53`) never
   see transport errors and the new catch is a no-op.

---

## 7. References

- **Post-mortem 2026-10-07** (backoffice) — full text in `soldi/backoffice/docs/ADRs/adr-013-fork-kntgraph-para-correcoes-de-oom.md`. The "Resilience scope summary" sub-section enumerates the 9 mechanisms; §1.3 of this ADR cross-references each.
- **Commit `fe6ba89`** (PR #63, 2026-10-10, "feat: add socket timeout configuration and handle dead socket scenarios") — the upstream change that absorbed 8 of the 9 mechanisms. This ADR closes the loop.
- **ADR-013** (backoffice, "Fork kintgraph para correções de OOM") — the backoffice ADR that triggered this workstream. The ADR will be marked "Resolvido (ADR-082 upstream)" once this ADR ships.
- **ADR-068** — `block_ms` invariant on `_log.subscribe`; preserved by §3.2 (the widening does not change the cadence).
- **ADR-077** — narrow-catch pattern at the storage adapter boundary; §3.1 and §3.2 follow the same discipline at the consumer boundary.
- **ADR-078** — the parallel workstream (process-pool OOM). Both workstreams stem from the same post-mortem; both target the same goal (backoffice consumes `main` unmodified).
- **`soldi/backoffice/src/backoffice/infra/resilience_patch.py:1-227`** — the patch being promoted. The `git rm` of this file is the acceptance test for this ADR.
- **`src/kntgraph/tools/_message_handlers.py:249-290`** — `reconnect_redis_after_runtime_error`; renamed (deprecation cycle in §3.3) and reused by §3.1, with `Any` parameter tightened to `RedisLike`.
- **`src/kntgraph/tools/manager.py:543-616`** — the post-`fe6ba89` `_consume_loop`; §3.1 modifies line 610.
- **`src/kntgraph/runner/reactive.py:805-823`** — the post-`fe6ba89` `_wake_once`; §3.2 modifies lines 812-823.
- **`src/kntgraph/infra/redis/_pool.py:73-81`** — the post-`fe6ba89` `RedisPool.from_settings`; §3.4 modifies the `ConnectionPool.from_url` call.
- **`src/kntgraph/infra/config/_redis.py:44-58`** — `RedisSettingsMixin`; §3.4 adds the `redis_health_check_interval` field.
- **`src/kntgraph/infra/redis/_client.py`** — `RedisLike` Protocol; the typed seam §3.1 uses for the `reconnect_redis_after_transport_error` parameter.
- **`stream/event_log/store.py:373, 389, 392`** — the existing documentation that reaching through `_redis` is an anti-pattern; the precedent for §3.2's Protocol method.
- **`stream/event_log/dispatch.py:83`** — `(ConnectionError, TimeoutError) → PersistenceError`; the precedent for §3.2's narrow catch.
- **`infra/redis/_pool.py:111-129`** — `RedisPool.aclose`'s narrow swallow; the precedent for the close-side catch.
- **`infra/redis/_errors.py:18-19`** — `RedisUnavailableError`; the typed error the storage adapter raises at the boundary (ADR-077).
- **Skills** (under `.agents/skills/`):
  - `kntgraph-deprecation-policy` — §3.3 follows the standard one-minor deprecation cycle.
  - `kntgraph-type-discipline` — §3.1 tightens the helper signature from `Any` to `RedisLike`; §3.1's predicate uses primitives from the skill §3.
  - `kntgraph-typed-errors` — §3.2 follows the never-`raise-Exception` rule.
  - `kntgraph-file-layout` — §3.2 follows the public/private boundary via Protocol.
  - `kntgraph-testing` — §5.3 follows the happy-path-plus-one-failure-mode gate.
  - `kntgraph-environment` — `KNT_REDIS_FAKE=1` is the test path that §6.1 establishes is a no-op for the new catch.

---

## 8. Open questions for review

1. **`BusyLoadingError`.** §3.1 includes `redis.exceptions.BusyLoadingError` in the transport tuple. The argument for: Redis is still loading the RDB snapshot; the connection is alive but the server cannot serve commands — this is a transport-class failure that requires reconnect-or-retry. The argument against: `BusyLoadingError` may clear in milliseconds; reconnecting discards the in-flight request. Recommendation: include in the tuple; the helper closes the client (best-effort) and the next `xreadgroup` reconnects; this is strictly better than the current behaviour (silent stall). Reviewer can veto.
2. **`EventLogStorage.close_transport()` as Protocol vs runtime attribute.** §3.2 proposes a Protocol method, with a `hasattr` guard at the EventLog forwarder for backwards compatibility with adapters that have not yet implemented it. The long-term plan is for every adapter to implement the Protocol; tracked in §5.3 row 12 and §5.4. Alternative: runtime `hasattr` check everywhere, no Protocol change — rejected because `kntgraph-file-layout` §3.3 says the public API is the `__all__` contract, and a Protocol method is the typed way to express the contract.
3. **Default 15 s.** `redis-py` documentation suggests 30 s; the backoffice has been running 15 s in production without false-positives. ADR-078 §6.2 establishes "follow what the backoffice already proved" as the default heuristic; 15 s it is. Override via `KNT_REDIS_HEALTH_CHECK_INTERVAL`. **This question is closed**; carried for record.
4. **Naming — `is_transport_error` vs `RedisTransportError` class.** §3.1 proposes a function predicate; an alternative is a marker class `class RedisTransportError(BaseException): pass` that adapters raise and consumers catch. Recommendation: function predicate — the class-based marker would require every adapter to wrap third-party exceptions, while the predicate catches them in-place. Reviewer can veto.
5. **Removal target for `reconnect_redis_after_runtime_error`.** §3.3 proposes v0.19. The `kntgraph-deprecation-policy` skill §2.2 says "Removal targets are not optional" — once committed in `DeprecationWarning`, the helper must be `git rm`'d in v0.19. Reviewer can veto (e.g. propose v0.20 if the v0.19 cycle is too tight for backoffice alignment).