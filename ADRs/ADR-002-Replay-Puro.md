<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-002: Replay Canônico de Eventos como Única Forma de Construir o World

**Status:** Aceito
**Data:** 06 de junho de 2026
**Versão:** 1.0
**Autores:** Equipe de Arquitetura FMH
**Relacionado a:** [ADR-001](./ADR-001-Arquitetura.md)

---

## 1. Contexto

No modelo v1.x, o `World` era um Pydantic mutável. Tinha um
`ArchetypeStorage` interno mutável e podia ser construído de
várias formas:

- `World.empty()` — vazio
- `World.with_agents(Map)` — a partir de um snapshot de
  agentes
- `World.load_from_repository()` — a partir de FalkorDB
- Múltiplas chamadas `world.emit(...)` que mutavam o `outbox`

A consequência é que existiam **múltiplas fontes de verdade**:

1. O `outbox` do World (eventos pendentes)
2. O `EventStore` em Redis (eventos já publicados)
3. O `repository` FalkorDB (agentes materializados)
4. O próprio `ArchetypeStorage` do World (estado em memória)

Essas fontes podiam divergir. Replay era difícil (não-determinístico).
Auditoria era custosa (múltiplos lugares para conferir).

---

## 2. Decisão

Adotar a regra: **O `World` é uma função pura
`Sequence[Event] → WorldState`. Existe apenas uma forma de
construí-lo: `World.fold(events)` (e `World.empty()` como caso
base).**

```python
@dataclass(frozen=True, slots=True)
class AgentView:
    agent_id: str
    components: Mapping[str, Any]   # derivados do último domain event
    operational_phase: OperationalPhase
    operational_at: datetime | None
    domain_phase: str | None
    domain_at: datetime | None
    last_event_id: str | None
    last_event_at: datetime | None


class World:
    tick: int
    storage: ArchetypeStorage
    views: dict[str, AgentView]

    @classmethod
    def empty(cls, tick: int = 0) -> "World":
        ...

    @classmethod
    def fold(
        cls,
        events: Sequence[Event],
        *,
        projection: Callable = project_default,
        tick: int | None = None,
    ) -> "World":
        ...
```

### 2.1 O que sai

- `World.with_agents(Map)` — substituído por `World.fold`
- `World.emit(entity_id, event)` — sistemas retornam eventos
- `World.drain_outbox()` — sem outbox no World
- `World.save_to_repository()` / `load_from_repository()` —
  World é volátil por design
- `World.next_tick()` — tick é parte do fold

### 2.2 O que entra

- `World.fold(events, projection=..., tick=...)` — **única**
  forma de materializar o mundo
- `World.with_event(event)` — helper para adicionar 1 evento
  (apenas para testes; em produção o runner faz fold completo)
- `project_default(events) → dict[agent_id, AgentView]` —
  projeção default (último evento vence)

### 2.3 Projeção Default

```python
def project_default(events: Sequence[Event]) -> dict[str, AgentView]:
    """
    Last-event-wins projection:

    - Para cada evento do agente, atualiza last_event_*
    - Para lifecycle: operational_phase = event_type
    - Para domain: components = event.data, domain_phase = event_type
    """
    views: dict[str, AgentView] = {}

    for e in events:
        prev = views.get(e.agent_id) or AgentView(agent_id=e.agent_id)

        if e.event_class == "lifecycle":
            views[e.agent_id] = AgentView(
                agent_id=e.agent_id,
                components=prev.components,
                operational_phase=_phase_from_event(e.event_type),
                operational_at=e.timestamp,
                ...
            )
        else:  # "domain"
            views[e.agent_id] = AgentView(
                agent_id=e.agent_id,
                components={e.event_type: dict(e.data)},
                operational_phase=prev.operational_phase,
                domain_phase=e.event_type,
                ...
            )

    return views
```

Aplicações podem fornecer projeções mais ricas (agregações,
joins, snapshots intermediários). A assinatura é
`Sequence[Event] → dict[agent_id, AgentView]`.

### 2.4 Idempotência do `event_id`

Para que o replay seja idempotente, o `event_id` é
determinístico:

```python
def generate_deterministic_event_id(
    causation_id: UUID, event_type: str, data: Mapping
) -> UUID:
    payload_str = json.dumps(dict(data), sort_keys=True)
    return uuid5(KNT_EVENT_NAMESPACE, f"{causation_id}|{event_type}|{payload_str}")
```

Sistemas que respondem a um evento devem propagar
`causation_id = event.event_id` do evento recebido. Isso
garante que re-executar o sistema no mesmo input produz o
mesmo `event_id` → o `EventLog.append` é no-op → o replay
não duplica.

---

## 3. Consequências

### Positivas

- ✅ **Replay determinístico**: dado o stream, o mundo é
  unicamente determinado. Snapshots do mundo são serializáveis.
- ✅ **Auditoria trivial**: o stream é a única fonte; o mundo é
  sempre consistente com ele.
- ✅ **Testes simples**: sistemas são funções puras; testes
  constroem World em memória, sem Redis.
- ✅ **Idempotência**: rodar o runner N vezes produz o mesmo
  estado.

### Negativas

- ⚠️ **Replay completo por tick**: O(N) onde N = número de
  eventos até o tick. Para deployments grandes, sharding
  por tenant (F8) e snapshots incrementais.
- ⚠️ **Projeção default é simples**: "último evento vence"
  para components. Aplicações com agregações não triviais
  precisam fornecer projeção custom.
- ⚠️ **Sistemas devem cooperar com idempotência**: propagar
  `causation_id` corretamente.

### Mitigações

| Problema | Mitigação |
|----------|-----------|
| O(N) replay | Snapshots parciais em F8 (FalkorDB como cache) |
| Projeção simples | API extensível: `World.fold(projection=...)` |
| Idempotência | Helper `Event.create(causation_id=event.event_id)` |

---

## 4. Garantias do Modelo

Dado um stream `S` e uma projeção `P`:

1. `World.fold(S, projection=P)` é uma **função pura**:
   `(S1 == S2) → (W1 == W2)`.
2. Rodar o runner em `S` produz um novo stream `S'` tal que
   `World.fold(S ∪ S', projection=P) == World.fold(S ∪ sys_k(S), ...)`
   para todos os sistemas `sys_k`.
3. Re-rodar o runner em `S ∪ S'` é no-op (todos os
   `event_id`s já estão no idempotency index).
4. O `tick` do World é determinado pelo chamador (pode ser o
   tick atual, o tick do último evento, ou outro critério).

---

## 5. Exemplo

```python
# Event sourcing puro
events = [
    Event.create(event_type="agent.spawned",     agent_id="nf-001", event_class="lifecycle"),
    Event.create(event_type="document.received", agent_id="nf-001", event_class="domain", data={"xml": "..."}),
    Event.create(event_type="document.validated",agent_id="nf-001", event_class="domain", data={"cnpj": "..."},
                 causation_id=events[1].event_id),
]

world = World.fold(events, tick=3)

print(world.agents["nf-001"].operational_phase)  # "spawned"
print(world.agents["nf-001"].domain_phase)      # "document.validated"
print(world.agents["nf-001"].components["document.validated"])
# {"cnpj": "..."}
```

---

## 6. Referências

- [ADR-001: Arquitetura geral](./ADR-001-Arquitetura.md)
- [ADR-003: Ciclo dual](./ADR-003-Ciclo-Dual.md)
- [Greg Young: Event Sourcing](https://www.youtube.com/watch?v=8JKjvYI-H-Y)
- [Fowler: Event Sourcing](https://martinfowler.com/eaaDev/EventSourcing.html)
