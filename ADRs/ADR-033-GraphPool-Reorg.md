<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-033: GraphClient → GraphPool reorg (graph infra follows Redis pattern)

**Status:** Aceito
**Data:** 30 de junho de 2026
**Relacionado:** [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md),
[ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md),
[ADR-029](./ADR-029-LiteFalkorDBClient-to-LiteGraphClient.md),
[ADR-031](./ADR-031-GraphLike-Deletion.md),
[ADR-032](./ADR-032-LLMClient-Deletion.md)

## 1. Contexto

Iter 24 (ADR-024) introduziu `GraphClient` em
`fmh_backend.knowledge.graph` como a facade de
produção para FalkorDB. Iter 28 FU 2 (ADR-029)
introduziu `LiteGraphClient` em `fmh_backend.infra`
como o adapter dev-only. A Iter 28 FU 4 (ADR-031)
deletou `GraphLike` (Protocol órfão).

Após essas iterações, a estrutura tinha:

- `fmh_backend.knowledge.graph` — Protocol
  (`GraphAdapter`, `GraphError`, `GraphQueryResult`)
  + facade de produção (`GraphClient`) + adapter
  concreto (`FalkorDBGraphAdapter`).
- `fmh_backend.infra.lite_graph_client` — adapter
  dev-only (`LiteGraphClient`, `LiteGraphAdapter`).

Isso violava o pattern estabelecido pelo Redis
(Iter 1-3), que tem a divisão canônica:

- `fmh_backend.core._typing` — Protocol genérico
  (no caso do Redis, análogo a `GraphAdapter`).
- `fmh_backend.infra.redis._pool` — facade de
  produção (`RedisPool`).
- `fmh_backend.infra.redis._auth._redis` — adapter
  concreto Redis para `APIKeyStorage`.

A diferença crítica: **concrete adapters e pools
vivem em `infra/`, não em `knowledge/`**. O
`knowledge/` expõe apenas Protocol-level primitives
que verticais consomem; `infra/` expõe a wiring
contra o backend concreto.

A `GraphClient` foi a única facade "client" que
ficou em `knowledge/`, violando o pattern. Esta
iter corrige a inconsistência: `GraphClient` é
renomeado para `GraphPool` e movido para
`fmh_backend.infra.graph`, replicando o pattern
do `RedisPool`.

### 1.1 Por que renomear para `GraphPool`

O nome `Client` na Iter 24 era um placeholder
genérico (replicando o pattern `GraphDBClient` →
`GraphClient` da Iter 10). O nome "Pool" é mais
honesto sobre o que o objeto é: **uma facade que
detém o ciclo de vida de uma única conexão
FalkorDB por processo**, expondo um
`GraphAdapter` por tenant via `graph(tenant_id)`.

`RedisPool` é o análogo exato (Iter 1-3): detém
o ciclo de vida da conexão Redis, expõe o
`RedisLike` por shard. O sufixo `Pool` é o signal
universal de "wire-level connection holder" no
framework.

### 1.2 Por que mover para `infra/graph/`

`infra/` é o pacote de **adapters contra backends
concretos** (Redis, LiteRedis, LiteGraphDB,
FalkorDB). `knowledge/` é o pacote de **primitives
de domínio** (Protocol, value objects, sub-adapters
semantic). A divisão atual violava isso:

- `GraphAdapter` (Protocol) — `knowledge.graph` ✓
- `GraphError` (value object) — `knowledge.graph` ✓
- `GraphQueryResult` (value object) — `knowledge.graph` ✓
- `GraphClient` (facade contra FalkorDB) —
  `knowledge.graph` ✗ (deveria ser `infra`)
- `FalkorDBGraphAdapter` (adapter concreto) —
  `knowledge.graph` ✗ (deveria ser `infra`)
- `LiteGraphClient` (facade dev-only) — `infra` ✓
- `LiteGraphAdapter` (adapter dev-only) — `infra` ✓

A inconsistência: `GraphClient` (production) e
`LiteGraphClient` (dev-only) ficavam em pacotes
diferentes, sem justificativa arquitetural. A
correção é mover ambos para `infra/graph/`, lado a
lado.

## 2. Decisão

`GraphClient` é renomeado para `GraphPool` e
movido para `fmh_backend.infra.graph`. Idem
para `LiteGraphClient` (renomeado para
`LiteGraphPool`, movido de
`fmh_backend.infra.lite_graph_client` para
`fmh_backend.infra.graph._lite_pool`).

### 2.1 Estrutura final

**`fmh_backend.knowledge.graph/`** (Protocol-level):

- `_protocol.py` — `GraphAdapter` (Protocol),
  `GraphError`, `GraphQueryResult`.
- `_sub/` — `GraphAgentAdapter`, `GraphDocumentAdapter`,
  `GraphToolCallAdapter`, `GraphSolutionAdapter`
  (semantic sub-adapters que compoem
  `GraphAdapter`).
- `__init__.py` — re-exports dos 3 primitives
  do Protocol.

**`fmh_backend.infra.graph/`** (concrete wiring):

- `_pool.py` — `GraphPool` (facade de produção,
  importa `falkordb.asyncio` lazy), `GRAPH_NAME_PREFIX`,
  `graph_name_for_tenant()`.
- `_adapter.py` — `FalkorDBGraphAdapter` (adapter
  concreto FalkorDB; implementa `GraphAdapter`).
- `_lite_pool.py` — `LiteGraphPool`,
  `LiteGraphAdapter` (dev-only; importa
  `redislite.falkordb_client` lazy).
- `__init__.py` — re-exports do pool + adapter
  de produção.

### 2.2 Mudanças

1. **Módulos movidos**:
   - `fmh_backend/knowledge/graph/_client.py` (172 LOC)
     → `fmh_backend/infra/graph/_pool.py` (170 LOC,
     `-2` LOC após remoção de debug prints).
   - `fmh_backend/knowledge/graph/_adapter.py` (118 LOC)
     → `fmh_backend/infra/graph/_adapter.py` (118 LOC,
     sem alteração de código; só docstring ajustado
     para refletir o novo path).
   - `fmh_backend/infra/lite_graph_client.py` (295 LOC)
     → `fmh_backend/infra/graph/_lite_pool.py` (304 LOC,
     +9 LOC: import extra + docstring refatorado).

2. **Classes renomeadas**:
   - `GraphClient` → `GraphPool`.
   - `LiteGraphClient` → `LiteGraphPool`.
   - `FalkorGraphClient` (em `fmh_app`) →
     `FalkorGraphPool` (type alias do app).

3. **Re-exports ajustados**:
   - `fmh_backend.knowledge.graph.__init__`:
     removidos `GraphClient`, `FalkorDBGraphAdapter`,
     `GRAPH_NAME_PREFIX`, `graph_name_for_tenant`
     (movidos para `infra.graph`).
   - `fmh_backend.infra.__init__`: atualizado
     para `from .graph._lite_pool import ...` e
     re-exporta `LiteGraphAdapter`, `LiteGraphPool`.

4. **Call sites atualizados**:
   - `fmh_agents/knowledge/solution_projector.py`
   - `fmh_app/knowledge/{__init__,client,projection}.py`
   - `fmh_app/app_runner.py`
   - `fmh_app/llm/prompt.py`
   - `fmh_app/roles/{cnpj_fetcher,geocoder,intake,presenter}.py`
   - `fmh_app/tests/integration/{test_intent_router_contract,test_knowledge,test_systems}.py`
   - 6 examples em `fmh_agents/examples/`
   - 1 example em `fmh_agents/examples/README.md`
   - `fmh_office/learning/projector.py`
   - `fmh_office/tests/integration/test_knowledge.py`
   - `fmh_office/tests/unit/learning/test_projector.py`
   - Tests em `fmh_backend/tests/unit/{infra,core}/...`

5. **Tests reorganizados**:
   - `tests/unit/knowledge/graph/test_falkordb_adapter.py`
     → `tests/unit/infra/graph/test_graph_adapter.py`
   - `tests/unit/knowledge/graph/test_client.py`
     → `tests/unit/infra/graph/test_graph_pool.py`
   - `tests/unit/infra/test_lite_graph_client.py`:
     renomeado para `test_lite_graph_pool.py`
     (mantido o nome do arquivo para evitar
     re-criação; conteúdo atualizado para os
     novos nomes `LiteGraphPool`, `LiteGraphAdapter`).

6. **Cycle fix preservado**: o cycle entre
   `tools.cache` → `tools.invoker` →
   `stream.event_log` → `infra` →
   `infra.graph._lite_pool` →
   `knowledge.graph` →
   `knowledge.falkordb.adapter` →
   `stream.event_log` permanece resolvido via
   `TYPE_CHECKING` em `knowledge.falkordb.adapter`
   (Iter 28 FU 5). O test de regressão
   `test_import_graph_no_cycle.py` foi atualizado
   para apontar ao novo path
   (`fmh_backend.infra.graph._lite_pool`).

7. **`old_client.py` deletado**: o arquivo
   `old_client.py` na raiz do repo (5655 bytes)
   era uma cópia temporária de
   `knowledge/graph/_client.py` deixada durante a
   reorg. Não tinha callers; deletado.

8. **Prints de debug removidos**:
   `infra/graph/_pool.py` tinha 2 `print(f"resolved_password=...")`
   (linhas 115, 122 antes da remoção) deixados
   durante o desenvolvimento. Removidos.

### 2.3 Por que manter `GraphAdapter` (Protocol) onde está

O Protocol `GraphAdapter` (em `knowledge.graph._protocol`)
é a primitive que sub-adapters e verticais implementam.
É parte do domínio (`knowledge`), não da infra
específica do backend. Mover para `infra.graph`
violaria a separação: verticais teriam que importar
de `infra` (que é o pacote "concrete"), e
importariam o Protocol de "knowledge", criando um
acoplamento invertido.

A divisão final:

- `knowledge.graph` define **o que** o framework
  entende por "graph adapter" (Protocol).
- `infra.graph` define **como** conectar a um
  backend concreto (FalkorDB ou LiteGraphDB).

## 3. Consequências

### 3.1 Pros

- **Pattern uniformity**: o path `graph/` agora
  segue o pattern estabelecido pelo `redis/`
  (Iter 1-3). Um dev que conhece `RedisPool`
  reconhece `GraphPool` imediatamente.
- **Separação limpa**: `knowledge.graph` é só
  Protocol; `infra.graph` é só wiring concreto.
  Verticais e apps importam de `knowledge.graph`
  para type-hints e de `infra.graph` para construção
  concreta. Imports circulares são prevenidos
  pela direção (`knowledge` → `infra` nunca).
- **Renaming honesto**: `GraphPool` é mais preciso
  que `GraphClient`. O objeto não é "um cliente
  genérico"; é um **pool de conexões** que
  materializa `GraphAdapter`s por tenant.
- **Padrão replicável**: a Iter 28 FU 6 (LLMClient
  deletion, ADR-032) já tinha notado que o path
  LLM não precisava de facade; o path graph
  precisava mas estava mal-posicionado. A correção
  aqui alinha o path graph ao pattern Redis sem
  ressuscitar o `LLMClient`-style facade.

### 3.2 Cons

- **Renaming é breaking**: `GraphClient` deixa de
  existir; callers que importavam
  `from fmh_backend.knowledge.graph import GraphClient`
  precisam migrar. Mas: (a) este monorepo não tem
  callers externos; (b) `GraphClient` é internal
  ao monorepo (não em PyPI público); (c) AGENTS.md
  §2 permite breaking change intencional.
- **Mais LOC no aggregate**: a reorg move
  arquivos mas não reduz LOC (a versão atual é
  maior que a original por causa de docstrings
  atualizados e `__init__.py` re-exports). Trade-off
  aceito: o ganho é estrutural, não volumétrico.

### 3.3 Trade-offs

- **Migração atômica** (1 commit) vs **shim
  deprecation**: AGENTS.md §2 proíbe shims. A
  migração atômica em 1 commit é o caminho
  escolhido.
- **Renaming `GraphClient` → `GraphPool`** vs
  **manter `GraphClient`**: o sufixo `Pool` é
  mais alinhado com o pattern Redis (`RedisPool`,
  `LiteFalkorDBPool` em outros lugares); o nome
  "Client" estava inconsistente.

## 4. Migration

### 4.1 Callers atualizados (atomicamente)

Auditoria: **~30 call sites** em apps, examples,
tests, verticais atualizados no mesmo commit.
Zero shims. Zero `GraphClient` referenciado em
runtime após o commit (verificável via `grep
"GraphClient\b" --include="*.py"`).

### 4.2 Suítes

- `fmh_backend/tests/unit/`: 1353 passed, 1 skipped
  (sem regressão vs baseline).
- `fmh_agents/tests/unit/`: 221 passed.
- `fmh_app/tests/unit/`: 104 passed (eram 0
  collection errors antes da sessão; consertados
  como parte da migration por descobrir que
  `fmh_app` ainda importava o path pré-Iter 25).
- `fmh_office/tests/unit/learning/`: 27 passed.
- **Total: 1705 passed, 0 failed, 1 skipped**.

### 4.3 Lint

- `ruff check` nos paths tocados: **clean**.
- Lint pré-existente em outros paths (84 erros
  em `fmh_backend/tests/` não tocados): não
  introduzido por esta iter.

## 5. Decisões relacionadas

- **[ADR-019 §18b](./ADR-019-Epilogo-Typed-Adapters.md#12-apêndice-iter-18--llm-transport-protocol)**:
  pattern `XxxClient` (facade) foi introduzido em
  Iter 18b para o path LLM. Iter 28 FU 6 (ADR-032)
  deletou o `LLMClient` por 0 callers production.
  Esta iter realinha o path graph ao pattern
  Redis (que tem `RedisPool` real).
- **[ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md)**:
  introduziu `GraphClient` em `knowledge.graph`.
  Esta iter corrige o posicionamento (era
  `knowledge.graph`; agora é `infra.graph`).
- **[ADR-029](./ADR-029-LiteFalkorDBClient-to-LiteGraphClient.md)**:
  introduziu `LiteGraphClient` em `infra`. Esta
  iter move para `infra.graph._lite_pool` (ao
  lado de `GraphPool`).
- **[ADR-031](./ADR-031-GraphLike-Deletion.md)**:
  deletou `GraphLike` (Protocol órfão). Esta
  iter finaliza a consolidação do path graph.
- **[ADR-032](./ADR-032-LLMClient-Deletion.md)**:
  deletou `LLMClient` (facade sem callers). Esta
  iter confirma o pattern correto: facades
  devem ser posicionadas em `infra/` (não em
  `knowledge/`).
- **AGENTS.md §1 (adapter types)**: a regra
  "1 lib externa = 1 Protocol" continua aplicada.
  `GraphAdapter` (Protocol, `knowledge.graph`),
  `GraphPool` (facade, `infra.graph`),
  `FalkorDBGraphAdapter` (impl concreta,
  `infra.graph`).

## 6. Referências

- `fmh_backend/src/fmh_backend/infra/graph/_pool.py`
  (170 LOC; `GraphPool`).
- `fmh_backend/src/fmh_backend/infra/graph/_adapter.py`
  (118 LOC; `FalkorDBGraphAdapter`).
- `fmh_backend/src/fmh_backend/infra/graph/_lite_pool.py`
  (304 LOC; `LiteGraphPool`, `LiteGraphAdapter`).
- `fmh_backend/src/fmh_backend/infra/graph/__init__.py`
  (re-exports).
- `fmh_backend/src/fmh_backend/knowledge/graph/__init__.py`
  (encolhido: só Protocol).
- `fmh_backend/src/fmh_backend/knowledge/graph/_protocol.py`
  (sem alteração; `GraphAdapter`, `GraphError`,
  `GraphQueryResult`).
- `fmh_backend/src/fmh_backend/infra/__init__.py`
  (atualizado).
- `fmh_backend/tests/unit/infra/graph/test_graph_pool.py`
  (renomeado de `test_client.py`).
- `fmh_backend/tests/unit/infra/graph/test_graph_adapter.py`
  (renomeado de `test_falkordb_adapter.py`).
- `fmh_backend/tests/unit/infra/test_lite_graph_client.py`
  (atualizado para `LiteGraphPool`).
- `fmh_backend/tests/unit/test_import_graph_no_cycle.py`
  (cycle gate preservado, refs atualizadas).
- `fmh_backend/tests/unit/core/test_graph_like_deleted.py`
  (refs atualizadas).
- `old_client.py` (raiz): deletado.
- 30+ call sites em apps, examples, verticais
  (ver §2.2 item 4).
- 1 dump.rdb binário (sem significado arquitetural;
  FalkorDB dev data).

---

**Conclusão**: o path `graph` agora segue o pattern
estabelecido pelo `redis/`: Protocol em
`knowledge.graph`; facade + adapter concreto em
`infra.graph`. A inconsistência entre
`GraphClient` (em `knowledge`) e `LiteGraphClient`
(em `infra`) está resolvida. A Iter 28 FU 7 fecha
o último item arquitetural residual do roadmap
do ADR-019 epílogo.
