<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ECS (Entity-Component-System)

O FMH usa o padrão ECS para modelagem de agentes autônomos.

---

## Visão Geral

```
┌─────────────────────────────────────────────────┐
│              ECS Architecture                    │
├─────────────────────────────────────────────────┤
│  Entity (AgentState)                            │
│    - ID único (UUIDv5 determinístico)           │
│    - Componentes (dados)                        │
│    - Eventos pendentes                          │
├─────────────────────────────────────────────────┤
│  Components (dados puros)                       │
│    - DocumentComponent                          │
│    - ClientContextComponent                     │
│    - NotificationComponent                      │
│    - WorkflowComponent                          │
├─────────────────────────────────────────────────┤
│  Systems (lógica)                               │
│    - Document Validation System                 │
│    - Notification System                        │
│    - Workflow System                            │
└─────────────────────────────────────────────────┘
```

---

## Componentes

### O que são?

Componentes são **dados puros** (sem lógica) que descrevem aspectos de um agente.

```python
from kntgraph.core.component import Component
from pydantic import ConfigDict

class DocumentComponent(Component):
    model_config = ConfigDict(frozen=True)  # Imutável
    
    document_type: str      # "nota_fiscal", "darf", "holerite"
    document_id: str
    status: str            # "received", "validating", "validated"
    extracted_data: dict
    validation_errors: list[str] = []
```

### Componentes Built-in

```python
# Contexto do cliente
class ClientContextComponent(Component):
    client_id: str          # CNPJ/CPF
    client_name: str
    client_type: str        # "pessoa_fisica", "pessoa_juridica"
    segment: str            # "servicos", "comercio"
    risk_level: str         # "low", "medium", "high"

# Tarefa em execução
class TaskComponent(Component):
    task_id: str
    task_type: str          # "coleta_docs", "validacao"
    status: str             # "pending", "in_progress", "completed"
    due_date: date
    priority: int           # 1-5 (1 = mais urgente)

# Notificação pendente
class NotificationComponent(Component):
    notification_type: str  # "vencimento", "pendencia"
    recipient: str          # email, phone
    message: str
    scheduled_date: datetime
    sent: bool = False

# Workflow em execução
class WorkflowComponent(Component):
    workflow_type: str      # "onboarding_cliente"
    current_step: int
    total_steps: int
    steps_completed: list[int]
```

### Criando Componentes Customizados

```python
class PriorityComponent(Component):
    """Prioridade baseada em SLA."""
    
    sla_deadline: datetime
    urgency_level: int = 1  # 1-5
    client_tier: str = "standard"  # "vip", "standard", "basic"
```

---

## Entities (Agentes)

### AgentState

Entidade = ID + Componentes + Eventos

```python
from kntgraph.core.world import AgentState

# Cria com factory method (ID determinístico)
agent = AgentState.create(
    agent_type="service",
    tenant_id="123456789",
    unique_key="NF-001",
    status="initialized",
    components={
        "document": DocumentComponent(
            document_type="nota_fiscal",
            document_id="NF-001",
            extracted_data={"valor_total": 1500.50}
        ),
        "client": ClientContextComponent(
            client_id="123456789",
            client_name="Empresa XYZ",
            client_type="pessoa_juridica"
        )
    }
)

print(f"Agent ID: {agent.agent_id}")
print(f"Componentes: {list(agent.components.keys())}")
```

### ID Determinístico (UUIDv5)

```python
# Mesmos inputs → mesmo ID (idempotência)
agent1 = AgentState.create("service", "123456789", "NF-001")
agent2 = AgentState.create("service", "123456789", "NF-001")

assert agent1.agent_id == agent2.agent_id  # ✅ True
```

---

## Sistemas

### O que são?

Sistemas são **funções async** que processam agentes e retornam novo estado.

```python
from kntgraph.core.world import World
from immutables import Map

async def document_validation_system(world: World) -> World:
    """Valida documentos de agentes."""
    new_agents = {}
    
    for agent_id, agent in world.query_agents(DocumentComponent):
        doc = agent.components["document"]
        
        # Validação
        errors = validate_document(doc)
        
        if errors:
            # Emite evento de rejeição
            event = AgentEvent.create(
                event_type="document.rejected",
                agent_id=agent_id,
                data={"errors": errors}
            )
            agent = agent.emit(event).unwrap()
        else:
            # Emite evento de validação
            event = AgentEvent.create(
                event_type="document.validated",
                agent_id=agent_id,
                data={"document_id": doc.document_id}
            )
            agent = agent.emit(event).unwrap()
        
        new_agents[agent_id] = agent
    
    return world.with_agents(Map(new_agents))
```

### Sistemas Built-in

```python
# Validação de documentos
async def document_validation_system(world: World) -> World

# Envio de notificações
async def notification_system(world: World) -> World

# Execução de workflows
async def workflow_system(world: World) -> World

# Cálculo de prioridade
async def priority_system(world: World) -> World
```

### Composição de Sistemas

```python
from kntgraph.core.world import pipe_async

# Pipeline de processamento
world = await pipe_async(
    world,
    document_validation_system,
    priority_system,
    notification_system
)
```

---

## World

### Estado Global

World contém todos agentes:

```python
from immutables import Map

# World vazio
world = World.empty()

# Adiciona agentes
world = world.with_agents(Map({
    agent1.agent_id: agent1,
    agent2.agent_id: agent2,
}))

print(f"Agentes: {len(world.agents)}")
```

### Query System

Filtra agentes por componentes:

```python
# AND logic (todos componentes)
for agent_id, agent in world.query_agents(DocumentComponent, ClientContextComponent):
    # Agentes com documento E cliente
    ...

# OR logic (qualquer componente)
for agent_id, agent in world.or_query.any_of(DocumentComponent, TaskComponent):
    # Agentes com documento OU tarefa
    ...

# Com filtro customizado
for agent_id, agent in world.query_agents(DocumentComponent).filter(
    lambda a: a.components["document"].status == "received"
):
    # Apenas documentos recebidos
    ...

# Utilitários
count = world.query_agents(DocumentComponent).count()
first = world.query_agents(DocumentComponent).first()
is_empty = world.query_agents(DocumentComponent).is_empty()
```

---

## Ciclo de Vida

```
1. Cria Agente
   ↓
2. Adiciona Componentes
   ↓
3. Adiciona ao World
   ↓
4. Sistemas Processam
   ↓
5. Eventos Emitidos
   ↓
6. Eventos Persistidos
   ↓
7. Estado Reconstruído
```

### Exemplo Completo

```python
import asyncio
from immutables import Map
from datetime import datetime
from kntgraph.core.world import World, pipe_async
from kntgraph.core.event import AgentEvent, correlation_middleware

async def main():
    with correlation_middleware.context_manager({"source": "example"}) as ctx:
        # 1. Cria agente
        agent = AgentState.create(
            agent_type="service",
            tenant_id="123456789",
            unique_key="NF-001",
            components={
                "document": DocumentComponent(
                    document_type="nota_fiscal",
                    document_id="NF-001",
                    status="received",
                    extracted_data={"valor": 1500.50}
                )
            }
        )
        
        # 2. Cria world com agente
        world = World.empty().with_agents(Map({
            agent.agent_id: agent
        }))
        
        # 3. Processa com sistemas
        world = await pipe_async(
            world,
            document_validation_system,
            notification_system
        )
        
        # 4. Verifica eventos gerados
        for agent_id, agent in world.agents.items():
            print(f"Eventos pendentes: {len(agent.pending_events)}")
            for event in agent.pending_events:
                print(f"  - {event.event_type}")

asyncio.run(main())
```

---

## Best Practices

### ✅ Faça

```python
# Componentes pequenos e focados
class DocumentComponent(Component):
    document_id: str
    status: str

# Sistemas puros (sem side effects)
async def validation_system(world: World) -> World:
    # Apenas transforma estado
    ...

# Query eficiente
for agent in world.query_agents(DocumentComponent):
    # Processa apenas agentes relevantes
    ...
```

### ❌ Não Faça

```python
# ❌ Componentes gigantes
class MegaComponent(Component):
    # 50+ fields
    ...

# ❌ Sistema com side effects
async def bad_system(world: World) -> World:
    await send_email()  # Side effect!
    ...

# ❌ Iterar todos agentes manualmente
for agent_id, agent in world.agents.items():
    if "document" in agent.components:  # Ineficiente
        ...
```

---

## Performance

### Otimizações

- **Query System**: Lazy evaluation
- **Imutabilidade**: Sem locks necessários
- **Map imutável**: O(1) para leitura/escrita

### Benchmarks

| Operação | Tempo |
|----------|-------|
| Criar agente | ~0.1ms |
| Query 1000 agentes | ~1ms |
| Sistema processa 100 agentes | ~10ms |

---

## Exemplos

### Agente de Validação

```python
agent = ServiceAgentState.create(
    tenant_id="123456789",
    unique_key="NF-001",
    components={
        "document": DocumentComponent(
            document_type="nota_fiscal",
            document_id="NF-001"
        ),
        "priority": PriorityComponent(
            sla_deadline=datetime.now() + timedelta(hours=2),
            urgency_level=2
        )
    }
)
```

### Sistema com Retry

```python
from kntgraph.resilience.retry import retry_with_backoff

@retry_with_backoff(max_attempts=3)
async def external_validation(doc_data):
    # Chama API externa
    ...
```

### Sistema com Circuit Breaker

```python
from kntgraph.resilience.circuit_breaker import get_circuit_breaker

cb = get_circuit_breaker("llm_service")

async def ai_validation_system(world: World) -> World:
    for agent_id, agent in world.query_agents(DocumentComponent):
        result = await cb.call(llm.analyze, agent.components["document"])
        ...
```

---

## Recursos

- [World & AgentState](world.md)
- [Componentes](components.md)
- [Sistemas](systems.md)
- [Query System](query.md)
