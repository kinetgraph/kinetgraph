<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# GraphRAG

GraphRAG no FMH = **busca em dois sub-grafos FalkorDB por
tenant**, mantidos por consolidadores separados:

- **Sub-grafo de Documentos** — busca semântica livre sobre
  eventos indexados (`Document.embedding` por cosine). Path
  herdado do MVP (v0.2.x).
- **Solutions sub-graph** — retrieve oriented to **reuse of
  successful tool calls**. Path introduced by
  [ADR-010](../ADRs/ADR-010-Memory-Business-Tier.md).

Both share the same graph (`fmh_tenant_{cnpj}`) and the
same embedding dimension, but have **separate labels**,
**separate vector indices**, and **separate lifecycle**:
Documents are updated by `FalkorDBProjector` (one-shot,
manual), Solutions by `SolutionPromoter` (an adapter drained
by a post-tick coroutine, opt-in per tenant).

This document covers:

1. The two sub-graphs and their responsibilities.
2. How to run `FalkorDBProjector` (Documents).
3. How to run the Solutions coroutine (extract → bus → promote).
4. The 3 retrieve modes of `GraphRAGRetriever`:
   `vector_search` (legacy), `find_solutions_by_problem` and
   `find_solutions_by_tool` (ADR-010).
5. PII: how `PiiRedactionTool` gates the Solutions write.
6. Man-in-the-loop: review queue.
7. Tests.

> **Pré-requisitos**
>
> - Redis Streams rodando (fonte da verdade).
> - FalkorDB rodando (`uv add 'kntgraph[falkordb]'`).
> - Um `EmbeddingProvider` (veja [embedding.md](./embedding.md)).
> - For the Solutions sub-graph with PII level 2/3:
>   `uv add 'kntgraph[gliner]'` (opt-in).

---

## 1. The two sub-graphs

Per tenant (`fmh_tenant_{cnpj}`):

### 1.1 Documents sub-graph (ADR-004)

```
(:Agent   {agent_id, last_seen, tenant_id})
(:Document {id, agent_id, event_type, data_json, embedding: vec_f32})
(:ToolCall {id, tool, request_id, status, latency_ms, agent_id})
(:Entity  {name, type, embedding: vec_f32})    # F9+

(a:Agent)-[:HAS_DOC]->(d:Document)
(d:Document)-[:MENTIONS]->(e:Entity)
(a:Agent)-[:CALLED]->(t:ToolCall)
```

Maintained by `FalkorDBProjector.project_all()` (one-shot).
Retrieval: `vector_search` (top-k by cosine) or
`find_documents_by_entity` (F9+).

### 1.2 Solutions sub-graph (ADR-010)

```
(:Tool     {name, description, input_schema_json})
(:Problem  {fingerprint, embedding: vec_f32, tags_json})
(:Action   {request_event_id, params_fingerprint, params_json})
(:Outcome  {status, latency_ms, result_signature, error_message})

(p:Problem)-[:SOLVED_BY  {confidence, validated_count}]->(a:Action)
(p:Problem)-[:FAILED_WITH {confidence, validated_count}]->(a:Action)
(a:Action)-[:ON_TOOL]->(tool:Tool)
(a:Action)-[:PRODUCED]->(o:Outcome)
```

Maintained by `SolutionPromoter.pump_once()` (called from
a post-tick coroutine, opt-in per tenant). Retrieval:
`find_solutions_by_problem` (top-k by cosine on
`Problem.embedding`) and `find_solutions_by_tool`
(structural filter on `Tool.name`).

### 1.3 Embedding dimension convention

**One embedding dimension per tenant**, fixed at deploy
time. The vector indices on `Document.embedding` and
`Problem.embedding` (and `Entity.embedding` in F9+) use
the same dimension. Switching the `EmbeddingProvider`
requires **re-projecting** the affected sub-graph (FalkorDB
accepts per-label indices with different dimensions, but
the maintenance cost isn't worth it).

---

## 2. Standing up FalkorDB

```bash
docker run -d --name fmh-falkordb \
  -p 16379:6379 \
  -e FALKORDB_PASSWORD=falkordb \
  falkordb/falkordb:latest
```

The default port in `GraphPool` is **16379** and the
default password is **`falkordb`** (change in production).

---

## 3. Provisioning the vector index

The `GraphRAGRetriever` query uses `vec.cosineDistance`,
which requires a vector index with the **same dimension** as
the `EmbeddingProvider` in use.

```cypher
CREATE VECTOR INDEX FOR (d:Document) ON (d.embedding)
OPTIONS {dim: 768, similarityFunction: 'cosine'}

CREATE VECTOR INDEX FOR (p:Problem) ON (p.embedding)
OPTIONS {dim: 768, similarityFunction: 'cosine'}
```

| Provider | `dimension` |
|----------|-------------|
| `EmbeddingClient` (paraphrase-multilingual) | `768` |
| `EmbeddingClient` (nomic-embed-text) | `768` |
| `EmbeddingClient` (mxbai-embed-large) | `1024` |
| `HashEmbeddingProvider` | `256` |
| OpenAI text-embedding-3-small (if implemented) | `1536` |

> `FalkorDBProjector` and `SolutionProjector` (ADR-010)
> try to create the index automatically on first run. If
> your FalkorDB version doesn't accept the syntax, create
> it manually before populating.

---

## 4. Documents sub-graph — `FalkorDBProjector`

(This section is the legacy MVP documentation, kept for
back-compat. The detailed content is the same as
`graphrag.md` v0.2.x.)

### 4.1 Rebuild one-shot (cold start / full replay)

```python
import asyncio
import redis.asyncio as aioredis
from kntgraph.stream.event_log import EventLog
from kntgraph.knowledge.embedding import (
    EmbeddingClient,
)
from kntgraph.infra.graph import GraphPool
from kntgraph.knowledge.falkordb.adapter import (
    FalkorDBProjector,
)

async def main():
    redis = aioredis.from_url("redis://localhost:6379")
    log = EventLog(redis)

    embedding = EmbeddingClient()  # 768d
    fdb = GraphPool(host="localhost", port=16379)
    projector = FalkorDBProjector(
        log=log, client=fdb, embedding=embedding,
        tenant_id="12.345.678/0001-90",
    )
    stats = await projector.project_all()
    print(stats)
    # {"agents": 12, "documents": 87, "tool_calls": 23, "edges": 122}
    await redis.aclose()

asyncio.run(main())
```

### 4.2 Idempotency

- `MERGE` by id → running twice doesn't duplicate.
- The embedding is recomputed on every projection (no
  content-based dedup today; F9+ may add one). For
  workloads with frequent re-projection, consider
  scheduling `project_all()` in a low-traffic window.

### 4.3 What gets indexed

`FalkorDBProjector._is_document_candidate` considers a
node a **Document** only for these event types:

```python
{"nf.received", "nf.validated", "nf.transmitted", "empresa.upserted"}
```

To index other types, override the function or subclass
the projector. The embedded text is
`{agent_id} | {event_type} | {json(data)}` — keep it
concise if the provider has a token limit.

---

## 5. Solutions sub-graph — `SolutionPromoter`

(This section is new in ADR-010. See the full ADR for
justification and trade-offs.)

### 5.1 How the sub-graph is built

`SolutionExtractor` (pure) reads the `World` filtering
`tool.*.completed` and `tool.*.failed`, reconstructs
`(Problem, Action, Outcome)` from each event's `data`, and
publishes `SolutionCandidate`s on the `SolutionPromotionBus`.
`SolutionPromoter` (adapter) drains the bus, runs the
`data` through `PiiRedactionTool`, and performs the
FalkorDB `MERGE`.

The post-tick coroutine (driven by the `Consolidator` or a
custom integration) calls `SolutionPromoter.pump_once(bus)`
in a loop. The promoter is fail-closed: a PII failure or
a projector I/O failure aborts the candidate (counted in
`pii_blocked` or `failed`) and the loop moves to the next.

### 5.2 Enabling per tenant

Opt-in per tenant via a Redis hash:

```bash
# Enable the Knowledge tier for the tenant
redis-cli HSET fmh:tenant:12.345.678/0001-90:flags knowledge_enabled 1
```

The coroutine checks the flag before calling `pump_once`.
A tenant without the flag has its events ignored by the
promoter (structured log `knowledge.tenant_disabled`).

### 5.3 Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `FMH_KNOWLEDGE_INTERVAL_S` | `10.0` | Post-tick loop interval |
| `FMH_PII_LEVEL` | `1` | PII level: 1 (regex), 2 (GLiNER2 NER), 3 (GLiNER2 v1.5 `pii` task) |
| `FMH_SOLUTIONS_TOOL_ALLOWLIST` | (empty = all) | CSV of tool names that generate candidates |
| `FMH_SOLUTIONS_CONFIDENCE_BUMP_AGENTS` | `2` | N distinct agents to trigger a confidence bump |
| `FMH_SOLUTIONS_REVIEW_TTL_S` | `604800` | Review queue TTL (7 days) |
| `FMH_SOLUTIONS_REVIEW_THRESHOLD` | `1` | Confidence below which the candidate goes to review |

### 5.4 Wiring

```python
import asyncio
import redis.asyncio as aioredis
from kntgraph.stream.event_log import EventLog
from kntgraph.knowledge.embedding import (
    EmbeddingClient,
)
from kntgraph.infra.graph import GraphPool
from kntgraph.agents.memory.solutions import (
    SolutionExtractor,
    SolutionPromoter,
    SolutionPromotionBus,
)
from kntgraph.agents.tools.pii import PiiRedactionTool
from kntgraph.agents.knowledge.solution_projector import (
    SolutionProjector,
)

async def main():
    redis = aioredis.from_url("redis://localhost:6379")
    log = EventLog(redis)
    fdb = GraphPool(host="localhost", port=16379)
    embedding = EmbeddingClient()

    # PiiRedactionTool exposes `redact` as a pure callable
    # (it does NOT go through the Tool Protocol here).
    pii = PiiRedactionTool(level=1)  # regex only
    projector = SolutionProjector(
        client=fdb, embedding=embedding,
        tenant_id="12.345.678/0001-90",
    )
    promoter = SolutionPromoter(
        tenant_id="12.345.678/0001-90",
        projector=projector,
        redactor=pii.redact,
    )
    bus = SolutionPromotionBus()
    extractor = SolutionExtractor()

    # The post-tick coroutine (simplified example):
    async def pump_loop():
        while True:
            # 1) Pure: World fold -> candidates
            events = await log.read(extractor.target_agent_id)
            for candidate in extractor.extract_from_events(events):
                await bus.publish(candidate)
            # 2) Adapter: PII + FalkorDB write
            stats = await promoter.pump_once(bus)
            await asyncio.sleep(10.0)

    await pump_loop()

asyncio.run(main())
```

### 5.5 What is written

`(:Problem)` receives `embedding` + `tags_json` (extracted
from the tool call's `data`). `(:Action)` receives
`request_event_id` (natural key) + `params_fingerprint`
(for the cross-agent bump) + `params_json` (**redacted**,
never the original). `(:Outcome)` receives `status`,
`latency_ms`, `result_signature`, `error_message` (redacted).

The `(:Problem)-[:SOLVED_BY]->(:Action)` edge (or
`[:FAILED_WITH]` when status=failed) carries `confidence`
and `validated_count`.

`(:Tool)` is populated once on promoter boot via
`ToolRegistry.list_tool_descriptors()` (introspection of
the `Tool` Protocol).

### 5.6 PII gate

Every `data` that reaches `SolutionPromoter` goes through
`PiiRedactionTool` before the `MERGE`. Default `level=1`
(regex) replaces CPF/CNPJ/email/phone/CEP/PIX keys with
placeholders. Levels 2/3 use GLiNER2 (opt-in, extra
`kntgraph[gliner]`).

A redaction failure produces a `pii.check_failed` event on
the EventLog + DLQ. **Nothing is written**. This policy is
fail-closed by design (LGPD).

Default labels:

```python
DEFAULT_PII_LABELS = (
    "cpf", "cnpj", "email", "telefone",
    "endereco", "nome_pessoa", "chave_pix",
    "cartao_credito",
)
```

Per-tenant override: Redis set `fmh:tenant:{cnpj}:pii_labels`.

---

## 6. Man-in-the-loop: review queue

Two thresholds divert candidates to the review queue
instead of auto-promoting:

- **Threshold 1 — confidence (cross-agent bump)**: when the
  pair `(problem_fingerprint, action_params_fingerprint)`
  appears in `>= FMH_SOLUTIONS_CONFIDENCE_BUMP_AGENTS`
  distinct agents, `confidence++`. Candidates with
  `confidence < FMH_SOLUTIONS_REVIEW_THRESHOLD` go to
  review.
- **Threshold 2 — approval list**: tool names in
  `fmh:tenant:{cnpj}:approval_list` (Redis set) **never**
  auto-promote.

The review queue is a Redis Stream `fmh:solutions:review`
with TTL `FMH_SOLUTIONS_REVIEW_TTL_S` (default 7 days).
When the TTL expires, the candidate goes to the DLQ.

The HTTP review API is **out of scope of the framework** —
the application plugs in its own endpoint that drains the
stream, lists candidates (`{candidate_id, problem_summary,
action_summary, outcome, source_event_ids}`), and emits
`solution.promoted` (approval) or `solution.rejected`
(rejection) on the EventLog. `SolutionPromoter` consumes
those events and updates the graph.

---

## 7. Querying: `GraphRAGRetriever`

The retriever exposes **3 modes** corresponding to the 2
sub-graphs + 1 structural path. All return `RetrievalResult`
or `SolutionResult` (frozen dataclass, slots).

### 7.1 Mode 1 — `vector_search` (legacy, Documents sub-graph)

Top-k by cosine on `Document.embedding`. The path that
existed before ADR-010. Continues to work unchanged.

```python
from kntgraph.knowledge.graphrag.retriever import (
    GraphRAGRetriever,
    RetrievalResult,
)

retriever = GraphRAGRetriever(
    client=fdb, embedding=embedding,
    tenant_id="12.345.678/0001-90",
)

results: list[RetrievalResult] = retriever.vector_search(emb, k=5)
for r in results:
    print(
        f"doc={r.doc_id}  agent={r.agent_id}  "
        f"type={r.event_type}  score={r.score:.4f}"
    )
    print(f"  data={r.data}")
```

`retriever.retrieve(query, k=5)` is a shortcut that wraps
`await embedding.embed(query)` + `vector_search`.

### 7.2 Mode 2 — `find_solutions_by_problem` (ADR-010, Solutions sub-graph)

Embed the current problem state (e.g. the payload of the
new NF-e to be transmitted) -> top-k most similar
`(:Problem)` -> the `(:Action)`s that solved them. Returns
the **Action** (what the agent will reuse), with the
`Problem` anchored in the hit's `data`.

```python
from kntgraph.knowledge.graphrag.retriever import (
    GraphRAGRetriever,
    SolutionResult,
)

# Embed the payload of the new NF-e
problem_emb = await embedding.embed(json.dumps(payload))

results: list[SolutionResult] = retriever.find_solutions_by_problem(
    problem_emb,
    tags={"cnpj": "12.345.678/0001-90", "uf": "SP"},  # optional
    tool_name="invoice.issue",  # optional
    k=5,
)
for r in results:
    print(
        f"tool={r.tool_name}  params={r.action_params_example}  "
        f"status={r.outcome_status}  confidence={r.confidence}  "
        f"score={r.score:.4f}"
    )
```

The executed Cypher combines `vec.cosineDistance` with
structural MATCH (optional tags become `WHERE`). Structural
filters WITHOUT cosine also work — just omit the embedding
and use `tags` or `tool_name`.

### 7.3 Mode 3 — `find_solutions_by_tool` (ADR-010, structural only)

Retrieves all `(:Action)`s for a given `(:Tool)`, ordered
by `last_validated_at DESC` (most recent first). Useful for
debugging and for "list everything we've done with this
tool".

```python
# Default: only `completed` outcomes.
results: list[SolutionResult] = retriever.find_solutions_by_tool(
    "invoice.issue",
    tags={"uf": "SP"},  # optional
    k=20,
)

# Failures too: `status="failed"`.
failures = retriever.find_solutions_by_tool(
    "bank.transfer", status="failed", k=10,
)

# All: completed + failed.
all_hits = retriever.find_solutions_by_tool(
    "invoice.issue", status="all",
)
```

The `Problem -> Action` edge is determined by the
`Outcome` status: `[:SOLVED_BY]` for `completed`,
`[:FAILED_WITH]` for `failed`. The retriever picks the
right edge type automatically. `status="all"` matches
either.

### 7.4 `SolutionResult` shape

```python
@dataclass(frozen=True, slots=True)
class SolutionResult:
    problem_fingerprint: str
    action_params_example: dict
    tool_name: str
    outcome_status: str       # "completed" | "failed"
    latency_ms: float | None
    confidence: int
    last_validated_at: str | None  # ISO timestamp
    score: float              # cosine or 1.0 (structural)
```

### 7.5 Executed Cypher (summary)

**`vector_search`** (legacy):

```cypher
MATCH (d:Document)
WHERE d.embedding IS NOT NULL
WITH d, vec.cosineDistance(d.embedding, vecf32([...])) AS score
RETURN d.id, d.agent_id, d.event_type, d.data_json, score
ORDER BY score ASC LIMIT $k
```

**`find_solutions_by_problem`** (ADR-010):

```cypher
MATCH (p:Problem)
WITH p, vec.cosineDistance(p.embedding, vecf32([...])) AS score
MATCH (p)-[:SOLVED_BY|FAILED_WITH]->(a:Action)        -- status="all"
  -- or [:SOLVED_BY] for "completed", [:FAILED_WITH] for "failed"
MATCH (a)-[:ON_TOOL]->(t:Tool)
MATCH (a)-[:PRODUCED]->(o:Outcome)
WHERE o.status = $status                                -- when not "all"
  AND ($tool_name IS NULL OR t.name = $tool_name)
  AND ($tags_json substring filters ...)
RETURN p.fingerprint, a.params_json, t.name, o.status,
       o.latency_ms, score, p.last_validated_at
ORDER BY score ASC LIMIT $k
```

**`find_solutions_by_tool`** (ADR-010):

```cypher
MATCH (a:Action)-[:ON_TOOL]->(t:Tool {name: $tool_name})
MATCH (a)-[:PRODUCED]->(o:Outcome)
MATCH (p:Problem)-[:SOLVED_BY|FAILED_WITH]->(a)          -- status="all"
  -- or [:SOLVED_BY] / [:FAILED_WITH] for a specific status
WHERE o.status = $status                                -- when not "all"
  AND ($tags_json substring filters ...)
RETURN p.fingerprint, a.params_json, t.name, o.status,
       o.latency_ms, p.last_validated_at, 1.0 AS score
ORDER BY p.last_validated_at DESC LIMIT $k
```

---

## 8. Resilience and observability

- `vector_search` swallows errors and logs
  `graphrag.vector_search.failed` — returns `[]`. Pure
  systems never break because FalkorDB is down.
- `find_solutions_by_problem` / `find_solutions_by_tool`
  follow the same pattern: structured log (`*.failed`) and
  `[]` on failure.
- `SolutionPromoter` logs (replacing the old
  `KnowledgeConsolidator` events):
  - `solution.promoter.start` / `solution.promoter.stop`
    (boot/shutdown).
  - `knowledge.tenant_disabled` (tenant without the
    opt-in flag, events ignored).
  - `solution.promoter.pump` (one event per pump, with
    `candidates`, `bumped`, `auto_promoted`, `to_review`,
    `skipped_tenant_disabled`).
  - `solution.promoter.flag_check_failed` /
    `solution.promoter.approval_list_failed` (Redis
    failures in the opt-in path; fail-soft).
  - `solution.promoter.upsert.skeleton_only` (mode
    without a projector; legacy).
  - `solution.promoter.pii_redact_raised` /
    `solution.promoter.pii_blocked` (PII gate failed;
    candidate discarded, not written).
  - `solution.promoter.upsert_failed` (projector I/O
    failed).
- `PiiRedactionTool` logs `pii.redaction.applied`
  indirectly (via the `invoke` wrapper that returns
  `Err(ToolError)` on failure).

**Metrics are structlog-only** (Phase 4). The exporters
(Prometheus, OTel, Datadog, etc.) are plugged in by the
operator via structlog config; the framework doesn't embed
them. The `solution.promoter.pump` event carries all pump
counters in a single, easily-scrapeable log line.

---

## 9. Tests

```bash
# Unit tests (no FalkorDB; use HashEmbeddingProvider + HeuristicEntityExtractor)
uv run --package kntgraph pytest \
    fmh_backend/tests/unit/knowledge/ -v
uv run --package kntgraph pytest \
    fmh_backend/tests/unit/memory/ -v

# Integration tests (require Redis + FalkorDB running)
uv run --package kntgraph pytest \
    fmh_backend/tests/integration/ -v
```

See `fmh_backend/tests/README_FALKORDB_TESTS.md` for
FalkorDB setup instructions in tests.

---

## 10. See also

- [embedding.md](./embedding.md) — providers, dimensions, vector index.
- [consolidation.md](./consolidation.md) — Redis + FalkorDB consolidation loop.
- [ADR-004: Memory Tiers, Tools and Knowledge](../ADRs/ADR-004-Memory-Tools-Knowledge.md) — Documents sub-graph.
- [ADR-010: Memory Tier "business"](../ADRs/ADR-010-Memory-Business-Tier.md) — Solutions sub-graph.
- [GraphRAG retriever](../../src/fmh_backend/knowledge/graphrag/retriever.py)
- [FalkorDB projector](../../src/fmh_backend/knowledge/falkordb/adapter.py)
- [FalkorDB client](../../src/fmh_backend/knowledge/falkordb/client.py)
- [Example: Documents projection](../../../fmh_agents/examples/08_falkordb_projection.py)
- [Example: Solutions consolidation](../../../fmh_agents/examples/09_knowledge_consolidation.py)
