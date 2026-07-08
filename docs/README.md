<!--
SPDX-FileCopyrightText: 2026 kinetgraph
SPDX-License-Identifier: Apache-2.0
-->

# kntgraph — Documentation

Public documentation for the `kntgraph` framework
and the `agents` sub-module.

The docs in this directory are the **canonical,
curated, and translated to English** reference.
Historical context lives in the
[`ADRs/`](https://github.com/kinetgraph/kntgraph/tree/main/ADRs)
directory (some are still in PT-BR — kept as
historical records).

## Getting started

- [Quick Start](quickstart.md) — 5-minute
  installation and "hello world" example.
- [Architecture](architecture.md) — overview of
  the three pillars (ECS, event sourcing,
  resilience) and how the pieces fit together.

## Concepts

- [Event Sourcing](event_sourcing.md) — the
  `EventLog` and the `World.fold` algorithm.
- [ECS](ecs.md) — pure-ECS agent model with
  `World`, `AgentView`, and `WorldSystem`.
- [Dual Lifecycle](architecture.md#dual-lifecycle)
  — operational vs. domain events.
- [Querying the World](query.md) — efficient
  queries over agent state.
- [Resilience](resilience.md) — circuit breaker,
  retry, bulkhead, timeout, fallback.
- [Security](security/) — event signing, API
  keys, RBAC, ACL.
- [Checkpoints](checkpoints.md) — durable commit
  points in the `ReactiveDispatcher`.

## Capabilities

- [Tools](tools.md) — the `Tool` Protocol,
  `ToolRegistry`, `ToolInvoker`, idempotency.
- [Routing](routing.md) — semantic routing
  (GLiNER2-based intent + argument extraction).
- [Solution tier (ADR-010)](consolidation.md) —
  tool-call re-use, PII gate, man-in-the-loop
  review.
- [GraphRAG](graphrag.md) — FalkorDB projection,
  `GraphRAGRetriever`, top-k by cosine distance.
- [Embeddings](embedding.md) — Hash, Ollama
  (`paraphrase-multilingual`), vector index
  dimension.
- [Dead Letter Queue](dead_letter_queue.md) —
  failure replay path.

## Architecture decisions

The full list of ADRs is in the
[`ADRs/`](https://github.com/kinetgraph/kntgraph/tree/main/ADRs)
directory. The most important ones for new
contributors:

- [ADR-001: Arquitetura](../ADRs/ADR-001-Arquitetura.md) —
  the three pillars (ECS / event sourcing /
  resilience).
- [ADR-002: Replay canônico](../ADRs/ADR-002-Replay-Puro.md) —
  `World` as a pure function of events.
- [ADR-019: Epílogo — Typed Adapters](../ADRs/ADR-019-Epilogo-Typed-Adapters.md) —
  the adapter pattern (Protocol + facade + impl)
  used throughout the codebase.
- [ADR-036: Open-Source — Rename to `kntgraph`](../ADRs/ADR-036-Open-Source-KntGraph.md) —
  the rename from `fmh_*` to `kntgraph`.
