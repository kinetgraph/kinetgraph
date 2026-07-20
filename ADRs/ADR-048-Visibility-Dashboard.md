<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-048: Observability Dashboard and Control Panel API

**Status:** Proposed

**Date:** July 19, 2026

**Version:** 0.1.0

**Authors:** Architecture Team

**Related:** ADR-012-IntentRouter-HTTP-Gateway, ADR-017-Identity-Authorization, ADR-034-ToolCall-ECS-Components, ADR-036-Tool-Worker-Pattern, ADR-037-Mandatory-Correlation-Propagation

---

## 1. Context

Kinetgraph has evolved into a decoupled, event-driven architecture where domain systems, intent classification, and tool executions run concurrently across background tasks, processes, and consumer groups.

Currently, operators and developers lack real-time visibility into the system's runtime state. Debugging or monitoring a live deploy requires reading raw Redis stream logs or raw stdout. This creates three critical visibility challenges:

1. **State Auditing:** No central place to inspect the current state of an agent (ECS snapshot, active memory slots, or domain phase).
2. **Correlation Flow Tracing:** Difficult to visualize the lifecycle of a request flow across async boundaries (`user.intent` -> `tool.requested` -> `tool.completed` -> state mutation) linked by Correlation and Causation IDs (ADR-037).
3. **Queue & Worker Monitoring:** No direct way to observe queue depth, average latency, concurrency saturation, or cost metrics of tool workers (ADR-036).

To bridge this gap, we need to introduce a dedicated observability layer composed of a read-only HTTP API and a modern web frontend dashboard.

---

## 2. Decision

We will design and implement a framework-native **Observability Dashboard and Control Panel**.

This feature will consist of two parts:
1. **HTTP Dashboard API (`kntgraph.api.dashboard`):** A FastAPI/Starlette router exposing endpoints that read system state from Redis storage adapters.
2. **Web Frontend Dashboard:** A modern Single Page Application (SPA) providing real-time visualizations and tracing controls.

### 2.1 Architecture Overview

```text
       [ Web Browser (Frontend SPA) ]
                     │
                     ▼  (HTTP REST / WebSockets)
       [ Dashboard API (FastAPI Router) ]
         │                  │
         ├──────────────────┼──────────────────┐
         ▼                  ▼                  ▼
  [EventLog Store]   [Memory Storage]    [Worker Queue]
 (RedisEventLog)    (RedisContinuity)   (XPENDING / Stream)
```

---

## 3. Detailed Specifications

### 3.1 HTTP API Endpoints (`/api/v1/dashboard/`)

The API will expose the following endpoints:

- **`GET /api/v1/dashboard/agents`**
  - Returns a list of active agents with their current domain phase and loaded ECS components.
  
- **`GET /api/v1/dashboard/agents/{agent_id}`**
  - Returns a detailed snapshot of a single agent's `AgentView` components, including memory, active tool calls (ADR-034), and history.

- **`GET /api/v1/dashboard/events`**
  - Queries the global event log stream. Supports pagination, filters (by `agent_id`, `event_type`), and sorting.

- **`GET /api/v1/dashboard/traces/{correlation_id}`**
  - Aggregates and returns the chronological event sequence sharing the given `correlation_id` (ADR-037), rendering the full causation tree.

- **`GET /api/v1/dashboard/tools`**
  - Lists registered tool workers, active concurrency, pending message counts (`XPENDING` metrics), average latency, and accumulated model execution costs.

- **`GET /api/v1/dashboard/dispatcher`**
  - Returns health, latency, and throughput statistics for the active `ReactiveDispatcher` tick loops.

- **`GET /api/v1/dashboard/ws/stream` (WebSocket)**
  - A pub-sub endpoint that streams new event logs in real-time as they are written to the Redis stream.

---

### 3.2 Frontend Dashboard Design

The frontend will be built as a responsive Single Page Application (SPA), styled with a premium dark-themed aesthetic, modern typography, glassmorphism card layouts, and micro-animations.

#### Main Views
1. **System Overview (Dashboard):** Summary cards showing total active agents, event throughput (events/sec), tool queue saturation, and daily LLM costs.
2. **Agent Explorer:** A grid/list of active agents with searchable filters. Clicking an agent expands to show a tree view of its ECS components and memory slots.
3. **Trace Visualizer:** A timeline or flowchart representation of a specific flow (`correlation_id`). It chains events by their `causation_id`, showing step latencies and pinpointing where failures occurred.
4. **Tool Monitor:** Real-time progress bars for consumer queue depths, worker status, and cost breakdown charts per model (gpt-4o-mini, ollama/qwen3.5, etc.).

---

### 3.3 Implementation Rules

1. **Read-Only Separation:** The Dashboard API must access data exclusively through read-only queries or dedicated adapters. Under no circumstances should it alter event logs or overwrite agent state.
2. **Modular Dependency:** The dashboard code must reside in a dedicated vertical (`src/kntgraph/api/dashboard/`) and be package-isolated under the `[api]` extra. The core framework must remain dashboard-free.
3. **Static Embedding option:** The SPA assets should be compilable to static HTML/JS and optionally served directly by the FastAPI application under `/dashboard/` for zero-configuration deployments.

---

### 3.4 Security & Agent Isolation

Because Kinetgraph is built around secure, multi-agent boundaries, the Observability Dashboard MUST enforce strict agent isolation:

1. **Tenant & Agent Scoping:** The HTTP API endpoints (especially `/agents` and `/events`) must enforce tenant partition scopes. A dashboard caller must only be allowed to read metrics and state snapshots of agents they are explicitly authorized to view.
2. **Access Control Integration:** API queries must validate permissions against the existing identity verification policies and ACL frameworks (ADR-017).
3. **PII and Data Redaction:** Serializer modules of the API must redact sensitive attributes inside agent ECS components (such as private credentials, raw API keys, or raw memory slots containing personal data) before sending responses to the client.

---

## 4. Consequences

### Pros

- **Drastic Debugging Improvements:** Developers can visualize complex reactive loops and event traces in real-time instead of tailing logs.
- **Improved Operations:** Operators get instant metrics on queue delays, token usage costs, and system throughput.
- **Zero Framework Bloat:** The dashboard vertical remains completely isolated and is only loaded when explicitly enabled in settings.
- **Secure by Default:** Incorporating tenant isolation at the boundary prevents horizontal privilege escalation across distinct agents.

### Cons

- **Increased Repository Size:** Adds frontend assets, router configurations, and dashboard tests.
- **Slight Redis Read Overhead:** Real-time polling or WebSocket streams will generate additional read traffic on Redis. (Mitigation: Use lightweight Redis stream consumers with strict page bounds).

---

## 5. Recommendation

We recommend creating the `kntgraph.api.dashboard` package immediately. The API should be built on FastAPI, and the frontend should be scaffolded as a Single Page Application using Vite, HTML, and CSS, served as static files by the dashboard router.
