<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-070: Pipeline de Extração de Entidades e Relações via WorkflowSagaConcordo e Tools Especializadas

- **Status:** Proposed
- **Data:** 2026-09-11
- **Autor:** Time de Arquitetura KNTGraph
- **Relacionado a:**
  - [ADR-013](./ADR-013-Semantic-Routing-GLiNER2.md) — Roteamento Semântico e Extração GLiNER2
  - [ADR-055](./ADR-055-GLiNER2-Model-Registry-and-Structured-Extraction.md) — GLiNER2 Model Registry
  - [ADR-062](./ADR-062-GraphRAG-redis-graphblas.md) — GraphRAG em Redis com PyGraphBLAS e Saga
  - [ADR-066](./ADR-066-Single-Tool-Path.md) — Single Tool Path (Mutações estritamente via `@tool_worker`)
  - [ADR-069](./ADR-069-Agent-Concordo-Macro-Behaviors.md) — Agent Concordo: BusinessFSM e WorkflowSaga

---

## 1. Contexto e Motivação

No `kntgraph`, a extração de Entidades e Relações a partir de textos não estruturados, transcrições de chat, documentos fiscais e saídas de ferramentas é o ponto de partida crítico para a construção da memória corporativa (GraphRAG / Knowledge Graph).

Historicamente, essa extração ocorria de duas formas:
1. **Chamadas Procedurais Diretas**: Funções imperativas acopladas ao `SLMEntityExtractor` ou chamadas ad-hoc a LLMs.
2. **Falta de Orquestração Declarativa**: Não havia visibilidade de cada etapa intermediária (chunking, reconhecimento de entidades nomeadas, extração de relações e normalização canônica de IDs).

Com a consolidação do **ADR-066 (Single Tool Path)** e do **ADR-069 (Agent Concordo Macro Behaviors)**, todas as cadeias de processamento multietapas e mutações no ecossistema KNT devem ser modeladas como **Concordos declarativos** que orquestram **Tools Especializadas (`@tool_worker`)**.

Este ADR formaliza a arquitetura do **`EntityExtractionPipelineConcordo`**, estabelecendo um pipeline desacoplado, governado, auditável e altamente extensível para a extração de conhecimento.

---

## 2. Decisão Arquitetural: `EntityExtractionPipelineConcordo`

Decidimos padronizar a extração de entidades e relações como um **WorkflowSagaConcordo** (`saga:entity_relation_extraction_pipeline`) estruturado em **4 passos determinísticos**:

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │                     WorkflowSagaConcordo (ADR-069)                       │
 │              "saga:entity_relation_extraction_pipeline"                  │
 └──────────────────────────────────────────────────────────────────────────┘
                                      │
  Step 1: ChunkAndPreprocess ─────────┼──► Tool: 'text_chunker_tool'
                                      │    (Particionamento inteligente de texto mantendo contexto)
                                      │
  Step 2: ExtractEntities ────────────┼──► Tool: 'gliner2_entity_extraction_tool'
                                      │    (Reconhecimento de Entidades Nomeadas com GLiNER2 / SLM)
                                      │    (Specification: StepCompleted("ChunkAndPreprocess"))
                                      │
  Step 3: ExtractRelations ───────────┼──► Tool: 'slm_relation_extraction_tool'
                                      │    (Identificação de arestas e relacionamentos n-ários)
                                      │    (Specification: StepCompleted("ExtractEntities"))
                                      │
  Step 4: NormalizeAndDisambiguate ──┼──► Tool: 'entity_schema_normalizer_tool'
                                      │    (Resolução de Canonical IDs, Deduplicação e Score de Confiança)
                                      │
                                      ▼ Avaliação de Limiar de Confiança (Confidence Threshold)
                     ┌────────────────┴────────────────┐
                     │                                 │
           [Score >= Threshold (0.80)]       [Score < Threshold (0.80)]
                     │                                 │
                     ▼                                 ▼
      EMITE: 'knowledge.entities_extracted'  Step 5: EnqueueHumanReview
      (Ingestão Automática no KG)            └──► Tool: 'human_review_dispatcher_tool'
                                                  (Transição: WAITING_HUMAN_REVIEW)
                                                  (Emite: 'knowledge.extraction.review_requested')
                                                  │
                                                  ▼ Curadoria Humana (Aprovar / Editar / Rejeitar)
                                                  └──► Emite: 'knowledge.extraction.review_completed'
                                                       └──► EMITE: 'knowledge.entities_extracted'
```

---

## 3. As Tools Especializadas da Pipeline (`@tool_worker`)

Alinhado ao **ADR-066**, nenhuma lógica de IA ou algoritmo heurístico de extração roda solto na Saga. Toda execução é encapsulada em ferramentas com o decorador `@tool_worker`:

### 3.1 `text_chunker_tool`
- **Função**: Recebe o payload bruto (`document_text`, `chat_transcript`, `tool_payload`).
- **Mecanismo**: Aplica algoritmos de chunking semântico (respeitando quebras de parágrafos, limites de sentenças e overlap de tokens).
- **Entrada**: `{ text: str, max_chunk_size: int, overlap: int }`.
- **Saída**: `{ chunks: List[TextChunk] }`.

### 3.2 `gliner2_entity_extraction_tool`
- **Função**: Extrai entidades nomeadas de cada chunk utilizando modelos locais leves e rápidos (GLiNER2 / SLM conforme ADR-013/ADR-055) ou fallback LLM.
- **Tipos de Entidades Padrão**: `Customer`, `Company`, `CNPJ`, `Invoice`, `Product`, `Error`, `ToolCall`, `User`, `Location`, `Date`.
- **Entrada**: `{ chunks: List[TextChunk], schema_types: List[str] }`.
- **Saída**: `{ extracted_entities: List[EntitySpan] }`.

### 3.3 `slm_relation_extraction_tool`
- **Função**: Dado o conjunto de entidades identificadas e o texto do chunk, identifica as relações direcionadas entre pares ou tuplas de entidades.
- **Tipos de Relações Padrão**: `:PURCHASED`, `:ISSUED_BY`, `:MENTIONS`, `:DEPENDS_ON`, `:CONNECTED_TO`, `:FAILED_WITH`, `:SHARED_DEVICE`.
- **Entrada**: `{ chunks: List[TextChunk], entities: List[EntitySpan] }`.
- **Saída**: `{ extracted_relations: List[RelationTriple] }`.

### 3.4 `entity_schema_normalizer_tool`
- **Função**: Aplica regras de higienização, desambiguação, cálculo de score de confiança médio e resolução de IDs canônicos.
- **Operações**:
  1. **Resolução Canônica de ID**: Converte variações ("ACME S.A.", "ACME SA", "Acme") para o ID único `company:acme`.
  2. **Cálculo do Confidence Score Aggregate**: Combina a confiança da extração de entidade e da relação em um score composto $S \in [0.0, 1.0]$.
  3. **Roteamento de Confiança**: Se $S \ge 0.80$, gera a carga útil para emissão direta de `knowledge.entities_extracted`. Se $S < 0.80$, sinaliza para a Saga acionar o Human-in-the-Loop.

### 3.5 `human_review_dispatcher_tool`
- **Função**: Enfileira candidatas de baixa confiança na Fila de Curadoria Humana e gerencia o estado `WAITING_HUMAN_REVIEW`.
- **Mecanismo**: Notifica o agente/humano curador emitindo `knowledge.extraction.review_requested`. Aguarda o comando de aprovação/edição/rejeição humana via `knowledge.extraction.review_completed`.

---

## 4. Human-in-the-Loop (HITL) & Prevenção de Ontology Drift

A introdução do HITL para extrações com confiança $< 0.80$ resolve o problema crítico de **Ontology Drift** (degradação e contaminação do Grafo de Conhecimento):

1. **Prevenção de Arestas Alucinadas**: Evita que relações ambíguas geradas por modelos de IA poluam a matriz de adjacência e distorçam algoritmos analíticos como Spreading Activation e PageRank (ADR-062).
2. **Curadoria Assistida**: O operador humano recebe no payload a frase original com as entidades destacadas e opções rápidas:
   - **`APPROVE`**: Confirma as entidades/relações e libera a publicação de `knowledge.entities_extracted`.
   - **`EDIT`**: Corrige os tipos ou canonical IDs antes de aprovar.
   - **`REJECT`**: Descarta a extração e registra o caso no log de auditoria para ajuste de fine-tuning dos modelos locais.
3. **Auditabilidade Total (ADR-069)**: O estado do Concordo permanece pausado em `WAITING_HUMAN_REVIEW` até a interação humana, garantindo que nada de baixa confiança entre silenciosamente no KG.

---

## 5. Desacoplamento Produtor-Consumidor (ADR-070 ➔ ADR-062)

Uma das maiores forças desta arquitetura é o **desacoplamento total via Event Sourcing**:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                 PRODUTOR: EntityExtractionPipelineConcordo                  │
│                                 (ADR-070)                                   │
└─────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼ Emite evento de saída no World
                         'knowledge.entities_extracted'
                                       │
                                       ▼ Escuta evento no World.fold
┌─────────────────────────────────────────────────────────────────────────────┐
│                 CONSUMIDOR: KnowledgeConsolidationSaga                      │
│                                 (ADR-062)                                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

1. **Autonomia do Produtor (ADR-070)**: O `EntityExtractionPipelineConcordo` preocupa-se unicamente em transformar texto bruto em uma estrutura limpa de entidades e relações. Ele **não sabe** se o resultado será salvo em Redis, PyGraphBLAS, Postgres ou exibido na UI.
2. **Autonomia do Consumidor (ADR-062)**: O `KnowledgeConsolidationSaga` escuta o evento `knowledge.entities_extracted` e aciona sua própria saga de 4 passos (PII check ➔ Redis Vector/Adjacency Index ➔ PyGraphBLAS Analytics).

---

## 5. Estratégias Pluggáveis de Extração

A pipeline expõe um registro de **Estratégias de Extração** configuráveis por tenant ou tipo de documento:

| Estratégia | Ferramenta Principal | Indicado para | Vantagens |
|---|---|---|---|
| **`GLINER_SLM` (Default)** | `gliner2_entity_extraction_tool` | Documentos operacionais, chats | Latência < 10ms, custo zero de LLM |
| **`LLM_STRUCTURED`** | LiteLLM com JSON Schema | Documentos jurídicos complexos | Alta capacidade de inferência profunda |
| **`HEURISTIC_REGEX`** | Pattern Matching com Regex | Logs do sistema, `ToolCallRequest` | Latência sub-milissegundo (< 1ms) |

---

## 6. Evolução da Estrutura de Código em `src/kntgraph/`

A nova estrutura do módulo de extração passa a integrar o ecossistema `knowledge`:

```
src/kntgraph/
├── concordos/
│   └── saga/
│       ├── knowledge.py             # KnowledgeConsolidationSaga (ADR-062)
│       └── extraction.py            # EntityExtractionPipelineConcordo (ADR-070)
└── knowledge/
    ├── extraction/
    │   ├── __init__.py
    │   ├── pipeline.py              # Orquestrador da Pipeline de Extração
    │   └── strategies.py            # Estratégias (GLiNER2, SLM, LLM, Regex)
    └── tools/
        ├── text_chunker.py          # @tool_worker: 'text_chunker_tool'
        ├── entity_extractor.py      # @tool_worker: 'gliner2_entity_extraction_tool'
        ├── relation_extractor.py    # @tool_worker: 'slm_relation_extraction_tool'
        ├── schema_normalizer.py     # @tool_worker: 'entity_schema_normalizer_tool'
        └── human_reviewer.py        # @tool_worker: 'human_review_dispatcher_tool' (HITL)
```

---

## 7. Plano de Implementação

1. **Fase 1**: Criar o `EntityExtractionPipelineConcordo` em [`src/kntgraph/concordos/saga/extraction.py`](file:///home/adriano/Projects/kinetgraph/kinetgraph/src/kntgraph/concordos/saga/extraction.py) estendendo `WorkflowSagaConcordo` com o estado `WAITING_HUMAN_REVIEW`.
2. **Fase 2**: Implementar as 5 Tools Especializadas em [`src/kntgraph/knowledge/tools/`](file:///home/adriano/Projects/kinetgraph/kinetgraph/src/kntgraph/knowledge/tools/):
   - `text_chunker_tool`
   - `gliner2_entity_extraction_tool`
   - `slm_relation_extraction_tool`
   - `entity_schema_normalizer_tool`
   - `human_review_dispatcher_tool`
3. **Fase 3**: Integrar a emissão do evento `knowledge.entities_extracted` com a ingestão da `KnowledgeConsolidationSaga` (ADR-062).
4. **Fase 4**: Desenvolver a suíte de testes unitários com mocks de GLiNER2, LiteLLM e curadoria humana.

---

## 8. Conclusão e Status

O **ADR-070** estabelece um padrão moderno, declarativo, desacoplado e governado para a extração de entidades e relações em todo o ecossistema `kntgraph`. Ao combinar o padrão **WorkflowSagaConcordo (ADR-069)** com **Tools Especializadas (ADR-066)**, garantimos total auditabilidade, resiliência e desacoplamento do motor de persistência Redis + PyGraphBLAS (ADR-062).
