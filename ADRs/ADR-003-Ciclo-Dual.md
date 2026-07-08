<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-003: Ciclo de Vida Dual — Operacional × Domínio

**Status:** Aceito
**Data:** 06 de junho de 2026
**Versão:** 1.0
**Autores:** Equipe de Arquitetura FMH
**Relacionado a:** [ADR-001](./ADR-001-Arquitetura.md), [ADR-002](./ADR-002-Replay-Puro.md)

---

## 1. Contexto

Um agente FMH tem, na prática, **duas vidas** acontecendo em
paralelo:

1. **Vida operacional** (gerida pelo framework):
   - Foi criado?
   - Está processando algo?
   - Está bloqueado esperando I/O?
   - Foi terminado?

2. **Vida de domínio** (gerida pela aplicação):
   - Em que etapa do workflow BPMN está?
   - Foi validado, lançado, pago?
   - Espera aprovação?

Na v1.x, o framework misturava os dois em um único
`AgentState.status: str` (lifecycle) e em componentes
específicos do domínio. A consequência era:

- Status operacional era mutável e podia divergir da
  realidade.
- Replay não reconstruía a fase de domínio (havia que
  manter estado separado).
- Sistemas de domínio precisavam inspecionar vários
  componentes para saber a fase.

---

## 2. Decisão

Adotar **dois eixos ortogonais de ciclo de vida**, codificados
como `event_class` no `Event`:

| Eixo | event_class | Origem | Significado |
|------|-------------|--------|-------------|
| **Operacional** | `"lifecycle"` | Framework | Fase de runtime do agente |
| **Domínio** | `"domain"` | Aplicação | Etapa de negócio do agente |

Ambos viajam no **mesmo Redis Stream** (per-agent). O framework
**não** define as fases de domínio; a aplicação **não** define
as fases operacionais.

### 2.1 Fase Operacional (do framework)

```python
OperationalPhase = Literal[
    "spawned",       # acabou de ser criado
    "idle",          # existe, sem trabalho
    "running",       # um sistema está processando eventos
    "blocked",       # aguardando dependência externa
    "checkpointed",  # pausa controlada (long-running)
    "terminated",    # descontinuado (terminal)
]

TERMINAL_OPERATIONAL: frozenset = frozenset({"terminated"})
```

**Convenção de `event_type` (lifecycle):**
- `agent.spawned`      → `"spawned"`
- `agent.idle`         → `"idle"`
- `agent.running`      → `"running"`
- `agent.blocked`      → `"blocked"`
- `agent.checkpointed` → `"checkpointed"`
- `agent.terminated`   → `"terminated"`

A fase atual é derivada: `last_lifecycle_event.event_type` →
mapeamento → `OperationalPhase`.

### 2.2 Fase de Domínio (da aplicação)

```python
@dataclass(frozen=True, slots=True)
class DomainPhase:
    phase: str          # ex: "received", "validated", "transmitted"
    updated_at: datetime
    reason: str | None  # ex: "missing CNPJ"
```

A aplicação é livre para definir o `event_type` e o
significado das fases. Exemplos para notas fiscais:

| event_type (domain) | phase (str) |
|---------------------|-------------|
| `document.received` | `"received"` |
| `document.validated`| `"validated"` |
| `document.rejected` | `"rejected"` |
| `document.lancada`  | `"lancada"` (lançada na contabilidade) |
| `document.transmitted` | `"transmitted"` (external service) |
| `document.paid`     | `"paid"` |

### 2.3 Mesma Stream, Dois Eixos

```
fmh:agents:{agent_id}:events
  ├── e1: { type: "agent.spawned",      class: "lifecycle" }
  ├── e2: { type: "document.received",  class: "domain" }
  ├── e3: { type: "document.validated", class: "domain" }
  └── e4: { type: "agent.idle",         class: "lifecycle" }
```

Após fold:

```
AgentView(
    agent_id="...",
    operational_phase="idle",      # último lifecycle event (e4)
    domain_phase="document.validated",  # último domain event (e3)
    components={                    # do último domain event
        "document.validated": {...}
    }
)
```

### 2.4 Por que Mesma Stream?

- **Atomicidade**: o ciclo dual é indivisível. Mudar o
  `tick` afeta ambos os eixos simultaneamente.
- **Replay único**: `World.fold` lê uma vez e deriva ambos.
- **Cursor único**: o cursor de replay não precisa
  sincronizar dois streams.
- **Correlação natural**: um evento de domínio pode causar
  um evento de lifecycle (ex: "documento validado" →
  "agent.idle") com `causation_id` apontando entre eles.

### 2.5 Filtros

Sistemas podem filtrar por `event_class`:

```python
async def reactive_validator(world, event):
    if event.event_class != "domain":
        return []  # só nos importam eventos de domínio
    if event.event_type != "document.received":
        return []
    ...
```

Ou, se quiser reagir a lifecycle:

```python
async def spawn_handler(world, event):
    if event.event_class != "lifecycle":
        return []
    if event.event_type != "agent.spawned":
        return []
    ...
```

---

## 3. Consequências

### Positivas

- ✅ **Separação de responsabilidades**: framework cuida do
  operacional, aplicação cuida do domínio.
- ✅ **Replay canônico**: ambos os eixos são derivados dos
  mesmos eventos.
- ✅ **Tipos distintos**: `OperationalPhase` é
  `Literal[Framework]`, `DomainPhase.phase` é `str` (livre).
- ✅ **Filtros simples**: `event.event_class` em
  `if event_class == ...` é explícito e legível.
- ✅ **Múltiplas fases de domínio simultâneas não-violam
  invariantes**: cada agente tem no máximo uma fase
  operacional e uma de domínio.

### Negativas

- ⚠️ **Convenção não enforçada**: o framework não valida
  que `event_class="lifecycle"` só venha com tipos do
  framework. Documentação + lint no CI.
- ⚠️ **Fase de domínio é `str`**: tipos fracos. Aplicações
  devem declarar `Literal[...]` próprio.

### Mitigações

| Problema | Mitigação |
|----------|-----------|
| Convenção violada | Lint / test que rejeita `event_class="lifecycle"` com tipo de domínio |
| `str` fraco em DomainPhase | Aplicação declara `type DomainLiteral = Literal[...]` e converte no boundary |

---

## 4. Exemplo Completo

```python
from fmh_backend.core.event import Event
from fmh_backend.core.lifecycle import OperationalPhase
from fmh_backend.core.world import World

# 1. Aplicação cria um agente (lifecycle)
spawned = Event.create(
    event_type="agent.spawned",
    agent_id="nf-001",
    event_class="lifecycle",
)

# 2. Adaptação externa emite evento de domínio
received = Event.create(
    event_type="document.received",
    agent_id="nf-001",
    event_class="domain",
    data={"xml_b64": "...", "cnpj_emitente": "..."},
)

# 3. Sistema de domínio valida
validated = Event.create(
    event_type="document.validated",
    agent_id="nf-001",
    event_class="domain",
    data={"cnpj": "...", "valor": 1500.0},
    causation_id=received.event_id,
)

# 4. Sistema operacional marca o agente como idle
idle = Event.create(
    event_type="agent.idle",
    agent_id="nf-001",
    event_class="lifecycle",
)

# 5. Replay puro
world = World.fold([spawned, received, validated, idle], tick=4)

assert world.agents["nf-001"].operational_phase == "idle"
assert world.agents["nf-001"].domain_phase == "document.validated"
assert world.agents["nf-001"].components["document.validated"] == {
    "cnpj": "...", "valor": 1500.0
}
```

---

## 5. Referências

- [ADR-001: Arquitetura geral](./ADR-001-Arquitetura.md)
- [ADR-002: Replay canônico](./ADR-002-Replay-Puro.md)
- [BPMN 2.0 Spec](https://www.omg.org/spec/BPMN/2.0/)
- [Workflow patterns](https://workflowpatterns.com/)
