<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-035: Sharding and Dispatcher Coordination for Horizontal Scaling

**Status:** Proposed (Under Review)
**Date:** July 3, 2026
**Version:** 1.0
**Authors:** FMH Architecture Team
**Related:** [ADR-001](./ADR-001-Architecture.md), [ADR-005](./ADR-005-Checkpoints-Idempotency.md), [ADR-018](./ADR-018-Incremental-World.md)

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

We need a **coordination (sharding)** strategy to ensure each `agent_id` is processed by **only one Pod at a time**.

---

## 2. Proposed Decision

Adopt a **Global Notification Stream (Wakeup Stream) with Redis Consumer Groups**.

The `ReactiveDispatcher` will no longer poll a static list of agents. Instead, it will react to "wakeup" messages published in a global stream coordinated by Redis.

### 2.1 The Mechanism (Wakeup Stream)

1. **The Global Stream:** We create a single Redis stream named `fmh:wakeup`.
2. **The Consumer Group:** All `ReactiveDispatcher` Pods connect to this stream using the same Consumer Group (e.g., `fmh:dispatchers_group`).
3. **Publishing (XADD):** Whenever an external webhook arrives or a scheduler timer triggers for `agent-123`, the system publishes a lightweight message to the stream: `{"agent_id": "agent-123"}`.
4. **Exclusive Consumption (XREADGROUP):** Redis mathematically guarantees that only *one* Pod will receive this message.
5. **Processing and Acknowledge (XACK):** The Pod that received the message will:
* Load the checkpoint for `agent-123` (ADR-018).
* Fetch new events.
* Apply business rules (`WorldSystems`).
* Save the new checkpoint.
* Send an `XACK` to Redis, confirming the agent was successfully processed.



---

## 3. Considered Alternatives (Rejected)

### 3.1 Strategy A: Consistent Hashing (Kubernetes StatefulSets)

Each Pod is assigned an ordinal ID (0, 1, 2) and processes only the agents where `hash(agent_id) % TOTAL_PODS == MY_ID`.

* **Pros:** Zero extra network dependencies; no database contention.
* **Cons:** Scaling dynamically from 3 to 4 Pods rebalances the hash math. Agents "jump" between machines, which renders any hot memory caches useless and forces mass cold boots. Rejected due to operational inflexibility.

### 3.2 Strategy B: Distributed Locks (Redis SET NX)

Before processing an agent, the Pod attempts to acquire a temporary lock (`fmh:lock:{agent_id}`).

* **Pros:** Extremely simple to implement in the current codebase (just one extra `if` statement in the dispatcher loop).
* **Cons:** Predatory network traffic (aggressive polling). With 50 Pods and 10,000 agents, we would generate thousands of `SET NX` requests per second returning `False`, saturating the Redis Single-Thread entirely with lock checks and stealing bandwidth from actual event writes.

---

## 4. Consequences of the Chosen Decision (Wakeup Stream)

### Positives

* ✅ **Infinite Horizontal Scaling:** We can scale from 1 to 1,000 Pods. Load balancing is handled natively and perfectly by Redis.
* ✅ **Zero CPU Waste:** No Pod processes redundant agents.
* ✅ **Spike Isolation (Backpressure):** If events arrive for 50,000 agents simultaneously, the `fmh:wakeup` queue absorbs the spike. Pods consume at their own pace, preventing the cluster from crashing due to Out Of Memory (OOM) errors.

### Negatives / Risks

* ⚠️ **Failure Management (XPENDING):** If a Pod receives a wakeup message and crashes (OOM) before sending the `XACK`, that notification remains "pending".
* **Mitigation:** We must implement a Garbage Collection process in the Dispatcher. A background coroutine will periodically run the `XPENDING` command to claim abandoned notifications (e.g., older than 5 minutes) and re-route them to healthy Pods.

---

## 5. Migration Plan (Rollout)

1. **Phase 1:** Create the Consumer Group infrastructure during server boot (`XGROUP CREATE fmh:wakeup fmh:dispatchers_group MKSTREAM`).
2. **Phase 2:** Update inbound adapters (HTTP Gateway, Schedulers) to emit messages to `fmh:wakeup` whenever they receive intents.
3. **Phase 3:** Refactor the `ReactiveDispatcher` class, replacing the static loop with continuous consumption via `XREADGROUP`.
4. **Phase 4:** Monitor the `consumer_lag` metric in Datadog/Grafana to create auto-scaling rules for Kubernetes Pods based on the wakeup queue size.

---

### Open Question for Architecture Review

**Strategy C (Consumer Groups) will introduce the concept of `XACK` into our framework's flow.** Are you fully on board with this event-driven approach for routing (Strategy C), or would you prefer starting with the simplified Distributed Lock approach (Strategy B) as a quick MVP, accepting the short-term Redis I/O cost?