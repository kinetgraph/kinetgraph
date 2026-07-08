<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Dead Letter Queue (DLQ)

Armazena eventos que falharam no processamento para análise e reprocessamento.

---

## Visão Geral

```
┌─────────────────────────────────────────────────┐
│          Dead Letter Queue Flow                  │
├─────────────────────────────────────────────────┤
│  Evento → Processamento → Falha                 │
│                ↓                                │
│         Retry (3x backoff)                      │
│                ↓                                │
│         Ainda falhou?                           │
│                ↓                                │
│         DLQ (armazena)                          │
│                ↓                                │
│    ┌───────────┴───────────┐                    │
│    │                       │                    │
│    ▼                       ▼                    │
│ Reprocessar            Descartar                │
└─────────────────────────────────────────────────┘
```

---

## Quando Usar

- ✅ Evento falhou após retries
- ✅ Timeout recorrente
- ✅ Validação falhou
- ✅ Circuito breaker aberto
- ✅ Poison pill (evento corrompido)

---

## Setup

```python
from kntgraph.events.store import EventStore
from kntgraph.events.dead_letter import DeadLetterQueue
from kntgraph.infra.redis import get_redis

# Setup Redis
redis = await get_redis()

# Cria DLQ
dlq = DeadLetterQueue(redis)

# Cria EventStore com DLQ
store = EventStore(redis, dlq=dlq)
```

---

## Mover para DLQ

### Após Falha

```python
from kntgraph.events.dead_letter import DLQReason

async def process_event(event):
    try:
        await validate(event)
    except Exception as e:
        # Move para DLQ
        await store.move_to_dlq(
            event,
            reason=DLQReason.PROCESSING_FAILED,
            error_message=str(e),
            retry_count=3
        )
```

### Razões

```python
class DLQReason(str, Enum):
    PROCESSING_FAILED = "processing_failed"
    MAX_RETRIES_EXCEEDED = "max_retries_exceeded"
    VALIDATION_ERROR = "validation_error"
    TIMEOUT = "timeout"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    POISON_PILL = "poison_pill"
    UNKNOWN_ERROR = "unknown_error"
```

### Com Metadados

```python
await store.move_to_dlq(
    event,
    reason=DLQReason.VALIDATION_ERROR,
    error_message="CNPJ inválido",
    retry_count=0,
    metadata={
        "document_type": "nota_fiscal",
        "tenant_id": "123456789",
        "validation_errors": ["CNPJ required", "Invalid format"]
    }
)
```

---

## Consultar Eventos

### Todos Eventos

```python
events = await store.get_dlq_events()

for dl_event in events:
    print(f"Agent: {dl_event.event.agent_id}")
    print(f"Event: {dl_event.event.event_type}")
    print(f"Reason: {dl_event.reason.value}")
    print(f"Error: {dl_event.error_message}")
```

### Filtrar por Agente

```python
events = await store.get_dlq_events(agent_id="agent-123")
```

### Filtrar por Razão

```python
events = await store.get_dlq_events(
    reason=DLQReason.TIMEOUT
)
```

### Evento Específico

```python
dl_event = await dlq.get_event("dlq:agent-123:event-456")
```

---

## Reprocessamento

### Manual

```python
# Recupera evento da DLQ
result = await store.reprocess_from_dlq(dlq_id)

if result.is_ok():
    event = result.unwrap()
    
    # Tenta processar novamente
    try:
        await process_event(event)
        print("✅ Reprocessamento bem-sucedido")
    except Exception as e:
        # Falhou novamente → volta para DLQ
        await store.move_to_dlq(
            event,
            reason=DLQReason.POISON_PILL,
            error_message=f"Reprocessamento falhou: {e}"
        )
```

### Worker Automático

```python
async def dlq_reprocessor_worker():
    """Reprocessa eventos periodicamente."""
    while True:
        events = await store.get_dlq_events(count=100)
        
        for dl_event in events:
            result = await store.reprocess_from_dlq(dl_event.dlq_id)
            
            if result.is_ok():
                event = result.unwrap()
                try:
                    await process_event(event)
                except Exception as e:
                    # Poison pill
                    await store.move_to_dlq(
                        event,
                        reason=DLQReason.POISON_PILL,
                        error_message=str(e)
                    )
        
        await asyncio.sleep(60)  # A cada minuto

# Inicia worker
asyncio.create_task(dlq_reprocessor_worker())
```

---

## Descartar Eventos

### Descartar Único

```python
result = await dlq.discard(dlq_id)

if result.is_ok():
    print(f"✅ Evento {dlq_id} descartado")
```

### Purge (Limpeza)

```python
# Remove todos
result = await dlq.purge()

# Remove antigos (> 30 dias)
from datetime import datetime, timedelta, timezone

cutoff = datetime.now(timezone.utc) - timedelta(days=30)
result = await dlq.purge(older_than=cutoff)
```

---

## Estatísticas

```python
stats = await dlq.get_stats()

print(f"Total eventos: {stats['total_events']}")
print(f"Agentes únicos: {stats['unique_agents']}")
print(f"Por razão:")
for reason, count in stats['by_reason'].items():
    print(f"  {reason}: {count}")
```

### Exemplo de Saída

```json
{
  "total_events": 42,
  "unique_agents": 15,
  "by_reason": {
    "processing_failed": 20,
    "timeout": 10,
    "validation_error": 5,
    "poison_pill": 7
  },
  "oldest_event": "1698765432000-0",
  "newest_event": "1698876543000-0"
}
```

---

## Monitoramento

### Alertas

```python
async def monitor_dlq():
    """Monitora DLQ e dispara alertas."""
    stats = await dlq.get_stats()
    
    # Alerta se muitos eventos
    if stats["total_events"] > 100:
        send_alert(f"DLQ com {stats['total_events']} eventos!")
    
    # Alerta se muitos poison pills
    if stats["by_reason"].get("poison_pill", 0) > 10:
        send_alert(f"{stats['by_reason']['poison_pill']} poison pills!")
    
    # Report diário
    send_daily_report(stats)
```

### Logs

```python
import structlog

logger = structlog.get_logger()

# Ao mover para DLQ
logger.warning(
    "Event moved to DLQ",
    dlq_id=dlq_id,
    agent_id=event.agent_id,
    event_type=event.event_type,
    reason=reason.value,
    retry_count=retry_count
)
```

---

## Exemplo Completo

```python
import asyncio
from kntgraph.events.store import EventStore
from kntgraph.events.dead_letter import DLQReason, DeadLetterQueue
from kntgraph.infra.redis import get_redis
from kntgraph.resilience.retry import retry_with_backoff

@retry_with_backoff(max_attempts=3, base_delay=2.0)
async def process_with_retry(event):
    """Processa evento com retry."""
    return await validate_and_process(event)

async def process_event_with_dlq(event):
    """Processa evento e move para DLQ se falhar."""
    try:
        return await process_with_retry(event)
    except Exception as e:
        # Move para DLQ após retries
        await store.move_to_dlq(
            event,
            reason=DLQReason.MAX_RETRIES_EXCEEDED,
            error_message=str(e),
            retry_count=3
        )
        logger.warning("Event moved to DLQ", event_id=event.event_id)

async def reprocess_failed():
    """Reprocessa eventos falhos."""
    events = await store.get_dlq_events(count=50)
    
    for dl_event in events:
        result = await store.reprocess_from_dlq(dl_event.dlq_id)
        
        if result.is_ok():
            event = result.unwrap()
            try:
                await process_event_with_dlq(event)
                logger.info("Reprocessed successfully", dlq_id=dl_event.dlq_id)
            except Exception as e:
                logger.error("Reprocess failed", dlq_id=dl_event.dlq_id, error=str(e))

async def main():
    # Setup
    redis = await get_redis()
    dlq = DeadLetterQueue(redis)
    store = EventStore(redis, dlq=dlq)
    
    # Processa evento
    event = AgentEvent.create("document.received", "agent-1", {})
    await process_event_with_dlq(event)
    
    # Monitora
    stats = await dlq.get_stats()
    print(f"DLQ: {stats['total_events']} eventos")
    
    # Reprocessa
    await reprocess_failed()

asyncio.run(main())
```

---

## Padrões de Uso

### 1. Retry → DLQ

```python
async def process_with_dlq(event, max_retries=3):
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            return await process(event)
        except Exception as e:
            retry_count += 1
            await asyncio.sleep(2 ** retry_count)  # Backoff
    
    # Falhou → DLQ
    await store.move_to_dlq(
        event,
        reason=DLQReason.MAX_RETRIES_EXCEEDED,
        error_message=f"Failed after {max_retries} retries"
    )
```

### 2. Validação → DLQ

```python
async def validate_document(doc):
    errors = validate(doc)
    
    if errors:
        event = AgentEvent.create("doc.validation_failed", agent_id, {})
        await store.move_to_dlq(
            event,
            reason=DLQReason.VALIDATION_ERROR,
            error_message=str(errors)
        )
```

### 3. Circuit Breaker → DLQ

```python
from kntgraph.resilience.circuit_breaker import CircuitBreakerError

cb = get_circuit_breaker("external_api")

async def call_with_dlq(event):
    result = await cb.call(external_api.process, event.data)
    
    if result.is_err():
        await store.move_to_dlq(
            event,
            reason=DLQReason.CIRCUIT_BREAKER_OPEN,
            error_message=str(result.err())
        )
```

---

## Best Practices

### ✅ Faça

```python
# Retry antes de DLQ
@retry_with_backoff(max_attempts=3)
async def process(event):
    ...

# Log ao mover para DLQ
logger.warning("Event moved to DLQ", event_id=event.event_id, reason=reason)

# Reprocessamento periódico
async def dlq_worker():
    while True:
        await reprocess_failed()
        await asyncio.sleep(60)

# Purge periódico
await dlq.purge(older_than=datetime.now() - timedelta(days=30))
```

### ❌ Não Faça

```python
# ❌ Mover para DLQ sem retry
await store.move_to_dlq(event, reason=...)  # Sem retry!

# ❌ Ignorar DLQ
# Eventos acumulam sem reprocessamento

# ❌ Manter eventos indefinidamente
# DLQ cresce infinitamente

# ❌ DLQ como logging
# Use para eventos recuperáveis, não como log
```

---

## Configuração

```python
# DLQ Settings
DLQ = {
    "max_size": 1000000,        # 1M eventos
    "retention_days": 30,       # Remove após 30 dias
    "reprocess_interval": 60,   # Reprocessa a cada 60s
    "alert_threshold": 100,     # Alerta se > 100 eventos
    "poison_pill_threshold": 10 # Alerta se > 10 poison pills
}
```

---

## Troubleshooting

### DLQ Crescendo Rapidamente

**Causa**: Taxa de falhas > taxa de reprocessamento

**Solução**:
1. Investigue causa das falhas (logs)
2. Aumente frequência de reprocessamento
3. Adicione mais workers

### Poison Pills Recorrentes

**Causa**: Evento corrompido ou bug no código

**Solução**:
1. Analise evento na DLQ
2. Se corrompido → descarte
3. Se bug → corrija e reprocessa

### Reprocessamento Falha Sempre

**Causa**: Problema sistêmico não resolvido

**Solução**:
1. Pause reprocessamento
2. Corrija causa raiz
3. Reprocessa manualmente

---

## Recursos

- [Event Store](event_store.md)
- [Resilience Patterns](resilience.md)
- [Retry](retry.md)
- [Circuit Breaker](circuit_breaker.md)
