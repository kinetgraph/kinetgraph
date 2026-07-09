<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-001: Arquitetura de Agentes com ECS Puro, Redis Streams e Ciclos Dual

**Status:** Aceito
**Data:** 06 de junho de 2026
**Versão:** 2.0 (refactor completo: ECS puro, event-sourced, dual lifecycle)
**Autores:** Equipe de Arquitetura FMH
**Stakeholders:** Desenvolvimento, DevOps, Produto

---

## 1. Contexto

O FMH (Framework Multi-Agente) precisa suportar **automação de
processos de negócio (BPM) em PMEs brasileiras**, com:

- Execução contínua de longa duração
- Múltiplos agentes operando em paralelo
- Dois eixos ortogonais de ciclo de vida por agente
  (operacional × domínio de negócio)
- Comunicação agente-a-agente e com usuários externos
- Processos longos sem bloqueio (horas, dias)
- Audit trail completo (compliance contábil/fiscal)
- Resiliência a falhas transitórias
- Recuperação por replay (a partir do log)
- Latência baixa no caminho crítico

A versão 1.3 do framework atendia parte disso, mas sofria de:

- **Estado mutável por agente** (`AgentState.status`,
  `pending_events`, `outbox`) acoplado ao ECS, em vez de
  derivado dos eventos
- **Persistência opcional** (FalkorDB), com World podendo ter
  ou não repositório
- **Componentes acoplados a Pydantic**, limitando modelos de
  domínio a Pydantic BaseModel
- **Sistema como função impura** (mutava `world.outbox`), o que
  impedia replay determinístico
- **Storage colunar PyArrow** que aumentava dependências sem
  entregar valor (replay já era O(N))
- **Sem distinção clara** entre "o agente está em que fase do
  runtime" vs "o agente está em que etapa do negócio"

Esta ADR registra a decisão de migrar para a v2.0.

---

## 2. Decisão

Adotar uma arquitetura **event-sourced pura** com:

1. **ECS puro e funcional** — `World` é uma função pura
   `Sequence[Event] → WorldState`. Componentes são valores
   imutáveis; nada no core é mutável.
2. **Redis Streams como única fonte de verdade** — eventos são
   append-only, um stream por agente, com índice global.
3. **Idempotência por `event_id` determinístico** — replay
   pode ser executado N vezes sem duplicar.
4. **Dois ciclos de vida** por agente:
   - **Operacional** (`event_class="lifecycle"`): fases de
     runtime (spawned → idle → running → blocked → ... →
     terminated).
   - **Domínio** (`event_class="domain"`): fases de negócio,
     definidas pela aplicação (ex: NF: recebida → validada →
     lançada → transmitida → paga).
5. **Sistemas puros** — `CyclicSystem = (World) → list[Event]`,
   `ReactiveSystem = (World, Event) → list[Event]`. Sem I/O.
6. **Eventual consistency explícita** — o sistema que decide
   no tick T só vê o mundo como era em T; seus efeitos
   aplicam em T+1.
7. **Replay canônico** — `World.fold(stream, up_to_tick)` é a
   única forma de construir o mundo; toda partição do estado
   é reproduzível.
8. **Resiliência em adaptadores** — circuit breaker, retry,
   bulkhead ficam na borda (adaptadores que emitem eventos
   externos); sistemas permanecem puros.

### 2.1 Princípios Invioláveis

| # | Princípio | Implicação |
|---|-----------|------------|
| 1 | **Imutabilidade total no core** | `Component`, `Event`, `World`, `AgentView` são frozen. Apenas `SessionState` (volátil, F8) será mutável. |
| 2 | **Core funcional** | Sistemas são funções `World → list[Event]`. Side effects via adaptadores. |
| 3 | **Event Sourcing estrito** | Estado derivado de eventos. Redis Streams é a fonte, World é projeção. |
| 4 | **Async-First** | Todo I/O é non-blocking. Workers são stateless. |
| 5 | **Fail Fast** | Circuit breaker, timeout, DLQ. Nenhuma operação trava. |
| 6 | **Determinismo** | `event_id = uuid5(causation_id, type, payload)`. Replay é idempotente. |

### 2.2 Tier de Memória

| Tier | Tecnologia | Latência | Durabilidade | Uso |
|------|-----------|----------|--------------|-----|
| **Working** | Python heap (World, AgentView) | < 1μs | Volátil | Replay do tick atual |
| **Session** | Redis JSON (F8) | < 1ms | TTL (min-hrs) | Contexto conversação |
| **Event Log** | Redis Streams (per-agent) | < 5ms | Persistente | Source of truth |
| **Global Stream** | Redis Streams (concat) | < 5ms | Auto-trim 1M | Dashboards / fan-in |
| **DLQ** | Redis Streams | < 5ms | Persistente | Eventos falhos |
| **Projeção opcional** | FalkorDB (F8) | < 50ms | Persistente | GraphRAG (F8) |
| **Archive** | Iceberg (F8) | < 1s | Permanente | Analytics |

---

## 3. Arquitetura de Alto Nível

```
┌─────────────────────────────────────────────────────────────────────┐
│                       USUÁRIO / SISTEMA EXTERNO                      │
│              (API, UI, Webhook, Scheduler, ERP)                     │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ adaptação (F8)
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    ADAPTERS / I/O BOUNDARY                          │
│  Circuit Breaker · Retry · Bulkhead · Timeout · Fallback            │
│  - Publicam: Event (domain | lifecycle) no EventLog                │
│  - Consomem: Event (idem)                                           │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ append (idempotente)
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    REDIS STREAMS (FONTE DA VERDADE)                │
│  Per-agent:  knt:agents:{id}:events  (MAXLEN 100k)                 │
│  Idemp:      knt:eventids:{id}                                    │
│  DLQ:        knt:dlq:events + indexes                              │
└────────┬──────────────────────────────────┬─────────────────────────┘
         │ XRANGE                              │ XADD
         ▼                                     ▲
┌─────────────────────────────────┐    ┌──────┴──────────────────────┐
│   WORLD FOLD (PURO)             │    │   RUNNER (side effects)     │
│                                 │    │                              │
│  fold(events) → AgentView      │    │  - Polling tick              │
│  - last lifecycle event        │    │  - Reactive dispatcher       │
│    = operational_phase         │    │  - DLQ on system failure     │
│  - last domain event           │    │                              │
│    = domain_phase              │    │                              │
│  - components = latest data    │    │                              │
│                                 │    │                              │
└────────┬────────────────────────┘    └──────┬──────────────────────┘
         │                                     │
         │ World                              │ new events
         ▼                                     │
┌─────────────────────────────────┐           │
│   SYSTEMS (PUROS)               │───────────┘
│                                 │
│  CyclicSystem: World → [Event]  │
│  ReactiveSystem:                │
│    (World, Event) → [Event]     │
│                                 │
│  - Sem I/O                      │
│  - Sem mutação                  │
│  - Determinísticos              │
└─────────────────────────────────┘
```

---

## 4. Decisões Detalhadas

### 4.1 ECS Puro (sem mutação no core)

**Antes (v1.x):** `World` era Pydantic com `outbox: dict` mutável;
`AgentState` tinha `status` e `pending_events` mutáveis; sistemas
chamavam `world.emit(entity_id, event)` e.g.

**Agora (v2.0):** `World` é fold puro de eventos.
- `World.empty(tick=0) → World`
- `World.fold(events, projection=...) → World`
- `World.with_event(event) → World` (helper para 1 evento)
- Sem outbox, sem `emit`, sem `drain_outbox`, sem `pending_events`
- `AgentView` substitui `AgentState`: derivado, imutável

### 4.2 Event como Única Forma de Mudança

```python
@dataclass(frozen=True, slots=True)
class Event:
    event_id: UUID                    # uuid5(causation, type, payload)
    agent_id: str
    event_type: str                   # "agent.spawned" | "document.validated"
    event_class: Literal["lifecycle", "domain"]
    timestamp: datetime
    data: Mapping[str, Any]
    correlation: CorrelationContext   # correlation_id, causation_id, span_id
    causation_id: UUID | None
    version: int = 1
```

**Convenções de `event_type`:**
- Lifecycle: `agent.spawned`, `agent.idle`, `agent.running`,
  `agent.blocked`, `agent.checkpointed`, `agent.terminated`.
- Domain: livre, definido pela aplicação (ex: `document.received`,
  `task.completed`, `invoice.paid`).

### 4.3 Dois Ciclos de Vida

**Operacional (do framework):**

```python
OperationalPhase = Literal[
    "spawned",      # acabou de ser criado
    "idle",         # existe, sem trabalho
    "running",      # um sistema está processando
    "blocked",      # aguardando dependência externa
    "checkpointed", # pausa controlada
    "terminated",   # descontinuado (terminal)
]
```

`agent.operational_phase` = `event_type` do último evento
`lifecycle`.

**Domínio (da aplicação):**

```python
@dataclass(frozen=True, slots=True)
class DomainPhase:
    phase: str          # ex: "validated", "lancada", "transmitida"
    updated_at: datetime
    reason: str | None
```

`agent.domain_phase` = `event_type` do último evento
`domain`.

### 4.4 Sistema como Função Pura

```python
System = (World) → list[Event]                    # CyclicSystem
System = (World, Event) → list[Event]             # ReactiveSystem
```

**Garantias:**
- Idempotente: `(world, event)` produz a mesma `list[Event]`
  toda vez.
- Sem estado: sistema não guarda variáveis entre invocações.
- Componível: pode-se encadear `pipe(world, sys1, sys2, ...)`.
- Testável: constrói-se um World em memória, chama-se o
  sistema, assert sobre o output.

**Limitação importante:** sistemas cíclicos não encadeiam no
mesmo tick. Vendo o World em T, cada um produz seus eventos; o
efeito agregado só é visível em T+1. Isso **é** a consistência
eventual do modelo.

### 4.5 EventLog: Redis Streams com Idempotência

Layout:

```
knt:agents:{agent_id}:events     # per-agent stream (MAXLEN 100k)
knt:eventids:{event_id}         # idempotency index → stream_id
```

**Protocolo de append:**

1. Caller computa `event_id` (já determinístico via `uuid5`).
2. `SETNX knt:eventids:{event_id}` — se já existir, no-op.
3. `XADD knt:agents:{id}:events` (per-agent).
4. (sem global stream desde 2026-06-22; ver ADR-022)
5. `SET knt:eventids:{event_id} = stream_id` (valor final).

### 4.6 DLQ

```
knt:dlq:events                   # stream de eventos falhos
knt:dlq:by_event_id              # hash {event_id:reason: stream_id}
knt:dlq:by_agent                 # hash {agent_id: first_failed_stream_id}
knt:dlq:reasons                  # hash {reason: count}  (stats)
```

Idempotente em `(event_id, reason)`. Reprocess devolve o
`Event` original; discard remove sem reprocessar.

### 4.7 Resiliência

Os padrões de resiliência (Circuit Breaker, Retry, Bulkhead,
Timeout, Fallback) migram para a **camada de adaptadores**:
sistemas puros não usam circuit breaker. Adaptadores que
fazem I/O (LLM, API externa, banco) usam.

### 4.8 O que sai do core

- `AgentState` (mutável) → `AgentView` (derivado, imutável)
- `outbox`, `emit`, `drain_outbox` (sistemas retornam eventos)
- `with_agents(Map)` (World construído só por fold)
- `pipe_async` (sistemas são callable direto)
- `repository` (World não tem storage backend; é fold puro)
- `lifecycle.LifecycleComponent` (lifecycle é derivado de
  evento, não componente)
- `falkordb.WorldRepository` (substituído por FalkorDB como
  projeção opcional em F8)
- `pyarrow.ArrowBackedArchetypeStorage` (storage é dict puro)
- `events/store.EventStore` (substituído por `stream.EventLog`)

---

## 5. Estrutura de Diretórios (v2.0)

```
fmh_backend/src/fmh_backend/
├── core/                   # Puro, sem I/O
│   ├── archetype.py        # ArchetypeId (canonical key)
│   ├── component.py        # ComponentMeta (sem Pydantic obrigatório)
│   ├── event.py            # Event, CorrelationContext, middleware
│   ├── lifecycle.py        # OperationalPhase, DomainPhase
│   ├── query.py            # WorldQuery, AsyncWorldQuery
│   ├── result.py           # Railway Pattern
│   ├── storage.py          # ArchetypeStorage (dict)
│   ├── system.py           # CyclicSystem, ReactiveSystem protocols
│   └── world.py            # World, AgentView, project_default
│
├── stream/                 # Redis Streams
│   ├── event_log.py        # EventLog (append, read, idempotency)
│   └── projection.py       # fold_world, fold_world_for_agent
│
├── runner/                 # Side effects
│   ├── reactive.py         # ReactiveDispatcher
│   └── runner.py           # Runner (cyclic tick)
│
├── events/                 # DLQ
│   └── dead_letter.py      # DeadLetterQueue
│
├── resilience/             # Adapter-layer
│   ├── circuit_breaker.py
│   ├── retry.py
│   ├── bulkhead.py
│   ├── timeout.py
│   └── fallback.py
│
└── infra/
    ├── config.py
    └── redis.py
```

---

## 6. Consequências

### Positivas

- ✅ **Replay determinístico**: rebuild do mundo a partir do
  stream a qualquer momento.
- ✅ **Idempotência total**: re-rodar o runner não duplica.
- ✅ **Testes puros**: sistemas são funções, testáveis sem
  Redis (com World em memória).
- ✅ **Separação clara**: operacional (framework) × domínio
  (aplicação).
- ✅ **Sem dependência de Pydantic** em componentes: Pydantic
  continua suportado, mas não obrigatório.
- ✅ **Menos dependências externas**: PyArrow removido;
  FalkorDB postergado para F8 (GraphRAG).
- ✅ **Consistência eventual explícita** no modelo: sistemas
  sabem que o que decidem hoje vira fato em T+1.

### Negativas

- ⚠️ **Mudança significativa de paradigma**: código escrito
  contra a v1.3 (mutação, outbox) precisa ser reescrito.
- ⚠️ **Sistemas cíclicos não encadeiam no mesmo tick**:
  exige cuidado do desenvolvedor.
- ⚠️ **Volumetria**: replay completo por tick = O(N eventos
  por tick). Para 10K+ agentes, sharding por tenant
  (F8).
- ⚠️ **Idempotência requer causation_id correto**: sistemas
  devem propagar o `event_id` recebido como `causation_id`
  do evento produzido.

### Mitigações

| Problema | Mitigação |
|----------|-----------|
| Reescrita de código | Documentação clara + exemplos verticais em F7 |
| Encadeamento de sistemas | Documentar; usar tick_next ou múltiplos ticks |
| Volumetria | Sharding por tenant (F8) |
| Idempotência | Helper `Event.create(causation_id=event.event_id)` |

---

## 7. Roadmap

| Fase | Status | Entrega |
|------|--------|---------|
| F0 — Remover PyArrow | ✅ | Lock sem pyarrow |
| F1 — Reescrever core/ | ✅ | ECS puro, Event, World, System |
| F2 — stream/ + runner/ | ✅ | EventLog + Runner + ReactiveDispatcher |
| F3 — DLQ atualizado | ✅ | DeadLetterQueue com Event novo |
| F4 — Limpar legado | ✅ | FalkorDB removido, shims removidos |
| F5 — Remover fmh_agents legado | ✅ | Pronto para F7 |
| F6 — Esta ADR + ADR-002 + ADR-003 | ✅ | Documentação alinhada |
| F7 — fmh_agents vertical PME | ⏳ | Componentes + sistemas + exemplo |
| F8 — GraphRAG + Iceberg | ⏳ | FalkorDB como projeção opcional |

---

## 8. Referências

- [ADR-002: Replay canônico de eventos](./ADR-002-Replay-Puro.md)
- [ADR-003: Ciclo dual operacional × domínio](./ADR-003-Ciclo-Dual.md)
- [Martin Fowler: Event Sourcing](https://martinfowler.com/eaaDev/EventSourcing.html)
- [Martin Fowler: Circuit Breaker](https://martinfowler.com/bliki/CircuitBreaker.html)
- [Reactive Manifesto](https://www.reactivemanifesto.org/)
- [Redis Streams](https://redis.io/docs/latest/develop/data-types/streams/)
