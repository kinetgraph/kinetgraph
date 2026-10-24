<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-084: One Redis connection pool per process; the caller owns its lifecycle

- **Status:** Accepted (ratified 2026-10-23; shipped in v0.17.0 — §6.4 carries the version-label correction)
- **Date:** 2026-10-23
- **Author:** kinetgraph architecture team (raised by the operator, "as conexões estão ficando presas")
- **Related to:**
  - [ADR-077](./ADR-077-Storage-Adapter-Exception-Translation.md) — the narrow-catch discipline at the storage boundary. §3.1 of this ADR keeps the same narrow catch on the new close path.
  - [ADR-082](./ADR-082-Promote-Redis-Transport-Defense-In-Depth.md) — the transport-defense workstream from the 2026-10-07 post-mortem. §2.3 shows that its `close_transport()` reconnect path is what turns a saturated pool into a reconnect storm, and §3.4 narrows it.
  - [ADR-076](./ADR-076-Service-Scoped-Redis-Key-Prefix.md) — the namespace prefix. Unchanged by this ADR; the pool ownership change does not touch key composition.
  - [ADR-078](./ADR-078-Decommision-ProcessPoolExecutor.md) — the sibling workstream from the same post-mortem. ADR-078 fixed the *process* pool; this ADR fixes the *connection* pool. Its §1.4 scenario (13 tools) is the load profile that decides the sizing rule in §3.5.
  - [`kntgraph-typed-errors`](../.agents/skills/kntgraph-typed-errors/SKILL.md) §6.2 — mutating storage adapters return `Result[T, E]`. The close path follows the same discipline.
  - [`kntgraph-deprecation-policy`](../.agents/skills/kntgraph-deprecation-policy/SKILL.md) §2.1 — no compat shims. §4.2 commits to removal targets rather than keeping a deprecated fallback alive.
  - [`kntgraph-testing`](../.agents/skills/kntgraph-testing/SKILL.md) §7.2 — happy path plus one failure mode per public function. §5 enumerates the coverage gate.
  - [`kntgraph-file-layout`](../.agents/skills/kntgraph-file-layout/SKILL.md) §3.3 — the public/private boundary. The new lifecycle module is private (`_lifecycle.py`); the public `__init__.py` re-exports the surface.

---

## 1. Context

### 1.1 The symptom

The operator reported that Redis connections are "getting stuck". The report was
not about latency or throughput — it was about **connections that are never
released**. Three separate defects share that root, and all three were
confirmed by measurement (the raw numbers are in §2).

### 1.2 Defect 1 — `RedisPool.aclose()` closes nothing

`RedisPool.from_settings` builds the pool explicitly:

```python
pool = ConnectionPool.from_url(settings.redis_url, ...)
client = redis_async.Redis(connection_pool=pool)
```

redis-py is explicit about what this implies:

> *"auto_close_connection_pool only has an effect if connection_pool is None.
> It is assumed that if connection_pool is not None, the user wants to manage
> the connection pool themselves."* (redis-py, `Redis.__init__`)

So `client.aclose()` releases the client's own connection and **leaves the pool
open**. `RedisPool.aclose` (`src/kntgraph/infra/redis/_pool.py:112`) calls
exactly that method and treats the result as a full close:

```python
async def aclose(self) -> None:
    try:
        await self._client.aclose()
    except (...):
        logger.warning("redis_pool.aclose.failed", error=str(exc))
```

Measured against the live Redis (via `CLIENT LIST`), `RedisPool.aclose()`
leaves the socket connected:

```
conexoes disponiveis antes: 1
depois do RedisPool.aclose(): disponiveis: 1
ping depois do aclose: OK -> a ligacao NAO foi fechada
```

**The framework built the pool and therefore owns it — but the close path
treats the client as if it owned it.** Every call to `RedisPool.aclose()` in
the codebase is a no-op with respect to the pool.

### 1.3 Defect 2 — the factories build a pool nobody can close

Nine `create_*` factories in `src/kntgraph/infra/redis/_factory.py` resolve
their client through `_resolve_client`:

```python
def _resolve_client(settings, client):
    if client is None:
        pool = RedisPool.from_settings(settings)
        return pool.client        # <-- the pool is dropped on the floor
    return client
```

The `RedisPool` is constructed, its `.client` is returned, and the wrapper
becomes unreachable. The caller holds a `RedisLike` with no handle on the pool
behind it. Measured:

```
log:     pool id=124524711792320
session: pool id=124524711928016
dlq:     pool id=124524711932176

pools DISTINTOS para 3 factories: 3
```

A service that wires the EventLog, the session store and the DLQ holds three
independent pools, each with up to `redis_max_connections` (default 50)
sockets, for one Redis.

Note that the factories' own docstrings already state the intended contract:

> *"`client=None` builds a fresh `RedisPool` from `Settings`; `client=` is the
> seam for tests and **external lifecycle owners**."*

The intent is documented. The implementation does not deliver it: the
"external lifecycle owner" is given a `RedisLike`, not a pool, and has no way
to reach the pool it now owns.

### 1.4 Defect 3 — `close_transport()` neither reconnects nor spares the siblings

`RedisEventLogAdapter.close_transport()` (`src/kntgraph/infra/redis/_event_log/_adapter.py:358`)
is the ADR-082 reconnect path. Its docstring states the intent: *"Force the
next op to reconnect (ADR-082 §3.2)"*.

It calls `self.client.aclose()`. Measured against a live Redis, that call
does **not** close the socket:

```
ligacao inicial: id=549437328
apos disconnect do pool: id=549437328 (mesma Connection object)
socket fechado? False
RECONNECT? False
```

`Redis.aclose()` releases the client's connection *object* back to the pool
and leaves the socket open. The next command picks up the **same** socket —
so the "force the next op to reconnect" promise is **not kept**. On a
half-open TCP (the exact scenario ADR-082 §3.4's health check exists for),
the broken socket survives the reconnect path.

The framework-wide `reconnect_redis_after_transport_error`
(`src/kntgraph/tools/_message_handlers.py:335`) has the same shape but adds
`pool.disconnect()` after `aclose()`. That *does* close the sockets — but it
closes **every** socket the pool holds, including the ones the sibling
consumers are using. Measured with two adapters on one client:

```
--- a1.close_transport() ---
pool._in_use_connections: 0
--- a2 tenta escrever ---
a2.append: OK
--- a2 tenta ler ---
a2.read FALHOU: AuthenticationError: HELLO must be called with the client
                already authenticated
```

So the two reconnect paths are each wrong in a different way:

| Path | Socket closed? | Siblings spared? | Reconnect happens? |
|---|---|---|---|
| `client.aclose()` (adapter) | **No** | Yes | **No** |
| `aclose()` + `pool.disconnect()` (manager) | Yes | **No** | Yes |

Neither does both. §3.3 specifies the operation that does.

### 1.5 The compounding factor — pool saturation raises, it does not queue

redis-py does **not** wait when the pool is exhausted. It raises immediately:

```
redis.exceptions.MaxConnectionsError: Too many connections
```

`MaxConnectionsError` inherits from redis-py's `ConnectionError`, so
`is_transport_error` (`src/kntgraph/infra/redis/_transport.py:51`) classifies
it as a transport failure. The consumer path reacts accordingly
(`src/kntgraph/tools/manager.py:621`):

```python
if is_transport_error(e):
    await self._reconnect_after_transport_error()
```

That reconnect calls `pool.disconnect()` — which closes **every** socket the
pool holds (§1.4). So the failure sequence is:

1. The pool saturates → `MaxConnectionsError`
2. The consume loop classifies it as a transport failure
3. It calls `close_transport()` → `pool.disconnect()` closes every socket
4. Every other consumer on that pool loses its socket
5. Those consumers raise `MaxConnectionsError` too → back to step 2

**A saturated pool becomes a reconnect storm.** This is the mechanism by which
"stuck connections" and "too many connections" are the same incident.

### 1.6 The measurement

13 consume loops (the ADR-078 §1.4 backoffice scenario: 13 registered tools,
each with its own `XREADGROUP block=1000` loop), measured against a live Redis:

| Design | Slots | Loop reads | Errors | Rate |
|---|---|---|---|---|
| 1 pool × `max_conn=10` | 10 | 9 | 20 | **69.0%** `ConnectionError` |
| 1 pool × `max_conn=13` | 13 | 39 | 0 | 0% |
| 1 pool × `max_conn=50` | 50 | 39 | 0 | 0% |
| 3 pools × `max_conn=2` | 6 | 3 | 30 | **90.9%** `ConnectionError` |
| 3 pools × `max_conn=4` | 12 | 27 | 8 | **22.9%** `ConnectionError` |
| 3 pools × `max_conn=5` | 15 | 39 | 0 | 0% |

Two facts fall out:

1. **The threshold is `max_connections >= number of blocking consumers`.**
   Below it, the framework collapses; at or above it, it is clean.
2. **Separate pools do not raise the threshold — they hide it.** The 3-pool
   layout only stays healthy when `3 × max_conn >= consumers`, i.e. when the
   *aggregate* is large enough anyway. It reached that aggregate by holding
   three times the sockets.

Latency is not a factor: p50 was **0.5 ms** in every configuration. A shared
pool costs no latency; it costs sockets.

### 1.7 What this ADR is, and what it is not

It ratifies a **lifecycle ownership rule** for the Redis connection pool:
the framework no longer creates a pool the caller cannot close, and the
caller owns exactly one.

It is **not**:

- A change to the wire format. No key changes.
- A change to the adapter APIs. `RedisEventLogAdapter(client=...)` and friends
  keep their signatures.
- A new global. §3.2 rejects the registry / singleton design explicitly.

---

## 2. Decision

> **One `RedisPool` per process, owned by the caller. `RedisPool.aclose()`
> closes the pool it built. The `create_*` factories stop silently building
> an uncloseable pool: `client=None` becomes an explicit error, and the
> caller must pass a `client=` it owns. `close_transport()` stops tearing
> down the pool — it drops the connection only.**

### 2.1 The three defects and their fixes

| Defect | Fix | Where |
|---|---|---|
| `aclose()` closes nothing | Close the `ConnectionPool` explicitly | §3.1 |
| Factories build an uncloseable pool | `client=None` → explicit error | §3.2 |
| `close_transport()` kills a shared pool | Disconnect the connection, not the pool | §3.3 |

### 2.2 Why "the caller owns it" and not a framework-owned singleton

The framework already has this exact pattern for configuration
(`src/kntgraph/infra/config/__init__.py:209`):

```python
@lru_cache(maxsize=1)
def fresh_settings() -> Settings:
    """... Cached so repeated calls within a single process do not re-parse
    env vars; tests that need to see ``monkeypatch.setenv(...)`` changes can
    call ``fresh_settings.cache_clear()`` ..."""

settings = fresh_settings()
```

The same shape applies to the pool: a process-scoped default with an explicit
override for tests. But the pool is *not* a configuration value — it owns
sockets, and its lifetime is tied to the process's I/O, not to the import
graph. A module-level singleton captured at import time would:

- open sockets during `import kntgraph` (including in every test process and
  every CLI invocation that never touches Redis),
- make the shutdown path ambiguous (who closes it, and when?),
- and contradict the framework's own rule that the *caller* owns external
  resources.

§3.2 therefore keeps ownership with the caller, in the same shape the
official scaffold already uses.

### 2.3 The reconnect path must not close the pool

§1.5 established that the ADR-082 reconnect path is what turns pool
saturation into a storm. The fix is to narrow it: `close_transport()` drops
the *connection* (so the next command reconnects) and leaves the *pool* (so
the sibling consumers keep theirs).

### 2.4 `MaxConnectionsError` needs its own handling

The saturation failure is not a transport failure. Treating it as one causes
the reconnect storm of §1.5. §3.4 gives it a distinct, non-destructive
response: back off, do not tear down the pool.

---

## 3. The four changes

### 3.1 `RedisPool.aclose()` closes the pool it built

```python
async def aclose(self) -> None:
    """Close the pool and every connection it holds. Idempotent.

    ``client.aclose()`` is not enough: redis-py only closes the
    connection pool when the client owns it, and this class builds
    the pool explicitly (ADR-084 §1.2). The pool is therefore closed
    here, on the object that created it.
    """
    try:
        await self._client.aclose()
        await self._client.connection_pool.aclose()
    except (
        redis_exceptions.RedisError,
        ConnectionError,
        TimeoutError,
        OSError,
    ) as exc:  # pragma: no cover
        logger.warning("redis_pool.aclose.failed", error=str(exc))
```

The narrow catch is unchanged (ADR-077); `asyncio.CancelledError` still
propagates.

### 3.2 `client=None` becomes an explicit error

`_resolve_client` currently builds a pool and drops it. It must not:

```python
def _resolve_client(
    settings: Settings | None,
    client: RedisLike | None,
) -> RedisLike:
    """Return the caller-owned client.

    ``client=None`` is an error, not a convenience: the framework
    cannot build a pool the caller can close, and an uncloseable
    pool is the defect this ADR exists to remove (ADR-084 §1.3).
    Callers create the pool and pass its client:

        pool = create_redis_pool(settings)
        log = create_event_log_storage(settings, client=pool.client)
        ...
        await pool.aclose()
    """
    if client is None:
        raise RedisClientRequiredError(
            "a Redis client is required: create one with "
            "create_redis_pool(settings) and pass client=pool.client. "
            "The framework will not build a connection pool it cannot "
            "close (ADR-084 §3.2)."
        )
    return client
```

The error is a typed framework exception (ADR-077's discipline), not a
`ValueError`.

The scaffold the CLI generates already does the right thing and needs only
the handle kept:

```python
pool = create_redis_pool(settings)
redis = pool.client
log = EventLog(RedisEventLogAdapter(client=redis))
tool_router = create_tool_router(settings=settings, client=redis)
worker_manager = create_worker_manager(settings=settings, redis=redis, event_log=log)
```

### 3.3 `close_transport()` drops the sockets, not the pool

The operation that satisfies both requirements — *the broken socket goes*
and *the siblings keep theirs* — is `ConnectionPool.disconnect()`:

```python
async def close_transport(self) -> None:
    """Close the sockets so the next command reconnects.

    Deliberately does NOT close the connection pool: the pool may
    be shared with sibling adapters that are still healthy, and
    closing it takes them down too (ADR-084 §1.4).

    ``client.aclose()`` is not sufficient: it releases the
    connection object back to the pool WITHOUT closing the socket,
    so the next command reuses the same (possibly half-open) TCP.
    """
    try:
        await self.client.connection_pool.disconnect(inuse_connections=True)
    except (...):
        logger.warning("event_log.close_transport.failed", error=str(exc))
```

`disconnect(inuse_connections=True)` calls `connection.disconnect()` on every
socket the pool holds — available *and* in use. The socket is closed; the
`Connection` object stays in the pool and re-opens its socket on the next
acquisition. Verified against a live Redis:

```
pool ok, disponiveis=1
apos disconnect: disponiveis=1
ping apos disconnect: True -> POOL SOBREVIVEU
```

The pool survives (siblings keep working), the socket is closed (the next
command reconnects), and the pool object is reusable.

The same narrowing applies to `reconnect_redis_after_transport_error`
(`src/kntgraph/tools/_message_handlers.py:335`), which currently does
`aclose()` **and** `pool.disconnect()` — the `aclose()` is redundant there
(the `disconnect()` already closes the sockets) and it is the `aclose()`
that releases the client connection into the pool.

### 3.4 `MaxConnectionsError` is saturation, not a broken transport

```python
class MaxConnectionsError(redis_exceptions.ConnectionError):
    """The pool is exhausted.

    Distinct from a broken transport: the sockets are fine, there
    are simply not enough of them. Closing the pool (the ADR-082
    reconnect path) makes it worse — every sibling consumer loses
    its socket too (ADR-084 §1.5).
    """
```

The consumer loop must distinguish it:

```python
except redis_exceptions.MaxConnectionsError:
    # Back off; do NOT tear down the pool.
    logger.warning("worker.pool_saturated", tool=tool_name)
    await asyncio.sleep(self._pool_saturation_backoff)
```

The backoff is short and bounded (default 0.25 s, doubling to a 2 s
ceiling). Saturation is a signal to slow down, not to reset.

### 3.5 Sizing: `max_connections` must cover the blocking consumers

The threshold from §1.6 is `max_connections >= number of registered tools`
(plus headroom for concurrent commands), because each consume loop holds its
socket for the duration of `XREADGROUP block=`.

The default `redis_max_connections = 50` is adequate for a single shared pool
with up to ~40 tools. It is **not** adequate if the caller sizes three pools
at 50 each.

This ADR adds one operational note to the ADR-076-style key/settings docs:
`KNT_REDIS_MAX_CONNECTIONS` is sized against the number of registered tools,
not against a default.

---

## 4. Consequences

### 4.1 Positive

- **Connections stop leaking.** `RedisPool.aclose()` now does what its name
  says; the pool the framework built is the pool the framework closes.
- **One pool per process instead of one per factory.** The measured 3 pools
  collapse to 1, holding 50 sockets instead of 150.
- **Saturation degrades instead of cascading.** `MaxConnectionsError` no
  longer triggers a pool teardown, so a momentary burst cannot take down
  every consumer in the process.
- **A sibling adapter survives its peer's reconnect.** The `close_transport`
  narrowing removes the `AuthenticationError` cascade of §1.4.
- **The reconnect path actually reconnects.** Today `client.aclose()` leaves
  the socket open (§1.4), so a half-open TCP survives the "force the next op
  to reconnect" path. `pool.disconnect()` closes it.
- **The ownership rule is documented once.** `client=` is already the seam
  the factories' docstrings advertise; this ADR makes it true.

### 4.2 Negative

- **This is a breaking change for callers that omit `client=`.** Every
  `create_*` call without a `client=` now raises. §4.2 sets the removal
  target.
- **The shutdown path becomes the caller's responsibility.** Previously the
  connections simply stayed open and nobody noticed. Now a service that
  creates a pool must close it, or the process leaks on shutdown.
- **Sizing becomes an operator concern.** A deployment that registers more
  tools than `max_connections` will saturate. §3.5 documents the rule; the
  failure is now a logged backoff instead of a reconnect storm.

### 4.3 Risks and mitigations

| Risk | Mitigation |
|---|---|
| A caller upgrades and every `create_*` without `client=` raises | The error message names the exact two-line fix (§3.2). The scaffold the CLI generates is updated in the same change, so new projects are correct by construction. |
| A caller creates one pool per adapter and hits saturation | The sizing rule is documented (§3.5) and `MaxConnectionsError` now back-pressures instead of cascading (§3.4). |
| `close_transport` no longer closes the pool, so a genuinely dead pool persists | The pool survives but its sockets do not — the next command reconnects. A pool whose *URL* is wrong fails at connect time, which is not a transport-class failure. |
| The pool is never closed on shutdown | The framework's own `EventLog`/`WorkerManager` are long-lived process components; the *service* (FastAPI lifespan, CLI main) owns the pool. §4.2 makes that explicit. |

### 4.4 Alternatives considered

| Alternative | Why not |
|---|---|
| **A. Framework-owned singleton pool** (`lru_cache` over `RedisPool`) | Opens sockets at import time; ambiguous shutdown; contradicts the caller's ownership of external resources; makes the CLI open a pool it never uses. §2.2. |
| **B. One pool per factory, each exposed for closing** | Does not change the socket count (3 × 50 instead of 1 × 50) and keeps the duplication the caller must manage. It fixes the *closeability* but not the *saturation threshold*, which §1.6 shows is aggregate. |
| **C. A registry object the caller threads through** | More machinery than the problem needs: the `client=` kwarg already is the thread. §3.2. |
| **D. Raise `max_connections` and leave the pool structure alone** | Treats the symptom. The reconnect storm of §1.5 remains, and the leak of §1.2 remains. |

---

## 5. Test plan

| Layer | File | Coverage |
|---|---|---|
| Unit (lifecycle) | `tests/unit/infra/redis/test_connection_lifecycle.py` | `aclose()` closes the pool; releases pooled connections; is idempotent; swallows transport errors; propagates `CancelledError`; `close_transport` leaves a shared pool intact; the factories do not each build a pool; the adapter exposes a close path. |
| Integration (lifecycle) | `tests/integration/infra/test_connection_lifecycle.py` | The socket is really gone from the server's `CLIENT LIST` after `aclose()`; a shared pool survives one adapter's `close_transport()`. |
| Unit (saturation) | `tests/unit/infra/redis/test_pool_saturation.py` | `MaxConnectionsError` is classified as saturation, not transport; the consumer back-offs without closing the pool. |
| Unit (factories) | `tests/unit/infra/redis/test_factory.py` | Every `create_*` raises `RedisClientRequiredError` without `client=`; returns the caller's client with it. |

The first two files already exist on this branch, in the RED state: 5 of the
9 unit cases and 1 of the 2 integration cases fail against the current code.

---

## 6. Implementation plan

### 6.1 Step 1 — close correctly (no API change)

- `src/kntgraph/infra/redis/_pool.py` — `aclose()` closes the pool (§3.1).
- `src/kntgraph/infra/redis/_event_log/_adapter.py` — `close_transport()`
  calls `pool.disconnect()` instead of `client.aclose()`, so the socket is
  closed *and* the siblings keep theirs (§3.3).
- `src/kntgraph/tools/_message_handlers.py` —
  `reconnect_redis_after_transport_error` drops the redundant `aclose()`
  and keeps only the `pool.disconnect()` (§3.3).

Acceptance: the RED tests in §5 go green; no public signature changes.

### 6.2 Step 2 — saturation is not a transport error

- `src/kntgraph/infra/redis/_transport.py` — classify `MaxConnectionsError`
  as saturation, distinct from transport failure (§3.4).
- `src/kntgraph/tools/manager.py` — the consume loop back-offs on
  saturation instead of reconnecting (§3.4).

Acceptance: a saturated pool logs a backoff and keeps the other consumers
alive.

### 6.3 Step 3 — one pool, caller-owned

- `src/kntgraph/infra/redis/_factory.py` — `client=None` raises
  `RedisClientRequiredError` (§3.2).
- `src/kntgraph/infra/redis/_errors.py` — the new typed error.
- `examples/knt-cli/weather_platform/.../main.py` — keep the pool handle
  and close it in the lifespan.
- `src/kntgraph/cli/templates/main.py.jinja` — the generated scaffold
  teaches the correct wiring.
- `docs/architecture.md`, `docs/quickstart.md` — the ownership rule.

Acceptance: a service built from the scaffold holds exactly one pool and
closes it on shutdown.

### 6.4 Removal targets

Per the deprecation policy (§2.1 — no compat shims), the `client=None`
convenience is removed rather than deprecated-in-place. The migration window
is one minor release:

| Version | State |
|---|---|
| **v0.17.0** | `client=None` raises `RedisClientRequiredError` with a migration message. `RedisPool.aclose()` closes the pool. |
| **v0.19.0** | The `redis=None` kwarg on `create_worker_manager` / `create_tool_router` is removed entirely (they keep only the required form). |

> **Note (2026-10-24).** This table originally assigned the
> `RedisClientRequiredError` to "v0.18.x". The ADR was ratified
> on 2026-10-23 and shipped in **v0.17.0** — the release level was
> chosen before this ADR's own timeline was reconciled with the
> version numbering. The behaviour and the removal targets are
> unchanged; only the version label moved. The same correction
> applies to ADR-078 §5.1, whose "soft warning" step was also
> never released on its own (see ADR-078 §12.8).

---

## 7. Open questions

1. **Should `RedisPool` be an async context manager** (`async with
   create_redis_pool(...) as pool`)? It would make the shutdown obligation
   impossible to forget. The framework's `redis-py` dependency supports it
   (`ConnectionPool.__aenter__` / `__aexit__` exist). Deferred to a follow-up
   — it is an ergonomics improvement, not a correctness fix.
2. **Should the framework expose the connection count as a metric?** The
   saturation backoff in §3.4 is invisible to the operator until they read
   the logs. A `redis_pool.saturated` counter would let the existing
   `worker.consume_loop.heartbeat` line carry it. Deferred to a follow-up ADR
   on pool observability.
3. **What about the `GraphPool` (FalkorDB)?** It has the same
  "framework owns the pool" shape (`src/kntgraph/infra/graph/_lite_pool.py`)
   and its `close()` is synchronous. Whether it needs the same
   disconnect-not-close narrowing is a separate question — the FalkorDB
   driver has a different lifecycle contract.

---

## 8. References

- redis-py `Redis.__init__` — the `auto_close_connection_pool` contract quoted
  in §1.2.
- redis-py `Redis.aclose` / `ConnectionPool.aclose` / `ConnectionPool.disconnect`
  — the close semantics used in §3.1 and §3.3.
- [ADR-082](./ADR-082-Promote-Redis-Transport-Defense-In-Depth.md) — the
  reconnect path this ADR narrows.
- [ADR-077](./ADR-077-Storage-Adapter-Exception-Translation.md) — the
  narrow-catch and typed-error discipline.
- [ADR-078](./ADR-078-Decommision-ProcessPoolExecutor.md) — the sibling
  process-pool workstream; its §1.4 scenario is the load profile in §1.6.
- [`src/kntgraph/infra/config/__init__.py`](../src/kntgraph/infra/config/__init__.py)
  — the `fresh_settings()` precedent quoted in §2.2.
- [`examples/knt-cli/weather_platform/.../main.py`](../examples/knt-cli/weather_platform/src/weather_platform/main.py)
  — the scaffold that already wires one shared client.

---

## 9. Revision history

- **2026-10-23** — Initial draft. Ratifies the caller-owns-the-pool lifecycle
  after measuring three connection-lifecycle defects against a live Redis.
- **2026-10-24** — §6.4: the version label for the
  `RedisClientRequiredError` moved from "v0.18.x" to "v0.17.0",
  the release it actually shipped in. Behaviour and removal
  targets unchanged.
