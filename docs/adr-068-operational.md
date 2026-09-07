<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-068 Operational Guide

## Redis Configuration

### Running Redis

The framework requires a running Redis instance (Redis 6+ or Redis 7+/FalkorDB).

```bash
# Start Redis via Docker (recommended for development)
docker run -d -p 6379:6379 --name kntgraph-redis redis:latest

# Verify
docker ps | grep kntgraph-redis

# Or run Redis locally
redis-server --save "" --appendonly no
```

### Connection String

The framework uses `KNT_REDIS_URL` to connect to Redis:

```bash
# With password (default setup)
export KNT_REDIS_URL="redis://:redispassword@localhost:6379"

# Without password (development)
export KNT_REDIS_URL="redis://localhost:6379"

# With custom password
export KNT_REDIS_URL="redis://:mypassword@localhost:6379/0"
```

### Redis Cluster Considerations

The current implementation uses a **single Redis instance** (standalone mode). 
Cluster mode (Redis Cluster) is **not supported** for the EventLog 
subscription feature (P1/P2) due to the difficulty of guaranteeing 
stream ownership across shards.

## KNT_* Environment Variables (ADR-068 §3.8)

The following environment variables control the reactive dispatcher 
behavior and can be tuned without code redeployment:

| Variable | Default | Description | Example |
|----------|---------|-------------|---------|
| `KNT_REACTIVE_POLL_INTERVAL` | `0.25` | Poll interval for the reactive dispatcher (seconds) | `export KNT_REACTIVE_POLL_INTERVAL=1.0` |
| `KNT_REACTIVE_REDISCOVERY_SECONDS` | `5` | How often the dispatcher re-discovers agents (seconds) | `export KNT_REACTIVE_REDISCOVERY_SECONDS=10` |
| `KNT_WARMER_PUMP_INTERVAL` | `0.25` | How often the cache warmer runs its pump (seconds) | `export KNT_WARMER_PUMP_INTERVAL=2.0` |
| `KNT_FALLBACK_POLL_INTERVAL` | `5` | Fallback poll interval when no events are received (seconds) | `export KNT_FALLBACK_POLL_INTERVAL=10` |

### Verifying at Runtime

```bash
python -c "
from kntgraph.infra.config import Settings
s = Settings()
print(f'reactive_poll_interval: {s.reactive_poll_interval}')
print(f'reactive_rediscovery_seconds: {s.reactive_rediscovery_seconds}')
print(f'warmer_pump_interval: {s.warmer_pump_interval}')
print(f'fallback_poll_interval: {s.fallback_poll_interval}')
"
```

## Resilience to Redis Failures

### Redis Temporarily Unavailable

When Redis goes temporarily unavailable:

1. **Wake loop degradation**: The `subscribe` call raises an exception, 
   which is caught and logged. The dispatcher then falls back to 
   `dispatch_once()` which runs the full tick pipeline.

2. **Convergence**: The `dispatch_once()` call ensures that any pending 
   system work (TTL sweeper, `_pending_results`) is still processed.

3. **Recovery**: When Redis comes back online, the dispatcher resumes 
   using the last saved cursor. No events are lost due to the idempotency 
   guarantees (ADR-005).

### Redis Latency

If Redis becomes slow (high latency but still responsive):

1. The `subscribe` call blocks for up to `KNT_FALLBACK_POLL_INTERVAL` 
   seconds (default: 5s).

2. If events arrive within that window, they are processed immediately.

3. If the timeout expires without events, the fallback poll runs, 
   processing any pending work and then re-subscribing.

4. **Impact**: Increased latency but no data loss or crashes.

### Redis Going Down Completely

When Redis is completely unavailable:

1. **All subscriber connections are lost**.

2. **The wake loop catches the exception** and falls back to `dispatch_once()`.

3. **System work continues**: The TTL sweeper and `_pending_results` 
   systems still run on each tick.

4. **Recovery**: When Redis comes back online, the dispatcher resumes 
   using the last saved cursor. No events are lost due to the 
   idempotency guarantees.

### Monitoring Recommendations

- Monitor `KNT_REDIS_POLL_INTERVAL` changes via logs
- Track `reactive.wake.subscribe_failed` warning frequency
- Set up alerts if `subscribe_failed` occurs repeatedly
- Monitor Redis connectivity and latency

## Running with fakeredis (CI/CD)

For CI environments without a Redis server, the framework supports 
`KNT_REDIS_FAKE=1` which uses [fakeredis](https://github.maximuskim/fakeredis).

```bash
# Run tests with fakeredis
KNT_REDIS_FAKE=1 uv run pytest tests/unit/ -q

# Or set permanently in .env
echo "KNT_REDIS_FAKE=1" >> .env
```

**Note**: The fakeredis implementation must support blocking `XREAD` 
for the wake loop tests to pass. The current CI configuration 
validates this requirement.

## Running the ReactiveDispatcher

The ReactiveDispatcher's wake loop operates in two modes:

### Wake Path (event-driven)

1. The dispatcher blocks in `subscribe_many()` (one held connection 
   for all agents via fan-in).

2. When an event arrives, the dispatcher processes it immediately.

3. After processing, the dispatcher immediately re-subscribes.

4. **Idle cost**: One held connection, zero round-trips.

### Fallback Path (poll-driven)

1. A timer fires every `KNT_FALLBACK_POLL_INTERVAL` seconds (default: 5s).

2. The timer triggers `dispatch_once()`, which runs the full tick 
   pipeline.

3. If events were processed, the dispatcher resumes the wake loop.

4. **Purpose**: Ensures system work continues even when no events 
   arrive (covers TTL sweeper, `_pending_results`, etc.).

### Example: Monitoring Dispatcher Logs

```bash
# Watch for subscribe_failed warnings
docker logs kntgraph-redis 2>&1 | grep "subscribe_failed"

# Or via structured logging
python -c "
import structlog
from kntgraph.infra.redis._event_log._adapter import RedisEventLogAdapter
# Monitor subscribe_failed events
"
```

## Development Setup

### Local Development with Redis

```bash
# Start Redis
docker run -d -p 6379:6379 --name kntgraph-redis redis:latest

# Run tests with Redis
KNT_REDIS_URL="redis://:redispassword@localhost:6379" uv run pytest tests/unit/ -q

# Or with fakeredis
KNT_REDIS_FAKE=1 uv run pytest tests/unit/ -q
```

### Development without Redis

For development or CI without a Redis server:

```bash
KNT_REDIS_FAKE=1 uv run pytest tests/unit/ -q
```

**Note**: Some tests (particularly integration-style tests) require Redis 
and will be skipped when `KNT_REDIS_FAKE=1` causes a "no tests running" 
condition. This is expected and handled by the test infrastructure.

## ADR-068 Related Links

- [ADR-068 Full Text](adr-068-idle-redis-traffic-and-eventlog-subscribe.md)
- [Quick Start](quickstart.md)
- [Architecture](architecture.md)
- [CLI Guide](cli_guide.md)
- [Resilience Guide](resilience.md)
