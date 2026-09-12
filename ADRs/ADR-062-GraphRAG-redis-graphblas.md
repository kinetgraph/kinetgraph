<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-062: GraphRAG em Redis com Redis Streams, PyGraphBLAS e WorkflowSagaConcordo (Descomissionamento do FalkorDB)

- **Status:** Proposed (Accepted for Implementation)
- **Data:** 2026-09-11
- **Autor:** Time de Arquitetura KNTGraph
- **Substitui e Descomissiona:** Uso de FalkorDB como projeção de grafos (ADR-004 §2.4, ADR-010 §5, ADR-024) e corrotina ad-hoc `pump_once`
- **Relacionado a:**
  - [ADR-004](./ADR-004-Memory-Tools-Knowledge.md) — Tiers de Memória e Grafo de Documentos
  - [ADR-010](./ADR-010-Memory-Business-Tier.md) — Sub-grafo de Soluções
  - [ADR-049](./ADR-049-Zero-Token-Architecture.md) — Reuso de Soluções sem consumo de LLM
  - [ADR-069](./ADR-069-Agent-Concordo-Macro-Behaviors.md) — Agent Concordo: BusinessFSM e WorkflowSaga
  - [ADR-070](./ADR-070-Entity-Relation-Extraction-Concordo-Pipeline.md) — Pipeline de Extração de Entidades e Relações via Concordo

---

## 1. Contexto e Decisão de Descomissionamento do FalkorDB

No `kntgraph`, o Grafo de Conhecimento e Memória de Agente (sub-grafos de *Documents* e *Solutions*) é atualmente projetado no **FalkorDB** (porta 16379, consultas Cypher).

Decidimos **DESCOMISSIONAR COMPLETAMENTE O FALKORDB** do ecossistema KNTGraph.

### Motivos do Descomissionamento:
1. **Redução de Complexidade de Infraestrutura**: Elimina a necessidade de manter o contêiner `falkordb/falkordb` na porta 16379 em ambientes de desenvolvimento, testes e produção. O **Redis** passa a ser o único banco de dados de infraestrutura necessário.
2. **Eliminação do Overhead de Parsing Cypher**: Consultas operacionais em tempo real (latência $< 2\text{ms}$) são executadas nativamente no Redis Vector Search (HNSW) sem necessidade de compilação ou transporte de queries Cypher em string.
3. **Padronização pelo ADR-069**: A esteira de memória deixa de ser um script solto e passa a ser orquestrada formalmente pela `KnowledgeConsolidationSaga` (`WorkflowSagaConcordo`).

---

## 2. Nova Arquitetura: Redis + PyGraphBLAS + WorkflowSagaConcordo + Tools Especializadas

A nova arquitetura alinha a construção e consolidação do Grafo de Conhecimento aos princípios dos **ADR-066 (Single Tool Path)** e **ADR-069 (Agent Concordo Macro Behaviors)**.

Em vez de utilizar workers de background imperativos e não-governados para mutar o banco de dados, o ciclo de vida da memória é orquestrado por um **WorkflowSagaConcordo** (`KnowledgeConsolidationSaga`) que emite requisições para **Tools Especializadas (`@tool_worker`)**:

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │                     WorkflowSagaConcordo (ADR-069)                       │
 │              "saga:knowledge_consolidation_redis_graphblas"              │
 └──────────────────────────────────────────────────────────────────────────┘
                                      │
  Step 1: ExtractCandidate ───────────┼──► Tool: 'entity_extraction_tool'
                                      │    (Ou recepção do evento 'knowledge.entities_extracted')
                                      │
  Step 2: PiiCheck ───────────────────┼──► Tool: 'pii_redaction_tool' (fail-closed por LGPD)
                                      │    (Specification: StepCompleted("ExtractCandidate"))
                                      │
  Step 3: IndexInRedis ───────────────┼──► Tools: 'redis_vector_indexer_tool' + 'redis_graph_adjacency_tool'
                                      │    (Specification: StepCompleted("PiiCheck"))
                                      │
  Step 4: ComputeGraphBLASMetrics ────┼──► Tool: 'graphblas_analytics_tool' (SuiteSparse / SciPy)
                                           (Specification: StepCompleted("IndexInRedis"))
```

### Vantagens do Padrão Concordo + Tools Especializadas:
1. **Governança Unificada (ADR-066)**: Toda mutação no Redis e no Grafo passa pelo protocolo de `@tool_worker`, garantindo telemetria, interceptadores Zero-Token e idempotência.
2. **Auditabilidade & Replayability via EventLog**: Cada passo produz um evento `tool.<name>.completed`. Todo o Grafo de Conhecimento pode ser reconstruído deterministicamente a qualquer momento reprocessando o log.
3. **Resiliência Nativa com Resoluções de Conflito**: Falhas na execução de uma tool de indexação acionam regras de retry e compensação declaradas nas `Specifications` da Saga.

---

## 3. Desacoplamento do Produtor de Extração (Pipeline Concordo ➔ KnowledgeSaga)

A etapa de extração de entidades e relações **NÃO é acoplada rigidamente** a uma única implementação dentro da Saga. Seguindo o modelo Event-Driven do KNT:

1. **Produtor de Extração Intercambiável**: Qualquer pipeline de extração — seja o `SLMEntityExtractor` nativo, um extrator heurístico, um modelo LLM ou um **Concordo dedicado à extração** (ex: `EntityExtractionPipelineConcordo`) — pode processar os dados brutos e emitir o evento padronizado:
   `knowledge.entities_extracted` contendo `entities=[...]` e `relations=[...]`.
2. **Consumo Desacoplado na Saga**: O `KnowledgeConsolidationSaga` consome a saída desse evento no `World.fold`. Se o mecanismo de extração for alterado (por exemplo, migrando de Regex para um modelo de PNL treinado sob medida em um Concordo separado), **nenhuma linha da Saga de consolidação precisa ser alterada**.

---

## 4. Padrões de Integração na Arquitetura KNT (System vs Tool vs Concordo)

No `kntgraph`, o consumo do GraphRAG combina três padrões de integração complementares no modelo **ECS (Entity-Component-System) e Event Sourcing**:

```
┌─────────────────────────────────────────────────────────────────────────┐
│              PADRÕES ARQUITETURAIS DE CONSUMO DO GRAPHRAG               │
└─────────────────────────────────────────────────────────────────────────┘
                                     │
    ┌────────────────────────────────┼────────────────────────────────┐
    ▼                                ▼                                ▼
[Padrão A: WorldSystem]      [Padrão B: Tool Worker]    [Padrão C: Concordo-to-Concordo]
Interceptador Reativo        Ferramenta de Busca        Composição via EventLog
(SolutionLookupSystem)       (KnowledgeTool)            (KnowledgeConsolidationSaga)
```

### Padrão A: Consumo Interno via `WorldSystem` (`SolutionLookupSystem` — ADR-049)
- **Como funciona**: O `SolutionLookupSystem` é um `WorldSystem` reativo do framework.
- **Fluxo**: Quando uma intenção/requisição de ação (`ToolCallRequest`) chega no `AgentView`, o `SolutionLookupSystem` lê a requisição, consulta o `RedisSolutionStore` (GraphRAG) e, se houver uma solução salva com alta confiança, **emite um evento sintético `tool.<name>.completed` diretamente no `World`**.
- **Natureza**: Um **System do framework** consumindo a memória para ignorar a chamada para a LLM e o `@tool_worker` (Zero-Token Architecture).

### Padrão B: Consumo Explícito via Tool (`KnowledgeTool` / `@tool_worker`)
- **Como funciona**: O GraphRAG é envelopado e exposto como uma ferramenta do protocolo `@tool_worker` (`knowledge_search`).
- **Fluxo**: Quando um agente ou LLM decide explicitamente consultar o conhecimento corporativo, emite um evento `tool.knowledge_search.requested`. O `@tool_worker` executa o `RedisGraphRAGRetriever.retrieve()` e retorna os documentos no evento `tool.knowledge_search.completed`.
- **Natureza**: Uma **Tool consumida pelos Agentes/LLM**.

### Padrão C: Composição Reativa Concordo-para-Concordo via EventLog (ADR-069)
- **Como funciona**: Um Concordo **nunca chama outro Concordo de forma síncrona/direta**. A comunicação é estritamente desacoplada via **Event Sourcing no `EventLog`**.
- **Fluxo**:
  1. O Concordo de Negócio `BusinessFSMConcordo` (ex: `fsm:Invoice`) conclui uma transição e emite o evento `invoice.validated`.
  2. O Concordo de Conhecimento `KnowledgeConsolidationSaga` (`saga:knowledge_consolidation`) escuta o evento no `World.fold`, aciona a esteira de 4 passos (extração ➔ PII check ➔ indexação Redis ➔ métricas PyGraphBLAS).
- **Natureza**: **Orquestração Event-Driven desacoplada entre Concordos**.

---

## 5. Taxonomia dos 3 Padrões Fundamentais de Busca do GraphRAG

O GraphRAG no `kntgraph` consolida exatamente os **3 padrões fundamentais de busca** exigidos por arquiteturas avançadas de memória de agentes:

```
┌─────────────────────────────────────────────────────────────────────────┐
│               TAXONOMIA DOS 3 PADRÕES DE BUSCA DO GRAPHRAG              │
└─────────────────────────────────────────────────────────────────────────┘
                                     │
    ┌────────────────────────────────┼────────────────────────────────┐
    ▼                                ▼                                ▼
[1. Busca por Similaridade]    [2. Mínimo Local por Entidade]   [3. Mínimo/Síntese Global]
Vetorial Pura / HNSW Index     Sub-grafo Ancorado em Entidade   Análise da Matriz Global
Redis RediSearch (< 2ms)       Navegação 1..3 saltos (Sets)     Particionamento C (PyGraphBLAS)
```

1. **Busca por Similaridade (Vetorial Pura / KNN)**:
   - **Mecanismo**: Redis Vector Search (`FT.SEARCH` com HNSW/FLAT index em $< 2\text{ms}$).
   - **O que faz**: Dado um vetor de consulta $q$, encontra os $k$ nós mais próximos por similaridade de cosseno ($score < \epsilon$).
   - **Casos de Uso**: Reuso de Ações Zero-Token (`find_solutions_by_problem`) ou busca por documentos parecidos.

2. **Mínimo Local por Entidade (Sub-grafo Ancorado em Entidades/Sementes)**:
   - **Mecanismo**: Redis Sets/Hashes + PyGraphBLAS Spreading Activation ancorado ($v_{seed} \times A$).
   - **O que faz**: Dado um nó inicial conhecido (ex: Cliente $cust\_101$, Nota Fiscal $NF\_001$ ou uma Entidade semente), expande e navega localmente as arestas de relacionamentos em 1 a 3 saltos.
   - **Casos de Uso**: Profiling de Cliente por rede de relacionamentos, verificação de dependências locais de ferramentas, busca contextual a partir de uma entidade conhecida.

3. **Mínimo Global com Relacionamentos (Síntese Holística sobre o Grafo Inteiro)**:
   - **Mecanismo**: Worker PyGraphBLAS (SuiteSparse em C) executando análise matricial global (PageRank, Particionamento de Comunidades Hierárquicas / Louvain / Leiden).
   - **O que faz**: Analisa a matriz de adjacência global $A \in \mathbb{R}^{N \times N}$ para identificar clusters de conhecimento, comunidades de entidades e centralidades macros em todo o repositório.
   - **Casos de Uso**: Perguntas holísticas sobre a organização ("Quais são os principais erros operacionais do trimestre?"), resumos macro-estratégicos para agentes executivos.

### 5.1 Consultas Híbridas: Combinando Vetor e Relações de Grafo (Hybrid Retrieval)

É 100% possível e incentivado **combinar Vetor e Relações de Grafo na mesma consulta**. A arquitetura expõe 3 modos de fusão híbrida:

#### Estratégia A: Vetor Semente ➔ Expansão por Grafo (Vector-Seeded Spreading Activation)
1. **Passo 1 (Vetor no Redis)**: O Redis executa a busca vetorial KNN ($< 2\text{ms}$) resgatando as entidades semente mais próximas semânticamente.
2. **Passo 2 (Grafo no PyGraphBLAS)**: O PyGraphBLAS usa as entidades semente como vetor inicial de ativação $v_{init}$ e multiplica pela matriz de adjacência $A$ ($v = v_{init} \times A$).
3. **Resultado**: Retorna nós conectados estruturalmente que a busca vetorial pura não encontrou, ordenados por score híbrido.

#### Estratégia B: Filtro Estrutural de Grafo ➔ Distância Vetorial Restrita
1. **Passo 1 (Filtro de Grafo no Redis)**: Restringe o espaço de busca apenas aos nós que possuem relacionamentos específicos (ex: `MATCH (Customer)-[:PURCHASED]->(Product)` ou `@tags:{SP}`).
2. **Passo 2 (Vetor Restrito)**: Calcula a distância vetorial de cosseno apenas dentro do sub-grafo filtrado.

#### Estratégia C: Fusão de Scores por RRF (Reciprocal Rank Fusion)
Calcula o score final combinando o ranking vetorial do Redis ($R_{vec}$) e o ranking de centralidade de grafo do PyGraphBLAS ($R_{graph}$):
$$\text{Score}_{\text{RRF}}(d) = \frac{1}{k + R_{\text{vec}}(d)} + \frac{1}{k + R_{\text{graph}}(d)}$$

---

## 6. Como o GraphRAG é Usado no KNTGraph (Modos de Consumo pelos Agentes)

---

### Modo 1: Reuso Operacional de Soluções (Zero-Token Architecture — ADR-049)

- **Cenário**: O agente recebe uma tarefa operacional repetitiva (ex: "Emitir nota fiscal para o cliente X na UF Y" ou "Validar cadastro CNPJ Z").
- **Como funciona**:
  1. Antes de chamar a LLM para raciocinar, a ferramenta `SolutionLookupSystem` consulta o GraphRAG.
  2. Executa `retriever.find_solutions_by_problem(embedding_do_problema, tags={"uf": "SP"})`.
  3. O Redis busca em $< 2\text{ms}$ no índice vetorial o problema mais similar que foi resolvido com sucesso no passado.
  4. Se encontrar um resultado com alta confiança, **o agente executa direto a ação com os parâmetros em cache**, ignorando a chamada de LLM (Zero Token Reuse).

---

### Modo 2: Busca Híbrida de Conhecimento RAG (Vetor + Grafo de Entidades)

- **Cenário**: O agente precisa responder uma pergunta complexa ou tomar uma decisão com base no histórico de documentos e interações (ex: "Qual foi a justificativa para a rejeição de crédito do cliente Silva no mês passado?").
- **Como funciona**:
  1. O agente chama `retriever.retrieve(query="Rejeição crédito cliente Silva", k=5)`.
  2. **Etapa 1 (Vetor)**: Busca no Redis os documentos com embeddings de texto mais similares.
  3. **Etapa 2 (Grafo PyGraphBLAS)**: O PyGraphBLAS roda o algoritmo de **Spreading Activation (PageRank Personalizado)** a partir do vetor de entidades ativadas. Ele navega pelas arestas `(:Document)-[:MENTIONS]->(:Entity)` no grafo.
  4. **Resultado**: O agente recebe não apenas os documentos que contêm as palavras exatas, mas também documentos correlacionados pelo grafo de entidades (ex: o relatório de análise patrimonial do sócio do cliente Silva), que não continham as palavras da busca mas estão conectados no grafo.

---

### Modo 3: Consultas Macroscópicas sobre Resumos Globais de Grafos (Hierarchical Community Summaries)

- **Cenário**: O agente precisa responder uma pergunta ampla e holística sobre todo o banco de conhecimento (ex: "Quais foram os principais gargalos e erros operacionais ocorridos no setor fiscal neste trimestre?").
- **Como funciona**:
  1. Uma busca vetorial comum falharia porque nenhum documento sozinho responde a uma pergunta macro desse tipo.
  2. **Worker PyGraphBLAS**: O motor matricial particiona o grafo de entidades em **comunidades/clusters de conhecimento** (utilizando algoritmos de detecção de comunidades em C no GraphBLAS) e gera resumos sintéticos para cada comunidade.
  3. **Consulta do Agente**: O agente lê os resumos de alto nível das comunidades no Redis (`knt:community:<level>:<id>`), obtendo uma visão panorâmica e estruturada de todo o conhecimento corporativo.

---

## 7. Comparação Detalhada: Versão FalkorDB vs. Redis + PyGraphBLAS + Saga

| Característica / Dimensão | Versão Legada (FalkorDB) | Nova Arquitetura (Redis + PyGraphBLAS + Saga ADR-069) |
|---|---|---|
| **Mecanismo de Grafo** | Engine Cypher externo (FalkorDB / RedisGraph C-module) | **Nativo Redis (KV/Sets/HNSW) + PyGraphBLAS (SuiteSparse em C)** |
| **Infraestrutura** | 2 serviços (Redis para Streams/State + FalkorDB na porta 16379) | **1 único serviço de infraestrutura (Redis)** |
| **Orquestração da Pipeline** | Script/corrotina ad-hoc `pump_once()` fora dos padrões | **`KnowledgeConsolidationSaga` declarativa (`Concordo` ADR-069)** |
| **Governança de PII (LGPD)** | Checagem imperativa solta no loop | **Fail-closed via `Specification` (`StepCompleted("ExtractCandidate")`)** |
| **Latência de Leitura Online** | ~10-30ms (Parsing de String Cypher e execução via socket) | **< 2ms (Leitura direta Hash `HGETALL` + RediSearch HNSW Index)** |
| **Protocolo de Ingestão** | Queries `MERGE` em Cypher montadas em string | **Redis Streams (`XADD`/`XREADGROUP`) com entrega determinística** |
| **Análise de Grafos Globais** | Limitada a procedural calls Cypher | **Matrizes Esparsas C (PageRank, Spreading Activation $v \times A$)** |
| **Ambiente de Testes / CI** | Requer subida de contêiner `falkordb/falkordb` ou mocks complexos | **Roda diretamente com `redis` ou `fakeredis` (100% em memória)** |

---

## 8. Avaliação Crítica: O PyGraphBLAS é a Melhor Escolha?

### 8.1 Quando o PyGraphBLAS é o Melhor (Imbatível)
- **Álgebra Linear de Grafos Esparsos ($v \times A$)**: Para computar **Personalized PageRank (PPR)** e **Spreading Activation** em milissegundos sobre grandes grafos.
- **Semirrings Customizados**: Capacidade única de trocar os operadores de soma e produto (ex: `FP64.PLUS_TIMES` para propagação, `FP64.MIN_PLUS` para caminhos mínimos).
- **Execução C em Background**: Processa matrizes na biblioteca SuiteSparse em C sem travar o Event Loop do Python e sem fazer tráfego de rede no Redis.

### 8.2 Onde o PyGraphBLAS NÃO Atua (E o Redis assume)
- **Busca Vetorial HNSW/KNN**: O PyGraphBLAS não é um banco vetorial. A busca KNN de embeddings é executada pelo **Redis Vector Search (RediSearch / HNSW)** em latência $< 2\text{ms}$.
- **Armazenamento de Atributos/Texto**: Os textos e atributos JSON residem nos **Hashes do Redis**.

### 8.3 Arquitetura de Worker Pluggable (PyGraphBLAS + Fallback SciPy/iGraph)
Para evitar que a dependência da biblioteca C `libgraphblas` trave ambientes simples de desenvolvimento ou CI:
- **Default / Modo Leve**: O worker de métricas possui um fallback em **`scipy.sparse` / `python-igraph`** (instaláveis via `pip`).
- **Modo Alta Performance**: Quando a extra `kntgraph[graphblas]` estiver instalada, o worker ativa o **PyGraphBLAS (SuiteSparse)** para máxima aceleração C.

---

## 9. Detalhamento Técnico e Evolução da Estrutura de Pacotes

### 9.1 Evolução do Pacote `kntgraph.knowledge.graphrag`
Para suportar nativamente as consultas híbridas, profiling de entidades e desacoplamento do FalkorDB, a estrutura de pacotes evolui conforme a especificação abaixo:

```
src/kntgraph/knowledge/
├── graphrag/
│   ├── __init__.py          # Exporta GraphRAGRetriever, HybridResult, ProfileResult
│   ├── retriever.py         # Orquestrador de busca híbrida (Vetor Redis + Grafo PyGraphBLAS)
│   └── _hybrid_fuser.py     # Algoritmo de fusão RRF (Reciprocal Rank Fusion)
├── redis/
│   ├── __init__.py
│   ├── store.py             # RedisSolutionStore / RedisDocumentStore (Hashes + RediSearch)
│   └── projector.py         # Projeção reativa de eventos no Redis
├── tools/
│   ├── __init__.py
│   ├── pii_redactor.py      # @tool_worker: 'pii_redaction_tool' (LGPD Redaction)
│   ├── vector_indexer.py    # @tool_worker: 'redis_vector_indexer_tool' (RediSearch Embeddings)
│   ├── adjacency_mutator.py # @tool_worker: 'redis_graph_adjacency_tool' (Redis Sets/Hashes)
│   └── analytics_runner.py  # @tool_worker: 'graphblas_analytics_tool' (PyGraphBLAS/SciPy)
└── graphblas/
    ├── __init__.py
    ├── analytics.py         # Motor de matrizes esparsas em C (SuiteSparse / SciPy fallback)
    └── algorithms.py        # Spreading Activation, PPR, Louvain Community Detection
```

---

## 10. Roteiro Detalhado para Descomissionamento do FalkorDB KG

Para garantir um desmame seguro e sem regressões na memória do agente, o descomissionamento do **FalkorDB KG** é estruturado em **5 Etapas Sequenciais**:

### Etapa 1: Emissão de Deprecation Warnings & Feature Flag de Transição
- Adicionar avisos de deprecação (`DeprecationWarning`) nas chamadas a `src/kntgraph/knowledge/falkordb/` e `src/kntgraph/infra/graph/_adapter.py`.
- Introduzir a variável de configuração `KNT_KG_BACKEND` (valores: `"redis"` por padrão, `"falkordb"` para retrocompatibilidade temporária).
- Garantir que a nova arquitetura Redis + PyGraphBLAS seja ativada por padrão nos ambientes de dev e staging.

### Etapa 2: Repopulação e Validação de Dados via Replay de Eventos
- Como a infraestrutura do KNT é orientada a **Event Sourcing**, não é necessário exportar/importar dados brutos via Cypher.
- Executar a `KnowledgeConsolidationSaga` em modo de Replay consumindo o `EventLog` do Redis (`knowledge.entities_extracted`, `solution.promoted`, `document.ingested`).
- Validar a paridade dos dados indexados nos **Redis Hashes + RediSearch HNSW Index** comparando os resultados com as respostas do FalkorDB.

### Etapa 3: Cutover (Virada de Chave na Leitura Operacional)
- Redirecionar os componentes de leitura — `GraphRAGRetriever` (Busca RAG Híbrida) e `SolutionLookupSystem` (Zero-Token Architecture — ADR-049) — para consumir exclusivamente o backend Redis + PyGraphBLAS.
- Verificar métricas de latência para assegurar a queda no tempo de consulta de ~15-30ms (Cypher string parsing over TCP) para **< 2ms** (Redis HNSW native read).

### Etapa 4: Remoção Física do Código do Codebase
Remover definitivamente do repositório os módulos e classes legadas:
1. **Pacote de Projeção Legada**: Excluir o diretório [`src/kntgraph/knowledge/falkordb/`](file:///home/adriano/Projects/kinetgraph/kinetgraph/src/kntgraph/knowledge/falkordb/).
2. **Adaptador de Grafo e Pools Legados**: Excluir [`src/kntgraph/infra/graph/_adapter.py`](file:///home/adriano/Projects/kinetgraph/kinetgraph/src/kntgraph/infra/graph/_adapter.py), `_pool.py` e `_lite_pool.py`.
3. **Configuração de Infraestrutura**: Remover a classe `FalkorDBConfig` de `src/kntgraph/infra/config/_falkordb.py`.
4. **Substituição de Testes**: Migrar `tests/integration/knowledge/test_falkordb_projection.py` e unitários associados para suítes baseadas em `fakeredis` / `redis`.

### Etapa 5: Limpeza de Infraestrutura, CI/CD e Dependências
1. **`pyproject.toml`**: Remover a dependência `falkordb` e a extra `kntgraph[falkordb]`.
2. **Ambiente de Testes / CI**: Remover o contêiner `falkordb/falkordb:latest` na porta 16379 dos arquivos `docker-compose.yml`, scripts de CI (`scripts/ci.py`) e do documento de ambiente `.agents/skills/kntgraph-environment/SKILL.md`.
3. **Documentação**: Atualizar a arquitetura dos ADRs legados (ADR-004 §2.4, ADR-010 §5, ADR-024) registrando que foram superados e substituídos pelo **ADR-062**.

---

## 11. Plano de Migração e Execução

1. **Fase 1 — Saga Concordo**: Criar a `KnowledgeConsolidationSaga` em `src/kntgraph/concordos/saga/knowledge.py` implementando o contrato `WorkflowSagaConcordo` (ADR-069).
2. **Fase 2 — Tools Especializadas de KG (`@tool_worker`)**: Implementar as tools em `src/kntgraph/knowledge/tools/` (`pii_redaction_tool`, `redis_vector_indexer_tool`, `redis_graph_adjacency_tool`, `graphblas_analytics_tool`).
3. **Fase 3 — Busca Híbrida & Profiling**: Refatorar `src/kntgraph/knowledge/graphrag/retriever.py` introduzindo os métodos `retrieve_hybrid` e `get_entity_profile` conectando diretamente ao Redis.
4. **Fase 4 — Motor PyGraphBLAS**: Criar as rotinas de cálculo matricial em `src/kntgraph/knowledge/graphblas/analytics.py` (com suporte a SuiteSparse C e fallback para `scipy.sparse`).
5. **Fase 5 — Descomissionamento do FalkorDB**: Executar a limpeza completa do código legado, dependências e contêineres CI conforme o Roteiro da Seção 10.
