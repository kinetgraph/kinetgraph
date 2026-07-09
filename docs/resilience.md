<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Padrões de Resiliência

O FMH implementa padrões de resiliência para tolerância a falhas.

---

## Visão Geral

```
┌─────────────────────────────────────────────────┐
│           Resilience Patterns                    │
├─────────────────────────────────────────────────┤
│  Circuit Breaker  → Previne cascata de falhas   │
│  Retry            → Tentativas automáticas      │
│  Bulkhead         → Isolamento de recursos      │
│  Timeout          → Limite de tempo             │
│  Fallback         → Plano B                     │
└─────────────────────────────────────────────────┘
```

---

## Circuit Breaker

### Problema

Serviço indisponível causa cascata de falhas:

```
Client → Serviço A (lento) → Serviço B (lento) → Timeout
   ↓
Múltiplas requisições travadas
   ↓
Sistema todo falha
```

### Solução: Circuit Breaker

```python
from kntgraph.resilience.circuit_breaker import get_circuit_breaker

cb = get_circuit_breaker(
    "llm_service",
    failure_threshold=5,      # Abre após 5 falhas
    recovery_timeout=30       # Testa recuperação em 30s
)

# Uso
result = await cb.call(llm.chat, prompt)

if result.is_ok():
    response = result.unwrap()
else:
    # Circuit breaker aberto ou falha
    print(f"Erro: {result.err()}")
```

### Estados

```
         [CLOSED]
         Normal operation
              │
              │ 5 falhas
              ▼
         [OPEN]
         Rejeita chamadas
              │
              │ 30s timeout
              ▼
        [HALF-OPEN]
        Testa recuperação
         /        \
    Sucesso      Falha
       │           │
       ▼           ▼
  [CLOSED]     [OPEN]
```

### Monitoramento

```python
from kntgraph.resilience.circuit_breaker import get_all_breakers

for name, cb in get_all_breakers().items():
    state = cb.get_state()
    print(f"{name}: {state['state']}")
    print(f"  Falhas: {state['failure_count']}")
    print(f"  Sucessos: {state['success_count']}")
```

---

## Retry com Backoff

### Problema

Falhas transitórias (rede, timeout) deveriam ser temporárias.

### Solução: Retry Exponencial

```python
from kntgraph.resilience.retry import retry_with_backoff

@retry_with_backoff(
    max_attempts=3,
    base_delay=2.0,    # 2s, 4s, 8s...
    max_delay=30.0
)
async def redis_get(key):
    return await redis.get(key)

# Uso
result = await redis_get("user:123")
```

### Backoff Curve

```
Tentativa 1: 0s (imediata)
Tentativa 2: 2s
Tentativa 3: 4s
Tentativa 4: 8s
Tentativa 5: 16s
Tentativa 6: 30s (max)
```

### Retry Manual

```python
from kntgraph.resilience.retry import retry_async

result = await retry_async(
    redis.get,
    "key",
    max_attempts=3,
    base_delay=2.0
)
```

### Configs Predefinidas

```python
from kntgraph.resilience.retry import (
    retry_fast,    # 2 attempts, 1s base
    retry_normal,  # 3 attempts, 2s base
    retry_slow     # 5 attempts, 3s base
)

@retry_fast.decorate
async def quick_operation():
    ...
```

---

## Bulkhead

### Problema

Um serviço lento consome todos recursos:

```
Thread pool: [====X====X====X====]
                  ↑
           Todas threads bloqueadas
```

### Solução: Bulkhead (Isolamento)

```python
from kntgraph.resilience.bulkhead import Bulkhead

# Pool isolado para LLM
llm_bulkhead = Bulkhead(
    "llm_pool",
    max_concurrent=10,     # Max 10 chamadas simultâneas
    max_queue_size=50      # Max 50 na fila
)

# Uso
result = await llm_bulkhead.execute(llm.chat, prompt)
```

### Isolamento

```
┌─────────────────────────────────┐
│      Bulkheads (Pools)          │
├──────────┬──────────┬───────────┤
│   LLM    │  Redis   │   HTTP    │
│   (10)   │   (20)   │   (50)    │
└──────────┴──────────┴───────────┘
```

### Monitoramento

```python
stats = llm_bulkhead.get_stats()
print(f"Concorrentes: {stats['current_concurrent']}")
print(f"Na fila: {stats['queue_size']}")
print(f"Rejeitados: {stats['rejected_count']}")
```

---

## Timeout

### Problema

Operação trava indefinidamente.

### Solução: Timeout

```python
from kntgraph.resilience.timeout import with_timeout

async def slow_operation():
    await asyncio.sleep(60)  # Lento!

# Timeout de 10s
result = await with_timeout(
    slow_operation,
    timeout_seconds=10,
    operation_name="document_validation"
)
```

### Retry com Timeout

```python
from kntgraph.resilience.timeout import with_timeout
from kntgraph.resilience.retry import retry_with_backoff

@retry_with_backoff(max_attempts=3)
async def operation_with_retry():
    return await with_timeout(
        external_api.call,
        timeout_seconds=5
    )
```

---

## Fallback

### Problema

Serviço primário falha, não há plano B.

### Solução: Fallback

```python
from kntgraph.resilience.fallback import fallback

@fallback(
    primary=llm.analyze,
    fallback_fn=heuristic_rules.analyze,
    fallback_on=[TimeoutError, ConnectionError]
)
async def analyze_document(doc):
    pass

# Uso: Tenta LLM, se falhar usa regras heurísticas
result = await analyze_document(document)
```

### Fallback em Cache

```python
from kntgraph.resilience.fallback import fallback_with_cache

@fallback_with_cache(
    primary=api.get_data,
    cache_fn=cache.get,
    cache_set_fn=cache.set,
    ttl=300  # 5 minutos
)
async def get_user_data(user_id):
    pass
```

### Fallback Chain

```python
from kntgraph.resilience.fallback import fallback_chain

chain = fallback_chain([
    ("primary", api_v1.get_data),
    ("secondary", api_v2.get_data),
    ("cache", cache.get),
    ("default", lambda: default_data())
])

result = await chain.execute()
```

---

## Composição de Padrões

### Circuit Breaker + Retry + Fallback

```python
from kntgraph.resilience.circuit_breaker import get_circuit_breaker
from kntgraph.resilience.retry import retry_with_backoff
from kntgraph.resilience.fallback import fallback

cb = get_circuit_breaker("external_api")

@fallback(
    primary=cb.call,
    fallback_fn=default_response,
    fallback_on=[CircuitBreakerError]
)
@retry_with_backoff(max_attempts=3)
async def robust_operation():
    return await external_api.call()

# Uso
result = await robust_operation()
```

### Bulkhead + Timeout

```python
from kntgraph.resilience.bulkhead import Bulkhead
from kntgraph.resilience.timeout import with_timeout

bulkhead = Bulkhead("api_pool", max_concurrent=20)

async def api_call_with_timeout():
    return await with_timeout(
        external_api.call,
        timeout_seconds=5
    )

result = await bulkhead.execute(api_call_with_timeout)
```

---

## Exemplo: Sistema de Validação Resiliente

```python
from kntgraph.core.world import World
from kntgraph.core.event import AgentEvent
from kntgraph.resilience.circuit_breaker import get_circuit_breaker
from kntgraph.resilience.retry import retry_with_backoff
from kntgraph.resilience.timeout import with_timeout

cb_llm = get_circuit_breaker("llm_service")
cb_redis = get_circuit_breaker("redis_service")

@retry_with_backoff(max_attempts=3, base_delay=1.0)
async def validate_with_llm(doc_data):
    return await with_timeout(
        cb_llm.call(llm.analyze, doc_data),
        timeout_seconds=10
    )

async def document_validation_system(world: World) -> World:
    new_agents = {}
    
    for agent_id, agent in world.query_agents(DocumentComponent):
        doc = agent.components["document"]
        
        try:
            # Validação resiliente
            result = await validate_with_llm(doc.extracted_data)
            
            if result.is_ok():
                event = AgentEvent.create(
                    "document.validated",
                    agent_id,
                    {"validation_result": result.unwrap()}
                )
                agent = agent.emit(event).unwrap()
            else:
                # Circuit breaker aberto ou erro
                event = AgentEvent.create(
                    "document.validation_failed",
                    agent_id,
                    {"error": str(result.err())}
                )
                agent = agent.emit(event).unwrap()
        
        except Exception as e:
            # Fallback: validação básica
            event = AgentEvent.create(
                "document.validated_fallback",
                agent_id,
                {"fallback_reason": str(e)}
            )
            agent = agent.emit(event).unwrap()
        
        new_agents[agent_id] = agent
    
    return world.with_agents(Map(new_agents))
```

---

## Monitoramento

### Logs Estruturados

```python
import structlog

logger = structlog.get_logger()

# Circuit breaker
logger.info(
    "Circuit breaker state changed",
    name="llm_service",
    old_state="closed",
    new_state="open",
    failure_count=5
)

# Retry
logger.warning(
    "Retry attempted",
    operation="redis_get",
    attempt=2,
    max_attempts=3,
    delay=2.0
)
```

### Métricas

```python
# Prometheus example
from prometheus_client import Counter, Histogram

CB_STATE = Counter('fmh_circuit_breaker_state', 'CB state', ['name', 'state'])
RETRY_COUNT = Histogram('fmh_retry_attempts', 'Retry attempts', ['operation'])

# Incrementa
CB_STATE.labels(name="llm_service", state="open").inc()
```

---

## Best Practices

### ✅ Faça

```python
# Circuit breaker por serviço
cb_llm = get_circuit_breaker("llm")
cb_redis = get_circuit_breaker("redis")
cb_http = get_circuit_breaker("http")

# Retry apenas para falhas transitórias
@retry_with_backoff(retry_on=(TimeoutError, ConnectionError))
async def operation():
    ...

# Timeout sempre em I/O
result = await with_timeout(db.query, timeout_seconds=5)

# Fallback para casos críticos
@fallback(primary=api_call, fallback_fn=cache_get)
async def get_data():
    ...
```

### ❌ Não Faça

```python
# ❌ Circuit breaker único para tudo
cb = get_circuit_breaker("everything")  # Ruim!

# ❌ Retry infinito
@retry_with_backoff(max_attempts=999)  # Ruim!
async def operation():
    ...

# ❌ Sem timeout
result = await external_api.call()  # Pode travar!

# ❌ Retry em erro não transitório
@retry_with_backoff(retry_on=ValidationError)  # Não ajuda!
async def operation():
    ...
```

---

## Configuração Recomendada

```python
# Circuit Breaker
CIRCUIT_BREAKER = {
    "failure_threshold": 5,
    "recovery_timeout": 30,
    "half_open_max_calls": 3
}

# Retry
RETRY = {
    "max_attempts": 3,
    "base_delay": 2.0,
    "max_delay": 30.0,
    "retry_on": (TimeoutError, ConnectionError)
}

# Timeout
TIMEOUT = {
    "default": 10,
    "llm": 30,
    "http": 5,
    "redis": 2
}

# Bulkhead
BULKHEAD = {
    "llm": {"max_concurrent": 10, "max_queue": 50},
    "redis": {"max_concurrent": 50, "max_queue": 200},
    "http": {"max_concurrent": 100, "max_queue": 500}
}
```

---

## Recursos

- [Circuit Breaker](circuit_breaker.md)
- [Retry](retry.md)
- [Bulkhead](bulkhead.md)
- [Timeout](timeout.md)
- [Fallback](fallback.md)
