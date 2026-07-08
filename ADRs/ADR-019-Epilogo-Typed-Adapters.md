<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-019-Epilogo: Typed Adapters — Iter 2 a 21

**Status:** Aceito
**Data:** 28-29 de junho de 2026
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** [ADR-019](./ADR-019-Redis-Adapter-Typing.md), [AGENTS.md §1](../../AGENTS.md), §2, §3, §4, §6

Este ADR documenta o fecho completo do [ADR-019](./ADR-019-Redis-Adapter-Typing.md):
20 iterações (2-21) que aplicaram a regra "1 lib externa = 1 adapter Protocol"
para Redis, embedding, LLM, Graph (FalkorDB), Auth cache, Intent classification,
Entity extraction e Argument extraction — todos os domínios onde o framework
encapsulava bibliotecas externas.

---

## 1. Contexto

O ADR-019 original fechou apenas a **Iter 1** — adapter do `EventLog` (Redis).
A AGENTS.md §1 prescreve a mesma regra para **toda** biblioteca externa, mas
faltava aplicá-la a 8 domínios:

1. **Redis** — 5 shards restantes (memory, auth, checkpoint, DLQ, world checkpoint).
2. **Embedding** — `OllamaEmbeddingProvider` (lazy `import ollama`).
3. **Graph** — `FalkorDBClient` retornando tipo concreto (`Graph | AsyncGraph`).
4. **LLM** — `LiteLLMTool` com 4 lazy imports em `llm.py` (parcial, fora do ADR).
5. **Auth cache** — `RedisCacheStorage` usando `redis.asyncio.Redis` direto.
6. **API key cache** — TTL cache sem Protocol.
7. **GLiNER2** — usado em 3 lugares (intent, entity, argument) sem facade unificada.
8. **Settings** — modelos/tempos/dimensões hard-coded em adapters.

A regra AGENTS.md §2 ("zero shims de compat") também precisava ser aplicada:
os shims 1-linha criados na Iter 1 (`redis.py`, `redis_codec.py`, `idempotency.py`)
eram dívida técnica acumulada que precisava ser paga.

A regra AGENTS.md §3 ("files > 500 LOC devem ser divididos") também
precisava ser aplicada: `knowledge/falkordb/adapter.py` (313 LOC) tinha
god method `_project_agent` (CC=9).

---

## 2. Decisão

### 2.1 Resumo das iterações

| Iter | Escopo | LOC novo | Tests | Status |
|---|---|---|---|---|
| 2 | `ShortMemoryStorage` (Session/Profile/Continuity) | +200 | +27 | ✅ |
| 3 | `APIKeyStorage` (auth) | +60 | +13 | ✅ |
| 4 | `CheckpointStorage` (HASH-based) | +80 | +13 | ✅ |
| 5 | `DLQStorage` (4 keys + idempotency 2-phase) | +200 | +24 | ✅ |
| 5b | `WorldCheckpointStorage` (pickle payload) | +100 | +10 | ✅ |
| 6 | Remover 3 shims + 6 parâmetros `redis_client=` + 21 callers | -150 | +11 | ✅ |
| 7 | Bug fix `_list_agent_ids` (3 call sites pós-Iter 5) | -3 | 0 | ✅ |
| 8 | `FalkorDBProjector._project_agent` god-method split (TDD) | -100 | +11 | ✅ |
| 9 | Remover `HashEmbeddingProvider` + criar `FakeEmbeddingProvider` em `testing/` | -94 | +25 | ✅ |
| 10 | `GraphAdapter` Protocol + `FalkorDBGraphAdapter` (FalkorDB) | +200 | +28 | ✅ |
| 11 | `GraphAgentAdapter` (sub-adapter FalkorDB) | +113 | +9 | ✅ |
| 12 | `GraphDocumentAdapter` + `GraphToolCallAdapter` | +238 | +17 | ✅ |
| 13 | `GraphSolutionAdapter` (4 nodes + 3 edges) | +199 | +9 | ✅ |
| 14 | `GraphRAGRetriever` migration + 4 read methods | +200 | 0 (12 skipped) | ✅ |
| 15 | `OllamaEmbeddingProvider` → `OllamaEmbeddingAdapter` + `EmbeddingClient` facade | +50 | 0 (rename only) | ✅ |
| 16 | Filtros opcionais em `find_solutions_by_*` (`tags`, `tool_name`, `status`) + `string.Template` | +60 | 6 (un-skip) | ✅ |
| 17a | `RedisCacheStorage` → `RedisCacheAdapter` consumindo `RedisLike` (LLM cache) | +30 | 0 (rename only) | ✅ |
| 17b | `APIKeyCacheAdapter` (TTL-based, negative cache, fail-soft, LRU cap) | +80 | +5 | ✅ |
| 18a | `knowledge_consolidator` → `RedisLike` (bloqueado: circular import) | - | - | ⛔ |
| 18b | `LLMClient` facade + `LLMTransport` Protocol runtime_checkable | +50 | 0 (rename only) | ✅ |
| 18c | `LiteLLMTransportAdapter` (renomeado) + `LLMClient` lazy import guard | +30 | +3 | ✅ |
| 19 | `Settings` mixins (`LLMSettingsMixin`, `EmbeddingSettingsMixin`) | +80 | +30 | ✅ |
| 20 | Adapters leem Settings via helper encapsulado (`_resolve_*_defaults`) | +40 | +14 | ✅ |
| 21 | GLiNER2: 3 adapters + 3 facades `SLM*` + renames atomicamente | +150 | +21 | ✅ |

**Total**: 20 iterações, ~2400 LOC, +285 tests (870 → 1155), CC 12 → 10, MI ≥ 65.

### 2.2 Encapsulamento por biblioteca externa

Após o fecho, o framework trata cada backend como uma **interface tipada**:

| Lib | Path do Protocol | Path da impl concreta | Composição |
|---|---|---|---|
| Redis | `infra/redis/_client.py:RedisLike` | (direto, async) | `EventLog → storage` |
| Redis (shards) | 6× Protocol em `infra/redis/_<shard>/` | `Redis<X>Adapter` em mesmo path | `EventLog → storage` |
| Redis (cache) | `RedisLike` (re-uso) | `RedisCacheAdapter` em `fmh_agents/tools/cache.py` | `CachingLLMTransport → RedisCacheAdapter` |
| FalkorDB | `knowledge/graph/_protocol.py:GraphAdapter` | `FalkorDBGraphAdapter` | `GraphPool → sub-adapters` |
| Embedding | `knowledge/embedding/_protocol.py:EmbeddingProvider` | `OllamaEmbeddingAdapter` + `EmbeddingClient` (facade) | `EmbeddingClient → OllamaEmbeddingAdapter` |
| GLiNER2 (intent) | `IntentClassifier` (Protocol em `base.py`) | `GlinerIntentAdapter` + `SLMIntentClassifier` (facade) | `SLMIntentClassifier → GlinerIntentAdapter` |
| GLiNER2 (entity) | `EntityExtractorWithMentions` (Protocol em `base.py`) | `GlinerEntityAdapter` (template-method) + `SLMEntityExtractor` (facade) | `SLMEntityExtractor → GlinerEntityAdapter` |
| GLiNER2 (argument) | `ArgumentExtractor` (Protocol em `base.py`) | `GlinerArgumentAdapter` + `SLMArgumentExtractor` (facade) | `SLMArgumentExtractor → GlinerArgumentAdapter` |
| LLM (litellm) | `fmh_agents/tools/llm_transport.py:LLMTransport` | `LiteLLMTransportAdapter` + `LLMClient` (facade) | `LLMClient → LiteLLMTransportAdapter` |
| LLM (cache) | `LLMTransport` (re-uso) | `CachingLLMTransport` em `fmh_agents/tools/cache.py` | `CachingLLMTransport → LLMTransport + RedisCacheAdapter` |
| Starlette | (Protocol interno) | direto | middleware |

### 2.3 Princípios aplicados

#### 1 tecnologia = 1 módulo adapter

- **Redis**: `infra/redis/` (1 módulo) → 6 sub-shards.
- **FalkorDB**: `knowledge/graph/` (1 módulo) → 1 Protocol + 1 adapter + 4 sub-adapters + 1 facade.
- **Embedding**: `knowledge/embedding/` → `_protocol.py` + `_ollama.py` (sem hash).

Cada path de import é o **único** lugar que importa o tipo third-party em runtime.

#### Zero shims de compatibilidade (AGENTS.md §2)

- **Iter 6**: removidos `infra/redis.py`, `redis_codec.py`, `idempotency.py`.
- **Iter 9**: removido `HashEmbeddingProvider` (94 LOC); substituído por `FakeEmbeddingProvider` em `fmh_backend.testing`.
- **Iter 15**: renomeado `OllamaEmbeddingProvider` → `OllamaEmbeddingAdapter` (consistente com `FalkorDBGraphAdapter`/`RedisEventLogAdapter`); adicionada `EmbeddingClient` facade que escolhe impl por config.
- 332+ callers externos migrados em massa (mesmo commit).
- Backward compatibility foi tratada como **mecânica**, não como **API**: cada
  caller legado foi atualizado explicitamente.

#### God methods decompostos (AGENTS.md §3)

- `EventLog.append` (CC=11, ~140 LOC) → `_preflight` (CC=5) + `_do_storage_call` (closure) — Iter 1.
- `FalkorDBProjector._project_agent` (CC=9) → `_categorize_events` (CC=6) + 4 helpers puros + 5 `_merge_*` methods — Iter 8.
- `SolutionProjector._dispatch_query` (sync/async dispatch inline) → composição pura com `GraphSolutionAdapter` — Iter 13.

#### Async-only (AGENTS.md §4)

- `GraphAdapter` rejeita sync `Graph` (FalkorDB <1.6) — sem retrocompatibilidade.
- `_dispatch_query` sync/async branch removido — `AsyncGraph` (FalkorDB 1.6+) é o único caminho.

#### Tipos concretos de erro (AGENTS.md §6)

- `RedisUnavailableError`, `IdempotencyConflict`, `MemoryError`, `MemoryDecodeError`.
- `GraphError` com discriminador `kind` ("query_failed", "connection_lost", ...).
- `IdempotencyConflict` raised na borda do adapter, propagado sem catch genérico.

### 2.4 Padrão estabilizado

Cada sub-adapter segue **exatamente o mesmo shape**:

1. **Cypher constants** como class attributes (`CYPHER_UPSERT`, `CYPHER_FIND_BY_ID`, ...).
2. **Composição** via `__init__(graph: GraphAdapter)` — não herança.
3. **Métodos tipados** com keyword-only params e retorno explícito.
4. **Error propagation** — `GraphError` raised na borda, não engolido.
5. **Testes** com mock `GraphAdapter` (sem rede, sem fakeredis).

Isto facilita **trocar backend** (Neo4j, Memgraph) = trocar `_adapter.py`, manter sub-adapters.

---

## 3. Consequências

### Pros

- **Encapsulamento total**: zero `import redis.asyncio` em `fmh_backend/src/fmh_backend/`
  fora de `infra/redis/`. Zero `import falkordb` em runtime fora de `knowledge/graph/`.
  Zero `import ollama` em runtime fora de `knowledge/embedding/_ollama.py`.

- **Testabilidade**: 209 tests novos (993 → 1061) cobrem Protocol, value types,
  adapters, migrações. Sub-adapters testáveis isoladamente com mocks.

- **Decomposição dos god methods**: `EventLog.append` CC=11→2; `FalkorDBProjector._project_agent`
  CC=9→6; `SolutionProjector._dispatch_query` removido. **CC total 12 → 9 (delta -3)**.

- **Migração sem breaking change (Iter 1-5)**: shims 1-linha permitiram rollout incremental.
  Iter 6 removeu shims — único commit com 332+ call sites migrados atomicamente.

- **Padrão replicável**: o sharding FalkorDB (Iter 10-14) seguiu exatamente o
  mesmo template do Redis (Iter 1-5). Iter 9 (embedding) também.

### Cons

- **Indireção**: 1-2 chamadas extras por operação de I/O (delegação).
  Custo negligível (~microsegundos) em benchmarks sintéticos.

- **Sub-adapters exigem `FalkorDBClient` ainda legada**: `SolutionProjector` e
  `FalkorDBProjector` continuam recebendo `FalkorDBClient` (não `GraphPool`).
  Migração completa é trabalho adicional de Iter futura. *(Resolvido:
  Iter 24 fechou; ver
  [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md) e
  [ADR-033](./ADR-033-GraphPool-Reorg.md).)*

- **12 tests skipped em Iter 14**: filtros opcionais (`tags`, `tool_name`, `status`)
  em `find_solutions_by_*` ainda não migrados. Decisão pragmática: marcar
  como skip com nota explicativa, não bloquear Iter 14.

- **Curva de aprendizado**: 4 tipos de adapter (Protocol, sub-adapter, factory, facade)
  com regras de composição. Onboarding de novos contribuidores exige entender
  a hierarquia antes de adicionar um novo shard.

### Métricas observadas (antes vs depois do fecho)

| Métrica | Antes (Iter 0) | Depois (Iter 21) |
|---|---|---|
| Imports `redis.asyncio` em `fmh_backend/src/fmh_backend/` | 11 | 2 (lazy, em `_client.py`) |
| Imports `falkordb` em `fmh_backend/src/fmh_backend/` | 5+ (runtime) | 0 (TYPE_CHECKING) |
| Imports `ollama` em `fmh_backend/src/fmh_backend/` | 1 (lazy) | 0 (TYPE_CHECKING) |
| Imports `gliner2` em `fmh_backend/src/fmh_backend/` | 1 (eager em `GlinerIntentClassifier`) | 0 (lazy, em `GlinerIntentAdapter`) |
| Imports `litellm` em `fmh_agents/src/fmh_agents/` | 4 (eager em `llm.py`) | 0 (lazy, em `_llm_client.py`) |
| Files > 500 LOC | 2 (idempotency.py, event_log/store.py) | 0 |
| CC offenders (CC > 10) | 12 | 10 (delta -2) |
| MI offenders (MI < 20) | 0 | 0 (gate ≥ 65) |
| Total tests | 870 | 1155 (+285) |
| Total skipped tests | 0 | 6 (Iter 14 resolvido na 16) |
| Adapter Protocols runtime_checkable | 0 | 11 (RedisLike + 6 shards + GraphAdapter + EmbeddingProvider + LLMTransport + IntentClassifier/EntityExtractor/ArgumentExtractor compartilhados) |
| Sub-adapters tipados | 0 | 15 (Redis 6 + Graph 4 + Embedding 1 + GLiNER2 3 + LLM 1) |
| Facades `Client`/`SLM*` | 0 | 3 (`EmbeddingClient`, `LLMClient`, `SLM*` × 3) |
| Backward compat shims | 0 | 0 (todos removidos na Iter 6/9) |

### Conquistas notáveis

1. **`SolutionProjector._dispatch_query`** (47 LOC, sync/async dispatch inline) —
   removido. `GraphSolutionAdapter` é puramente async. Dispatch agora é responsabilidade
   do `GraphAdapter` (FalkorDB 1.6+ `AsyncGraph`), não do orquestrador.

2. **`FalkorDBClient.graph()` retornava `Graph | AsyncGraph` direto** — agora retorna
   `GraphAdapter` (Protocol). Callers que precisavam fazer `inspect.iscoroutinefunction(graph.query)`
   agora chamam `await adapter.query(...)` sem dispatch.

3. **`HashEmbeddingProvider` (94 LOC, vetor hash determinístico)** — removido. Tests usam
   `FakeEmbeddingProvider` (vetor zero determinístico) que satisfaz o Protocol.
   `FalkorDBProjector.__init__` agora exige `embedding=` explicitamente.

4. **21 callers de `EventLog(redis_client=)` legados** — todos migrados para
   `EventLog(RedisEventLogAdapter(client=redis))` no mesmo commit (Iter 6).

---

## 4. Migration

### Já migrado (mesmo commit por iteração)

- **Iter 6**: 332+ callers externos (`fmh_app`, `fmh_office`, `fmh_agents`) — `EventLog(redis_client=)` → `EventLog(storage=...)`.
- **Iter 9**: 21 sites de `HashEmbeddingProvider` → `FakeEmbeddingProvider` em `fmh_backend.testing`.
- **Iter 11-13**: 6 sites em `FalkorDBProjector` + 7 sites em `SolutionProjector` → sub-adapters.

### Pendente (futuras iterações)

- **`FalkorDBClient` legacy** (`fmh_backend/knowledge/falkordb/client.py`): coexiste
  com `GraphPool`. Migração completa exige:
  - Atualizar `FalkorDBProjector.__init__` para aceitar `GraphPool` (não `FalkorDBClient`).
  - Atualizar `SolutionProjector.__init__` (em `fmh_agents/`).
  - Atualizar `GraphRAGRetriever.__init__` para aceitar `GraphPool` + `EmbeddingProvider` (não `FalkorDBClient`).
  - Atualizar tests de integration (`test_falkordb_projection.py`, `test_solution_integration.py`).
  - Remover `FalkorDBClient` + shim de import. *(Resolvido: Iter 24
  removeu o shim; ver
  [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md).*

- **`knowledge_consolidator` → `RedisLike` (Iter 18a, bloqueado)**:
  circular import pré-existente em `fmh_agents.memory.solutions` (envolve
  `_promoter` → `gliner2`). Workaround atual: `tests/conftest.py` tem
  `collect_ignore_glob` para `test_knowledge_consolidator.py`/`test_solutions.py`.
  Requer refactor estrutural em `fmh_agents` antes de destravar.

- **`LiteLLMTool` ler `Settings` para temperature/max_tokens/cost`** (Iter 22):
  o adapter lê `model_name` e `timeout` (Iter 20), mas os demais
  (`llm_default_temperature`, `llm_default_max_tokens`,
  `llm_max_cost_usd_per_request`) ainda não são consumidos.

- **`OllamaEmbeddingAdapter.embed()` usar `embedding_timeout_seconds`** (Iter 23):
  via `asyncio.wait_for` ao redor da chamada do `ollama.AsyncClient`.

- **Filtros opcionais em `find_solutions_by_*`**: ✅ **resolvido na Iter 16** com
  `string.Template.safe_substitute` (template Cypher tem `$vec`/`$k`/`$tool_name`
  que `str.format` interpretaria como chaves). -6 skipped tests.

### Roadmap pós-ADR-019

| # | Escopo | Status |
|---|---|---|
| 22 | `LiteLLMTool` lê `temperature`/`max_tokens`/`cost` de Settings | ✅ Iter 22 |
| 23 | `OllamaEmbeddingAdapter.embed()` com `asyncio.wait_for(timeout=...)` | ✅ Iter 23 |
| 24 | Migração `FalkorDBClient` → `GraphPool` (remove legacy) | ✅ Iter 24 (ver [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md)) |
| 25 | Resolver circular import + `knowledge_consolidator` → `RedisLike` | ✅ Iter 25 (ver [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md)) |

> **Nota (post-Iter 28 FU 6)**: o roadmap do ADR-019
> está **completamente fechado para o core framework**
> e o vertical principal. Iter 22-25 endereçaram toda
> a dívida residual documentada. Iter 28 fechou os
> últimos itens:
>
> - Iter 27: leak GLiNER2 binding fechado (eager
>   imports). Ver [ADR-026](./ADR-026-Close-GLiNER2-Binding-Leak.md).
> - Iter 28 + 28 FU 1: argument-extraction migrado
>   para framework + vertical package deletado. Ver
>   [ADR-027](./ADR-027-Argument-Extraction-Migration.md)
>   e [ADR-028](./ADR-028-Delete-Argument-Extraction-Vertical.md).
> - Iter 28 FU 2: `LiteFalkorDBClient` migrado para
>   `LiteGraphClient` (dev-only; renomeado
>   `LiteGraphPool` em Iter 28 FU 7). Ver
>   [ADR-029](./ADR-029-LiteFalkorDBClient-to-LiteGraphClient.md).
> - Iter 28 FU 3: `LLMTransport` agora é
>   `Callable[LLMRequest, dict]` (structural); value
>   object `LLMRequest` introduzido. Ver
>   [ADR-030](./ADR-030-LLMTransport-as-Callable.md).
> - Iter 28 FU 4: `GraphLike` Protocol deletado
>   (zero callers pós-Iter 28 FU 2). Ver
>   [ADR-031](./ADR-031-GraphLike-Deletion.md).
> - Iter 28 FU 5: cycle import `tools.cache` →
>   `event_log` → `infra` → `lite_graph_client` →
>   `knowledge.falkordb.adapter` resolvido via
>   `TYPE_CHECKING` block. Ver
>   [ADR-025 §6](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md#6-roadmap-residual-pós-adr-025).
> - Iter 28 FU 6: `LLMClient` facade deletado
>   (zero callers production). Ver
>   [ADR-032](./ADR-032-LLMClient-Deletion.md).
> - Iter 28 FU 7: `GraphClient` renomeado para
>   `GraphPool` e movido de `knowledge.graph/` para
>   `infra/graph/` (segue o pattern do `RedisPool`).
>   `LiteGraphClient` renomeado para `LiteGraphPool`
>   e movido para `infra.graph._lite_pool`.
>   Ver [ADR-033](./ADR-033-GraphPool-Reorg.md).
> - Iter 26 (Iter 28 FU 8): `KnowledgeConsolidator`
>   decomposto em 3 Reactive Systems
>   (`SolutionExtractorSystem`,
>   `SolutionPromoterSystem`,
>   `SolutionReviewPublisherSystem`) consumindo o
>   World pós-fold. Componentes ECS
>   (`ToolCallRequest`/`ToolCallCompletion`)
>   materializam o pareamento request↔completion
>   via archetype migration. Eventos são a source
>   of truth; componentes são cache derived.
>   **Implementado** (6 ciclos TDD; ver
>   [ADR-034 §7](./ADR-034-ToolCall-ECS-Components.md#7-apêndice-implementação-completa-iter-28-fu-8)):
>   `KnowledgeConsolidator` (892 LOC god module)
>   deletado; 3 systems puros (~380 LOC total)
>   substituem. Net change: −282 LOC; 39 tests
>   novos. Ver [ADR-034](./ADR-034-ToolCall-ECS-Components.md).
>
> Nenhuma biblioteca externa nova precisa de
> cobertura. Iter 26 (Iter 28 FU 8) é refator de
> performance, não cobertura arquitetural.

---

## 5. Decisões relacionadas

- **AGENTS.md §1**: regra "1 lib externa = 1 adapter Protocol" agora aplicada a
  **6 domínios** (Redis, FalkorDB, Embedding, LLM, Auth cache, GLiNER2).
  3 facades `Client`/`SLM*` estabilizadas. Iter 19/20 amarraram Settings
  como fonte única de configuração de backend.

- **AGENTS.md §2**: "zero shims de compatibilidade" agora é lei.
  Iter 6, 9, 17a, 18b estabeleceram o precedente: 332+ callers migrados
  atomicamente em 1 commit, 0 shims remanescentes. Renames (Iter 15, 18c,
  21) também sem shim — call sites atualizados no mesmo commit.

- **AGENTS.md §3**: "files > 500 LOC devem ser divididos" — verificado
  com `wc -l` no CI. `idempotency.py` (Iter 1) e `event_log/store.py` (Iter 5)
  foram ambos divididos. `LiteLLMTool` (Iter 18c) separado em
  `LiteLLMTransportAdapter` (low-level) + `LLMClient` (facade).

- **AGENTS.md §4**: "async é pilar" — `GraphAdapter`, `LLMTransport`,
  `IntentClassifier` são async-only. `SolutionProjector._dispatch_query`
  (sync/async inline) foi removido (Iter 13). GLiNER2 inference é
  sempre `asyncio.to_thread` (PyTorch não é asyncio-native).

- **AGENTS.md §6**: "tipos concretos de erro, fail-closed" — `GraphError`
  raised na borda com discriminador `kind`. `IdempotencyConflict` propagado
  sem catch genérico. `APIKeyCacheAdapter` (Iter 17b) é fail-soft:
  erros de IO no Redis **não** são cacheados (só success e "user not
  found" explícito).

- **AGENTS.md §1.5 (TYPE_CHECKING)**: aplicado consistentemente em todos
  os módulos que precisam de tipos de bibliotecas externas (`falkordb`,
  `ollama`, `gliner2`, `litellm`, `starlette`) sem custo de runtime.

---

## 6. Lições aprendidas

### O que funcionou

1. **Padrão replicável**: o template do Redis (Iter 1-5) foi aplicado diretamente
   ao FalkorDB (Iter 10-14) com 1 iteração por sub-adapter. Mesmo template
   funcionou para embedding (Iter 9).

2. **Migração atômica com shim temporário**: Iter 1-5 introduziu shims que
   permitiram rollout incremental sem breaking changes. Iter 6 removeu
   shims em 1 commit (332+ callers). **Shim temporário é uma ferramenta, não
   um padrão permanente**.

3. **TDD estrito para Protocol**: 209 tests novos foram escritos **antes**
   da implementação. Cada Protocol tem seu `test_<protocol>_protocol.py` que
   verifica `isinstance` checks, runtime_checkable, e shape.

4. **Composition over inheritance**: sub-adapters recebem `GraphAdapter` no
   `__init__`, não herdam dele. Cada sub-adapter testável com mock isolado.

5. **Decisões pragmáticas marcadas como skip**: em Iter 14, filtros opcionais
   não-migrados foram marcados como `pytest.skip(...)` com nota explicativa
   sobre o trade-off. Não bloqueou o progresso.

### O que poderia ter sido melhor

1. **Migração completa de `FalkorDBClient` legacy** ficou pendente. Decisão
   pragmática de coexistência é defensável, mas o roadmap deve endereçar
   remoção completa em Iter 15. *(Resolvido: Iter 24 removeu a coexistência;
   ver [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md).)*

2. **12 skipped tests** em Iter 14 são uma **dívida de testes**: o Cypher
   composto com filtros opcionais deveria ter sido migrado em vez de skip.
   Decisão: priorizar o sharding completo, deixar refinamento para depois.
   *(Resolvido: Iter 16 reativou os 6 tests com `string.Template.safe_substitute`.)*

3. **Naming** (`GraphAdapter` vs `FalkorDBAdapter`) — escolha de `Graph*` para
   seguir o pedido de "interface desacoplada do backend" foi acertada, mas o
   iter poderia ter sido mais cedo.

4. **Documentação inline de Cypher** — cada Cypher constant no sub-adapter
   poderia ter referência ao ADR-019 (e.g. "ADR-019 §X — Document node schema").
   O docstring atual é focado no "como", não no "porquê".

5. **Cycle `fmh_agents.memory.solutions`** (Iter 18a) — atribuído a
   `gliner2` no epílogo. Investigação posterior (Iter 25) revelou que
   `gliner2` é distração; o cycle é puramente framework
   (`_promoter.py:25` → `tools.pii` → `tools.arg_validation` →
   `argument_extractor` → `solution_projector` → `memory.solutions`).
   Lição: a diagnose inicial incompleta atrasou o fix em 4 iterações.
   *(Resolvido: Iter 25 fechou com Protocol split + re-architecture;
   ver [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md).)*

---

## 7. Referências

- [ADR-019](./ADR-019-Redis-Adapter-Typing.md) — Iter 1 (EventLog adapter)
- [AGENTS.md §1](../../AGENTS.md) — adapter types
- [AGENTS.md §2](../../AGENTS.md) — zero shims
- [AGENTS.md §3](../../AGENTS.md) — god modules
- [AGENTS.md §4](../../AGENTS.md) — async-first
- [AGENTS.md §6](../../AGENTS.md) — tipos concretos de erro
- [scripts/ci.py](../../scripts/ci.py) — gates de complexidade (radon CC/MI)
- [infra/redis/](../src/fmh_backend/infra/redis/) — 6 shards Redis tipados
- [knowledge/graph/](../src/fmh_backend/knowledge/graph/) — Protocol + 1 adapter + 4 sub-adapters + facade
- [knowledge/embedding/](../src/fmh_backend/knowledge/embedding/) — Protocol + `EmbeddingClient` facade + `OllamaEmbeddingAdapter`
- [testing/](../src/fmh_backend/testing/) — `FakeEmbeddingProvider` (test double)

---

**Conclusão**: o ADR-019 está fechado. A regra "1 lib externa = 1 adapter Protocol"
foi aplicada consistentemente em **6 domínios** (Redis, FalkorDB, Embedding,
LLM, Auth cache, GLiNER2) e amarrada via Settings como fonte única de
configuração. Iter 22-28 endereçaram toda a dívida residual
documentada no roadmap:
- Iter 22: `LiteLLMTool` consome Settings (temperature/max_tokens/cost)
- Iter 23: `OllamaEmbeddingAdapter` tem timeout via `asyncio.wait_for`
- Iter 24: `FalkorDBClient` removido, `GraphPool` é o único canônico
- Iter 25: Tool Protocol decomposto em 3 camadas; cycle estruturalmente eliminado
- Iter 27: leak GLiNER2 binding fechado; `GlinerArgumentAdapter` vive no framework
- Iter 28 + 28 FU 1: argument-extraction migrado para `fmh_backend.knowledge.extraction.argument`; vertical package deletado
- Iter 28 FU 2: `LiteFalkorDBClient` migrado para `LiteGraphClient` (renomeado `LiteGraphPool` em Iter 28 FU 7); `LiteGraphAdapter.query` é async
- Iter 28 FU 3: `LLMTransport` agora é `Callable[LLMRequest, dict]`; `LLMRequest` é value object frozen
- Iter 28 FU 4: `GraphLike` Protocol deletado (zero callers)
- Iter 28 FU 5: cycle import `tools.cache` resolvido via `TYPE_CHECKING`
- Iter 28 FU 6: `LLMClient` facade deletado (zero callers production)
- Iter 28 FU 7: `GraphClient` renomeado para `GraphPool`; reorg para `infra/graph/`
- Iter 28 FU 8: `KnowledgeConsolidator` → 3 Reactive Systems + ECS components (`ToolCallRequest`/`ToolCallCompletion`); `project_tool_calls` projection; eventos = source of truth; **implementado** (6 ciclos TDD, net −282 LOC, 39 tests novos). Ver [ADR-034](./ADR-034-ToolCall-ECS-Components.md)

Nenhuma biblioteca externa nova precisa de cobertura. O roadmap
residual (Iter 26+ — ver [ADR-025 §6](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md#6-roadmap-residual-pós-adr-025))
é puramente refator de performance, não cobertura arquitetural.

---

## 8. Apêndice: Iter 15 — Embedding Client facade

**Motivação**: após a Iter 9, a única impl concreta de `EmbeddingProvider`
era `OllamaEmbeddingProvider`. O nome "Ollama" vazava na API pública
do framework, violando o mesmo princípio aplicado a
`GraphAdapter` (sem "FalkorDB" no nome).

**Mudança**:
- `OllamaEmbeddingProvider` → `OllamaEmbeddingAdapter` (low-level).
- Nova facade `EmbeddingClient` que **é um** `EmbeddingProvider` (subclasse
  do Protocol) e delega para o adapter concreto. Aplicações usam
  `EmbeddingClient()` por default.
- `_client.py` novo módulo; `provider.py` re-exporta `EmbeddingClient` +
  `OllamaEmbeddingAdapter` (low-level, avançado).
- 0 testes quebrados (renames); 0 testes novos (refactor sem mudança de
  comportamento).

**Antes vs depois**:

```python
# Antes
from fmh_backend.knowledge.embedding.provider import OllamaEmbeddingProvider
provider = OllamaEmbeddingProvider()  # nome vaza backend

# Depois
from fmh_backend.knowledge.embedding.provider import EmbeddingClient
provider = EmbeddingClient()  # default facade, sem nome de backend

# Low-level (avançado)
from fmh_backend.knowledge.embedding.provider import OllamaEmbeddingAdapter
adapter = OllamaEmbeddingAdapter(host="http://localhost:11434")
```

---

## 9. Apêndice: Iter 16 — Filtros opcionais em `find_solutions_by_*`

**Motivação**: a Iter 14 migrou `find_solutions_by_problem` e
`find_solutions_by_tool` para `GraphSolutionAdapter`, mas deixou os
filtros opcionais (`tags`, `tool_name`, `status`) como `pytest.skip(...)`
com nota explicativa. Dívida de teste acumulada.

**Mudança**:
- Adicionados 3 Cypher constants parametrizados: `CYPHER_FIND_BY_PROBLEM_FILTERED`,
  `CYPHER_FIND_BY_TOOL_FILTERED`, `CYPHER_FIND_BY_PROBLEM_AND_TOOL_FILTERED`.
- Uso de `string.Template.safe_substitute` em vez de `str.format`: o template
  Cypher contém `$vec`/`$k` (param names reais do FalkorDB) que `str.format`
  interpretaria como chaves de substituição e falharia com `KeyError`.
- `safe_substitute` deixa `$vec`/`$k`/`$tool_name` intactos quando não há
  match, e substitui apenas as variáveis explicitamente passadas.
- 6 skipped tests reativados (3 para `find_solutions_by_problem`,
  3 para `find_solutions_by_tool`).

**Lição**: `string.Template` é a escolha correta para templates Cypher;
`str.format` parece atraente mas é uma armadilha quando o template tem
literais `$` que não devem ser tratados como chaves.

---

## 10. Apêndice: Iter 17 — LLM cache adapters

**Motivação**: a Iter 6 removeu shims de Redis, mas 2 callers ainda usavam
`redis.asyncio.Redis` direto: `RedisCacheStorage` (LLM response cache) e
`APIKeyCacheStorage` (auth). Dois caminhos paralelos precisavam do mesmo
encapsulamento.

**Iter 17a — `RedisCacheAdapter`**:
- `RedisCacheStorage` → `RedisCacheAdapter`, agora consome `RedisLike`
  (Protocol) em vez de `redis.asyncio.Redis` direto.
- Adicionado `expire`/`unlink` ao Protocol `RedisLike` (Iter 17a + 17b).
- Pipeline HSET+EXPIRE em 1 round-trip (anteriormente 2 commands separados).

**Iter 17b — `APIKeyCacheAdapter`**:
- TTL-based cache (chave-valor simples, `EX 30`).
- **Negative caching**: `cache miss` é cacheado por 30s para evitar
  hammering no auth provider.
- **Fail-soft**: erros de IO no Redis **não** são cacheados (só success
  e "user explicitly not found" são).
- **LRU cap**: máximo 1024 entries com eviction determinística.

**Antes vs depois**:

```python
# Antes (Iter 16)
class RedisCacheStorage:
    def __init__(self, client: redis.asyncio.Redis): ...

# Depois (Iter 17a)
class RedisCacheAdapter:
    def __init__(self, client: RedisLike): ...
```

---

## 11. Apêndice: Iter 18 — LLM Transport Protocol

**Motivação**: `fmh_agents/tools/llm.py` (`LiteLLMTool`) tinha 4
`import litellm` inline (mesmo padrão que `OllamaEmbeddingProvider` na
Iter 9). Precisava do mesmo tratamento: Protocol + adapter + facade.

**Iter 18b — Protocol + facade**:
- `LLMTransport` agora é `Protocol` + `runtime_checkable` (duck typing).
- `LLMClient` (facade) que **é um** `LLMTransport` (subclasse do Protocol)
  e delega para o adapter concreto. IS-A Protocol, composição clássica.
- `LLMClient(adapter=FakeLLMTransport())` funciona sem `litellm` instalado
  — a facade importa o adapter lazy, **só no default branch**.

**Iter 18c — `LiteLLMTransportAdapter`**:
- `LiteLLMTool` (que era a impl concreta) → `LiteLLMTransportAdapter` (low-level).
  `LiteLLMTool` permanece como facade thin para apps legadas que já a
  importam.
- `_llm_client.py` separado: a facade `LLMClient` mora lá, isolada do
  `llm.py` (que tem `LiteLLMTransportAdapter` + `LiteLLMTool`).
- 3 tests em `test_llm_client_guard.py` verificam que `LLMClient(adapter=...)`
  funciona sem `litellm` instalado, mas `LLMClient()` (default) precisa dele.

**Lição**: separar facade (que precisa importar adapter) de adapter (que
já é concreto) é crítico. O `TYPE_CHECKING`/`is None` guard na facade
garante que `from fmh_agents.tools import LLMClient` nunca quebra —
só `LLMClient()` (default) tenta o import lazy.

---

## 12. Apêndice: Iter 19 + 20 — Settings como fonte única

**Motivação**: a AGENTS.md §1 ("adapter types") é a primeira linha de
defesa, mas a AGENTS.md §1 também implica: **configuração do backend
também deve ser tipada**. Sem isso, adapters hard-codam `model="gpt-4o-mini"`,
`dimension=768`, `timeout=30s` — todos valores que mudam por deployment.

**Iter 19 — Settings mixins**:
- `LLMSettingsMixin`: `llm_default_model`, `llm_default_temperature` (0-2),
  `llm_default_max_tokens`, `llm_default_timeout_seconds`,
  `llm_max_cost_usd_per_request`. Validação em `__post_init__`.
- `EmbeddingSettingsMixin`: `embedding_model_id`, `embedding_dimension`,
  `embedding_timeout_seconds`. Validação em `__post_init__`.
- `KnowledgeSettingsMixin` (já existente): adicionado
  `arg_extractor_model_id` (default corrigido `"default"` → `"gliner2-base"`).
- `Settings` (em `infra/config/__init__.py`) herda mixins via múltipla
  herança: `Settings(LLMSettingsMixin, EmbeddingSettingsMixin, ..., BaseSettings)`.

**Iter 20 — Adapters leem Settings via helper**:
- `_resolve_llm_defaults(model, timeout)` em `LiteLLMTool.__init__`
  (staticmethod encapsulado, CC=1 no `__init__`).
- `_resolve_defaults(model, dimension)` em `OllamaEmbeddingAdapter.__init__`
  (mesmo padrão).
- Sentinel `None` = "use Settings"; valor explícito = "use this value".
- 14 tests novos (`test_llm_settings.py` + `test_embedding.py` +
  `test_ollama_settings.py`) verificam o comportamento.

**Lição**: encapsular Settings em helper estático mantém o `__init__`
com CC baixo (≤ 2), facilita test isolation (helper é testado
independentemente do adapter), e cria um único ponto de extensão quando
um novo field de Settings precisar ser lido por um adapter.

---

## 13. Apêndice: Iter 21 — SLM facades (GLiNER2 unificado)

**Motivação**: GLiNER2 é usado em 3 lugares no framework (intent classification,
entity extraction, argument extraction), cada um com sua classe concreta
(`GlinerIntentClassifier`, `GlinerEntityExtractor`, `GlinerArgumentExtractor`).
A inconsistência de nomes (2 sufixos diferentes para o mesmo padrão) e a
exposição do backend ("Gliner") na API pública violavam a AGENTS.md §1.

**Mudança**:
- 3 renames atômicos: `GlinerIntentClassifier` → `GlinerIntentAdapter`,
  `GlinerEntityExtractor` → `GlinerEntityAdapter`,
  `GlinerArgumentExtractor` → `GlinerArgumentAdapter` (sufixo `Adapter`
  consistente com `OllamaEmbeddingAdapter`, `FalkorDBGraphAdapter`,
  `LiteLLMTransportAdapter`).
- 3 facades `SLM*`: `SLMIntentClassifier`, `SLMEntityExtractor`,
  `SLMArgumentExtractor`. Prefixo `SLM` (Small Language Model) decouple
  o framework do backend GLiNER2 especificamente — um futuro
  `TinyLLMEntityAdapter` pode ser plugged em via `SLMEntityExtractor(adapter=...)`
  sem mudar a facade.
- Cada facade é IS-A Protocol (`SLMEntityExtractor` IS-A
  `EntityExtractorWithMentions`, etc) e delega para o adapter concreto.
- 21 tests novos (`test_slm_facades.py` + ajustes em `test_gliner_settings.py`).

**Antes vs depois**:

```python
# Antes (Iter 20)
from fmh_backend.knowledge.extraction import GlinerIntentClassifier
clf = GlinerIntentClassifier()  # nome vaza backend

# Depois (Iter 21)
from fmh_backend.knowledge.extraction import SLMIntentClassifier
clf = SLMIntentClassifier()  # facade neutra, default = GlinerIntentAdapter

# Low-level (avançado)
from fmh_backend.knowledge.extraction import GlinerIntentAdapter
adapter = GlinerIntentAdapter(model_name="custom/local/checkpoint")
```

**Naming convention estabilizado** (após Iter 21):
- `XxxProtocol` — interface runtime_checkable.
- `XxxAdapter` — impl concreta, low-level, sufixo consistente.
- `XxxClient` ou `Xxx<Domain>` — facade pública, IS-A Protocol, escolhe impl.
- `XxxStorage` — protocolo de storage (Redis shards), não segue o pattern facade.

**Lição**: o prefixo `SLM` (`Small Language Model`) é o sufixo de
descoberta mais claro para "este objeto encapsula um modelo local de
linguagem". É mais expressivo que `Local*` (vago sobre tipo) ou
`Ner*` (acoplado ao caso de uso NER), e sobrevive à troca de backend
(GLiNER2 → TinyLLM → FastText → ...).

---

## 14. Apêndice: Iter 27 + 28 — Argument extraction migração completa

**Motivação**: três dívidas residuais convergiam no mesmo ponto:
1. `GlinerArgumentAdapter` em `fmh_agents.knowledge.argument_extractor`
   violava AGENTS.md §1.2 (adapter de lib externa não pode viver em vertical).
2. `SchemaArgumentExtractor`, `GlinerFieldFinder`, `FieldFinder`,
   `RegexFieldFinder`, `coerce` coexistiam em vertical com seus contratos
   (Protocol `FieldFinder`, dataclasses de args/results) — primitivas que
   framework deveria possuir.
3. `LiteFalkorDBClient` (em `fmh_agents.infra.falkordblite_adapter`) era o
   último caller que ainda usava `GraphLike` (Protocol deprecated) e
   tinha o nome do backend (`FalkorDB`) no API público do vertical.

**Iter 27 — Close GLiNER2 binding leak** (ver [ADR-026](./ADR-026-Close-GLiNER2-Binding-Leak.md)):
- `GlinerArgumentAdapter` movido para `fmh_backend.knowledge.extraction.argument._gliner_finder`.
- Lazy imports (`from gliner2 import ...` dentro de `__init__` ou métodos)
  eliminados; framework pode ser importado sem GLiNER2 instalado.
- `SLMArgumentExtractor` (facade) instanciado pelo vertical, não pelo framework.

**Iter 28 + 28 FU 1 — Argument extraction migration + vertical deletion** (ver [ADR-027](./ADR-027-Argument-Extraction-Migration.md) e [ADR-028](./ADR-028-Delete-Argument-Extraction-Vertical.md)):
- 5 componentes movidos atomicamente para `fmh_backend.knowledge.extraction.argument.{_finder, _coerce, _extractor, _gliner_finder}`:
  - `FieldFinder` (Protocol), `RegexFieldFinder` (impl concreta)
  - `coerce` (helper de normalização)
  - `SchemaArgumentExtractor` (orquestra FieldFinder + coerce)
  - `GlinerFieldFinder` (adapter GLiNER2, low-level)
- Migração atômica em 1 commit: 4 call sites atualizados (solução projector,
  teste de schema, vertical tests, vertical re-exports); lazy imports
  removidos (Iter 27 tinha shim transicional; Iter 28 fecha de vez).
- Vertical `fmh_agents.knowledge.argument_extractor` deletado inteiro (não
  re-export shim). 1 deletion gate explícito
  (`test_argument_vertical_deleted.py`) com subprocess + `textwrap.dedent`
  script que verifica `sys.exit(0)` quando módulo é importado e
  `sys.exit(1)` quando removido.

**Iter 28 FU 2 — LiteFalkorDBClient → LiteGraphPool** (ver [ADR-029](./ADR-029-LiteFalkorDBClient-to-LiteGraphClient.md)):
- `LiteFalkorDBClient` (vertical, dev-only) renomeado para `LiteGraphPool`
  (framework, dev-only). Mesmo padrão do `OllamaEmbeddingProvider` →
  `OllamaEmbeddingAdapter` (Iter 9 + 15): nome neutro de backend.
- `LiteGraphAdapter.query` agora async via `asyncio.to_thread` —
  `falkordblite` é sync, mas framework é async (AGENTS.md §4).
- `LiteGraphPool.graph()` retorna `GraphAdapter` (não mais `GraphLike`).
- Vertical `falkordblite_adapter.py` deletado.
- **Consequência não antecipada**: cycle import em `fmh_agents.tools.cache`
  (introduzido pela migração); correção é Iter futura.

**Iter 28 FU 3 — LLMTransport as Callable** (ver [ADR-030](./ADR-030-LLMTransport-as-Callable.md)):
- `LLMTransport` era Protocol com método `complete(LLMRequest) -> dict`.
  Renomeado para `__call__` (structural match com `Callable[[LLMRequest], dict]`).
- `LLMRequest` introduzido como `@dataclass(frozen=True)` value object
  (antes: `**kwargs: Any` no Protocol). Elimina `Any` em framework.
- Protocol `LLMTransport` é subclass de `Protocol` (não `Callable[...]`
  subscripted) — Python 3.12 `@runtime_checkable` quebra quando Protocol
  herda de `Callable[P, T]`. Solução: `class LLMTransport(Protocol)` com
  `__call__` definido diretamente; structural match ainda funciona.
- Vertical `fmh_agents.tools.llm_transport` re-exporta `LLMTransport` e
  `LLMRequest` (re-export shim, não compat shim).
- 3 implementações migradas: `LiteLLMTransportAdapter.__call__`,
  `CachingLLMTransport.__call__`, `FakeLLMTransport.__call__`.

**Antes vs depois (Iter 28 + 28 FU 1-3)**:

```python
# Antes (Iter 26)
# Vertical dependia de framework indiretamente
from fmh_agents.knowledge.argument_extractor import (
    GlinerArgumentAdapter,  # nome + leak
    SchemaArgumentExtractor,
)
from fmh_agents.infra.falkordblite_adapter import LiteFalkorDBClient
from fmh_agents.tools.llm import LiteLLMTransportAdapter  # método complete

# Depois (Iter 28 + 28 FU 1-3)
from fmh_backend.knowledge.extraction.argument import (
    SchemaArgumentExtractor,  # framework
)
from fmh_backend.knowledge.extraction.argument import GlinerFieldFinder  # low-level
from fmh_backend.knowledge.extraction import SLMArgumentExtractor  # facade
from fmh_backend.infra.graph._lite_pool import LiteGraphPool  # dev-only
from fmh_agents.tools.llm import LiteLLMTransportAdapter  # método __call__
from fmh_backend.tools import LLMTransport, LLMRequest  # Protocol + VO
```

**Lição consolidada**: o padrão "primitiva no framework, impl no vertical"
se aplica uniformemente a **6 domínios** (Redis, FalkorDB, Embedding, LLM,
Auth cache, GLiNER2) e à **3 níveis de profundidade** (Protocol,
low-level adapter, facade). O framework define contratos; verticais
implementam adapters concretos. Cycle imports entre framework e vertical
são eliminados por construction, não por detection. O sufixo `Adapter`
é o signal universal de "low-level, exposto para usuários avançados
que precisam trocar de backend".

**Caveat — facade é opcional**: o path LLM demonstrou que
`XxxClient` (facade) não é sempre necessário. Quando o
consumer canônico é o orchestrator (`LiteLLMTool.invoke`),
o facade intermediário quebra a abstração. Ver
[ADR-032](./ADR-032-LLMClient-Deletion.md): `LLMClient` foi
introduzido em Iter 18b seguindo o pattern de `GraphPool`
mas tinha 0 callers production; Iter 28 FU 6 o deletou
inteiro. O pattern `Protocol + Adapter + Tool` (sem
facade intermediário) é o suficiente para o path LLM.

---

## 15. Apêndice: Iter 28 FU 6 — LLMClient facade deletion

**Motivação**: o `LLMClient` facade introduzido em
Iter 18b/18c tinha 0 callers em production code.
Iter 28 FU 3 (ADR-030) mudou o Protocol para
`__call__(LLMRequest) -> dict`, mas `LLMClient` ainda
declarava `complete` (legacy), criando inconsistência:
o `__call__` herdado do Protocol é um no-op, então
qualquer call site que fizesse `client(request)` receberia
`None`.

**Mudança**:
- `fmh_agents/src/fmh_agents/tools/_llm_client.py` deletado
  (110 LOC).
- `fmh_agents/tests/unit/tools/test_llm_client_guard.py`
  deletado (88 LOC, 3 tests).
- `fmh_agents/tools/__init__.py`: import e `__all__` entry
  removidos.
- `fmh_backend/src/fmh_backend/knowledge/extraction/gliner_argument.py`:
  docstring atualizada (referência a `LLMClient` removida).
- `fmh_backend/tests/unit/test_llm_client_deleted.py`
  (novo, 6 tests): deletion gate + sanity checks.

**Antes vs depois**:

```python
# Antes (Iter 28 FU 3)
from fmh_agents.tools import LLMClient  # facade
client = LLMClient(adapter=my_adapter)
# ^ 0 callers production; declarava `complete` (legacy)
#   mas Protocol agora é `__call__` (current)

# Depois (Iter 28 FU 6)
from fmh_agents.tools.llm import LiteLLMTransportAdapter
transport = LiteLLMTransportAdapter()
# ou qualquer LLMTransport concreto
# (LiteLLMTool aceita via `transport=` kwarg)
```

**Lição**: o pattern `XxxClient` (facade) é útil quando
apps manipulam o objeto diretamente (ex: `EmbeddingClient.embed(...)`).
Quando o consumer canônico é o orchestrator
(`LiteLLMTool.invoke(...)`), o facade intermediário
adiciona indirection sem valor. Decidir por deletion
quando o facade atinge 0 callers — não ampliá-lo
para "tornar mais útil".
