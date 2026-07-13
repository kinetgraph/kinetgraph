<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-035: Sharding and Dispatcher Coordination for Horizontal Scaling

**Status:** Proposed (Under Review)

**Date:** July 12, 2026

**Version:** 2.0 (Revised)

**Authors:** Kinetgraph Architecture Team

**Related:** [ADR-001](./ADR-001-Architecture.md), [ADR-005](./ADR-005-Checkpoints-Idempotency.md), [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md), [ADR-039](./ADR-039-Role-rethinking-and-intentions-routing.md)

---

## 1. Context

The evolution of the `fmh_backend` to the incremental `World` model (ADR-018) reduced the `ReactiveDispatcher` latency to the millisecond range ($O(M)$). However, the dispatcher's current design is based on a *Single-Node* architecture (a single polling process).

Currently, the dispatcher iterates over a static in-memory list:

```python
for agent_id in list(self._agents):
    # Fetch events, Fold, Run Systems, Save Checkpoint
```

As our volume grows (10k+ agents), we need to scale the framework horizontally by adding new Pods (servers). If we deploy 10 instances of the current codebase, we will trigger a **Thundering Herd** scenario: all 10 instances will attempt to process the exact same list of agents simultaneously.

While the EventLog's idempotency (ADR-001) and Checkpoints (ADR-005) prevent data corruption, this concurrency will generate massive CPU waste (9 out of 10 pods will perform redundant *folds*) and cause severe contention on Redis.

We need a **coordination (sharding)** strategy to ensure each `agent_id` is processed by **only one Pod at a time**, with deterministic ownership and zero coordination overhead.

---

## 2. Proposed Decision

Adopt a **Vnode-based Hash Ring Partitioning** with a **Partitioned Wakeup Stream**. Each pod owns a deterministic range of the hash space, and the EventLog remains the single source of truth in a standalone Redis instance.

### 2.1 Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     Kubernetes Cluster                       │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ Knetgraph    │  │ Knetgraph    │  │ Knetgraph    │      │
│  │ Pod A        │  │ Pod B        │  │ Pod C        │      │
│  │ vnodes:      │  │ vnodes:      │  │ vnodes:      │      │
│  │ [42,137,891] │  │ [7,234,512]  │  │ [99,456,789] │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                  │                  │               │
│         └──────────────────┼──────────────────┘              │
│                            │                                  │
│                            ▼                                  │
│                  ┌──────────────────┐                         │
│                  │   Redis          │                         │
│                  │   (StatefulSet)  │                         │
│                  │                  │                         │
│                  │  - EventLog      │                         │
│                  │  - Checkpoints   │                         │
│                  │  - Wakeup stream │                         │
│                  │  - Cluster reg.  │                         │
│                  │  - Pub/sub       │                         │
│                  └──────────────────┘                         │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 Application Lifecycle (Bootstrap)

When a pod starts, it performs a deterministic handshake with Redis to join the cluster:

```python
# Phase 1: Register pod identity
POD_ID = os.getenv("POD_NAME") or uuid4()
await redis.set(
    f"knt:pod:{POD_ID}",
    json.dumps({
        "host": socket.gethostname(),
        "port": os.getenv("POD_PORT", 8000),
        "started_at": time.time(),
    }),
    ex=30,  # TTL: 30 seconds
)

# Phase 2: Add to cluster registry (sorted by boot time)
await redis.zadd("knt:cluster:registry", {POD_ID: time.time()})

# Phase 3: Watchdog refreshes TTL every 10 seconds
async def watchdog():
    while running:
        await redis.expire(f"knt:pod:{POD_ID}", 30)
        await asyncio.sleep(10)

# Phase 4: Discover cluster membership
active_pods = await redis.zrange("knt:cluster:registry", 0, -1)

# Phase 5: Compute vnode assignments
my_vnodes = compute_vnodes(POD_ID, len(active_pods))

# Phase 6: Start ReactiveDispatcher with PartitionScope
scope = PartitionScope(pod_id=POD_ID, vnodes=my_vnodes)
dispatcher = ReactiveDispatcher(log, systems=systems, scope=scope)
```

### 2.3 Vnode-based Hash Ring Partitioning

Each pod owns a subset of **1024 virtual nodes** (vnodes) distributed uniformly across a hash ring. This reduces skew compared to modular hashing and enables incremental rebalancing.

```python
VNODES_PER_POD = 256
HASH_VNODES = 1024

def compute_vnodes(pod_id: str, total_pods: int) -> list[int]:
    """
    Compute the vnode assignments for a pod.
    
    Each pod gets VNODES_PER_POD vnodes, hashed uniformly
    across the ring. This ensures that:
    - Load is distributed evenly (vnodes are uniform)
    - Rebalancing affects only ~1/N of agents
    - Hot agents don't concentrate on one pod
    """
    if total_pods == 0:
        return []
    
    # Use pod_id + index to generate vnode hashes
    vnode_hashes = []
    for v in range(VNODES_PER_POD):
        h = int(hashlib.md5(f"{pod_id}:{v}".encode()).hexdigest()[:4], 16)
        vnode_hashes.append(h % HASH_VNODES)
    
    return sorted(set(vnode_hashes))

def owns(agent_id: str, my_vnodes: set[int]) -> bool:
    """Check if this pod owns the given agent_id."""
    h = int(hashlib.md5(agent_id.encode()).hexdigest()[:4], 16)
    vnode = h % HASH_VNODES
    return vnode in my_vnodes
```

**Why vnodes instead of modular hashing:**

- **Skew reduction:** If `agent_id`s are not uniform (e.g., `agent-1`, `agent-2`, ...), modular hashing concentrates load on one pod. Vnodes distribute uniformly.
- **Incremental rebalancing:** When a pod joins/leaves, only `~1/N` of vnodes migrate, not `~1/N` of agents.
- **Battle-tested:** Cassandra, Riak, Kafka use the same approach.

### 2.4 Partitioned Wakeup Stream

Instead of a global consumer group (which requires `XPENDING` GC and re-entrancy management), we use **per-vnode streams** with deterministic routing:

```python
def wakeup_stream_for(agent_id: str) -> str:
    """
    Route a wakeup to the correct stream based on agent_id.
    
    The publisher computes the vnode and writes to the
    corresponding stream. The pod that owns that vnode
    consumes from it. No coordination needed.
    """
    h = int(hashlib.md5(agent_id.encode()).hexdigest()[:4], 16)
    vnode = h % HASH_VNODES
    return f"knt:wakeup:{vnode}"
```

**Publisher (HTTP Gateway, Scheduler):**

```python
async def notify_agent(agent_id: str):
    stream = wakeup_stream_for(agent_id)
    await redis.xadd(stream, {"agent_id": agent_id})
```

**Consumer (ReactiveDispatcher):**

```python
class ReactiveDispatcher:
    async def dispatch_once(self):
        # Each pod reads only the streams it owns
        my_streams = {
            f"knt:wakeup:{v}": ">" 
            for v in self._scope.vnodes
        }
        
        messages = await redis.xreadgroup(
            group=f"knt:dispatchers_group:{POD_ID}",
            consumer=POD_ID,
            streams=my_streams,
            count=batch_size,
            block=poll_interval,
        )
        
        for stream, entries in messages:
            for msg_id, data in entries:
                agent_id = data["agent_id"]
                
                # Double-check ownership (in case of rebalance)
                if not self._scope.owns(agent_id):
                    # Re-route to correct stream
                    correct_stream = wakeup_stream_for(agent_id)
                    await redis.xadd(correct_stream, data)
                    await redis.xack(stream, group, msg_id)
                    continue
                
                # Process agent
                await self._dispatch_agent(agent_id)
                await redis.xack(stream, group, msg_id)
```

**Advantages over global consumer group:**

- ✅ **No XPENDING GC** — each vnode has exactly one owner
- ✅ **No re-entrancy** — no need for fencing tokens
- ✅ **Zero filter overhead** — publisher routes deterministically
- ✅ **Backpressure isolation** — hot agents don't block cold ones (different vnodes)

### 2.5 WorldScope Integration

The `ReactiveDispatcher` constructs a `PartitionScope` that filters events to only the agents this pod owns. The `World` is always scoped, never global.

```python
@dataclass(frozen=True)
class PartitionScope:
    """Scope that includes only agents owned by this pod's vnodes."""
    
    pod_id: str
    vnodes: tuple[int, ...]
    
    def matches_agent(self, agent_id: str) -> bool:
        h = int(hashlib.md5(agent_id.encode()).hexdigest()[:4], 16)
        vnode = h % 1024
        return vnode in self.vnodes
    
    def filter_events(self, events: list[Event]) -> list[Event]:
        return [e for e in events if self.matches_agent(e.agent_id)]
    
    def describe(self) -> str:
        return f"partition(pod={self.pod_id}, vnodes={len(self.vnodes)})"
```

**World construction:**

```python
@classmethod
def fold(cls, events, *, scope: WorldScope = NullScope(), ...):
    scoped_events = scope.filter_events(events)
    views = projection(scoped_events)
    # ... rest of fold logic
    world._scope = scope
    return world
```

**Consequence:** The pod's `World` contains **only** the agents it owns. The `IntentResolutionSystem` (ADR-039) operates on a naturally-restricted view without any code changes.

### 2.6 Rebalancing on Pod Join/Leave

When a pod joins or leaves the cluster, vnodes are reassigned. This requires:

1. **Lease fence:** Each checkpoint includes a `lease_until` timestamp. A new owner waits for the lease to expire before processing.
2. **Drain coroutine:** A pod receiving `SIGTERM` drains its current work-in-progress before exiting.
3. **Idempotency fallback:** If a lease expires mid-processing, the new owner re-processes the same events. EventLog deduplication (ADR-001) ensures correctness.

**Watchdog for membership changes:**

```python
async def watch_cluster():
    prev_members = set()
    while running:
        current = set(await redis.zrange("knt:cluster:registry", 0, -1))
        if current != prev_members:
            logger.info("cluster.membership_changed",
                       added=current - prev_members,
                       removed=prev_members - current)
            
            # Recompute vnode assignments
            new_vnodes = compute_vnodes(POD_ID, len(current))
            
            # Wait for old leases to expire (5 minutes)
            await drain_old_lease(old_vnodes)
            
            # Atomically update scope
            self._scope = PartitionScope(pod_id=POD_ID, vnodes=new_vnodes)
            prev_members = current
        
        await asyncio.sleep(5)
```

---

## 3. Considered Alternatives (Rejected)

### 3.1 Strategy A: Consistent Hashing with StatefulSets (Original)

Each pod is assigned an ordinal ID (0, 1, 2) and processes only the agents where `hash(agent_id) % TOTAL_PODS == MY_ID`.

- **Pros:** Zero extra network dependencies; no database contention.
- **Cons:** Scaling dynamically from 3 to 4 pods rebalances the hash math. Agents "jump" between machines, which renders any hot memory caches useless and forces mass cold boots.
- **Verdict:** Rejected due to operational inflexibility and cache invalidation.

### 3.2 Strategy B: Distributed Locks (Redis SET NX)

Before processing an agent, the pod attempts to acquire a temporary lock (`knt:lock:{agent_id}`).

- **Pros:** Extremely simple to implement in the current codebase.
- **Cons:** Predatory network traffic. With 50 pods and 10,000 agents, thousands of `SET NX` requests per second return `False`, saturating Redis's single-thread with lock checks.
- **Verdict:** Rejected due to Redis saturation.

### 3.3 Strategy C: Global Consumer Group (Original ADR-035 v1.0)

Use a single `knt:wakeup` stream with one consumer group. Redis guarantees exclusive delivery.

- **Pros:** Simple mental model; Redis handles load balancing.
- **Cons:** 
  - Requires `XPENDING` GC for crashed pods
  - Hot agents can monopolize one pod
  - No cache locality (any pod can get any agent)
- **Verdict:** Rejected in favor of partitioned streams (this ADR).

### 3.4 Strategy D: External Cluster Manager (Consul, etcd)

Delegate membership to an external system.

- **Pros:** Battle-tested; service discovery built-in.
- **Cons:** Additional operational complexity; Redis already covers the use case; YAGNI.
- **Verdict:** Rejected. Redis is the single authority.

---

## 4. Consequences of the Chosen Decision

### Positives

- ✅ **Zero CPU waste:** Each pod processes only agents it owns.
- ✅ **Infinite horizontal scaling:** Add pods without coordination overhead.
- ✅ **Cache locality:** Hot agents stay on the same pod (vnode stability).
- ✅ **Backpressure isolation:** Hot agents don't block cold ones (different vnodes).
- ✅ **No XPENDING GC:** Each vnode has exactly one owner.
- ✅ **Observability:** `world.scope.describe()` logs exactly what each pod sees.
- ✅ **Backward compatible:** `NullScope` preserves existing behavior.

### Negatives / Risks

- ⚠️ **Rebalancing latency:** When a pod joins/leaves, ~1/N of agents migrate. Mitigated by lease fence + drain coroutine.
- ⚠️ **Skew potential:** If `agent_id`s are highly non-uniform, some pods may have more load. Mitigated by vnodes (256 per pod) which distribute uniformly.
- ⚠️ **Membership authority:** Redis is the single source of truth for cluster state. If Redis is down, the cluster cannot rebalance. Mitigated by persistent volumes + snapshots (see §5).
- ⚠️ **Boot order dependency:** `booted_at` timestamp determines vnode assignment. Two pods booting in the same second could collide. Mitigated by adding `hash(POD_NAME)` as tiebreaker.

---

## 5. HA Strategy for Redis (Standalone)

The Kinetgraph cluster depends on Redis being available. We use a **standalone Redis** with **AOF persistence** and **periodic snapshots**.

### 5.1 Container with Volume (Development/Staging)

```yaml
# docker-compose.yml
services:
  redis:
    image: redis:7-alpine
    command: >
      redis-server
      --appendonly yes
      --appendfsync everysec
      --save 60 1000
      --maxmemory 4gb
      --maxmemory-policy noeviction
    volumes:
      - redis-data:/data
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 3

volumes:
  redis-data:
    driver: local
```

**Critical configuration:**

- **AOF + RDB:** AOF for durability, RDB for snapshots
- **`maxmemory-policy noeviction`:** Never evict keys (events are sacred)
- **Health check:** Enables orchestrator to detect failures

### 5.2 Kubernetes with PVC and Snapshots (Production)

```yaml
# redis-statefulset.yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis
spec:
  serviceName: redis
  replicas: 1
  selector:
    matchLabels: { app: redis }
  template:
    metadata:
      labels: { app: redis }
    spec:
      containers:
      - name: redis
        image: redis:7-alpine
        command:
          - redis-server
          - --appendonly=yes
          - --appendfsync=everysec
          - --maxmemory=4Gi
          - --maxmemory-policy=noeviction
        resources:
          requests: { memory: "5Gi", cpu: "500m" }
          limits: { memory: "6Gi", cpu: "2000m" }
        ports:
          - { containerPort: 6379, name: redis }
        volumeMounts:
          - { name: data, mountPath: /data }
        readinessProbe:
          exec: { command: ["redis-cli", "ping"] }
          initialDelaySeconds: 5
          periodSeconds: 5
        livenessProbe:
          exec: { command: ["redis-cli", "ping"] }
          initialDelaySeconds: 30
          periodSeconds: 10
  volumeClaimTemplates:
  - metadata: { name: data }
    spec:
      accessModes: ["ReadWriteOnce"]
      storageClassName: ssd-premium
      resources:
        requests: { storage: 50Gi }
```

**Periodic snapshots via CronJob:**

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: redis-snapshot
spec:
  schedule: "0 */6 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: redis-snapshotter
          containers:
          - name: snapshotter
            image: bitnami/kubectl
            command: ["/bin/sh", "-c", "..."]
          restartPolicy: OnFailure
```

### 5.3 Why Not Redis Cluster or Sentinel?

- **Redis Cluster:** Deferred until volume > 25GB or throughput > 100k ops/s. Standalone covers 90% of deployments.
- **Redis Sentinel:** Deferred until multi-region HA is required. Single-node with PVC + snapshots is sufficient for most use cases.
- **External coordinator (Consul, etcd):** YAGNI. Redis already covers membership.

---

## 6. Migration Plan (Rollout)

### Phase 1: Core Infrastructure (Week 1-2)

1. Implement `PartitionScope` in `core/world/scope.py`
2. Refactor `World.fold` to accept `scope` parameter
3. Add `World.with_event` filtering by scope
4. Update `ReactiveDispatcher` to construct `PartitionScope` on boot
5. Implement vnode computation in `infra/cluster_membership.py`

### Phase 2: Wakeup Stream (Week 3)

1. Create `knt:wakeup:{vnode}` streams on first boot (1024 streams)
2. Update HTTP Gateway to publish to `wakeup_stream_for(agent_id)`
3. Update schedulers to use the same routing logic
4. Refactor `ReactiveDispatcher` to consume from owned vnode streams
5. Add `XACK` after successful processing

### Phase 3: Rebalancing (Week 4)

1. Implement lease fence on checkpoints (`lease_until` field)
2. Implement drain coroutine for graceful shutdown
3. Add cluster membership watchdog (poll + pub/sub)
4. Test rebalancing scenarios (pod join/leave/during processing)

### Phase 4: Observability (Week 5)

1. Expose Prometheus metrics:
   - `knt_cluster_active_pods`
   - `knt_cluster_vnode_assignments`
   - `knt_world_scope_entities`
   - `knt_wakeup_stream_lag`
2. Add alerts for:
   - `knt_cluster_active_pods == 0`
   - `knt_wakeup_stream_lag > 10000`
3. Document operational runbooks

### Phase 5: Production Rollout (Week 6+)

1. Deploy to staging with 3 pods
2. Load test with 10k agents, 1k events/s
3. Monitor rebalancing behavior
4. Gradual rollout to production

---

## 7. Open Questions for Architecture Review

1. **Lease duration:** How long should `lease_until` be? (Proposed: 5 minutes)
2. **Vnode count:** Is 1024 vnodes the right balance between skew reduction and metadata size? (256 per pod with 4 pods = 1024 total)
3. **Snapshot frequency:** Is 6-hour snapshot interval sufficient? (Trade-off: RPO vs storage cost)
4. **Boot tiebreaker:** Should we use `hash(POD_NAME)` or `hash(HOSTNAME)` for boot collision resolution?

---

## 8. Future Work (Not in this ADR)

- **Redis Cluster support** (ADR-042): When volume exceeds single-node capacity
- **Redis Sentinel support** (ADR-043): When multi-region HA is required
- **External cluster manager** (ADR-044): Only if Redis-based membership proves insufficient
- **Multi-region replication** (ADR-045): For global deployments

---

## 9. References

- [ADR-001: Architecture](./ADR-001-Arquitetura.md) — Event sourcing foundation
- [ADR-005: Checkpoints and Idempotency](./ADR-005-Checkpoints-Idempotency.md) — Checkpoint durability
- [ADR-018: Incremental World](./ADR-018-WorldIncremental-WorldSystem.md) — World model
- [ADR-039: Role and Intent Resolution](./ADR-039-Role-rethinking-and-intentions-routing.md) — ECS purity
- [Kafka Protocol: MetadataRequest](https://kafka.apache.org/protocol#The_Metadata_API) — Inspiration for cluster metadata pattern
- [Cassandra Vnodes](https://cassandra.apache.org/doc/latest/cassandra/architecture/vnodes.html) — Inspiration for vnode-based partitioning
