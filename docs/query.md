<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Query System

Consulta de agentes por componentes de interesse.

---

## Visão Geral

O Query System permite filtrar agentes baseado nos componentes que possuem.

```python
# AND logic (todos componentes)
for agent_id, agent in world.query_agents(DocumentComponent, ClientContextComponent):
    # Agentes com documento E cliente
    ...

# OR logic (qualquer componente)
for agent_id, agent in world.or_query.any_of(DocumentComponent, TaskComponent):
    # Agentes com documento OU tarefa
    ...
```

---

## Query por Componente

### Único Componente

```python
# Filtra agentes com DocumentComponent
for agent_id, agent in world.query_agents(DocumentComponent):
    doc = agent.components["document"]
    print(f"Documento: {doc.document_id}")
```

### Múltiplos Componentes (AND)

```python
# Filtra agentes com DocumentComponent E ClientContextComponent
for agent_id, agent in world.query_agents(DocumentComponent, ClientContextComponent):
    doc = agent.components["document"]
    client = agent.components["client"]
    print(f"{doc.document_id} - {client.client_name}")
```

---

## OR Query

### Qualquer Componente

```python
# Agentes com DocumentComponent OU TaskComponent
for agent_id, agent in world.or_query.any_of(DocumentComponent, TaskComponent):
    print(f"Agente {agent_id} tem documento ou tarefa")
```

### Múltiplos Componentes (OR)

```python
# Agentes com DocumentComponent OU ClientComponent OU TaskComponent
for agent_id, agent in world.or_query.any_of(
    DocumentComponent,
    ClientContextComponent,
    TaskComponent
):
    ...
```

---

## Filtros Customizados

### Filter Simples

```python
# Apenas documentos do tipo "nota_fiscal"
for agent_id, agent in world.query_agents(DocumentComponent).filter(
    lambda a: a.components["document"].document_type == "nota_fiscal"
):
    print(f"NF: {agent.components['document'].document_id}")
```

### Filtros Encadeados

```python
# Documentos válidos de clientes VIP
for agent_id, agent in world.query_agents(
    DocumentComponent,
    ClientContextComponent
).filter(
    lambda a: a.components["document"].status == "validated"
).filter(
    lambda a: "priority" in a.components and a.components["priority"].client_tier == "vip"
):
    print(f"Documento válido de VIP: {agent.components['document'].document_id}")
```

---

## Métodos Utilitários

### Count

```python
# Conta agentes com documento
count = world.query_agents(DocumentComponent).count()
print(f"{count} agentes com documentos")
```

### First

```python
# Primeiro agente com notificação
result = world.query_agents(NotificationComponent).first()

if result:
    agent_id, agent = result
    print(f"Primeiro: {agent_id}")
else:
    print("Nenhum agente com notificação")
```

### Is Empty

```python
# Verifica se não há agentes com workflow
if world.query_agents(WorkflowComponent).is_empty():
    print("Nenhum workflow em andamento")
```

### To List

```python
# Converte para lista
agents_list = world.query_agents(DocumentComponent).to_list()

# [(agent_id, agent), ...]
for agent_id, agent in agents_list:
    ...
```

---

## Get Agent por ID

```python
# Obtém agente específico
agent = world.get_agent("agent-123")

if agent:
    print(f"Status: {agent.status}")
    print(f"Componentes: {list(agent.components.keys())}")
else:
    print("Agente não encontrado")
```

---

## Exemplos Práticos

### Sistema de Validação

```python
async def document_validation_system(world: World) -> World:
    """Valida apenas agentes com documentos."""
    new_agents = {}
    
    # Query eficiente - apenas agentes relevantes
    for agent_id, agent in world.query_agents(DocumentComponent):
        doc = agent.components["document"]
        
        # Validação
        errors = await validate(doc)
        
        if errors:
            event = AgentEvent.create("document.rejected", agent_id, {"errors": errors})
            agent = agent.emit(event).unwrap()
        else:
            event = AgentEvent.create("document.validated", agent_id, {})
            agent = agent.emit(event).unwrap()
        
        new_agents[agent_id] = agent
    
    return world.with_agents(Map(new_agents))
```

### Sistema de Notificação

```python
async def notification_system(world: World) -> World:
    """Envia notificações agendadas."""
    now = datetime.utcnow()
    new_agents = {}
    
    for agent_id, agent in world.query_agents(NotificationComponent):
        notification = agent.components["notification"]
        
        # Envia se chegou hora e não foi enviado
        if notification.scheduled_date <= now and not notification.sent:
            await send_notification(notification)
            
            event = AgentEvent.create("notification.sent", agent_id, {})
            agent = agent.emit(event).unwrap()
        
        new_agents[agent_id] = agent
    
    return world.with_agents(Map(new_agents))
```

### Prioridade por Cliente

```python
async def priority_system(world: World) -> World:
    """Calcula prioridade baseada em SLA."""
    new_agents = {}
    now = datetime.utcnow()
    
    # Apenas agentes com prioridade E cliente
    for agent_id, agent in world.query_agents(PriorityComponent, ClientContextComponent):
        priority = agent.components["priority"]
        client = agent.components["client"]
        
        # Calcula urgência
        hours_remaining = (priority.sla_deadline - now).total_seconds() / 3600
        
        if hours_remaining < 2:
            urgency = 1  # Crítico
        elif hours_remaining < 24:
            urgency = 2  # Alto
        else:
            urgency = 3  # Normal
        
        # Ajusta por tier
        if client.client_tier == "vip":
            urgency = max(1, urgency - 1)
        
        new_agents[agent_id] = agent
    
    return world.with_agents(Map(new_agents))
```

### Dashboard de Status

```python
def get_dashboard_stats(world: World) -> dict:
    """Estatísticas para dashboard."""
    return {
        "total_agents": len(world.agents),
        "with_documents": world.query_agents(DocumentComponent).count(),
        "with_notifications": world.query_agents(NotificationComponent).count(),
        "with_workflows": world.query_agents(WorkflowComponent).count(),
        "validated_docs": len([
            a for _, a in world.query_agents(DocumentComponent)
            if a.components["document"].status == "validated"
        ]),
        "pending_notifications": len([
            a for _, a in world.query_agents(NotificationComponent)
            if not a.components["notification"].sent
        ])
    }
```

---

## Performance

### Lazy Evaluation

```python
# ✅ Lazy - não processa todos agentes
query = world.query_agents(DocumentComponent)

# Processa apenas quando itera
for agent_id, agent in query:
    if some_condition:
        break  # Para early
```

### vs Iteração Manual

```python
# ❌ Ineficiente - itera todos
for agent_id, agent in world.agents.items():
    if "document" in agent.components:  # Check manual
        ...

# ✅ Eficiente - query otimizada
for agent_id, agent in world.query_agents(DocumentComponent):
    ...
```

### Benchmarks

| Operação | Tempo (1000 agentes) |
|----------|---------------------|
| `world.agents.items()` | ~1ms |
| `query_agents(Component)` | ~0.5ms |
| `query_agents(C1, C2)` | ~0.8ms |
| `query.filter(...)` | ~0.3ms |

---

## Composição

### Query + Filter + Count

```python
# Conta documentos pendentes de validação
pending_count = world.query_agents(DocumentComponent).filter(
    lambda a: a.components["document"].status == "received"
).count()
```

### Query + First + Process

```python
# Processa primeiro documento pendente
result = world.query_agents(DocumentComponent).filter(
    lambda a: a.components["document"].status == "received"
).first()

if result:
    agent_id, agent = result
    await process_document(agent.components["document"])
```

---

## Boas Práticas

### ✅ Faça

```python
# Query específica
for agent in world.query_agents(DocumentComponent):
    ...

# Filtros encadeados
for agent in world.query_agents(DocumentComponent, ClientContextComponent).filter(
    lambda a: a.components["document"].status == "validated"
):
    ...

# Utilitários
count = world.query_agents(NotificationComponent).count()
first = world.query_agents(WorkflowComponent).first()
```

### ❌ Não Faça

```python
# ❌ Iteração manual com check
for agent_id, agent in world.agents.items():
    if "document" in agent.components:  # Ruim
        ...

# ❌ Múltiplas queries desnecessárias
docs = world.query_agents(DocumentComponent).to_list()
clients = world.query_agents(ClientComponent).to_list()
# Em vez de query composta
for agent in world.query_agents(DocumentComponent, ClientComponent):
    ...
```

---

## Recursos

- [ECS Pattern](ecs.md)
- [World & AgentState](world.md)
- [Systems](systems.md)
