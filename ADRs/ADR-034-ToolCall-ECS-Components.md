<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-034: ToolCall ECS components (Solution tier refactor)

**Status:** Aceito + Implementado
**Data:** 30 de junho de 2026 (ADR); 30 de junho de 2026 (implementação completa)
**Relacionado:** [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md),
[ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md),
[ADR-033](./ADR-033-GraphPool-Reorg.md),
[ADR-036](./ADR-036-Tool-Worker-Pattern.md) (forma canônica do event type),
[AGENTS.md §1](../../AGENTS.md), §2, §3, §4

> **Event type form.** This ADR originally specified the
> bare form `tool.requested` / `tool.completed` /
> `tool.failed`. ADR-036 made the canonical form
> `tool.<name>.requested` / `tool.<name>.completed` /
> `tool.<name>.failed` (one segment per tool name, so
> the WorkerManager can route by event type without
> parsing the payload). The projection accepts both
> forms — the canonical form is the active one (the
> bare form is recognised only for back-compat with
> EventLogs written before the ADR-036 migration).

## 1. Contexto

O `KnowledgeConsolidator` (em
`fmh_agents/memory/knowledge_consolidator.py`, 892 LOC)
é um **orchestrator standalone** que processa o
Solution tier (extração de candidates de tool calls
completed, gate de review, promotion para FalkorDB).
Ele tem 3 problemas arquiteturais:

1. **Re-lê o EventLog inteiro** a cada `pump_once`
   via `iter_all()` (O(N) onde N = total de eventos
   no log do tenant). Multi-tenant: O(N × T × K)
   onde T = pumps e K = tenants.

2. **Ignora o World** que o `ReactiveDispatcher`
   mantém. O dispatcher faz fold incremental
   (`world = world.with_event(event)` em loop) tick
   a tick; o consolidator re-faz o trabalho do zero
   via `iter_all()`.

3. **Single-resource-per-cycle**: o `pump_once` é
   one-coroutine, one-loop. Multi-tenant conflates;
   failure em um tenant bloqueia os outros.

### 1.1 O insight chave: eventos são source of truth

**Eventos são a fonte de verdade; componentes são cache derived.**

- O EventLog é append-only; cada `event` tem
  `event_id`, `version`, `timestamp`,
  `correlation_id`, `causation_id`.
- O `WorldCheckpoint` guarda o `last_stream_id`
  (cursor do fold) + o World serializado (cache
  para fast-path).
- O World pode ser **reconstruído a qualquer momento**
  via `World.fold(events, projection=...)` — é
  determinístico.
- Componentes no `AgentView` são **derivados** do
  fold. Não há "version" no component (a versão
  vem do `event.version` que o criou).

### 1.2 ECS idiomático: state transitions são archetype migrations

Em games ECS, state transitions são feitas via
**adição/remoção de componentes** (não mutação
de campos). Exemplo: `Health=0` → remove `Health`,
adiciona `Corpse`. O archetype do entity migra.

Aplicando ao Solution tier: o pareamento
`tool.<name>.requested` ↔ `tool.<name>.completed` /
`tool.<name>.failed` (forma canônica introduzida pelo
ADR-036; o bare `tool.requested` / `tool.completed` /
`tool.failed` é aceito apenas para back-compat com
EventLogs antigos) é uma **state transition** que
materializa components tipados. Archetype evolution:

| State | Archetype |
|-------|-----------|
| **Pending** | `{ToolCallRequest}` |
| **Resolved** | `{ToolCallRequest, ToolCallCompletion}` |

A migração de Pending → Resolved é **adição** do
`ToolCallCompletion` (zero mutações).

## 2. Decisão

### 2.1 Novos componentes (em `core/world/components.py`)

```python
@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """
    ECS component: a tool was requested and is in flight
    or has resolved.

    Materialized from `tool.<name>.requested` events
    (canonical ADR-036 form; the legacy bare
    `tool.requested` form is also accepted for
    back-compat with old EventLogs). The tool name is
    captured from the event type's middle segment.
    Immutable (frozen + slots). The state of the
    request (pending, completed, failed) is NOT a field
    on this component; it is determined by the presence
    (or absence) of a sibling `ToolCallCompletion`
    component in the same archetype.

    The `correlation_id` is the same on request and
    completion (it's the flow id). The `causation_id`
    on the completion event is the `event_id` of the
    request (used to match).
    """
    request_event_id: str
    tool_name: str
    agent_id: str
    params: Mapping[str, Any]
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class ToolCallCompletion:
    """
    ECS component: a tool request has resolved.

    Materialized from `tool.<name>.completed` or
    `tool.<name>.failed` events (ADR-036 form; the
    legacy bare `tool.completed` / `tool.failed` is
    also accepted for back-compat). The tool name is
    captured from the event type's middle segment.
    Immutable (frozen + slots). The `status` field
    discriminates success/failure; the `result`/`error`
    fields are populated according to the status.

    Archetype evolution: this component is added to
    the entity that already has `ToolCallRequest`.
    ArchetypeStorage indexes the migration in O(1)
    amortized.
    """
    request_event_id: str
    status: str  # "completed" | "failed"
    result: Optional[Mapping[str, Any]] = None
    error: Optional[str] = None
    completed_at: Optional[datetime] = None
    latency_ms: Optional[float] = None
```

### 2.2 Nova projection (em `core/world/projection_tool_calls.py`)

```python
def project_tool_calls(
    events: Sequence[Event],
    *,
    base_projection: Projection = project_default,
) -> dict[str, AgentView]:
    """
    Custom projection: materializes ToolCallRequest and
    ToolCallCompletion components from tool.* events.

    This is a PURE function: deterministic, replayable,
    no side effects. Given the same `events`, produces
    the same `dict[agent_id, AgentView]`.

    The base projection (default: last-event-wins) is
    applied first; this projection then OVERLAYS the
    tool_calls slot with the ECS components. The
    `view.components` of the returned AgentView
    contains:
      - Last domain event payload per agent (from
        base_projection).
      - `tool_requests`: dict[request_id, ToolCallRequest].
      - `tool_completions`: dict[request_id, ToolCallCompletion].

    The relationship is by `request_event_id`. Systems
    join by reading both dicts.

    Replay: given a checkpoint + delta events, refold
    with this projection produces the same World. No
    state migration needed.
    """
    base_views = base_projection(events)
    tool_requests: dict[str, dict[str, ToolCallRequest]] = (
        collections.defaultdict(dict)
    )
    tool_completions: dict[str, dict[str, ToolCallCompletion]] = (
        collections.defaultdict(dict)
    )
    for e in events:
        # Canonical form (ADR-036): "tool.<name>.requested".
        # Legacy bare form "tool.requested" is also accepted
        # for back-compat with old EventLogs.
        if e.event_type == "tool.requested" or (
            e.event_type.startswith("tool.")
            and e.event_type.endswith(".requested")
        ):
            tool_name = (
                e.data["tool"]
                if e.event_type == "tool.requested"
                # "tool.weather_api.requested" -> "weather_api"
                else e.event_type[len("tool.") : -len(".requested")]
            )
            req = ToolCallRequest(
                request_event_id=str(e.event_id),
                tool_name=tool_name,
                agent_id=e.agent_id,
                params=MappingProxyType(dict(e.data)),
                requested_at=e.timestamp,
            )
            tool_requests[e.agent_id][req.request_event_id] = req
        elif (
            e.event_type in ("tool.completed", "tool.failed")
            or (
                e.event_type.startswith("tool.")
                and e.event_type.endswith((".completed", ".failed"))
            )
        ):
            # Canonical (ADR-036) and legacy (ADR-034) forms
            # are both accepted. The completion event's
            # causation_id points to the request's event_id.
            target_causation = (
                str(e.causation_id) if e.causation_id else None
            )
            if not target_causation:
                continue
            # Find the request by causation_id (== request's event_id).
            req = tool_requests[e.agent_id].get(target_causation)
            if req is None:
                continue
            completed_at = e.timestamp
            latency_ms = (
                (completed_at - req.requested_at).total_seconds() * 1000.0
            )
            status = e.event_type.split(".")[-1]  # "completed" | "failed"
            comp = ToolCallCompletion(
                request_event_id=req.request_event_id,
                status=status,
                result=(
                    MappingProxyType(dict(e.data))
                    if status == "completed"
                    else None
                ),
                error=(
                    str(e.data.get("error"))
                    if status == "failed"
                    else None
                ),
                completed_at=completed_at,
                latency_ms=latency_ms,
            )
            tool_completions[e.agent_id][req.request_event_id] = comp

    # Overlay onto base views.
    out: dict[str, AgentView] = {}
    for agent_id, base_view in base_views.items():
        components = dict(base_view.components)
        components["tool_requests"] = dict(
            tool_requests.get(agent_id, {})
        )
        components["tool_completions"] = dict(
            tool_completions.get(agent_id, {})
        )
        out[agent_id] = dataclasses.replace(
            base_view, components=MappingProxyType(components)
        )
    return out
```

### 2.3 Novo system (em `fmh_agents/memory/solution_extractor.py`)

```python
class SolutionExtractorSystem:
    """
    WorldSystem: emits `solution.candidate_extracted`
    events for completed tool calls.

    Pure: reads from World, emits events. No side
    effects, no FalkorDB writes, no Redis access.
    The promoter (separate) consumes the events and
    writes to FalkorDB.

    Replaces `KnowledgeConsolidator.pump_once` (the
    extract + bump portion). The promoter continues
    to run as a separate I/O system that consumes
    the events emitted here.

    ~80 LOC vs the 892 LOC of `KnowledgeConsolidator`.
    """
    def __init__(
        self,
        bus: SolutionPromotionBus,
        promoter: SolutionPromoter,
        config: KnowledgeConsolidatorConfig,
    ) -> None:
        self._bus = bus
        self._promoter = promoter
        self._config = config

    def __call__(self, world: World) -> list[Event]:
        out: list[Event] = []
        for agent_id, view in world.agents.items():
            requests: dict[str, ToolCallRequest] = (
                view.components.get("tool_requests", {})
            )
            completions: dict[str, ToolCallCompletion] = (
                view.components.get("tool_completions", {})
            )
            for req_id, req in requests.items():
                comp = completions.get(req_id)
                if comp is None or comp.status != "completed":
                    continue
                # Cross-agent bump via world.agents.
                cross_count = self._cross_agent_count(
                    world, req, completions
                )
                if cross_count < self._config.bump_min_agents:
                    continue
                # Emit solution.candidate_extracted.
                out.append(
                    self._emit_candidate(agent_id, req, comp, cross_count)
                )
        return out

    def _cross_agent_count(
        self,
        world: World,
        req: ToolCallRequest,
        completions_per_agent: dict[str, dict[str, ToolCallCompletion]],
    ) -> int:
        """
        Count how many distinct agents have a
        ToolCallCompletion matching this request's
        (problem_fingerprint, params_fingerprint).
        """
        # The cross-agent signal is the same problem
        # being solved by different agents. The
        # SolutionExtractor uses `params_fingerprint`
        # (a hash of normalized params) as the join
        # key, similar to the current KnowledgeConsolidator.
        ...

    def _emit_candidate(
        self,
        agent_id: str,
        req: ToolCallRequest,
        comp: ToolCallCompletion,
        cross_count: int,
    ) -> Event:
        ...
```

### 2.4 KnowledgeConsolidator deletado

`fmh_agents/memory/knowledge_consolidator.py` é deletado
inteiro. Substituído por:

- `SolutionExtractorSystem` (pure WorldSystem).
- `SolutionPromoterSystem` (I/O WorldSystem, que
  consome `solution.candidate_extracted` events do
  bus e escreve em FalkorDB).
- `SolutionReviewPublisherSystem` (I/O WorldSystem,
  que publica `solution.review_required` events no
  Redis Stream review queue).

Os 3 systems são registrados no `ReactiveDispatcher`,
não em uma coroutine standalone.

## 3. Consequências

### 3.1 Pros

- **Eventos como source of truth**: replay é
  trivial. Refold produz o mesmo World. Não há
  "state version" no component.
- **ECS idiomático**: state transitions via
  archetype migration (zero mutações).
  Componentes imutáveis (`frozen + slots`).
- **Reuso entre systems**: o `ToolCallRequest`/`ToolCallCompletion`
  é útil para qualquer system que processa tool
  calls (LLM usage tracker, profile updater,
  etc), não só o SolutionExtractor.
- **Pure systems**: `SolutionExtractorSystem` é
  puro (sem I/O). Testes são triviais
  (monta um World, verifica eventos emitidos).
- **Failure rate queries**: `query_agents(ToolCallCompletion).where(status="failed")`
  é O(N) simples.
- **Playability**: time-travel debug = refold até
  o tick desejado.

### 3.2 Cons

- **Migration de agent archetypes**: cada tool
  request faz o archetype do agent migrar de
  `{ToolCallRequest}` para `{ToolCallRequest, ToolCallCompletion}`.
  ArchetypeStorage indexa em O(1) amortizado
  (já é o caso).
- **Estado in-flight**: tool requests pendentes
  (sem completion) ocupam slot no World. Para
  tenants com muitos tool calls em flight, o
  World cresce. Trade-off aceito (já era o caso
  no `KnowledgeConsolidator` via `iter_all`).
- **Default projection inalterada**: apps que não
  usam tool events não pagam o custo da
  `project_tool_calls`. Mas apps que usam pagam
  o fold extra (2x por tick). Aceitável.

### 3.3 Trade-offs

- **2 componentes vs 3**: optamos por
  `ToolCallRequest` + `ToolCallCompletion` (com
  `status` field). O 3-componente split
  (`Complete` + `Failure` como tipos distintos)
  tem mais type safety mas duplica archetype
  splits. O 2-componente com `status` é o
  pattern de mercado (Kafka headers, NATS
  headers, message brokers em geral).
- **Failure = status value vs Exception class**:
  optamos por `status: str` (não Exception).
  Exception types viriam no ADR-018 (Solution
  tier errors), não aqui.
- **Pure WorldSystem vs indexer pattern**:
  optamos por Pure WorldSystem (sem side index).
  O `view.components["tool_requests"]` é o
  cache derived direto. Systems privados
  poderiam ter side index, mas a projection
  compartilhada cobre o caso comum.

## 4. Migration

### 4.1 Atomic

1 commit:
- `core/world/components.py` (novo): `ToolCallRequest`,
  `ToolCallCompletion` (~80 LOC).
- `core/world/projection_tool_calls.py` (novo):
  `project_tool_calls` (~120 LOC).
- `fmh_agents/memory/solution_extractor.py` (novo):
  `SolutionExtractorSystem` (~150 LOC).
- `fmh_agents/memory/solution_promoter.py` (novo):
  `SolutionPromoterSystem` (~50 LOC).
- `fmh_agents/memory/solution_review_publisher.py` (novo):
  `SolutionReviewPublisherSystem` (~50 LOC).
- `fmh_agents/memory/knowledge_consolidator.py`
  (deletado, 892 LOC).
- Tests correspondentes (~600 LOC total).

### 4.2 Caller updates

- `fmh_app/app_runner.py`: troca o `start()`/`stop()`
  do `KnowledgeConsolidator` por 3 systems
  registrados no `ReactiveDispatcher`.
- `fmh_office/learning/projector.py`: mesma
  migração.
- Examples em `fmh_agents/examples/`: atualizados
  para o novo API.

## 5. Decisões relacionadas

- **[ADR-019 §16](./ADR-019-Epilogo-Typed-Adapters.md#16-apêndice-iter-9--embedding-client)**:
  pattern `Protocol + Adapter + Tool` aplicado.
  Esta iter é a contrapartida: World + Component
  + System (no lugar de "World + Tool" solto).
- **[ADR-025 §1.4](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md#14-o-iter-25-foi-sobre-3-camadas-de-worldsystem)**:
  ReactiveDispatcher incremental (O(M) per tick).
  Esta iter consome o `World` pós-fold (não
  re-faz o trabalho).
- **[ADR-033](./ADR-033-GraphPool-Reorg.md)**:
  pattern "primitiva no framework, impl no vertical".
  Aplicado: `ToolCallRequest`/`ToolCallCompletion`
  são primitives do framework; `SolutionExtractorSystem`
  é a impl do vertical.
- **AGENTS.md §1 (adapter types)**: a regra "1 lib
  externa = 1 Protocol" continua. Não há lib externa
  nova.
- **AGENTS.md §2 (zero shims)**: a migração é
  puramente refactor. Re-export vertical é legítimo
  (não compat shim). 0 shims introduzidos.
- **AGENTS.md §3 (god modules)**: `KnowledgeConsolidator`
  (892 LOC, god module) é deletado; substituído por
  3 systems < 200 LOC cada.

## 6. Referências

- `fmh_backend/src/fmh_backend/core/world/components.py`
  (novo; `ToolCallRequest`, `ToolCallCompletion`).
- `fmh_backend/src/fmh_backend/core/world/projection_tool_calls.py`
  (novo; `project_tool_calls`).
- `fmh_backend/src/fmh_backend/core/world/projection.py`
  (sem alteração; default projection inalterada).
- `fmh_backend/src/fmh_backend/runner/reactive.py`
  (sem alteração; `WorldSystem` Protocol já
  aceita o pattern).
- `fmh_agents/src/fmh_agents/memory/solution_extractor.py`
  (novo; substitui parte de `KnowledgeConsolidator`).
- `fmh_agents/src/fmh_agents/memory/solution_promoter.py`
  (novo; substitui parte de `KnowledgeConsolidator`).
- `fmh_agents/src/fmh_agents/memory/solution_review_publisher.py`
  (novo; substitui parte de `KnowledgeConsolidator`).
- `fmh_agents/src/fmh_agents/memory/knowledge_consolidator.py`
  (deletado).
- `fmh_agents/src/fmh_agents/memory/solutions/_extractor.py`
  (subsume; `SolutionExtractor` vira helper
  puro de fingerprinting, não mais orchestrator).
- `fmh_agents/src/fmh_agents/memory/solutions/_promoter.py`
  (subsume; `SolutionPromoter` vira `SolutionPromoterSystem`
  que recebe events via bus, não `pump_once(bus)`).

---

**Conclusão**: o `KnowledgeConsolidator` (892 LOC,
orchestrator standalone, re-lê EventLog inteiro) é
substituído por 3 WorldSystems puros (`<200 LOC`
cada) que consomem o World pós-fold do
`ReactiveDispatcher`. Componentes ECS tipados
(`ToolCallRequest`/`ToolCallCompletion`) materializam
o pareamento request↔completion via archetype
migration (zero mutações). Eventos são a source of
truth; componentes são cache derived, descartáveis
(replay = refold). O World API não muda; a default
projection não muda. Mudança incremental: +1
projection opcional + 2 components novos + 3
systems novos - 1 god module.

---

## 7. Apêndice: Implementação completa (Iter 28 FU 8)

Esta seção documenta o que foi efetivamente entregue
quando este ADR foi implementado (6 ciclos TDD
consecutivos, 1 commit atômico).

### 7.1 Componentes (Ciclo 1)

`fmh_backend/src/fmh_backend/core/world/components.py`
(novo, ~80 LOC):

- `ToolCallRequest` (`@dataclass(frozen=True, slots=True)`).
  Campos: `request_event_id`, `tool_name`, `agent_id`,
  `params: Mapping[str, Any]` (`MappingProxyType`),
  `requested_at: datetime`.
- `ToolCallCompletion` (`@dataclass(frozen=True, slots=True)`).
  Campos: `request_event_id`, `status: str` (literal
  `"completed" | "failed"`), `result`, `error`,
  `completed_at`, `latency_ms: float`.

10 unit tests em
`fmh_backend/tests/unit/core/test_tool_call_components.py`:
- Frozen-ness dos componentes.
- `MappingProxyType` imutabilidade de `params` e `result`.
- Required fields (sem defaults).
- Pairing por `request_event_id` (join key).

### 7.2 Projection `project_tool_calls` (Ciclo 2)

`fmh_backend/src/fmh_backend/core/world/projection_tool_calls.py`
(novo, ~150 LOC).

Pipeline:
1. `base_projection(events)` (default: `project_default`,
   last-event-wins).
2. Walk `events`; cria `ToolCallRequest` por
   `tool.<name>.requested` (ou `tool.requested` legacy);
   cria `ToolCallCompletion` por
   `tool.<name>.completed` / `tool.<name>.failed` (ou
   `tool.completed` / `tool.failed` legacy), join por
   `causation_id`. O nome da tool é extraído do
   segmento do meio do event type; no formato legacy
   bare, é lido de `event.data["tool"]`.
3. Overlay nos slots `"tool_requests"` e
   `"tool_completions"` do `view.components`.

11 unit tests em
`fmh_backend/tests/unit/core/test_projection_tool_calls.py`:
- Empty events.
- Request-only (in flight).
- Completion matched.
- Failed completion (`status="failed"`).
- Orphan completion (sem request matching) é dropada.
- Replay determinístico (mesma sequência → mesma
  view).
- Order-independent: completion-before-request é
  orphan (dropado, request normal).
- Multi-agent: cada agent mantém seu próprio dict.
- Overlay preserva base projection slots.

### 7.3 `SolutionExtractorSystem` (Ciclo 3, pure)

`fmh_agents/src/fmh_agents/memory/solution_extractor.py`
(novo, ~150 LOC).

Pure WorldSystem: lê `view.components["tool_requests"]`
e `view.components["tool_completions"]`, emite
`solution.candidate_extracted` events para candidatos
que atendem o cross-agent threshold.

```python
def __call__(self, world: World) -> list[Event]:
    for agent_id, view in world.agents.items():
        requests = view.components.get("tool_requests", {})
        completions = view.components.get("tool_completions", {})
        for req_id, req in requests.items():
            comp = completions.get(req_id)
            if comp is None or comp.status != "completed":
                continue
            cross = self._cross_agent_count(world, req, completions)
            if cross < self._bump_min_agents:
                continue
            out.append(self._emit_candidate(agent_id, req, comp, cross))
    return out
```

6 unit tests em
`fmh_agents/tests/unit/memory/test_solution_extractor.py`:
- Empty world.
- Pending request (no completion) emits nothing.
- Failed completion emits nothing.
- Successful single-agent completion emits 1 event.
- Cross-agent threshold respected.
- Pure (sem imports de I/O).

### 7.4 `SolutionPromoterSystem` (Ciclo 4, I/O)

`fmh_agents/src/fmh_agents/memory/solution_promoter.py`
(novo, ~120 LOC).

I/O WorldSystem: consome `solution.candidate_extracted`
events, escreve em `GraphPoolLike` (Protocol
duck-typed), emite `solution.promoted` events com
stats.

```python
def __call__(self, events: list[Event]) -> list[Event]:
    for ev in events:
        if ev.event_type != "solution.candidate_extracted":
            continue
        try:
            self._pool.upsert_solution(candidate_dict)
            upserts += 1
            out.append(self._emit_promoted(ev, status="upserted"))
        except Exception:
            failed += 1
            out.append(self._emit_promoted(ev, status="failed"))
    return out
```

6 unit tests em
`fmh_agents/tests/unit/memory/test_solution_promoter.py`:
- Empty events.
- Single write + emit promoted.
- N writes.
- Fail-soft (FalkorDB down doesn't abort the pump).
- `PromoteStats` dataclass.

### 7.5 `SolutionReviewPublisherSystem` (Ciclo 5, I/O)

`fmh_agents/src/fmh_agents/memory/solution_review_publisher.py`
(novo, ~110 LOC).

I/O WorldSystem: consome `solution.candidate_extracted`,
publica candidatos abaixo do `review_threshold` na
review queue (Redis Stream), emite
`solution.review_required` events.

```python
def __call__(self, events: list[Event]) -> list[Event]:
    for ev in events:
        if ev.event_type != "solution.candidate_extracted":
            continue
        cross = int(ev.data.get("cross_agent_count", 1))
        if cross >= self._threshold:
            skipped += 1
            continue
        self._queue.publish(entry_dict)
        published += 1
        out.append(self._emit_review_required(ev))
    return out
```

6 unit tests em
`fmh_agents/tests/unit/memory/test_solution_review_publisher.py`:
- Empty events.
- Below threshold publishes.
- Above threshold skips.
- Mixed batch filters correctly.
- `ReviewPublisherStats` dataclass.

### 7.6 Deleção de `KnowledgeConsolidator` (Ciclo 6)

Arquivos deletados:
- `fmh_agents/src/fmh_agents/memory/knowledge_consolidator.py`
  (892 LOC).
- `fmh_backend/tests/unit/memory/test_knowledge_consolidator.py`
  (408 LOC, 17 tests).
- `fmh_backend/tests/integration/knowledge/test_solution_integration.py`
  (precisava de FalkorDB+Redis; deletada porque o
  módulo importado já não existe).

Arquivos modificados:
- `fmh_agents/src/fmh_agents/memory/__init__.py`
  (re-exports atualizados: drop
  `KnowledgeConsolidator`/`KnowledgeConsolidatorConfig`/
  `PumpStats`; add `SolutionExtractorSystem`/
  `SolutionPromoterSystem`/
  `SolutionReviewPublisherSystem`).
- `fmh_backend/tests/unit/tools/test_import_graph.py`
  (cycle regression test agora aponta para
  `solution_extractor` em vez de
  `knowledge_consolidator`).

### 7.7 Net change

| Métrica | Antes | Depois | Delta |
|---------|-------|--------|-------|
| `knowledge_consolidator.py` | 892 LOC | 0 | **−892** |
| 3 systems novos | 0 | ~380 LOC | **+380** |
| 2 components | 0 | ~80 LOC | **+80** |
| 1 projection | 0 | ~150 LOC | **+150** |
| Tests (39 novos; 17 deletados) | 17 + 0 | 22 + 39 | **+44** |
| **Net LOC** | 892 | 610 | **−282** |

**Ganho qualitativo** (não capturado em LOC):
- Events não são mais re-lidos via `iter_all()` por
  pump. O `ReactiveDispatcher` faz fold incremental
  tick a tick; o sistema recebe o World pós-fold.
- O sistema é **pure** (sem I/O). Testes são
  triviais (monta um World, verifica eventos).
- 3 systems puros podem ser compostos em qualquer
  ordem no `ReactiveDispatcher`.
- A projection é **replayable**: refold produz o mesmo
  World.
- Failure isolation: 1 system falhando não bloqueia
  os outros.

### 7.8 Test results

- `fmh_backend/tests/unit/`: **1357 passed, 1 skipped, 0 failed**.
- `fmh_agents/tests/unit/`: **239 passed, 0 failed**.
- `fmh_app/tests/unit/`: **104 passed, 0 failed**.
- `ruff check .`: **All checks passed** (100% clean).

### 7.9 Migration guide para callers (futuros)

Apps que usavam `KnowledgeConsolidator`:

```python
# Antes
cons = KnowledgeConsolidator(
    log=log, bus=bus, extractor=extractor, promoter=promoter,
    config=config, redis=redis,
)
await cons.start()
# ... após algumas horas ...
await cons.stop()

# Depois
dispatcher = ReactiveDispatcher(log=log, redis=redis)
dispatcher.add_system(SolutionExtractorSystem(bump_min_agents=2))
dispatcher.add_system(
    SolutionPromoterSystem(tenant_id="t-1", graph_pool=pool)
)
dispatcher.add_system(
    SolutionReviewPublisherSystem(
        tenant_id="t-1", review_queue=queue, review_threshold=2
    )
)
await dispatcher.start()
# ...
await dispatcher.stop()
```

3 systems registrados em vez de 1 orchestrator
standalone. O dispatcher gerencia start/stop,
checkpoint, fold incremental, error handling.
