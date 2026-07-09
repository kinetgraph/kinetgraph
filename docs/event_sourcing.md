<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Event Sourcing

Estado é derivado de eventos, não armazenado diretamente.

---

## Conceito

### Tradicional vs Event Sourcing

```
# Tradicional (Estado Atual)
┌─────────────┐
│  Database   │
│  ┌───────┐  │
│  │ State │  │  ← Atualizado diretamente
│  └───────┘  │
└─────────────┘

# Event Sourcing
┌─────────────┐
│ Event Store │
│ ┌─────────┐ │
│ │ Event 1 │ │
│ │ Event 2 │ │  ← Append-only
│ │ Event 3 │ │
│ └─────────┘ │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│   State =   │
│ fold(events)│  ← Derivado
└─────────────┘
```

---

## Eventos

### AgentEvent

```python
from kntgraph.core.event import AgentEvent

event = AgentEvent.create(
    event_type="document.validated",
    agent_id="agent-123",
    data={
        "document_id": "NF-001",
        "validated_at": "2024-05-26T10:00:00Z"
    },
    version=1
)
```

### Campos do Evento

```python
{
    "event_id": "uuid",           # ID único
    "agent_id": "agent-123",      # Dono do evento
    "event_type": "doc.validated",# Tipo
    "timestamp": "2024-05-26...", # Quando ocorreu
    "data": {...},                # Payload
    "version": 1,                 # Versão do schema
    "correlation": {              # Tracing
        "correlation_id": "uuid",
        "causation_id": "uuid",
        "span_id": "uuid"
    }
}
```

### Tipos de Eventos

```python
# Ciclo de vida de documento
"document.received"
"document.validating"
"document.validated"
"document.rejected"

# Notificações
"notification.scheduled"
"notification.sent"
"notification.failed"

# Workflows
"workflow.started"
"workflow.step_completed"
"workflow.finished"

# Erros
"event.processing_failed"
"event.moved_to_dlq"
```

---

## Correlation ID

### Tracing Distribuído

```python
from kntgraph.core.event import correlation_middleware

with correlation_middleware.context_manager({
    "tenant_id": "123456789",
    "document_id": "NF-001"
}) as ctx:
    # Evento 1
    event1 = AgentEvent.create(
        "document.received",
        "agent-1",
        data={},
        correlation=ctx
    )
    
    # Evento 2 (causado pelo 1)
    ctx2 = correlation_middleware.continue_correlation(event1)
    event2 = AgentEvent.create(
        "document.validated",
        "agent-1",
        data={},
        correlation=ctx2
    )
    
    # Ambos têm mesmo correlation_id
    assert event1.correlation.correlation_id == event2.correlation.correlation_id
```

### Árvore de Causalidade

```
correlation_id: abc-123
├─ event1: document.received
│  └─ event2: document.validating
│     ├─ event3: document.validated
│     │  └─ event5: notification.sent
│     └─ event4: calculation.completed
```

```python
# Reconstrói árvore
flow_tree = await store.get_flow_tree("abc-123")

print(flow_tree)
# {
#     "correlation_id": "abc-123",
#     "root_events": [event1],
#     "total_events": 5,
#     "started_at": "...",
#     "completed_at": "..."
# }
```

---

## Event Store

### Append de Eventos

```python
from kntgraph.events.store import EventStore

store = EventStore(redis)

# Single event
result = await store.append(event)
if result.is_ok():
    event_id = result.unwrap()

# Batch
events = [event1, event2, event3]
result = await store.append_batch(agent_id, events)
```

### Query de Eventos

```python
# Por agente
events = await store.get_events("agent-123")

# Por correlation
events = await store.get_by_correlation("flow-xyz")

# Com paginação
events = await store.get_events("agent-123", count=50)

# Último evento
last_id = await store.get_latest_event_id("agent-123")
```

### Streams Redis

```
fmh:agents:{agent_id}:events    # Stream por agente
fmh:dlq:events                   # Dead Letter Queue
fmh:revocations:{agent_id}       # Revogações de chaves (Nível 2+)
fmh:anchors:{agent_id}           # Hash-chain anchors (Nível 3+)
```

> **Nota histórica**: `fmh:events:global` foi removido na
> v0.7.0 (consumidor ausente, dead code). Stream global fica
> como responsabilidade do consumidor se necessário.

### Autenticação e autorização (Níveis 1-3)

Para deploys multi-tenant ou regulados, os eventos podem ser
assinados (Ed25519 + JCS) e validados contra políticas
declarativas por `agent_id`. Veja:

- [security/README.md](./security/README.md) - overview
- [security/signing.md](./security/signing.md) - Nível 1
- [security/authorization.md](./security/authorization.md) - Nível 2
- [security/anchor.md](./security/anchor.md) - Nível 3

---

## Reconstrução de Estado

### Fold/Reduce

```python
def reconstruct_state(events: list[AgentEvent]) -> AgentState:
    """Reconstrói estado aplicando eventos em sequência."""
    state = AgentState(agent_id=events[0].agent_id)
    
    for event in events:
        state = apply_event(state, event)
    
    return state

def apply_event(state: AgentState, event: AgentEvent) -> AgentState:
    """Aplica evento ao estado."""
    match event.event_type:
        case "document.received":
            return state.with_document(event.data)
        case "document.validated":
            return state.with_validated_document(event.data)
        case "document.rejected":
            return state.with_rejected_document(event.data)
        case _:
            return state
```

### Exemplo Prático

```python
# Eventos no store
events = [
    AgentEvent.create("document.received", "agent-1", {"doc_id": "NF-001"}),
    AgentEvent.create("document.validating", "agent-1", {}),
    AgentEvent.create("document.validated", "agent-1", {"status": "ok"}),
]

# Reconstrói estado
state = reconstruct_state(events)

print(state.components["document"].status)
# "validated"
```

---

## Imutabilidade

### Eventos são Imutáveis

```python
event = AgentEvent.create("doc.received", "agent-1", {})

# ❌ Não pode modificar
event.event_type = "modified"  # Erro!

# ✅ Cria novo evento
new_event = AgentEvent.create("doc.modified", "agent-1", {})
```

### Estado Derivado

```python
# ❌ Não atualiza estado diretamente
agent.status = "validated"

# ✅ Emite evento, estado é derivado
event = AgentEvent.create("document.validated", agent_id, {})
await store.append(event)
# Estado é reconstruído aplicando eventos
```

---

## Snapshots (Opcional)

Para performance, pode salvar snapshot periódico:

```python
# A cada 100 eventos, salva snapshot
if len(events) % 100 == 0:
    snapshot = {
        "agent_id": agent_id,
        "version": len(events),
        "state": serialize(state),
        "timestamp": datetime.now()
    }
    await save_snapshot(snapshot)

# Reconstrução otimizada
def reconstruct_with_snapshot(agent_id):
    snapshot = get_latest_snapshot(agent_id)
    events = get_events_after(agent_id, snapshot["version"])
    return fold(snapshot["state"], events)
```

---

## Dead Letter Queue

### Eventos Falhos

```python
from kntgraph.events.dead_letter import DLQReason

# Move evento falho para DLQ
await store.move_to_dlq(
    event,
    reason=DLQReason.MAX_RETRIES_EXCEEDED,
    error_message="Failed after 3 retries",
    retry_count=3
)
```

### Reprocessamento

```python
# Recupera evento da DLQ
result = await store.reprocess_from_dlq(dlq_id)

if result.is_ok():
    event = result.unwrap()
    # Tenta processar novamente
    await process_event(event)
```

---

## Audit Trail

### Completo e Imutável

```python
# Todo histórico disponível
events = await store.get_events("agent-123")

for event in events:
    print(f"{event.timestamp} - {event.event_type}")
    print(f"  Data: {event.data}")
    print(f"  Correlation: {event.correlation.correlation_id}")
```

### Compliance

- ✅ Todos eventos são registrados
- ✅ Timestamp preciso
- ✅ Autor/correlation rastreável
- ✅ Imutável (append-only)
- ✅ Queryable por correlation

---

## Best Practices

### ✅ Faça

```python
# Eventos no passado
event = AgentEvent.create(
    "document.validated",
    agent_id,
    data={"status": "ok"},
    timestamp=datetime(2024, 5, 26, 10, 0, 0)  # Explícito
)

# Versionamento
event = AgentEvent.create(
    "document.validated",
    agent_id,
    data={"status": "ok"},
    version=2  # Schema v2
)

# Correlation sempre
with correlation_middleware.context_manager({}) as ctx:
    event = AgentEvent.create("doc.received", agent_id, {}, correlation=ctx)
```

### ❌ Não Faça

```python
# ❌ Eventos sem correlation
event = AgentEvent.create("doc.received", agent_id, {})  # Sem correlation!

# ❌ Modificar evento após criação
event.data["modified"] = True  # Imutável!

# ❌ Deletar eventos
await store.delete(event_id)  # Append-only!
```

---

## Performance

### Otimizações

- **Redis Streams**: Append O(1)
- **Auto-trim**: Mantém últimos N eventos
- **Batch**: Múltiplos eventos em uma operação
- **Pipeline**: Reduz round-trips

### Benchmarks

| Operação | Tempo |
|----------|-------|
| Append single event | ~1ms |
| Append batch (10) | ~5ms |
| Query by correlation | ~5ms |
| Reconstruct state (100 events) | ~10ms |

---

## Exemplo Completo

```python
import asyncio
from kntgraph.core.world import World, AgentState
from kntgraph.core.event import AgentEvent, correlation_middleware
from kntgraph.events.store import EventStore
from kntgraph.infra.redis import get_redis

async def main():
    # Setup
    redis = await get_redis()
    store = EventStore(redis)
    
    # Correlation
    with correlation_middleware.context_manager({
        "tenant_id": "123456789",
        "document_id": "NF-001"
    }) as ctx:
        # 1. Cria agente
        agent = AgentState.create(
            agent_type="service",
            tenant_id="123456789",
            unique_key="NF-001"
        )
        
        # 2. Evento inicial
        event1 = AgentEvent.create(
            "document.received",
            agent.agent_id,
            {"document_type": "nota_fiscal"},
            correlation=ctx
        )
        await store.append(event1)
        
        # 3. Processa
        event2 = AgentEvent.create(
            "document.validated",
            agent.agent_id,
            {"status": "ok"},
            correlation=correlation_middleware.continue_correlation(event1)
        )
        await store.append(event2)
        
        # 4. Audit trail
        events = await store.get_by_correlation(ctx.correlation_id)
        print(f"{len(events)} eventos no fluxo")
        
        # 5. Reconstrói estado
        state = reconstruct_state(events)
        print(f"Estado: {state.status}")

asyncio.run(main())
```

---

## Recursos

- [Event Store](event_store.md)
- [Correlation ID](correlation.md)
- [Dead Letter Queue](dead_letter_queue.md)
- [World & AgentState](world.md)
