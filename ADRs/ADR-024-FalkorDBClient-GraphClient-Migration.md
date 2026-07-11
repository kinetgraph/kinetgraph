<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-024: Migrate `FalkorDBClient` → `GraphClient` (Iter 24) → `GraphPool` (Iter 28 FU 7)

**Status:** Aceito (obsoleto em parte: o protocolo GraphLike (§2.5) foi removido pelo ADR-031; a classe GraphClient foi renomeada/reorganizada para GraphPool pelo ADR-033)
**Data:** 29 de junho de 2026
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md), [ADR-033](./ADR-033-GraphPool-Reorg.md), [AGENTS.md §1](../../AGENTS.md), §2, §3, §6

Este ADR fecha o último item do roadmap do [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md) §4: a coexistência entre `FalkorDBClient` (legacy) e `GraphClient` (Iter 10, renomeado para `GraphPool` em Iter 28 FU 7 / ADR-033). Após este ADR, `FalkorDBClient` não existe mais; toda a framework consome `GraphPool` que retorna um `GraphAdapter` (Protocol).

---

## 1. Contexto

O ADR-019 epílogo introduziu `GraphClient` em `knowledge/graph/_client.py` (Iter 10, renomeado para `GraphPool` e movido para `infra/graph/_pool.py` em Iter 28 FU 7) com o objetivo explícito de desacoplar o framework do backend FalkorDB. O design foi:

| Cliente | `graph(tenant_id)` retorna | Status |
|---|---|---|
| `FalkorDBClient` (legacy, `knowledge/falkordb/client.py`) | `Graph \| AsyncGraph` (raw nativo) | coexiste |
| `GraphClient` (`knowledge/graph/_client.py`; renomeado `GraphPool` em Iter 28 FU 7) | `FalkorDBGraphAdapter` (Protocol) | coexiste |

O Iter 10 construiu a infraestrutura nova (`GraphAdapter`, `FalkorDBGraphAdapter`, 4 sub-adapters tipados), mas o Iter 13 notou que **3 projectors continuavam consumindo `FalkorDBClient`**: `FalkorDBProjector`, `SolutionProjector`, `ProcessLearnerProjector`. A coexistência era dívida técnica acumulada:

1. **Dois pontos de entrada paralelos** para a mesma conexão FalkorDB.
2. **Discriminação `inspect.iscoroutinefunction`** ainda necessária em alguns call sites (porque `FalkorDBClient.graph()` retorna `Graph | AsyncGraph`).
3. **Documentação e exemplos dividem-se** entre dois nomes.
4. **Tests de integração duplicados** (alguns usam `FalkorDBClient`, outros `GraphPool`).

A regra AGENTS.md §1 ("1 lib externa = 1 adapter Protocol") implica que deve haver **um** cliente canônico para FalkorDB. O ADR-019 §4 já classificou esta migração como "Iter 24 — Médio".

---

## 2. Decisão

### 2.1 Eliminação completa do `FalkorDBClient`

`FalkorDBClient` é **deletado** do framework em commit atômico. Toda a framework passa a consumir `GraphPool` exclusivamente:

```python
# Antes (Iter 1-23)
from fmh_backend.knowledge.falkordb.client import FalkorDBClient
client = FalkorDBClient(host="localhost", port=16379)
graph = client.graph(tenant_id)  # Graph | AsyncGraph

# Depois (Iter 24+)
from fmh_backend.infra.graph import GraphPool
client = GraphPool(host="localhost", port=16379)
graph = client.graph(tenant_id)  # GraphAdapter (Protocol)
```

### 2.2 Compatibilidade

**Zero compat shims.** A regra AGENTS.md §2 ("zero shims de compatibilidade") é aplicada estritamente. A migração é **breaking change** mas trivial:

- Assinatura de construtor: **byte-identical** (`__init__(host="localhost", port=16379, *, password=None)`).
- Métodos: `connect()`, `close()`, `graph(tenant_id)` — mesmos nomes e contratos.
- Única diferença observável: tipo de retorno de `graph()` muda de `Graph | AsyncGraph` para `GraphAdapter` (Protocol). Como toda a framework já opera via `await graph.query(...)`, e `GraphAdapter.query` tem a mesma assinatura assíncrona, **nenhum call site precisa de adaptação mecânica**.

### 2.3 Migração atômica

A migração foi feita em **commit único**, tocando:

| Categoria | Arquivos | LOC delta |
|---|---|---|
| Production source | 4 (`falkordb/adapter.py`, `graphrag/retriever.py`, `fmh_office/learning/projector.py`, `fmh_agents/knowledge/solution_projector.py`) | -8 |
| Public API | 1 (`knowledge/__init__.py`) | -2 / +5 (re-export) |
| Internal helper | 1 (`infra/falkordblite_adapter.py`) | -3 / +3 |
| Type comments | 1 (`core/_typing.py`) | -8 / +5 |
| Deletados | 2 (`knowledge/falkordb/client.py`, `tests/unit/knowledge/test_falkordb_client.py`) | -236 |
| Integration tests | 3 (`test_falkordb_projection.py`, `test_solution_integration.py`, `fmh_office/tests/integration/test_knowledge.py`) | -24 / +24 |
| Unit tests | 4 (`test_solution_projector.py`, `fmh_office/tests/unit/learning/test_projector.py`, etc.) | -12 / +12 |
| Docs | 5 (`graphrag.md`, `consolidation.md`, `embedding.md`, `learning.md`, `getting_started.md`) | -10 / +10 |
| Examples | 3 (`08_falkordb_projection.py`, `09_knowledge_consolidation.py`, `README.md`) | -3 / +4 |
| App | 1 (`fmh_app/src/fmh_app/knowledge/client.py`) | -1 / +1 |

**Total**: 23 arquivos modificados, 2 deletados. 0 call site quebrado.

### 2.4 O `LiteFalkorDBClient` permanece

`LiteFalkorDBClient` (em `infra/falkordblite_adapter.py`) é um adapter dev-only que delega para `falkordblite`/`redislite` (embedded server). Ele é **mantido** porque:

1. **Não viola §1**: `LiteFalkorDBClient` é ele mesmo um adapter Protocol (`GraphLike`), encapsula `falkordb` internamente.
2. **Não tem um substituto direto**: seria trabalho de uma futura Iter paralela (Lite `GraphPool`).
3. **É dev-only**: o uso em produção é desencorajado; `docs/learning.md` explicita que `GraphPool` é o caminho production.

A migração para um futuro `LiteGraphPool` é dívida aberta registrada em §6 (não bloqueia este ADR).

### 2.5 `GraphLike` Protocol preservado

O Protocol `GraphLike` (em `core/_typing.py`) é mantido e seu docstring foi atualizado para refletir que ele serve apenas ao `LiteFalkorDBClient` (sync). Toda a framework de produção consome `GraphAdapter` (async Protocol em `knowledge/graph/_protocol.py`). *(Resolvido: Iter 28 FU 4 / ADR-031 deletou `GraphLike`; ver [ADR-031](./ADR-031-GraphLike-Deletion.md). `knowledge/graph/_protocol.py` permanece como o local canônico do `GraphAdapter` Protocol pós-Iter 28 FU 7.)*

---

## 3. Consequências

### Pros

- **Single source of truth para FalkorDB**: apenas `GraphPool` (em `infra/graph/`, pós-Iter 28 FU 7; era `knowledge/graph/` no Iter 24). Reduz confusão de onboarding.
- **Eliminação do dispatch `inspect.iscoroutinefunction`**: `GraphAdapter` é `async def query(...)` por contrato. Não há mais necessidade de checar se o handle retornado é sync ou async (Iter 13 já tinha removido um caminho similar em `SolutionProjector._dispatch_query`).
- **Documentação unificada**: 5 docs + 3 examples agora referem um único nome.
- **Test suite mais coesa**: 5 tests em `test_falkordb_client.py` deletados; cobertura equivalente já existe em `test_graph_pool.py` para `GraphPool` (em `tests/unit/infra/graph/`, pós-Iter 28 FU 7; era `test_client.py` em `tests/unit/knowledge/graph/`).
- **Imports limpos**: o módulo `solution_projector.py` perdeu os 3 imports `TYPE_CHECKING` (`AsyncGraph`, `Graph`, `QueryResult`) que eram usados apenas para anotar o retorno de `FalkorDBClient.graph()`. Agora o tipo é `GraphAdapter`, encapsulado.

### Cons

- **`FalkorDBClient` é um nome conhecido em produção**: qualquer consumidor externo (não neste monorepo) que importe `from fmh_backend.knowledge.falkordb.client import FalkorDBClient` vai quebrar. Mitigação: este monorepo é o único consumidor (ADR-019 §2.2), e o nome `GraphPool` é semanticamente mais correto.
- **Migração requer `await` em 1 example**: `examples/09_knowledge_consolidation.py:132` tinha `g.query(...)` sync (legado), agora precisa de `await`. O `main()` já era `async def`, então é trivial.
- **Histórico do git é silencioso**: o `git log --follow fmh_backend/knowledge/falkordb/client.py` agora termina aqui. O conteúdo histórico vive no commit anterior (Iter 1-23). Mitigação: ADR-019 epílogo e este ADR documentam a evolução.

### Métricas observadas

| Métrica | Antes (Iter 23) | Depois (Iter 24) |
|---|---|---|
| Imports `falkordb` em `fmh_backend/src/` (runtime) | 1 (`GraphPool.connect`) | 1 (`GraphPool.connect`) — inalterado |
| Imports `falkordb` em `fmh_agents/src/` (runtime) | 1 (`SolutionProjector` indirect via client) | 0 (todo via `GraphAdapter`) |
| Clientes FalkorDB no framework | 2 (`FalkorDBClient`, `GraphPool`) | 1 (`GraphPool`) |
| Tests `FalkorDBClient` | 5 (em `test_falkordb_client.py`) | 0 (deletados) |
| Tests `GraphPool` | 12 (em `tests/unit/knowledge/graph/test_client.py`) | 12 (inalterado) *(Iter 28 FU 7: renomeado para `test_graph_pool.py` em `tests/unit/infra/graph/`)* |
| Compat shims | 0 | 0 |
| Files com `inspect.iscoroutinefunction(graph.query)` | 0 (já removido na Iter 13) | 0 |

### Conquistas notáveis

1. **`SolutionProjector` perdeu 3 imports TYPE_CHECKING órfãos** (`AsyncGraph`, `Graph`, `QueryResult`). Eram usados apenas para anotar o retorno de `FalkorDBClient.graph()`. Após a migração, o tipo é `GraphAdapter` (encapsulado, import só em `TYPE_CHECKING` se necessário).
2. **`core/_typing.py::GraphLike` ficou semanticamente mais claro**: o docstring agora explicita que serve apenas ao `LiteFalkorDBClient` (sync, dev-only). O framework production opera via `GraphAdapter`.
3. **5 tests redundantes deletados**: `test_falkordb_client.py` existia apenas para cobrir o cliente legacy. Cobertura equivalente já vivia em `test_client.py` para `GraphPool`.

---

## 4. Migration

### Já migrado (este commit)

- **Production source (4 files)**: `client: FalkorDBClient` → `client: GraphPool`. Internals `self._client.connect(); graph = self._client.graph(tenant_id)` ficaram textualmente idênticos — `graph` agora é `GraphAdapter` em vez de `Graph | AsyncGraph`. Sub-adapters (`GraphAgentAdapter`, `GraphDocumentAdapter`, `GraphToolCallAdapter`, `GraphSolutionAdapter`) já consumiam `GraphAdapter`, então funcionam sem mudanças.
- **Public API**: `knowledge/__init__.py` re-exporta `GraphPool` em vez de `FalkorDBClient`. `graph_name_for_tenant` é re-exportado do mesmo path.
- **LiteFalkorDBClient local import**: atualizado de `..knowledge.falkordb.client` para `..knowledge.graph._client` (cycle-break preservado).
- **Integration tests**: 3 files atualizados (`test_falkordb_projection.py`, `test_solution_integration.py`, `fmh_office/tests/integration/test_knowledge.py`). Fixtures e helpers usam `GraphPool` agora.
- **Unit tests**: 5 tests deletados (`test_falkordb_client.py`). Docstring do `MockClient` em `test_solution_projector.py` atualizada.
- **Docs + examples**: 5 markdown files + 3 examples atualizados. Um único `await` adicionado em `examples/09_knowledge_consolidation.py:132`.

### Pendente (Iter futura — fora deste ADR)

- ~~**`LiteFalkorDBClient` → `LiteGraphPool`**~~ —
  ✅ feito em [ADR-029](./ADR-029-LiteFalkorDBClient-to-LiteGraphClient.md)
  (Iter 28 FU 2). O dev-only adapter foi migrado
  para `LiteGraphPool`; `graph()` agora retorna
  `GraphAdapter` (não `GraphLike`). O arquivo
  `falkordblite_adapter.py` foi deletado; o módulo
  `lite_graph_client.py` é o canonical home. O
  `GraphLike` Protocol está sem callers (candidato a
  deleção em Iter futura).
- **Migração de `fmh_app.knowledge.client.FalkorGraphPool`**: este é um cliente independente (não confundir com `FalkorDBClient` da framework). Sua migração para `GraphPool` é trabalho do `fmh_app` vertical, não do core framework. Já está desacoplado da framework.

---

## 5. Decisões relacionadas

- **AGENTS.md §1**: a regra "1 lib externa = 1 adapter Protocol" agora está **completamente** materializada para FalkorDB. Após Iter 24, o único adapter é `GraphPool` (production) + `LiteFalkorDBClient` (dev-only, encapsula `falkordblite`).
- **AGENTS.md §2**: "zero shims de compatibilidade" — aplicada. 0 shims introduzidos. Migração atômica em 1 commit.
- **AGENTS.md §3**: god methods decompostos preservados. `FalkorDBProjector._project_agent` (CC=9 → 6 da Iter 8) e `SolutionProjector._dispatch_query` (removido na Iter 13) permanecem estáveis.
- **AGENTS.md §4**: async-only — `GraphAdapter` é `async def query(...)`. Não há mais dispatch `inspect.iscoroutinefunction` em runtime.
- **AGENTS.md §6**: erros concretos — `GraphError` (com discriminador `kind`) já existia. Não foi tocado nesta migração.

---

## 6. Roadmap residual (pós-ADR-024)

| # | Escopo | Status |
|---|---|---|
| 25 | `LiteFalkorDBClient` → `LiteGraphClient` (dev-only) | ✅ Iter 28 FU 2 (ver [ADR-029](./ADR-029-LiteFalkorDBClient-to-LiteGraphClient.md); renomeado `LiteGraphPool` em Iter 28 FU 7) |
| 26 | Resolver circular import + `knowledge_consolidator` → `RedisLike` (Iter 25 do ADR-019) | ✅ Iter 25 (ver [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md)) |
| 28 FU 4 | `GraphLike` Protocol deletion | ✅ Iter 28 FU 4 (ver [ADR-031](./ADR-031-GraphLike-Deletion.md)) |
| 28 FU 5 | Cycle `tools.cache` → `event_log` → `infra` resolvido | ✅ Iter 28 FU 5 (commit `51a2f48`) |
| 28 FU 6 | `LLMClient` facade deletion | ✅ Iter 28 FU 6 (ver [ADR-032](./ADR-032-LLMClient-Deletion.md)) |
| 28 FU 7 | `GraphClient` → `GraphPool` (reorg para `infra/graph/`) | ✅ Iter 28 FU 7 (ver [ADR-033](./ADR-033-GraphPool-Reorg.md)) |
| - | Migração `fmh_app.knowledge.client.FalkorGraphPool` → `GraphPool` (vertical `fmh_app`) | ✅ Iter 28 FU 7 (renomeação em `fmh_app/knowledge/client.py`; `FalkorGraphPool` é um type alias local) |

> **Conclusão**: o roadmap residual do ADR-019 está completamente fechado para o core framework. Iter 24 foi o último item de cobertura de biblioteca externa (FalkorDB). Iter 25+ são refinamentos internos (Lite adapter, consolidation path) que não envolvem cobertura de lib externa nova.

---

## 7. Lições aprendidas

### O que funcionou

1. **Padronização do sub-adapter pattern**: como Iter 10-13 já tinha criado `GraphAgentAdapter`, `GraphDocumentAdapter`, `GraphToolCallAdapter`, `GraphSolutionAdapter` consumindo `GraphAdapter` Protocol, a migração de `FalkorDBClient` → `GraphPool` foi puramente mecânica nos projectors. Os sub-adapters não precisaram de mudança alguma.
2. **Mesma assinatura de construtor**: `GraphPool(host, port, *, password=None)` é byte-identical a `FalkorDBClient`. Não houve friction de update em tests de integração — só `FalkorDBClient` → `GraphPool` no nome.
3. **Migração atômica funcionou**: 23 arquivos modificados + 2 deletados em 1 commit. Sem shims, sem partial states. Como o GraphAdapter Protocol já era estável desde Iter 10, a única mudança observável era o nome do cliente.

### O que poderia ter sido melhor

1. **ADR-019 epílogo poderia ter registrado uma data alvo para Iter 24**: como o roadmap §4 listava Iter 22-25 sem prazo, a migração ficou em "technical debt" até este momento. Lição: ADRs com roadmap devem incluir SLAs ou "triggers" (e.g. "Iter 24 será executado antes de qualquer nova feature que toque o projection system").
2. **`fmh_office/tests/integration/test_knowledge.py` tem dual-connection (raw `FalkorDB` + `GraphPool`)**: o raw `FalkorDB` é usado para reads (assertions), o `GraphPool` para writes (projector). Esta assimetria é confusa. Poderia ter sido normalizada para `GraphPool.graph(tenant).query(...)` em ambas as direções, mas isso requer mais cuidado com tipos (`GraphQueryResult.result_set` vs `result_set` raw). Não bloqueia — diferido.
3. **`SolutionProjector` ainda tem um docstring stale** (linha 110-118) que diz "Python `falkordb` package is sync" e "We do not wrap each query in `asyncio.to_thread`". Com `GraphAdapter` async, o docstring está parcialmente correto mas merece um refresh (mencionar `GraphAdapter.query(cypher, params)` em vez de "the FalkorDB driver"). Trabalho cosmético.

---

## 8. Referências

- [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md) — Iter 10-13 (GraphClient, sub-adapters; renomeado para GraphPool em Iter 28 FU 7)
- [ADR-033](./ADR-033-GraphPool-Reorg.md) — Iter 28 FU 7 (renomeação `GraphClient` → `GraphPool`; movido de `knowledge.graph/` para `infra.graph/`)
- [AGENTS.md §1](../../AGENTS.md) — adapter types
- [AGENTS.md §2](../../AGENTS.md) — zero shims
- `fmh_backend/src/fmh_backend/infra/graph/` — `GraphPool`, `FalkorDBGraphAdapter`, `LiteGraphPool` (pós-Iter 28 FU 7)
- `fmh_backend/src/fmh_backend/knowledge/graph/` — `GraphAdapter` (Protocol), `GraphError`, `GraphQueryResult`, sub-adapters
- `fmh_backend/src/fmh_backend/knowledge/falkordb/adapter.py` — `FalkorDBProjector` (Iter 24: aceita `GraphPool`)
- `fmh_agents/src/fmh_agents/knowledge/solution_projector.py` — `SolutionProjector` (Iter 24: aceita `GraphPool`)
- `fmh_office/src/fmh_office/learning/projector.py` — `ProcessLearnerProjector` (Iter 24: aceita `GraphPool`)
- `fmh_backend/src/fmh_backend/knowledge/graphrag/retriever.py` — `GraphRAGRetriever` (Iter 24: aceita `GraphPool`)

---

**Conclusão**: o framework FMH agora tem **um** cliente canônico para FalkorDB (`GraphPool`), que retorna um `GraphAdapter` (Protocol async). A migração foi puramente mecânica graças ao sub-adapter pattern estabelecido em Iter 10-13. Roadmap residual (Iter 25+) trata de refinamentos internos (Lite adapter, consolidation), não de cobertura de biblioteca externa nova.