<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-018: World Incremental + ReactiveSystem `(world) -> list[Event]`

**Status:** Aceito
**Data:** 25 de junho de 2026
**Versão:** 2.2 (modelo de fold incremental)
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** [ADR-001](./ADR-001-Arquitetura.md), [ADR-002](./ADR-002-Replay-Puro.md), [ADR-005](./ADR-005-Checkpoints-Idempotency.md), [ADR-010](./ADR-010-Memory-Business-Tier.md)

---

## 1. Contexto

O `ReactiveDispatcher` (v2.0/2.1) processa eventos por agente da
seguinte forma:

```python
# runner/reactive.py (modelo vigente até este ADR)
for agent_id in self._agents:
    new_raw = await self._fetch_new_events(agent_id, cursor)
    for stream_id, mdata in new_raw:
        event = _parse_event(stream_id, mdata)
        world = await fold_world_for_agent(self._log, agent_id)
        # ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
        # Custo: O(N) onde N = total de eventos do agente
        for system in self._systems:
            out = system(world, event)
            #                              ^^^^^
            # Sistema recebe o evento específico que disparou este tick
```

Este modelo tem três problemas:

### 1.1 Redundância log ↔ fold

O dispatcher **lê** o stream via `xrange` (linha 187) e em
seguida **faz fold** de todos os eventos do agent (linha 203) —
incluindo os que acabou de ler. O Redis Stream já entrega os
eventos **ordenados** (IDs monotônicos); o fold re-ordena o que
já vem ordenado.

### 1.2 Custo quadrático em batches

Se o dispatcher processa um batch de M eventos novos no mesmo
tick, o fold é refeito M vezes. Custo total: **O(N × M)**
onde N = total de eventos do agente, M = eventos novos. Para
um agente com 10k eventos e um batch de 100, são 1M operações
de fold por tick.

### 1.3 Acoplamento sistema ↔ evento

O contrato atual é:

```python
ReactiveSystem = Callable[[World, Event], list[Event]]
```

A intuição histórica era "sistema reage a um evento específico".
Na prática, sistemas reativos olham **o estado do World** (via
`world.query_agents(MyComponent)`) e emitem eventos baseados
em **regras de negócio** — eles não inspecionam o `event` em si.

Exemplos típicos no framework:

```python
# System de SLA: "se agent.idle_for > 30s, emitir reminder"
async def sla_watchdog(world):
    for agent in world.query_agents(IdleState):
        if agent.component.idle_seconds > 30:
            yield ReminderEvent(...)

# System de retry: "se última tool call falhou, agendar retry"
async def retry_on_failure(world):
    for agent in world.query_agents(ToolCall):
        if agent.component.status == "failed":
            yield RetryScheduledEvent(...)
```

O `event` que disparou o tick é irrelevante para essas regras.
A `World` (componentes processados via projeção) é o estado
que importa.

---

## 2. Decisão

Adotamos o **modelo de World incremental** com **sistemas
dirigidos pelo estado**.

### 2.1 Novo contrato de sistema

```python
# core/system.py
class WorldSystem(Protocol):
    """
    A pure function from World to list[Event]. The framework
    invokes each registered system once per tick (after the
    incremental World fold). Systems do NOT receive the
    triggering event — they inspect the World's components
    (via ``world.query_agents(...)``) and emit events based
    on the business rules they encode.
    """

    def __call__(self, world: "World") -> list["Event"]: ...


# Backwards-compat aliases (deprecated)
ReactiveSystem = WorldSystem
CyclicSystem = WorldSystem

System = Callable[["World"], list["Event"]]
```

**Princípio:** sistema reativo é uma **regra de negócio** que
reage ao **efeito que o evento gerou no mundo**, não ao evento
em si. O efeito é capturado pela projeção (`World.with_event` →
componentes atualizados).

### 2.2 Fold incremental do World

O `World.with_event(event)` (já existente em `core/world.py`)
é a primitive incremental. A nova API é:

```python
@dataclass(frozen=True, slots=True)
class WorldCheckpoint:
    """Persisted state of an agent's incremental World."""
    world: World
    last_stream_id: str


class IncrementalWorldStore:
    """
    Per-agent World checkpoint persistence.

    Storage layout:
        Key: ``knt:world:{agent_id}``
        Value: pickled ``(World, last_stream_id)`` tuple
        TTL: 7 days (matches continuity default)

    The checkpoint is durable; a dispatcher restart resumes
    from the last saved position. The EventLog dedupe ensures
    no duplicate appends on replay.
    """

    def __init__(self, redis: Redis):
        self._redis = redis

    async def load(self, agent_id: str) -> WorldCheckpoint:
        raw = await self._redis.get(self._key(agent_id))
        if raw is None:
            return WorldCheckpoint(
                world=World.empty(),
                last_stream_id="-",
            )
        world, last_stream_id = pickle.loads(raw)
        return WorldCheckpoint(world=world, last_stream_id=last_stream_id)

    async def save(self, agent_id: str, ckpt: WorldCheckpoint) -> None:
        await self._redis.set(
            self._key(agent_id),
            pickle.dumps((ckpt.world, ckpt.last_stream_id)),
            ex=7 * 24 * 60 * 60,
        )

    @staticmethod
    def _key(agent_id: str) -> str:
        return f"knt:world:{agent_id}"
```

### 2.3 ReactiveDispatcher refatorado

```python
# runner/reactive.py
class ReactiveDispatcher:
    """
    Polls the EventLog for new events per agent. When new
    events arrive:

      1. Fold them into the agent's incremental World
         (O(1) per event via ``World.with_event``).
      2. Call each registered system once with the
         post-fold World.
      3. Append the systems' emitted events to the log.
      4. Persist the new World checkpoint.
    """

    async def dispatch_once(self) -> int:
        processed = 0
        for agent_id in list(self._agents):
            ckpt = await self._world_store.load(agent_id)
            new_raw = await self._fetch_new_events(
                agent_id, ckpt.last_stream_id
            )
            if not new_raw:
                continue

            # Incremental fold — O(M) where M = new events
            world = ckpt.world
            for stream_id, mdata in new_raw:
                event = _parse_event(stream_id, mdata)
                if self._filter and not self._filter(event):
                    continue
                world = world.with_event(event)
                processed += 1

            # Single call per system with the post-fold World
            outgoing: list[Event] = []
            for system in self._systems:
                out = system(world)
                if asyncio.iscoroutine(out):
                    out = await out
                if out:
                    outgoing.extend(out)

            if outgoing:
                await self._log.append_batch(outgoing)

            # Persist AFTER append (durability ordering)
            last_stream_id = _cursor_to_str(new_raw[-1][0])
            await self._world_store.save(
                agent_id,
                WorldCheckpoint(world=world, last_stream_id=last_stream_id),
            )
        return processed
```

### 2.4 Tabela de cadência

| Dispatcher       | Driver             | Cadência                  |
| ---------------- | ------------------ | ------------------------- |
| `ReactiveDispatcher` | EventLog (xrange) | quando novos eventos chegam |
| `Runner`         | Schedule (timer)   | a cada N segundos         |

Ambos os dispatchers chamam o mesmo conjunto de sistemas
(`WorldSystem`) com o mesmo World pós-fold. A distinção é
puramente sobre **quando** rodam, não sobre **o que** recebem.

---

## 3. Consequências

### 3.1 Performance

| Operação                   | Antes (v2.1) | Depois (v2.2) |
| -------------------------- | ------------ | ------------- |
| Fold por evento novo       | O(N)         | O(1)          |
| Batch de M eventos          | O(N × M)     | O(M)          |
| Sistema por tick            | M × (N × log) | M + (M × fold) |
| Idempotência                | Explícita     | Natural       |

Para um agente com 10k eventos e batch de 100 eventos:

| Métrica                | Antes   | Depois |
| ---------------------- | ------- | ------ |
| Operações de fold       | 1.000.000 | 100 |
| Latência por tick      | ~5s     | ~50ms |

### 3.2 Compatibilidade

**Breaking change** no contrato de sistema:

- `ReactiveSystem(world, event) -> list[Event]` → `WorldSystem(world) -> list[Event]`
- `CyclicSystem(world) -> list[Event]` (sem mudança de assinatura)

Sistemas existentes precisam:

1. Remover o parâmetro `event` da assinatura
2. Usar `world.query_agents(MyComponent)` para acessar o estado
3. Substituir lógica baseada em `event.event_type` por lógica baseada em `MyComponent.field`

Exemplo de migração:

```python
# Antes
async def retry_on_failure(world, event):
    if event.event_type == "tool.<name>.failed":
        yield RetryScheduledEvent(...)

# Depois
async def retry_on_failure(world):
    for agent in world.query_agents(ToolCall):
        if agent.component.status == "failed":
            yield RetryScheduledEvent(...)
```

Os aliases `ReactiveSystem` e `CyclicSystem` continuam existindo
para **backwards compat** de imports, mas sistemas que ainda
assinam `(world, event)` quebram em runtime (o segundo arg
não é mais passado). A mensagem de erro sugere a migração.

### 3.3 Idempotência natural

Com o fold incremental + checkpoint do World, a idempotência
flui naturalmente:

- Reprocessar o mesmo batch produz o mesmo World
- Mesmo World → mesmo output dos sistemas
- Mesmo output → mesmo set de events no EventLog
- EventLog deduplica via `event_id` (idempotente)

Não há mais necessidade de o sistema se preocupar com
"já processei este event?" — o dispatcher garante isso via
checkpoint.

### 3.4 Crash recovery

O checkpoint do World é durável em Redis. Em caso de crash:

1. Dispatcher reinicia
2. Carrega `WorldCheckpoint(agent_id)` do Redis
3. Lê eventos novos desde `last_stream_id`
4. Aplica incrementalmente (O(M))
5. Roda sistemas
6. Salva novo checkpoint

Não há replay do zero (O(N) por restart). Em produção com 10k
eventos por agente, o restart cai de ~5s para ~50ms.

### 3.5 O que NÃO muda

- **EventLog**: invariante. Append-only, IDs monotônicos, dedupe por `event_id`.
- **ToolInvoker + idempotency_key**: invariante. Tools externas continuam precisando de dedup próprio (a re-execução por crash recovery ainda é possível).
- **`World.with_event`**: invariante. A primitive incremental já existe desde a v2.0; só não estava sendo usada pelo dispatcher.
- **Projection (`project_default`)**: invariante. A função de reduzir evento → componentes continua a mesma.

### 3.6 Custo do snapshot — vale pagar?

> **Origin.** This section was carried over from
> [ADR-058 §2.5](./ADR-058-disaster-recovery.md) (rev. 4)
> after a scope-split between DR and component ownership.
> The DR ADR keeps only the operational concern
> ("snapshot is not a recovery target — the EventLog is");
> this section owns the cost/value question of the
> `IncrementalWorldStore` as a derived cache.

The `IncrementalWorldStore` is a **derived** per-agent Redis
pickle cache, with the EventLog (`Redis Streams`) as the
primary source of history. That hierarchy is correct. The
open question is **whether the cache pays for itself**.

Evidence gathered against the current implementation:

| Aspect | Reference | Cost |
|---|---|---|
| **Write cost per tick** | `src/kntgraph/runner/reactive.py:380` + `_systems_runner.py:97` | 2 Redis round-trips (load + save) + one `pickle.dumps` of the entire `World` |
| **Read cost per tick** | same | `pickle.loads` of the entire `World` |
| **Cold-start fallback** | `world_checkpoint.py:157-160` | `load()` returning empty → `World.fold` from the EventLog, O(N) |
| **Trim-resilience** | `test_stream_trim_does_not_break_resume` | survives `XTRIM` past the cursor |
| **Restart cost saved** | §3.1 (narrative) | "5 s → 50 ms" for 10 k events / agent — **not measured** |
| **Pickle security** | `world_checkpoint.py:60-66` | `# nosec B403`; operator-only |
| **Legacy dead code** | `src/kntgraph/infra/checkpoint.py` (~397 LOC) | unrelated, but indicates a migration that was not finished |
| **Documentation drift** | `docs/checkpoints.md` (337 lines) | still describes the v2.1 `ReactiveCheckpoint` model |

Two failure modes the snapshot must guard against to justify
its cost:

1. **Long-horizon restart.** A node reboots; the dispatcher
   has to fold the whole stream before accepting new
   intents. Without the snapshot, this is O(N_events_per_agent).
2. **Cold start after `XTRIM`.** If the EventLog has been
   trimmed past the snapshot's cursor, the snapshot is
   authoritative for the trimmed region. Without the
   snapshot, the dispatcher would have to fold from a stale
   cursor and might over- or under-apply deltas.

For (1), the framework's existing cold-start path
(`World.fold` from `XRANGE - +`) **already handles this**.
The snapshot is an **optimisation**, not a correctness
guarantee. For (2), the trim is monotonic and the snapshot's
`last_stream_id` is verified against the stream before
reuse (`world_checkpoint.py:152-198`); this is also a
**safety net**, not a correctness requirement.

**Recommendation (proposed, to be validated by benchmark):**

The snapshot is kept as an **optimisation tier** behind a
flag. The default is **on** in production (restart latency
matters at scale) and **off** in dev / CI. The flag is
`KNT_WORLD_CHECKPOINT_ENABLED` (Pydantic Settings), default
`true`. When `false`, the dispatcher uses cold-start on every
restart; this is acceptable in dev because `N` is small.

The benchmark that justifies (or vetoes) this default lives
in `tests/stress/test_dispatcher_restart_latency.py`. It
measures `time_to_first_intent_after_restart` at three event
densities (1 k, 10 k, 100 k events/agent) with and without
the snapshot. The benchmark is the gate: if the snapshot
does not yield a ≥ 5× speedup at the 10 k density, the
default flips to `false`.

This decision does **not** delete `IncrementalWorldStore`.
The code stays; the default changes. The legacy
`CheckpointStore` (~397 LOC + ~337 LOC of docs) **is**
deleted as a separate concern (DEBT.md), not under §3.5
"what does not change" and not under this section's
benchmark-driven default flip.

**Per-class interaction (added by rev. after the 057 §4.11.6
per-class retention split).** The `IncrementalWorldStore`
carries `last_stream_id_per_class` as part of its metadata
(one extra `dict[str, str]` field per checkpoint; no
schema change). The benchmark must measure cold-start time
under the **default per-class retention** — i.e. with the
`"lifecycle"` tail truncated aggressively and the
`"domain"` tail intact — to be representative of
production workloads. A benchmark against an
un-trimmed-only stream is no longer realistic.

---

## 4. Migração

### 4.1 Plano de rollout

1. **Fase 1**: introduzir `WorldSystem` Protocol em `core/system.py`
   mantendo `ReactiveSystem`/`CyclicSystem` como aliases idênticos.
2. **Fase 2**: refatorar `ReactiveDispatcher` para usar `IncrementalWorldStore`
   internamente — a API pública do dispatcher permanece
   `add_reactive_system(system: ReactiveSystem)`.
3. **Fase 3**: atualizar sistemas no framework (`cache_warmer`,
   `knowledge_consolidator`, etc.) para a nova assinatura.
4. **Fase 4**: atualizar testes (mecânico — ignora segundo arg).
5. **Fase 5**: marcar `ReactiveSystem` como deprecated em favor
   de `WorldSystem`. Remover aliases em v3.0.

### 4.2 Compatibilidade durante transição

Sistemas que ainda assinam `(world, event)` recebem
`(world,)` (1-arg) durante a transição. O dispatcher detecta:

```python
# Pseudocódigo de compat no dispatcher
import inspect
sig = inspect.signature(system)
if len(sig.parameters) == 2:
    # Legacy: sistema espera (world, event)
    # Passamos o último event do batch (deprecation path)
    out = system(world, last_event_of_batch)
else:
    # Modern: sistema opera no World
    out = system(world)
```

A versão 2.2 mantém ambos. A versão 3.0 remove o path legacy.

### 4.3 Testes de regressão

- **Idempotência**: rodar dispatcher 2× com mesmo batch → mesmo output, mesmo checkpoint
- **Crash recovery**: save checkpoint, restart, resume → mesmo World pós-batch
- **Performance**: medir tempo por tick com N=10k, M=100 → < 100ms
- **Reordering**: eventos com mesmo `tick` aplicados em ordem → mesmo World
- **Composição**: `ReactiveSystem` + `CyclicSystem` ambos no mesmo dispatcher → ambos rodam com mesmo World

---

## 5. Decisões relacionadas

- **World serialization (pickle, MVP)**: optamos por pickle
  por simplicidade. O `World` tem `storage`
  (`ArchetypeStorage`) e `views` (dict de `AgentView`) —
  não-trivial serializar em JSON. Pickle é suficiente para
  checkpoint interno ao Redis **enquanto o sistema é
  single-process e single-language**.

  **Trade-offs documentados** — quando qualquer um dos
  seguintes virar realidade, migramos:

    1. **Cross-language**: outro serviço (Rust/Go) precisa
       ler os checkpoints. Pickle é Python-only.
    2. **Schema versioning**: o shape de `World` muda
       (novo campo, renomeação). Pickle não tem schema —
       checkpoints antigos podem quebrar em runtime.
    3. **Human-inspectable**: ops precisa inspecionar
       checkpoints via `redis-cli GET`. Pickle é binário
       ilegível.
    4. **Security review**: revisão de segurança exige que
       o formato de serialização NÃO execute código no
       load. Pickle é vetor de RCE se um atacante
       comprometer o Redis.

  **Migração futura recomendada**: Pydantic + JSON.

    - Adicionar `WorldSnapshot(BaseModel)` com sub-models
      para `AgentView` e `ArchetypeStorage` (Pydantic v2
      gera os validadores).
    - `IncrementalWorldStore.save` chama
      `WorldSnapshot.from_world(ckpt.world).model_dump_json()`.
    - `load` faz `WorldSnapshot.model_validate_json(raw)` e
      reconstrói `World` via `World.from_snapshot(snap)`.

  Benefícios: schema versioning automático, validação no
  load, human-readable (`redis-cli GET knt:world:a-1 |
  jq`), sem risco de execução de código.

- **TTL de 7 dias**: matches o TTL de `continuity_ttl_seconds`
  (90 dias foi considerado, mas 7 dias é suficiente — um agente
  inativo por mais de 7 dias é re-bootstrapado do zero via fold
  completo na próxima ativação).

- **Sem mudança na assinatura do Runner**: o `Runner` (scheduler)
  continua chamando `system(world)` para sistemas cíclicos.
  Apenas o `ReactiveDispatcher` ganha o caminho incremental.

---

## 6. Referências

- [World.with_event em core/world.py](../fmh_backend/src/fmh_backend/core/world/world.py)
- [World.query_agents em core/world.py](../fmh_backend/src/fmh_backend/core/world/world.py)
- [ADR-001 — Arquitetura geral](./ADR-001-Arquitetura.md)
- [ADR-002 — Replay puro](./ADR-002-Replay-Puro.md)
- [ADR-005 — Checkpoints e idempotency](./ADR-005-Checkpoints-Idempotency.md)
- [ADR-010 — Memory business tier](./ADR-010-Memory-Business-Tier.md)
