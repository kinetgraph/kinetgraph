<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# MetricsSink — pluggable observability for the dispatcher (ADR-075 Tier 4)

The framework exposes four ADR-075 Tier 4 read queries on the
`ReactiveDispatcher` plus a saga-side signal (`compensation_started`
events). Pushing those numbers to a metrics backend is useful for SRE
dashboards and alerts but must **not** make `prometheus_client` (or any
other backend) a hard dependency of the framework.

The pattern mirrors the LLM adapter, the graph client, and the GLiNER
entity extractor:

- The framework defines a small Protocol (`MetricsSink`) with the
  five primitives the dispatcher actually emits.
- A no-op implementation (`NullMetricsSink`) is the default; the
  dispatcher works without any backend installed.
- Concrete sinks (Prometheus, OpenTelemetry, statsd, ...) live in
  their own modules and are pulled in by their respective extras
  (`kntgraph[metrics]`, `kntgraph[otel]`).

> **Status**: implemented. The framework ships
> `MetricsSink` + `NullMetricsSink` (always installed) and
> `PrometheusMetricsSink` (under the `kntgraph[metrics]` extra).

---

## 1. What you get

- **No backend by default** — `NullMetricsSink` is wired in
  automatically; the dispatcher's hot path is unchanged.
- **Five primitives** — `record_in_flight`, `record_stale`,
  `record_stuck_in_queue`, `record_dead_lettered`,
  `incr_compensation_started`. Each one has a clear semantic; you
  can wire any backend that exposes these.
- **Drop-in Prometheus sink** — `PrometheusMetricsSink` exposes four
  `Gauge` instances + one `Counter`; works with the standard
  `prometheus_client.start_http_server(port)`.
- **No thread-safety story required from the framework** — the
  Protocol's methods are sync because the dispatcher's hot path
  is sync w.r.t. metrics. Async backends wrap their transport in
  `asyncio.run_coroutine_threadsafe` or a background queue.

---

## 2. The Protocol surface

The five primitives mirror ADR-075's Tier 4 surface plus the saga
crash-safety marker:

| Method | Called by | Meaning |
| --- | --- | --- |
| `record_in_flight(count)` | `dispatcher.in_flight_tasks()` | Tool tasks currently waiting for a terminal event (no `completed` / `failed` yet). |
| `record_stale(count)` | `dispatcher.stale_tasks()` | Subset of in-flight whose TTL has passed the recovery threshold. Spike ⇒ TTL sweeper is behind. |
| `record_stuck_in_queue(count)` | `dispatcher.stuck_in_queue()` | Tool queues with backlog and no active consumer. Sustained non-zero ⇒ worker pool down. |
| `record_dead_lettered(count)` | `dispatcher.dead_lettered_tasks()` | DLQ entries awaiting operator action. Leading indicator of saga compensation failures. |
| `incr_compensation_started()` | The dispatcher's tick loop | One increment per `*.compensation_started` event appended to the EventLog. |

The split (query-side gauges vs. event-side counter) reflects how the
signals are produced: the four gauges are sampled by the operator (a
cron, an alert, a dashboard refresh); the compensation counter is
incremented in real time as events flow. A single sink that exposes
a `CollectorRegistry` can serve both at `/metrics`; a sink that
splits query and event backends can implement each method
independently.

---

## 3. Wiring (Prometheus)

```python
# Install the extra
# uv pip install "kntgraph[metrics]"

from prometheus_client import start_http_server
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._world_checkpoint._redis import (
    RedisWorldCheckpointStorage,
)
from kntgraph.infra.world_checkpoint import IncrementalWorldStore
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.runner.metrics.prometheus import (
    PrometheusMetricsSink,
)
from kntgraph.stream.event_log import EventLog

# Start the /metrics endpoint on a dedicated port
# (the Prometheus scrape job will hit this).
start_http_server(port=9100)

# Wire the sink into the dispatcher. The default sink is
# ``NullMetricsSink``; here we swap it for the Prometheus
# implementation.
sink = PrometheusMetricsSink()
dispatcher = ReactiveDispatcher(
    log=EventLog(RedisEventLogAdapter(redis_client)),
    world_store=IncrementalWorldStore(
        RedisWorldCheckpointStorage(redis_client)
    ),
    metrics_sink=sink,
)

# Run as usual. From now on, every call to
# ``dispatcher.in_flight_tasks()`` (and friends) pushes to the
# Prometheus registry, and every ``*.compensation_started``
# event appended to the EventLog increments the counter.
dispatcher.run()
```

The `start_http_server(port)` helper is the standard
`prometheus_client` entry point; it runs the HTTP server in a daemon
thread. The framework does not own the thread's lifecycle; stop it
via `prometheus_client.core.REGISTRY` if you need a clean shutdown.

---

## 4. Wiring (custom backend)

Any class with the five methods is a `MetricsSink` (the Protocol is
`runtime_checkable`):

```python
from kntgraph.runner import MetricsSink

class StatsdSink:
    """Push metrics to a StatsD UDP endpoint."""

    def __init__(self, host: str = "localhost", port: int = 8125) -> None:
        import statsd
        self._client = statsd.StatsClient(host, port)

    def record_in_flight(self, count: int) -> None:
        self._client.gauge("knt.in_flight_tasks", count)

    def record_stale(self, count: int) -> None:
        self._client.gauge("knt.stale_tasks", count)

    def record_stuck_in_queue(self, count: int) -> None:
        self._client.gauge("knt.stuck_in_queue", count)

    def record_dead_lettered(self, count: int) -> None:
        self._client.gauge("knt.dead_lettered_tasks", count)

    def incr_compensation_started(self) -> None:
        self._client.incr("knt.compensations_started")

# Wire it:
dispatcher = ReactiveDispatcher(
    log=...,
    world_store=...,
    metrics_sink=StatsdSink(host="metrics.internal", port=8125),
)
```

> **Tip**: when implementing your own sink, batch the writes if your
> backend has per-call overhead (e.g. UDP socket per metric). The
> dispatcher calls the sink once per method invocation; debouncing
> inside the sink is fine.

---

## 5. Reading the metrics

The four gauge methods fire when the corresponding dispatcher method
is called:

```python
# Operator-driven dashboards / cron / alerts
in_flight = await dispatcher.in_flight_tasks()  # pushes to sink
stale = await dispatcher.stale_tasks()           # pushes to sink
```

The counter method fires **once per `*.compensation_started` event
appended to the EventLog** during the dispatcher's tick loop. No
operator action is required — the counter is incremented in real
time as compensation flows.

A minimal Prometheus alert:

```yaml
groups:
  - name: kntgraph_compensation
    rules:
      - alert: CompensationSurge
        # More than 100 compensation_started events in 5 minutes
        # usually means a downstream dependency is failing.
        expr: rate(knt_saga_compensations_started_total[5m]) > 100
        for: 5m
        labels:
          severity: warning
```

---

## 6. Operational notes

### Idempotency

The Protocol's methods are best-effort. A sink that fails internally
(e.g. a dropped UDP packet, a closed socket) **must not crash the
dispatcher**. Wrap your backend's write in a try/except and log
inside the sink, not in the dispatcher.

### Threading

The dispatcher calls all sink methods from the asyncio event loop
(the same thread that runs the tick). Sinks that need to talk to a
synchronous backend (e.g. `statsd`) can write directly. Sinks that
need to talk to an async backend should:

- Buffer writes in a thread-safe queue.
- Drain the queue from a background thread or task that does
  `loop.call_soon_threadsafe(queue.drain, loop)`.

### Multi-process / multi-tenant

`prometheus_client` exposes a singleton `REGISTRY` by default. In a
multi-process deployment (e.g. multiple workers behind one Prometheus
scrape), use `prometheus_client.multiprocess` to aggregate per-process
counters. The `PrometheusMetricsSink` accepts a custom
`CollectorRegistry` per sink instance, so you can isolate per-tenant
metrics by constructing one sink per tenant.

### Anti-patterns

- ❌ Constructing a sink that holds the dispatcher's reference
  (circular dep). Pass only the metrics you need; the dispatcher
  never calls back into the sink except via the five primitives.
- ❌ Blocking the tick loop in a sink (e.g. synchronous HTTP POST).
  Buffer and flush in a background thread.
- ❌ Using the same `Gauge` / `Counter` labels for in-flight and
  dead-lettered tasks (they are different signals; gauge name
  collisions cause silent overwrites).

---

## 7. References

- ADR-075 §2.3 — Tier 4 observability queries.
- `src/kntgraph/runner/_metrics.py` — Protocol + Null.
- `src/kntgraph/runner/metrics/prometheus.py` — Prometheus
  implementation (extra `kntgraph[metrics]`).
- `src/kntgraph/runner/reactive.py:ReactiveDispatcher` — sink
  wiring (`metrics_sink=` constructor arg).
- `tests/unit/runner/test_metrics_sink.py` — Protocol + Null
  contract tests.
- `tests/unit/runner/test_metrics_integration.py` — dispatcher
  wiring tests (recording sink).
- `tests/unit/runner/test_metrics_prometheus.py` — Prometheus
  implementation tests (private registry isolation).
- `tests/integration/test_metrics_sink_e2e.py` — end-to-end
  pipeline through real Redis (`clean_redis` fixture).
