<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-037: Propagação mandatória de `correlation` no `Event.create`

**Status:** Aceito
**Data:** 06 de julho de 2026
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md), [ADR-036](./ADR-036-Tool-Worker-Pattern.md) (follow-up §4.2.5), [AGENTS.md §4.6](../../AGENTS.md)

---

## 1. Contexto

A docstring de `Event.create` (linha 178-185 de `core/event/event.py`) promete:

> *When the caller does not supply a `CorrelationContext`, default
> the `correlation_id` to the event's own `event_id`. This makes
> the entry event of a flow self-identifying: downstream events
> in the same flow (linked by `causation_id`) can match the flow
> by `correlation_id == entry.event_id` without the caller having
> to pin it manually.*

A **promessa** é que a propagação do `correlation_id` é automática ao longo de um flow. A **implementação** (linha 196-198) é:

```python
if correlation is None:
    correlation = CorrelationContext.new(correlation_id=eid)
```

ou seja, **sempre** gera `correlation_id = eid(event)`, **nunca** propaga do `causation_id` event ou do `CorrelationMiddleware.current()`. O resultado é o **bug** documentado em ADR-036 §4.2.5: o `correlation_id` quebra em `tool.requested → tool.<name>.completed` quando o caller não propaga explicitamente.

Reprodução do bug (validado em `core/event/event.py`):

```python
entry = Event.create(
    event_type="user.intent", agent_id="a-1", event_class="domain",
    data={"intent": "get_weather"},
    correlation=correlation_middleware.start(correlation_id=uuid.uuid4()),
)
# entry.correlation.correlation_id = X (flow id)

req = Event.create(
    event_type="tool.weather.requested", agent_id="a-1", event_class="domain",
    data={"tool": "weather"},
    causation_id=entry.event_id,  # aponta para entry
)
# req.correlation.correlation_id = eid(req), != X  # BUG
```

A correcção manual via `correlation=correlation_middleware.continue_from(entry)` funciona, mas **nenhum** dos 16 call sites internos do framework (`WorkerManager`, `ToolAwareSystem.request_tool`, `memory/profile.py`, `memory/continuity/manager.py`, `events/dlq/`, `api/intent_router/`) faz isso hoje.

### 1.1. Por que isto é funcional, não cosmético

A `tool.<name>.completed` (emitida pelo `WorkerManager`) tem `causation_id = tool.requested.event_id` (= `idempotency_key` da tool). Para o sistema ser **auditável** — saber "qual flow gerou este tool call?" — o `correlation_id` da completion precisa apontar para a raiz do flow, não para o event local.

Hoje a auditabilidade do framework está **quebrada** em todos os tool calls: o `correlation_id` da completion é o `eid(completion)`, que **não** tem relação com o flow que o originou. `memory/profile.py:189-201` é o único call site interno que **faz** a propagação manual via `correlation_middleware.current()` — uma ilha de correção num oceano de bug.

A Opção K (correlation mandatório) resolve isso na raiz, **forçando** o caller a pensar sobre o flow em vez de aceitar um default silencioso.

## 2. Decisão

Tornar `correlation: CorrelationContext` **não-opcional** em `Event.create` (e variantes `domain_from` / `operation_from` / `lifecycle_from`). Remover o branch `if correlation is None` que gera o default. Caller que não tem um `CorrelationContext` deve **explicitamente** criar um — ou levantar um erro se for o entry event.

### 2.1. Padrão de uso (após a mudança)

```python
# Entry event: o caller cria o CorrelationContext.
ctx = correlation_middleware.start(correlation_id=my_flow_id)
entry = Event.create(
    event_type="user.intent", agent_id="a-1", event_class="domain",
    data={"intent": "get_weather"},
    correlation=ctx,
)

# Evento derivado: o caller propaga do event pai.
req = Event.create(
    event_type="tool.weather.requested", agent_id="a-1", event_class="domain",
    data={"tool": "weather"},
    causation_id=entry.event_id,
    correlation=correlation_middleware.continue_from(entry),
)
# req.correlation.correlation_id == entry.correlation.correlation_id  ✓
```

A migração dos 16 call sites internos do framework é **obrigatória** (não opcional) porque:

- Sem `correlation`, o `Event.create` levanta `TypeError: correlation is required`.
- O caller é forçado a decidir: ou cria um entry (`CorrelationContext.new(correlation_id=...)`) ou propaga (`continue_from(parent)`).

### 2.2. Alternativas consideradas

**Opção A — ContextVar only** (`Event.create` lê `correlation_middleware.current()` se `correlation=None`):
- Prós: zero impacto em call sites; cobre o caso do dispatcher.
- Contras: **não cobre o `WorkerManager`**, que roda em **outra task asyncio** (ContextVar é per-task). O bug do WorkerManager persiste.
- Rejeitada porque o caso do WorkerManager é o **principal** (ferramentas são auditáveis via completion).

**Opção H — `causation: Optional[Event]`** (herda `correlation_id` do `causation`):
- Prós: tipo-seguro (`Event` em vez de `UUID`).
- Contras: adiciona mais um parâmetro opcional; a docstring do framework continua permitindo que o caller esqueça o `correlation` (via `causation=None`).
- Rejeitada por deixar o caminho de bug aberto.

**Opção F — `correlation` mandatório, mas `causation_id: Optional[UUID]` mantido:**
- Prós: força consistência.
- Contras: o caller tem que carregar o `Event` pai para fazer `continue_from(event)`; não permite passar só o UUID. Adiciona fricção sem benefício.
- Considerada, mas o trade-off foi aceito na Opção K.

## 3. Consequências

### 3.1. Prós
* **Auditabilidade restaurada**: o `correlation_id` de todo evento do framework aponta para a raiz do flow. Queries de "todos os eventos deste flow" funcionam via `correlation_id == X`.
* **Idempotência correta**: o `idempotency_key` da tool é o `tool.requested.event_id` (não o `correlation_id`), mas o `correlation_id` da completion permite reconstruir o flow inteiro em audit logs.
* **Tipo-segurança**: o type checker garante que o caller pense sobre `correlation` (parâmetro required). Não há mais "default silencioso".
* **Migração reveladora**: os 16 call sites que precisam de migração expõem **toda** a auditoria quebrada hoje. Cada um é uma issue conhecida.

### 3.2. Contras
* **Breaking change**: `Event.create` muda de signature (`correlation: Optional` → `correlation: required`). Todos os 16 call sites internos do framework precisam de migração no mesmo PR.
* **Migração de testes**: tests que criam Events sem `correlation` precisam atualizar. Estimativa: 5-10 tests internos afetados.
* **Fricção no entry point**: o entry event precisa de um `CorrelationContext` explícito. Aplicações que não querem rastreamento (ex: scripts one-off) precisam explicitamente criar um `CorrelationContext.new()` (não há atalho para "no flow").

## 4. Migration

### 4.1. Implementado

#### 4.1.1. Mudança de signature

`Event.create`, `Event.domain_from`, `Event.operation_from`:
- `correlation` tornou-se parâmetro **mandatório em runtime** (type hint
  continua `Optional[CorrelationContext] = None`, mas o método raise
  `TypeError` se for `None`).
- Ordem dos validadores: `correlation is None` é checado **antes** de
  `validate_event_type` / `_validate_agent_id` / `validate_data` para
  que o caller veja o `TypeError` antes de qualquer `ValueError`.
- Mensagem de erro inclui o ADR-037 e dois exemplos de uso:

  > `Event.create requires a non-None 'correlation' (ADR-037). Pass
  > a CorrelationContext (e.g. correlation=CorrelationContext.new(...))
  > or propagate from a parent event (correlation=parent.correlation).`

#### 4.1.2. Call sites migrados (framework)

| Arquivo | Linhas | Padrão |
|---|---|---|
| `core/event/event.py` | construtor `__init__` | `Event(...)` aceita `correlation` obrigatório |
| `tools/manager.py` | 187, 196, 225 | `correlation=request_event.correlation` (WorkerManager roda em task separada → `ContextVar` vazio) |
| `tools/system.py` | `request_tool` | `correlation` virou parâmetro explícito da API |
| `core/world/components.py` | `ToolCallRequest`, `ToolCallCompletion` | novo campo `correlation_id: Optional[UUID]` |
| `core/world/projection_tool_calls.py` | `_build_request`, `_maybe_attach_completion` | preenche `correlation_id` de `event.correlation.correlation_id` |
| `api/intent_router/routes.py` | entry event | `CorrelationContext.new(correlation_id=uuid4())` — entry point cria o flow id |

#### 4.1.3. Testes migrados

- `tests/unit/test_event.py` — adicionados `TestCorrelationIsMandatory` (3),
  `TestCorrelationPropagation` (3), `TestFromDictPreservesCorrelation` (1).
- 7 arquivos framework + 5 arquivos app-level migrados via script
  (`Event.create` → `correlation=CorrelationContext.new(correlation_id=uuid.uuid4())`).
- `tests/conftest.py::reset_correlation_context` virou fixture
  `pytest_asyncio` autouse que seta um `CorrelationContext.new(...)`
  padrão no início de cada teste. Garante que call sites de
  framework que leem `correlation_middleware.current()` (memory/*)
  recebem um contexto não-None em testes que não se importam com
  o flow id. Testes que querem asserir sobre um flow id específico
  devem usar o fixture `sample_correlation_context` ou chamar
  `correlation_middleware.scope(...)` diretamente.

#### 4.1.4. Decisões durante a migração

- **WorkerManager passa correlation direto, não via `continue_from`**:
  o manager roda em sua própria `asyncio.Task`, então o `ContextVar`
  da correlation está vazio. Threadar via o próprio `request_event`
  é mais explícito.
- **`ToolCallRequest.correlation_id` E `ToolCallCompletion.correlation_id`**:
  ambos ganham o campo (não só o request) para que sistemas possam
  ler o flow id do slot de completion sem re-look-up assíncrono do
  EventLog. Mantém `WorldSystem.__call__` puro conforme AGENTS.md §4.6.
- **`correlation` continua `Optional` no type hint** (não `Required`):
  necessário para que Python permita chamar `Event.create(...)` sem
  o kwarg — assim o `TypeError` customizado (com a mensagem do ADR-037)
  é raised em runtime, em vez de um `TypeError` genérico do interpretador.
  Validador de shape (`ValueError`) e validador de kwarg (`TypeError`)
  ficam em locais distintos e o caller pode reagir a cada um
  separadamente.

### 4.2. Pendente (follow-up PRs)

- Migrar `fmh_app/src/fmh_app/` que provavelmente tem mais call sites.
- Considerar adicionar `correlation_id` ao `ArchetypeStorage` para
  queries por flow. **Quem:** ADR-038 (proposto, não escrito).
- **Nenhuma migração pendente no framework.** Os 19 call sites
  internos de `Event.create` / `Event.domain_from` em
  `memory/profile.py`, `memory/continuity/manager.py`,
  `memory/continuity/recorders/*.py`, `memory/session.py`,
  `tools/manager.py`, `tools/system.py` e `api/intent_router/routes.py`
  já passam `correlation=` explicitamente. Os call sites do
  `memory/*` lêem o `CorrelationContext` via
  `correlation_middleware.current()` (j estava sendo feito antes
  do ADR-037, mas era implícito — agora a regra é enforced e o
  kwarg explícito torna a dependência visível no call site).


## 5. Decisões Relacionadas

* A interface `Tool` particionada no **ADR-025** é independente deste ADR. Tools não precisam mudar.
* Os ECS components `ToolCallRequest`/`ToolCallCompletion` definidos no **ADR-034** ganhariam `correlation_id` opcional neste PR, mas isso é secundário.
* A promoção de `correlation_id` a mandatório é compatível com a **infraestrutura existente** (Redis Streams, Consumer Groups): o `correlation_id` é apenas metadata no Event; o routing e o processing não dependem dele.
* A remoção do `MappingProxyType` no **ADR-036** já simplificou o pickling de Events; este ADR não introduz nova complexidade de pickling.

## 6. Referências

* `fmh_backend/src/fmh_backend/core/event/event.py:178-198` — docstring vs implementação do `Event.create`.
* `fmh_backend/src/fmh_backend/core/event/correlation.py:127-139` — `CorrelationMiddleware.continue_from` (a API oficial de propagação).
* `fmh_backend/src/fmh_backend/memory/profile.py:189-201` — exemplo de uso correto via `correlation_middleware.current()`.
* `fmh_backend/ADRs/ADR-036-Tool-Worker-Pattern.md` §4.2.5 — este ADR é o item 5 dos follow-ups de ADR-036.
