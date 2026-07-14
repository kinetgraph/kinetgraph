<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-036: Padrão ECS Assíncrono para Execução de Tools (Tool Worker Pattern)

**Status:** Aceito
**Data:** 06 de julho de 2026 (proposto 05 de julho de 2026)
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md), [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md), [ADR-034](./ADR-034-ToolCall-ECS-Components.md), [AGENTS.md §4.6](../../AGENTS.md)

---

## 1. Contexto

Atualmente, o `ReactiveDispatcher` processa os eventos dos agentes iterando sobre eles em um único fluxo de controle (`_dispatch_for_agent`). Devido à tipagem que introduzimos em `SystemReturn` (`Union[list[Event], Awaitable[list[Event]]]`), tornou-se estaticamente válido escrever um `WorldSystem` assíncrono que realiza chamadas diretas a ferramentas de rede (ex: `await tool.invoke(...)`).

No entanto, essa abordagem gera um problema arquitetural severo conhecido como **Head-of-Line Blocking**:
1. Se o Sistema do Agente A faz uma chamada a um LLM que demora 10 segundos, o `ReactiveDispatcher` inteiro fica bloqueado.
2. Durante esses 10 segundos, os eventos urgentes dos Agentes B, C e D acumulam-se no Redis. O throughput global do sistema cai drasticamente.
3. Se houver falha (crash) durante essa espera de I/O, o estado da execução se perde e, ao reiniciar, a tool pode ser executada em duplicidade, a menos que a ferramenta seja perfeitamente idempotente na origem.

O [ADR-034](./ADR-034-ToolCall-ECS-Components.md) abriu o caminho para resolver isso via ECS, definindo os componentes `ToolCallRequest` e `ToolCallCompletion`. Contudo, faltava estabelecer esse fluxo como a **arquitetura padrão universal** para ferramentas que demandam rede no framework.

## 2. Decisão

Para proteger a performance do `ReactiveDispatcher` (mantendo-o estritamente CPU-bound), decidimos padronizar a separação entre **Intenção** (WorldSystem) e **Execução** (Tool Worker).

### 2.1. Padrão de Sistemas Puros (Intenção)
Nenhum `WorldSystem` deve fazer I/O bloqueante no seu ciclo `__call__`. Com isso, a antiga distinção entre "Sistemas" (puros) e "Sistemas de I/O" (impuros) perde o sentido dentro do motor principal: **todos** os sistemas avaliados pelo Dispatcher tornam-se obrigatoriamente puros.
Se um Sistema precisar usar uma ferramenta, ele deve apenas gerar um evento de intenção:
```python
return [
    Event.create(
        event_type="tool.pii_redactor.requested",
        agent_id=world.agent_id,
        data={"tool": "pii_redactor", "params": {"text": "..."}}
    )
]
```

> **Event type form.** The canonical form is
> `tool.<name>.requested` (one segment per tool name, so
> the `ToolRouter` can route by event type without parsing
> the payload). The bare `tool.requested` form is also
> accepted by the projection for back-compat with old
> EventLogs — see the callout in
> [ADR-034](./ADR-034-ToolCall-ECS-Components.md#event-type-form).

### 2.2. O `GenericToolWorker` (Execução)
Criaremos um componente de infraestrutura (o `GenericToolWorker`) que rodará **fora do loop de despacho principal**. Para simplificar a operação e manter os componentes atrelados, este Worker pode ser inicializado pela própria aplicação usando um `ProcessPoolExecutor` do Python durante o *startup*, atuando de forma semelhante a um "Pod" no Kubernetes com múltiplos containers (onde o app sobe o Dispatcher e os Workers simultaneamente). O seu contrato é:
1. Ler o stream de eventos buscando por `tool.<name>.requested` (usando Redis Consumer Groups para paralelismo e confiabilidade).
2. Localizar a ferramenta no `ToolRegistry`.
3. Invocar a ferramenta passando um `idempotency_key` determinístico (ex: o `event_id` do request). O retorno seguirá o padrão Railway (`Result` protocol) do [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md).
4. Emitir um evento `tool.<name>.completed` (se `Ok`) ou `tool.<name>.failed` (se `Err`) no stream original do agente.

### 2.3. Helper `ToolAwareSystem`
Para reduzir o *boilerplate* do desenvolvedor que precisará dividir sua lógica em dois estágios ("pedir" e "reagir"), forneceremos uma classe base (ou módulo helper) que facilita a verificação dos componentes:
```python
if not has_tool_completed(view, request_id):
    return [] # dispatcher processará outros agentes
return process_tool_result(view, request_id)
```
*(Nota: Isso integrará as projeções do ADR-034 na projeção padrão do sistema).*

### 2.4. Developer Experience (Metadados e DX)
Para abstrair a complexidade de infraestrutura dos desenvolvedores de ferramentas, o registro e acoplamento ao WorkerManager será feito via anotações:
```python
@tool_worker(name="pii_redactor", max_concurrency=5, retries=3)
class PiiRedactionTool:
    async def invoke(self, *, idempotency_key: str, **kwargs) -> Result[...]:
        ...
```
No *startup*, o `WorkerManager` escaneia os metadados, inicializa o `ProcessPoolExecutor`, e conecta cada *worker* à fila correta, criando uma experiência *plug-and-play*.

### 2.5. Roteamento (Full Payload Fan-Out) e Confiabilidade (Redis)
O roteamento entre o Agente e a Ferramenta priorizará latência mínima para a Tool:
1. **Full Payload Fan-Out:** O Sistema gera o `tool.<name>.requested` com o payload integral e o salva no `EventLog` do agente (garantindo que o agente crie o componente `ToolCallRequest` e mantenha a memória da requisição pendente). Um roteador automático copia o evento, com o payload intacto, para a fila global da ferramenta (`knt:tools:tool_name:queue`). Assim, a tool executa instantaneamente lendo da própria fila, sem necessidade de consultar o log do agente para resgatar parâmetros.
2. **Confiabilidade:** O `GenericToolWorker` implementa os padrões de stream do Redis nativamente: balanceamento via **Consumer Groups** (`XREADGROUP`), garantia de entrega at-least-once via **PEL e XACK**, recuperação de processos mortos via **XAUTOCLAIM**, e uma **Dead-Letter Queue (DLQ)** adaptada (se o limite de retries estourar, o worker injeta um `tool.<name>.failed` de volta no log do agente, evitando que a máquina de estados fique bloqueada eternamente).

## 3. Consequências

### 3.1. Prós
* **Escalabilidade Horizontal de I/O:** O `ReactiveDispatcher` pode rodar em 1 única réplica super-rápida (CPU-bound, sem I/O), enquanto podemos escalar o `WorkerManager` para N réplicas caso tenhamos gargalo em APIs externas (LLMs, HTTP).
* **Alta Tolerância a Falhas:** Se um Worker "crashar" fazendo I/O, o Redis Consumer Group redistribui o `tool.<name>.requested` para outro Worker após o timeout. O *state* do domínio não sofre corrupção.
* **Métricas Claras:** Tempo de I/O torna-se a diferença entre o timestamp do `tool.<name>.requested` e do `tool.<name>.completed` no EventLog (campo `latency_ms` no `ToolCallCompletion`).
* **Composição Default do `World`:** A projeção `overlay_tool_calls` é aplicada por default no `_fold_with_filter` do dispatcher. Sistemas que usam `ToolAwareSystem` veem os slots `tool_requests`/`tool_completions` sem subclassar nada — o boilerplate de `ToolAwareReactiveDispatcher` (exemplo 19 original) foi removido.
* **Cleanup colateral do `MappingProxyType`:** A investigação do pickle do `World` revelou que o `MappingProxyType` (read-only facade) era redundante e cerimonial. Removido de 5 sites (`component.freeze`, `World._views_ro`, `WorldQuery._world_ro`, `AgentView.components`, slots em `_overlay`). O framework voltou a ser serializável sem hooks custom.

### 3.2. Contras
* **Fluxo Assíncrono Fragmentado:** O desenvolvedor precisa desenhar a lógica de negócios em dois estágios (antes e depois do request). Não é mais um script sequencial direto (`response = await tool()`).
* **Complexidade de Concorrência Interna:** Mesmo que o Worker seja inicializado de forma transparente para o time de DevOps (via `ProcessPoolExecutor` no mesmo app), o framework agora tem que gerenciar ciclo de vida de múltiplos processos e filas do Redis para fechar um simples fluxo de domínio.
* **Test fixtures mais complexas:** O test end-to-end (`tests/integration/tools/test_dispatcher_to_worker.py`) precisa definir o `WeatherTool` no module-level para ser pickleable pelo `ProcessPoolExecutor`. Tests que dependiam do `MappingProxyType` como proteção implícita precisam garantir disciplina de não-mutação via `frozen=True` apenas.

## 4. Migration

### 4.1. Implementado neste PR (Iter 36)

Todos os items abaixo foram merged no branch `fix/slow-tools-execution`
(commits `cbe602c`, `4ab9d40`, `e4b12b4`, `66a5b18`, `a8d1860`,
`46b2ad6`, mais um commit de promoção do ADR-036). Gates `scripts/ci.py`
passing (`syntax`, `lint`, `format`, `complexity`, `pyright`).

- **Framework**:
  - `tools/worker.py` — `@tool_worker` decorator (valida `invoke` +
    `idempotency_key`, injeta `name`, `description`, `input_schema`,
    `max_concurrency`, `retries`).
  - `tools/manager.py` — `WorkerManager` com Consumer Groups
    (`xgroup_create`, `xreadgroup`, `xack`), `ProcessPoolExecutor`,
    reaper (`xautoclaim`), DLQ via `XPENDING` + re-emit de
    `tool.<name>.failed`.
  - `tools/router.py` — `ToolRouter.route_batch` (Full Payload
    Fan-Out para `knt:tools:<name>:queue`).
  - `tools/system.py` — `ToolAwareSystem` mixin com
    `request_tool`, `get_request`, `get_completion`,
    `has_requested`, `is_pending`.
  - `tools/__init__.py` — re-exports públicos
    (`tool_worker`, `WorkerManager`, `ToolRouter`,
    `ToolAwareSystem`).
  - `core/world/projection_tool_calls.py` — `overlay_tool_calls`
    (overlay-only) e `project_tool_calls` (wrapper fino).
    Reconhece o `tool.<name>.requested` /
    `tool.<name>.completed` / `tool.<name>.failed` (forma
    canônica WorkerManager) e a forma bare legada
    `tool.requested` / `tool.completed` / `tool.failed`
    do ADR-034 — esta última apenas para back-compat com
    EventLogs antigos. Removido `MappingProxyType`
    redundante (dataclass frozen já garante imutabilidade).
  - `runner/reactive.py` — `ReactiveDispatcher` aceita
    `tool_router=` (opt-in) e chama `route_batch` após
    `append_batch`. `_fold_with_filter` aplica
    `overlay_tool_calls` por default (zero alocação
    para batches sem tool events via object identity check).
  - `infra/redis/_client.py` — `RedisLike` Protocol estendido
    com 5 métodos de Consumer Groups.
  - `core/world/world.py`, `core/world/query.py`,
    `core/world/view.py` — removido `MappingProxyType` (read-only
    facade cerimonial). Frozen dataclasses são a única defesa
    estrutural.
  - `infra/world_checkpoint.py` — docstring atualizada (sem
    `_views_ro`).
  - `AGENTS.md §4.6` — nova subseção `WorldSystem.__call__` deve
    ser puro. Referência explícita a ADR-036.

- **Tests**:
  - `tests/unit/tools/test_worker.py` — `@tool_worker` decorator
    (5 testes: metadata, schema, Pydantic, missing invoke,
    missing idempotency_key).
  - `tests/unit/tools/test_system.py` — `ToolAwareSystem` mixin
    (4 testes).
  - `tests/unit/tools/test_public_api.py` — re-exports do
    `tools/__init__.py` (6 testes).
  - `tests/unit/runner/test_reactive_dispatcher_tool_router.py`
    — fan-out order (append → route) e no-op when
    `tool_router=None` (3 testes).
  - `tests/unit/runner/test_reactive_dispatcher_projection.py`
    — `overlay_tool_calls` aplicado por default no fold
    (5 testes, incluindo object identity pass-through).
  - `tests/unit/core/test_projection_tool_calls.py` — 4
    testes novos para `overlay_tool_calls` (preserva base,
    equivalência parcial com `project_tool_calls`,
    batch sem tool events = no-op, orphan completion).
  - `tests/integration/tools/test_dispatcher_to_worker.py`
    — end-to-end (dispatcher → router → WorkerManager → completion
    → system reage).

Total: 24 testes unitários novos, 1 teste de integração
novo. 54 verde.

### 4.2. Pendente (follow-up PRs)

1. **Migrar `fmh_app/src/fmh_app/systems/geoencoder_system.py:94`**
    (`GeoencoderSystem`) — atualmente faz
    `await self.ibge_geoencoder_tool.invoke(...)` dentro de
    `__call__`. Violar AGENTS.md §4.6. Plano: emitir
    `tool.ibge_geoencoder.requested`; mover a chamada
    para um `@tool_worker` registrado no `WorkerManager`
    do `fmh_app`. **Quem:** equipe `fmh_app`. **Prazo:**
    próximo PR de migração de tools (estimativa: 1-2 sprints).

2. **Migrar `fmh_app/src/fmh_app/app_runner.py:122-178`**
   (`ToolInvokerSystem`) — o caso de head-of-line blocking
   que motivou este ADR. Substituir pelo padrão emit/reage
   via `request_tool` + `ToolAwareSystem`. **Quem:**
   equipe `fmh_app`. **Prazo:** junto com item 1.

3. **Resolver inconsistência `systems=` vs `reactive_systems=`**
   — `app_runner.py:265` e `worker_template.py:154` passam
   `reactive_systems=` mas `ReactiveDispatcher.__init__`
   aceita `systems=`. Um dos dois lados está errado.
   Plano: investigar e corrigir no `fmh_app`. **Quem:**
   equipe `fmh_app`. **Prazo:** próximo PR.

4. **Bug pré-existente em `tests/integration/test_reactive_dispatcher.py`**
   (8 testes falhando) — não relacionado a este ADR. Causa
   provável: refactor de checkpoint (em outro commit) mudou
   a signature de `EventLog.append_batch`, mas o dispatcher
   ainda chama com kwargs incompatíveis
   (`BasicKeyCommands.append() got an unexpected keyword
   argument 'agent_id'`). **Quem:** investigar no
   `runner/reactive.py` e `stream/event_log/store.py`.
   **Prazo:** próximo PR.

5. **ADR-037 (proposto, não escrito): bug no `Event.create`**
   — o `correlation_id` deveria ser estável ao longo de um
   flow (herdado do `causation_id` event ou do ContextVar
   `correlation_middleware.current()`), mas a implementação
   atual sempre gera `correlation_id = eid(event)`. Resultado:
    o `correlation_id` quebra em `tool.<name>.requested` → `tool.<name>.completed`
    quando o caller não propaga explicitamente via
    `correlation=continue_from(cause)`. Hoje o framework usa
    `causation_id` (= `request_event_id`) como join key
    request↔completion (que funciona). Mas a docstring de
    `Event.create` (linha 178-185) promete propagação
    automática, o que está quebrado. **Quem:** quem pegar
   o ADR-037. **Prazo:** sem urgência (workaround funciona).

### 4.3. Não fazer (decisões de não-arquivamento)

- **Não usar `correlation_id` como join key no `ToolCallRequest`/
  `ToolCallCompletion`** — o framework tem o bug acima.
  `request_event_id` (= `causation_id` do completion) é o
  join key correto até o ADR-037.
- **Não remover `ReactiveDispatcher` legacy path** — o
  `ToolInvokerSystem` (app-level) ainda usa
  `world = World(world, event)`; a remoção deve ir junto
  com a migração de `fmh_app`.
- **Não criar uma `InMemoryWorldStore`** — tests que
  precisarem bypassar o checkpoint devem usar o path
  com `world_store=` explícito; o `World.empty()` é
  suficiente para unit tests.

## 5. Decisões Relacionadas

* A interface `Tool` monolítica já foi particionada no **ADR-025**, facilitando que o `GenericToolWorker` exija apenas contratos da Camada 3 (`idempotency_key` e `Result`).
* Os ECS components `ToolCallRequest` e `ToolCallCompletion` já foram definidos no **ADR-034**, fornecendo o modelo de dados subjacente. ADR-036 faz a forma `tool.<name>.requested` / `tool.<name>.completed` / `tool.<name>.failed` (WorkerManager) a canônica; a forma bare `tool.requested` / `tool.completed` / `tool.failed` (ADR-034) é mantida apenas para back-compat com EventLogs antigos via prefix-match em `overlay_tool_calls` (ver [ADR-034 §event-type-form](./ADR-034-ToolCall-ECS-Components.md#event-type-form)).
* **MappingProxyType removido** como side effect da investigação. A read-only facade cerimonial foi removida (era redundante com `frozen=True` nas dataclasses) e isso destravou o pickle do `World` sem necessidade de `__getstate__`/`__setstate__` no `AgentView`.

## 6. Referências
* Redis Consumer Groups documentation (para implementação do worker).
* `fmh_backend/src/fmh_backend/runner/reactive.py` (Dispatcher que será isolado do I/O).
