<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Redis connection lifecycle

> **One connection pool per process, owned by the caller.** The
> framework will not build a pool you cannot close. This is
> [ADR-084](../ADRs/ADR-084-Redis-Connection-Pool-Lifecycle.md).

## 1. The rule

Create the pool once, share its client with everything, close it
on shutdown:

```python
from kntgraph.infra.config import Settings
from kntgraph.infra.redis import (
    RedisEventLogAdapter,
    create_redis_pool,
    create_tool_router,
    create_worker_manager,
)
from kntgraph.stream.event_log.store import EventLog

pool = create_redis_pool(Settings())
redis = pool.client

log = EventLog(RedisEventLogAdapter(client=redis))
tool_router = create_tool_router(client=redis)
worker_manager = create_worker_manager(redis=redis, event_log=log)

# ... at shutdown:
await worker_manager.stop()
await pool.aclose()
```

Every `create_*` factory **requires** a `client=`. Passing none
raises `RedisClientRequiredError`:

```
RedisClientRequiredError: a Redis client is required: create one
with create_redis_pool(settings) and pass client=pool.client. The
framework will not build a connection pool it cannot close
(ADR-084 §3.2).
```

That is deliberate. A pool built inside a factory is unreachable
from outside it, so its sockets leak for the life of the process.

## 2. Why one pool, and not one per adapter

A saturated pool is the failure mode to understand. redis-py
**does not queue** when the pool is exhausted — it raises
`MaxConnectionsError: Too many connections` immediately.

Each registered tool runs its own consume loop, and each
`XREADGROUP` holds its socket for the duration of the `block`
window. So the pool needs at least as many connections as there
are registered tools:

| Registered tools | `redis_max_connections` | Result |
|---|---|---|
| 13 | 50 (default) | OK |
| 13 | 13 | OK — at the threshold |
| 13 | 10 | **69% of reads fail** |
| 13 | 5 | **88% of reads fail** |

Size `KNT_REDIS_MAX_CONNECTIONS` against the number of registered
tools, with headroom for concurrent commands. The default of 50
comfortably covers a single shared pool for up to ~40 tools.

## 3. What the framework does when the pool saturates

Since ADR-084, saturation is **not** treated as a broken
transport:

- **Saturation** (`MaxConnectionsError`) → the consume loop backs
  off (0.25 s, doubling to a 2 s ceiling) and logs
  `worker.pool_saturated`. The pool is left alone.
- **Transport failure** (connection reset, timeout, half-open
  TCP) → the reconnect path closes the pool's *sockets* and the
  next command reconnects.

Before ADR-084, saturation was classified as a transport failure
and the reconnect path tore the whole pool down. Every sibling
consumer lost its socket, raised `MaxConnectionsError` too, and
teared the pool down again — a momentary burst became a
reconnect storm.

## 4. Closing

`RedisPool.aclose()` closes the pool **and** every connection it
holds. It is idempotent, best-effort (a transport error is logged,
not raised), and it lets `asyncio.CancelledError` propagate.

Call it from your shutdown path — the FastAPI lifespan, the CLI
`main()`, or a `finally` block:

```python
@asynccontextmanager
async def lifespan(app):
    await worker_manager.start()
    try:
        yield
    finally:
        await worker_manager.stop()
        await pool.aclose()
```

## 5. Gotcha: a tool that crashes its own process

A tool that calls `os._exit`, `sys.exit`, or triggers a segfault
runs on the Manager's event loop by default — so the crash takes
the **Manager** down with it: no consume loop, no reaper, no
`XAUTOCLAIM`, and the pending message is never recovered.

Such a tool must declare a `ProcessPoolFactory` so it runs in a
worker process, where only the worker dies and the Manager reaps
the message. See
[ADR-078 §12.5](ADR-078-Decommision-ProcessPoolExecutor.md).

## 6. Troubleshooting

### "Too many connections"

The pool is exhausted. Either raise `KNT_REDIS_MAX_CONNECTIONS` or
reduce the number of registered tools. The framework logs
`worker.pool_saturated` with the tool name and the back-off it
applied.

### "AuthenticationError: HELLO must be called with the client already authenticated"

A shared pool was torn down by a sibling's reconnect while this
adapter still had a socket from it. ADR-084 §3.3 narrows the
reconnect path to close the sockets without closing the pool; if
you see this, check that every component uses the **same** client
from the **same** pool.

### The connections never go away

Check that `pool.aclose()` is actually called on shutdown, and
that the pool you create is the one you close. The framework
cannot close a pool it cannot reach — that is the whole point of
§1.

## 7. Reference

- [ADR-084](../ADRs/ADR-084-Redis-Connection-Pool-Lifecycle.md) —
  the decision, the measurements, and the migration plan
- `src/kntgraph/infra/redis/_pool.py` — `RedisPool`
- `src/kntgraph/infra/redis/_factory.py` — the `create_*` factories
- `src/kntgraph/infra/redis/_transport.py` — the saturation /
  transport classifiers
