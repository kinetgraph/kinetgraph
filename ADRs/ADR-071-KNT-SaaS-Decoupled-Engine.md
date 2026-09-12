<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-071: Arquitetura KNT-SaaS — Engine Distribuído com Desacoplamento entre Control Plane e Execution Plane

- **Status:** Proposed (Draft Preview for Architecture Review)
- **Data:** 2026-09-11
- **Autor:** Time de Arquitetura KNTGraph
- **Relacionado a:**
  - [ADR-005](./ADR-005-Checkpoints-Idempotency.md) — Checkpoints Duráveis e Idempotência
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — Tool Worker Pattern e WorkerManager
  - [ADR-049](./ADR-049-Zero-Token-Architecture.md) — Zero-Token Architecture
  - [ADR-066](./ADR-066-Single-Tool-Path.md) — Single Tool Path (Mutações via `@tool_worker`)
  - [ADR-068](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md) — Mitigação de Tráfego Redis e Subscrição em Bloco
  - [ADR-069](./ADR-069-Agent-Concordo-Macro-Behaviors.md) — Agent Concordo: BusinessFSM e WorkflowSaga
  - [ADR-070](./ADR-070-Entity-Relation-Extraction-Concordo-Pipeline.md) — Pipeline de Extração de Entidades e Relações via Concordo

---

## 1. Contexto e Motivação

Com o advento do padrão **Agent Concordo (ADR-069)**, o `kntgraph` evoluiu de uma biblioteca ECS para um **runtime completo de orquestração de comportamento macro para agentes**.

Para oferecer o `kntgraph` como uma plataforma **SaaS Multi-Tenant / Enterprise Managed Infrastructure**, surgiu a necessidade de desacoplar o sistema em dois domínios operacionais independentes:
1. **Control Plane (Módulo A — Concordo Engine)**: Responsável pelo `World.fold`, máquinas de estado (`BusinessFSMConcordo`), orquestração de workflows (`WorkflowSagaConcordo`) e avaliação de regras declarativas (`Specifications`).
2. **Execution Plane (Módulo B — WorkerManager Cluster)**: Responsável pela execução isolada de ferramentas pesadas (`@tool_worker`) como chamadas a LLMs (LiteLLM), GLiNER2, higienização LGPD, mutações Redis e matrizes PyGraphBLAS em C.

Este ADR estabelece o modelo **DSL Declarativo (Concordo Bundle)** e analisa detalhadamente a **resiliência contra quedas no Control Plane** e a **escalabilidade horizontal do WorkerManager**.

---

## 2. Visão Geral da Arquitetura Control Plane vs. Execution Plane

```
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │                   CONTROL PLANE (MÓDULO A: kntgraph-engine)                  │
  │                                                                              │
  │  - DSL Bundle Interpreter (DSL Manifests em YAML/JSON)                       │
  │  - EventLog Stream Subscriber & Pure Fold Engine (World.fold)                │
  │  - BusinessFSM State Machine & WorkflowSaga Orchestrator                     │
  │  - Pure Specifications Evaluator                                             │
  └──────────────────────────────────────────────────────────────────────────────┘
                                         │
                 ┌───────────────────────┴───────────────────────┐
                 │  Redis Streams Infra (Event Bus Unificado)     │
                 │  - Stream: 'knt:stream:<tenant_id>'           │
                 │  - Queue:  'knt:queue:tool_requests'          │
                 └───────────────────────┬───────────────────────┘
                                         │
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │                EXECUTION PLANE (MÓDULO B: WorkerManager Cluster)             │
  │                                                                              │
  │  - Dynamic Autoscaling Cluster (Pods K8s / Serverless Workers 0..N)          │
  │  - `@tool_worker` Implementations: LiteLLM, GLiNER2, PyGraphBLAS, Webhooks   │
  │  - Three-Gate Security & Idempotency Key Deduplicator                        │
  └──────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Especificação do Modelo DSL Declarativo (Concordo Bundle Manifest)

O Control Plane **não compila nem executa código Python imperativo arbitrário** enviado por usuários do SaaS. Em vez disso, lê e interpreta **Concordo Bundles Declarativos** em YAML/JSON validados via schemas Pydantic:

```yaml
bundle_id: "com.acme.knowledge_pipeline"
version: "1.0.0"

events:
  - name: "document.ingested"
    schema:
      document_id: "string"
      content: "string"

specifications:
  - id: "IsHighPriority"
    expression: "event.data.content_length > 50000"

  - id: "ExtractionConfidencePassed"
    expression: "event.data.confidence_score >= 0.80"

business_fsm:
  id: "fsm:KnowledgeLifecycle"
  initial_state: "INGESTED"
  states: ["INGESTED", "EXTRACTING", "WAITING_HUMAN_REVIEW", "CONSOLIDATED"]

  transitions:
    - from: "INGESTED"
      to: "EXTRACTING"
      on_event: "document.ingested"

    - from: "EXTRACTING"
      to: "CONSOLIDATED"
      on_event: "knowledge.entities_extracted"
      guard: "ExtractionConfidencePassed"

    - from: "EXTRACTING"
      to: "WAITING_HUMAN_REVIEW"
      on_event: "knowledge.entities_extracted"
      guard: "not(ExtractionConfidencePassed)"

workflow_sagas:
  - id: "saga:EntityExtractionSaga"
    trigger_event: "document.ingested"
    steps:
      - name: "TextChunking"
        tool: "text_chunker_tool"
        input_mapping:
          text: "event.data.content"

      - name: "EntityExtraction"
        tool: "gliner2_entity_extraction_tool"
        pre_condition: "StepCompleted('TextChunking')"
        input_mapping:
          chunks: "steps.TextChunking.output.chunks"
```

---

## 4. Análise Profunda 1: Quedas, Falhas e Resiliência no Control Plane (Módulo A)

O Control Plane é o "cérebro" orquestrador da plataforma. Uma queda do Control Plane não pode resultar em perda de estado, duplicidade de ações ou corrupção do fluxo do agente.

### 4.1 Recuperação de Crashes sem Perda de Estado (Crash Recovery & Re-Fold)
1. **Natureza Stateless do Control Plane**: O Control Plane não possui banco de dados relacional interno. Todo o seu estado é uma função pura dos eventos gravados no Redis Streams (`World.fold`).
2. **Durable Checkpoints (ADR-005 / ADR-057)**: O Control Plane salva periodicamente no Redis um checkpoint compactado (`knt:checkpoint:<agent_id>`).
3. **Tempo de Recuperação de Crash (MTTR < 100ms)**: Ao cair e reiniciar, uma nova instância do Control Plane:
   - Carrega o último checkpoint compactado.
   - Lê as poucas mensagens delta pendentes a partir do `cursor_id` usando `XREADGROUP`.
   - Reconstitui o estado exato da FSM e da Saga em menos de 100 milissegundos.

### 4.2 Prevenção de Duplicação de Tool Calls em Re-emissão (Idempotency Gating)
Se o Control Plane cair **exatamente após decidir emitir um `tool.requested`**, mas antes de confirmar a gravação do checkpoint:
- Ao reiniciar, o Control Plane tentará emitir novamente a chamada de ferramenta.
- **Proteção por `tool_call_id` Determinístico**: O `tool_call_id` é gerado deterministicamente a partir de `hash(saga_id, step_name, event_correlation_id)`.
- O Redis Stream de ferramentas ou o Worker Plane reconhecem o `tool_call_id` duplicado e **ignora a segunda execução**, garantindo semântica de execução **At-Most-Once**.

### 4.3 Alta Disponibilidade Ativa/Ativa e Evitação de Split-Brain
- **Redis Consumer Groups**: Múltiplas instâncias do Control Plane participam do mesmo Redis Consumer Group (`knt:cg:control_plane`).
- **Sharding por Tenant / Agent ID**: Cada réplica do Control Plane assume a leitura de um subconjunto de agentes baseando-se em Consistent Hashing (`hash(agent_id) % num_nodes`).
- **Lease Locks no Redis**: Se um nó do Control Plane cair, os leasings expirados são automaticamente assumidos pelas réplicas sobreviventes via rebalanceamento do Consumer Group.

---

## 5. Análise Profunda 2: Escala Horizontal e Orquestração do WorkerManager (Módulo B)

O Worker Plane lida com a parte pesada e não-determinística da plataforma (LLMs, GPUs, PNL, I/O externo).

### 5.1 Autoscaling Nativo de 0 a N Réplicas via Lag do Redis Stream (KEDA Integration)
- Os Workers não escutam HTTP; eles consomem a fila de eventos `knt:queue:tool_requests` no Redis Stream via `XREADGROUP`.
- **Autoscaling com KEDA (Kubernetes Event-driven Autoscaling)**:
  - O HPA (Horizontal Pod Autoscaler) é acionado pela métrica do Redis Stream Lag (`XPENDING` + `XLEN`).
  - **Escala para Zero (Scale to Zero)**: Se não houver requisições de ferramentas pendentes, o número de pods de workers pesados (ex: GPU/GLiNER2) reduz a 0, economizando custo de nuvem.
  - **Surge Scaling**: Se chegar uma rajada de 1.000 documentos para processamento, o KEDA sobe dinamicamente até $N$ réplicas de workers em paralelo.

### 5.2 Particionamento por Categoria de Tool (Worker Pools Isolados)
Para evitar que chamadas demoradas de LLM (ex: 10 segundos) travem ferramentas de execução rápida (< 5ms), o Worker Plane divide a infraestrutura em **3 Pools Isolados**:

```
                       ┌──► Pool 1: 'queue:tools:fast' (Redis Mutators, Regex)
                       │    (Workers Leves em CPU, Latência < 5ms)
                       │
'knt:queue:tool_reqs' ─┼──► Pool 2: 'queue:tools:ai' (LiteLLM, GLiNER2)
                       │    (Workers I/O e GPU-bound, Latência 1s..10s)
                       │
                       └──► Pool 3: 'queue:tools:analytics' (PyGraphBLAS C)
                            (Workers CPU-bound de Alta Performance)
```

### 5.3 Controle de Rate Limiting e Backpressure (Proteção contra APIs de Terceiros)
- Se 500 workers paralelos tentarem chamar a API da OpenAI/Anthropic simultaneamente, haverá erro de `429 Too Many Requests`.
- O `WorkerManager` integra um **Bulkhead & Rate Limiter Centralizado** (ADR-036 / ADR-047) via Redis Token Bucket.
- Se o limite de cota for atingido, os workers colocam as mensagens em estado de retencao temporária (Backpressure), notificando a Saga no Control Plane para estender o timeout do passo de forma graciosa.

### 5.4 Tratamento de Dead Letter Queue (DLQ) e Retry Exponencial
- Se um worker falhar 3 vezes seguidas ao executar uma ferramenta (ex: API externa indisponível), a ferramenta é movida para a **Dead Letter Queue (`knt:stream:dlq`)**.
- O evento `tool.<name>.failed` é emitido de volta para o Control Plane, permitindo que a `WorkflowSagaConcordo` execute a rota de compensação declarada no manifest DSL.

### 5.5 Reuso da Suíte Nativa de Primitivas de Resiliência (`src/kntgraph/resilience/`)
O Worker Plane não precisa reinventar mecanismos de resiliência; ele integra diretamente as primitivas nativas exportadas por `src/kntgraph/resilience/`:

| Primitiva Nativa | Arquivo no KNT | Aplicação na Arquitetura KNT-SaaS |
|---|---|---|
| **Circuit Breaker** | `src/kntgraph/resilience/circuit_breaker.py` | Proteção contra falhas em APIs de LLMs ou webhooks externos (estados `CLOSED`, `OPEN`, `HALF_OPEN`). |
| **Retry com Backoff & Jitter** | `src/kntgraph/resilience/retry.py` | Re-tentativas automáticas de chamadas a ferramentas com atraso exponencial e variação de *jitter* contra *thundering herd*. |
| **Bulkhead Isolation** | `src/kntgraph/resilience/bulkhead.py` | Isolamento concorrente por tenant (`get_bulkhead("tenant-1")`), impedindo que requisições pesadas de um cliente afetem os demais. |
| **Timeout Enforcement** | `src/kntgraph/resilience/timeout.py` | Cancelamento gracioso assíncrono fora do Event Loop via `with_timeout_and_retry`. |
| **Rate Limiter** | `src/kntgraph/resilience/rate_limit.py` | Token Bucket centralizado no Redis para prevenir estouro de cota (HTTP `429 Too Many Requests`). |
| **Fallback Chains** | `src/kntgraph/resilience/fallback.py` | Encadeamento de redundância (`with_fallback`) alternando de LLM primário (ex: OpenAI) para secundário ou SLM local. |
| **Resilient Edge** | `src/kntgraph/resilience/edge.py` | Pipeline de borda compondo Bulkhead ➔ Rate Limit ➔ Circuit Breaker ➔ Timeout ➔ Retry ➔ Fallback. |

---

## 6. Evolução da Estrutura de Pacotes do Framework

Para suportar essa divisão, o repositório evolui com o pacote `kntgraph.control_plane`:

```
src/kntgraph/
├── control_plane/
│   ├── __init__.py
│   ├── dsl/
│   │   ├── schema.py          # Schemas Pydantic do Concordo Bundle (DSL)
│   │   └── parser.py          # Parser e Validador de manifests YAML/JSON
│   ├── engine/
│   │   ├── runner.py          # Processo Principal do Control Plane Runner
│   │   ├── fsm_evaluator.py   # Interpretador de BusinessFSM
│   │   └── saga_evaluator.py  # Interpretador de WorkflowSagas & Specs
│   └── registry/
│       └── bundle_store.py    # Repositório de Bundles no Redis KV
└── tools/                     # Módulo Worker Plane (WorkerManager existento)
```

---

## 7. Plano de Implementação

1. **Fase 1 — Schema e Parser DSL**: Criar os schemas Pydantic e o parser de `ConcordoBundle` em `src/kntgraph/control_plane/dsl/`.
2. **Fase 2 — Control Plane Runner**: Implementar o `ControlPlaneRunner` em `src/kntgraph/control_plane/engine/runner.py` interpretando FSMs e Sagas declarativas sobre o `World.fold`.
3. **Fase 3 — Integrador de Consumer Groups**: Configurar a resiliência e sharding de leitura no Redis Stream com suporte a `XREADGROUP` e crash recovery.
4. **Fase 4 — Worker Pool Partitioning**: Ajustar o `WorkerManager` para suporte a consumo de filas segregadas (`queue:tools:*`) e integração KEDA.
5. **Fase 5 — Testes E2E de SaaS Simulado**: Criar testes de estresse simulando queda de nós do Control Plane e autoscaling de workers.

---

## 8. Status e Conclusão

O **ADR-071** estabelece os alicerces para transformar o `kntgraph` em uma plataforma SaaS Serverless completa. Ao desacoplar o **Control Plane (interpretador DSL determinístico e resiliente)** do **Execution Plane (WorkerManager altamente escalável)**, garantimos isolamento de falhas, custo otimizado e operação segura em ambientes multi-tenant.
