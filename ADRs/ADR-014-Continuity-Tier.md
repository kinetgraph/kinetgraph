<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-014: Memory Tier "continuity" — Separar Estado-de-Uso Recente de Preferências Estáticas

**Status:** Aceito
**Data:** 20 de junho de 2026
**Versão:** 1.0
**Autores:** Equipe de Arquitetura FMH
**Relacionado a:** [ADR-001](./ADR-001-Arquitetura.md), [ADR-004](./ADR-004-Memory-Tools-Knowledge.md), [ADR-010](./ADR-010-Memory-Business-Tier.md)

---

## 1. Contexto

O FMH v0.2.x tem três tiers de memória funcionando:

- **`session`** (`memory/session.py`) — conversa atual, TTL 24h, cache read-through.
- **`profile`** (`memory/profile.py`) — config estável da PME, sem TTL, cache read-through.
- **`business`** (ADR-010, FalkorDB) — knowledge agregado de tool calls, permanente.

O `profile` foi modelado em ADR-004 §2.1 como "Preferências da PME / usuário" — Redis Hash, meses–anos. Em produção, ele está sendo **sobrecarregado**: dois tipos de estado cohabitam o mesmo agente `profile:{tenant_id}:{user_id}` e o mesmo vocabulário `profile.preference_set`:

1. **Preferências estáticas** — coisas que definem a PME:
   `tier=vip`, `regime_tributario=simples`, `email_nfe=…`,
   `default_cfop=6102`, `idioma=pt-BR`. Mudam raramente, são
   auditadas por compliance, não carregam PII de cliente.

2. **Estado-de-uso recente** — coisas que definem o que a PME
   **estava fazendo**:
   `last_cnpj_cliente=…`, `last_cfop_used=6102`,
   `last_supplier_id=…`, `last_category=…`.
   Mudam a cada tool call, são o input do próximo agent run,
   e podem carregar PII do último cliente atendido.

Consequências de manter os dois misturados no `profile`:

- O `World.fold` retorna um dict achatado e o caller não sabe se está olhando `tier=vip` (config) ou `last_cnpj=…` (recência). Semântica ambígua no mesmo `agent_id`.
- TTL é o mesmo para os dois (sem TTL por design), mas o estado-de-uso recente **precisa** expirar (LGPD: o último CNPJ do cliente não pode ficar para sempre).
- PII gate: as preferências estáticas são da PME (não sensíveis), mas `last_cnpj_cliente` é PII de terceiro e exige redaction antes de qualquer projeção (cf. ADR-010 §2.5 para Solutions — o mesmo padrão precisa aplicar aqui).
- Confidence bump cross-agent (ADR-010 §2.6) não faz sentido para config estável, mas faria sentido para "CFOP 6102 é o mais usado pelo tenant nos últimos 30 dias".
- Audit trail misturado: um auditor buscando "quem mudou o regime tributário" tem que filtrar `profile.preference_set` para separar config de recência.

Este ADR registra a decisão de criar um **quarto tier**,
`continuity`, dedicado exclusivamente ao **estado-de-uso
recente**, mantendo `profile` focado em **preferências
estáticas**.

---

## 2. Decisão

### 2.1 `MemoryKind` ganha `"continuity"`

```python
MemoryKind = Literal["session", "profile", "continuity", "business"]
```

A tabela de tiers em ADR-004 §2.1 ganha uma linha:

| Tier | Caso de uso | Estrutura | Chave | Vida útil |
|------|-------------|-----------|-------|-----------|
| **Working** | ECS (World, AgentView) | In-memory | — | Tick (volátil) |
| **Session** | Conversa, working memory | Redis JSON com TTL | `knt:session:{session_id}` | min–horas |
| **Profile** | Preferências estáticas da PME | Redis Hash | `knt:profile:{tenant_id}:{user_id}` | meses–anos |
| **Continuity** | Estado-de-uso recente (última tool, último cliente) | Redis Hash com TTL sliding | `knt:continuity:{tenant_id}:{user_id}` | dias–semanas (sliding) |
| **Knowledge** (Documentos) | Busca semântica livre em eventos indexados | FalkorDB — sub-grafo `(:Document)` | `knt:tenant:{cnpj}` (graph) | permanente |
| **Knowledge** (Solutions) | Reuso de tool calls bem-sucedidas | FalkorDB — sub-grafo `(:Problem)-[:SOLVED_BY]->(:Action)-[:ON_TOOL]->(:Tool)` | `knt:tenant:{cnpj}` (graph) | permanente |
| **Event log** | Source of truth (TUDO) | Redis Streams per-agent | `knt:agents:{id}:events` | permanente (trim) |

### 2.2 Princípio de separação

> **`profile` modela "o que a PME é".**
> **`continuity` modela "o que a PME estava fazendo".**

Critério prático para classificar um novo campo:

- Se muda **sem interação do usuário** (ex: tier alterado por billing) → `profile`.
- Se muda **em resposta a uma tool call** (ex: CFOP escolhido pelo agent na última NF-e) → `continuity`.
- Se muda **por configuração explícita** (ex: e-mail de NF-e editado em tela) → `profile`.
- Se **carrega PII de terceiro** (ex: último CNPJ de cliente) → `continuity` com hash, não `profile`.

### 2.3 `ContinuityManager` segue o mesmo padrão de `ProfileManager`

Mesmo shape de `BaseShortTermMemory[ContinuityState]`:

```python
# agent_id = "continuity:{tenant_id}:{user_id}"
# Eventos: continuity.created, continuity.tool_used,
#          continuity.entity_seen, continuity.category_chosen,
#          continuity.cleared
# State: dict[str, str] com prefixos (tool:, entity:, last:)
```

**Princípio reusado do ADR-004 §2.2**: `continuity` é um **agente** no EventLog. As mesmas regras se aplicam:

- Replay puro reconstrói o estado a partir do log.
- Audit trail completo: "qual foi o último CFOP usado, quando e por qual tool" é uma query de correlação no EventLog.
- Idempotência do EventLog evita duplicação (dois adapters emitindo `continuity.tool_used` para o mesmo `tool` no mesmo tick = 1 evento).
- Redis Hash é **cache**, não fonte da verdade.

### 2.4 Vocabulário de eventos

```python
class ContinuityEventType:
    CREATED = "continuity.created"
    TOOL_USED = "continuity.tool_used"
    ENTITY_SEEN = "continuity.entity_seen"
    CATEGORY_CHOSEN = "continuity.category_chosen"
    CLEARED = "continuity.cleared"
```

**`continuity.tool_used`** — disparado pelo adapter após `tool.*.completed` (não no `requested` — espelha ADR-010 §2.3, evita promote de tentativas falhas):

```python
data = {
    "tool": "invoice.issue",
    "params_fingerprint": "sha256:abc123...",   # nunca o valor raw
    "result_signature":   "sha256:def456...",
    "latency_ms":         312,
    "at":                 <event.timestamp>,
}
```

**`continuity.entity_seen`** — disparado quando o agent encontra uma entidade extraída (CNPJ, CPF, chave de NF-e):

```python
data = {
    "kind":      "cnpj" | "cpf" | "chave_nfe" | "cep" | ...,
    "value_hash":"sha256:...",     # nunca o valor raw
    "source":    "tool_result" | "user_input" | "graphrag",
    "at":        <event.timestamp>,
}
```

> **Regra de PII (cf. ADR-010 §2.5)**: o `value_hash` é hash, não valor. Se o agent precisa do valor de volta, busca no EventLog completo (que tem o valor original com audit) ou na entidade canônica do Knowledge tier. `continuity` é índice de **recência**, não de **lookup**.

**`continuity.category_chosen`** — escolha de categoria operacional (CFOP, categoria de despesa, centro de custo):

```python
data = {
    "slot":  "cfop" | "cost_center" | "expense_category" | ...,
    "value": "6102",
    "at":    <event.timestamp>,
}
```

**`continuity.cleared`** — evento terminal disparado por **LGPD right-to-erasure** ou por expiração de janela de retenção. Após `cleared`, o `fold` retorna estado vazio até o próximo `tool_used`. Idempotente em `(tenant_id, user_id)`.

### 2.5 Hash layout (Redis cache)

`knt:continuity:{tenant_id}:{user_id}` — Hash com prefixos por tipo de slot (mesma convenção de `ProfileManager` em `profile.py:295-322`):

| Campo Hash | Conteúdo | Exemplo |
|---|---|---|
| `tool:{tool_name}` | `result_signature\|latency_ms\|at` | `tool:invoice.issue=sha256:def456\|312\|1718000000.0` |
| `entity:{kind}:{value_hash}` | `at` | `entity:cnpj:sha256:abc123=1718000000.0` |
| `last:{slot}` | `value\|at` | `last:cfop=6102\|1718000000.0` |
| `created_at`, `updated_at`, `cleared_at` | float | — |

O `DEL+HSET` transactional pipeline de `ProfileManager._store_cache` é reusado sem modificação.

### 2.6 TTL sliding

`continuity` expira por **inatividade**, não por idade fixa. A política default é sliding 90 dias: cada write renova o TTL.

```python
DEFAULT_TTL_SECONDS = 90 * 24 * 3600  # 90 dias
```

Implementação no `_store_cache` (paralelo a `ProfileManager._store_cache`):

```python
async with self._redis.pipeline(transaction=True) as pipe:
    pipe.delete(key)
    pipe.hset(key, mapping=payload)
    if ttl:
        pipe.expire(key, ttl)         # sliding: renova a cada write
    await pipe.execute()
```

Configurável por env `KNT_CONTINUITY_TTL_S` (default `7776000`).

**Por que sliding, não fixo**: o último CFOP usado em 2024-01 e nunca mais é informação morta — vence. O último CFOP usado ontem continua relevante. Sliding captura "ainda está em uso" sem precisar de job de limpeza.

### 2.7 PII: hash-only, fail-closed

`continuity.entity_seen.value_hash` é **sempre** `sha256(value)[:16]` — mesmo algoritmo de `(:Problem).fingerprint` em ADR-010 §3. **`value` raw nunca é gravado no EventLog do `continuity` agent**.

Política fail-closed herdada de ADR-010 §2.5:

- Se a função de hashing lançar exceção, o `ContinuityManager.record_entity_seen` **não emite o evento** e propaga o erro.
- O caller decide o que fazer (logar, enviar pro DLQ, etc.). Nada grava parcial.

`record_tool_used` e `record_category_chosen` também são fail-closed: se a serialização falhar, não emitem.

### 2.8 Sem confidence bump cross-agent

Diferente de ADR-010 §2.6 (Solutions), `continuity` **não** tem threshold de confidence. O último uso é o último uso — não precisa de cross-agent bump. Cross-agent aggregation é trabalho do `business` tier (FalkorDB Solutions).

Se um dia precisar de "CFOP mais frequente nos últimos 30 dias por tenant", é uma **query** sobre o `business` tier (`MATCH (p:Problem)-[:SOLVED_BY]->(a:Action) WHERE …`), não um campo do `continuity`.

### 2.9 LGPD: `continuity.cleared`

Quando o usuário exerce direito ao esquecimento (LGPD art. 18), o sistema emite `continuity.cleared` para `(tenant_id, user_id)`:

```python
data = {
    "reason": "lgpd_erasure" | "user_request" | "retention_expired",
    "at":     <event.timestamp>,
}
```

Após `cleared`:

- O `fold` retorna `ContinuityState` com `last_tools={}`, `last_entities={}`, `last_categories={}`, `cleared_at=<ts>`.
- Reads subsequentes recebem estado vazio até o próximo `tool_used`.
- O cache Redis é `DEL`-ado no `refresh_cache`.
- O histórico de eventos `tool_used` / `entity_seen` permanece no EventLog (audit), mas **não é mais projetado** no cache nem usado para `recency_suggest()`.

---

## 3. Pipeline Consolidator: 4 buses → 4 sinks

Extensão do diagrama de ADR-010 §4:

```
                       ┌─────────────────────────────────┐
                       │       EventLog (Redis Streams)   │
                       │  (source of truth)               │
                       └────────────────┬─────────────────┘
                                        │
                               World.fold(events)
                                        │
        ┌──────────────────┬────────────┼────────────┬─────────────────┐
        ▼                  ▼            ▼            ▼                 ▼
┌────────────────┐ ┌────────────────┐ ┌──────────────┐ ┌────────────┐ ┌────────────────┐
│ Consolidator  │ │ Consolidator  │ │ Consolidator │ │Consolidator│ │  Knowledge     │
│ (Redis cache) │ │ (Redis cache) │ │(Redis cache) │ │(Redis cache)│ │  Consolidator  │
│ cyclic, tick  │ │ cyclic, tick  │ │ cyclic, tick │ │cyclic, tick │ │  post-tick     │
└────────┬───────┘ └────────┬───────┘ └──────┬───────┘ └──────┬──────┘ └────────┬───────┘
         ▼                  ▼                ▼                ▼                ▼
┌────────────────┐ ┌────────────────┐ ┌──────────────┐ ┌────────────┐ ┌────────────────┐
│ CacheRefreshBus│ │ CacheRefreshBus│ │CacheRefreshBus│ │CacheRefreshBus│ │ SolutionPromo- │
│  (session)     │ │  (profile)     │ │ (continuity) │ │ (... futura) │ │ tionBus        │
└────────┬───────┘ └────────┬───────┘ └──────┬───────┘ └──────┬──────┘ └────────┬───────┘
         ▼                  ▼                ▼                ▼                ▼
┌────────────────┐ ┌────────────────┐ ┌──────────────┐ ┌────────────┐ ┌────────────────┐
│ CacheWarmer    │ │ CacheWarmer    │ │ CacheWarmer  │ │ CacheWarmer│ │ SolutionPromot.│
│ → Redis JSON   │ │ → Redis Hash   │ │→ Redis Hash  │ │ → (futuro) │ │ → FalkorDB     │
└────────────────┘ └────────────────┘ └──────────────┘ └────────────┘ └────────────────┘
```

`MemoryKind` discriminante cobre os 4 tipos no `match` exaustivo do `Consolidator.refresh_all`.

---

## 4. Critérios de Aceitação

- [ ] `MemoryKind` aceita `"continuity"` e o `match` no `Consolidator` trata exaustivamente.
- [ ] `ContinuityManager` herda de `BaseShortTermMemory` sem duplicar orquestração cache/fold.
- [ ] `agent_id_prefix = "continuity:"` em `ContinuityManager`; `parse_agent_id` reconhece o prefixo.
- [ ] `CacheRefreshKind = Literal["session", "profile", "continuity"]` no `cache_warmer.py`.
- [ ] `CacheWarmer.pump_once` despacha os 3 kinds; falhas em um não abortam o batch.
- [ ] `Projector.project_continuity(tenant_id, user_id)` + entrada em `project_all`.
- [ ] TTL sliding: `EXPIRE` renovado a cada `_store_cache`. Configurável via env `KNT_CONTINUITY_TTL_S`.
- [ ] `continuity.entity_seen` armazena **apenas** `value_hash` (sha256 truncado); `value` raw nunca chega no EventLog do `continuity` agent.
- [ ] `continuity.tool_used.params_fingerprint` é hash dos params, não params raw.
- [ ] Falha no hash → `record_entity_seen` propaga erro, não emite evento (fail-closed).
- [ ] `continuity.cleared` zera o estado projetado no cache; histórico no EventLog permanece intocado.
- [ ] `recency_suggest(tenant, user, slot) -> Optional[str]` retorna o último `last:{slot}` se não expirado.
- [ ] Confidence bump cross-agent **não** existe em `continuity` (responsabilidade do `business` tier).
- [ ] `recency_suggest` respeita `cleared_at` (não sugere após erasure).
- [ ] Documentação: `memory/__init__.py` lista os 4 tiers; `ARCHITECTURE.md §3.1` atualizado; ADR-004 §2.1 referência cruzada.

---

## 5. Consequências

### Positivas

- ✅ **Separação semântica clara**: `profile` = "o que a PME é", `continuity` = "o que a PME estava fazendo". Caller não confunde.
- ✅ **LGPD por design**: `continuity` tem `cleared` nativo e TTL sliding. Preferências estáticas (regime tributário, tier) ficam no `profile` sem prazo.
- ✅ **PII isolada**: `last_entities` guarda hash, não valor. Mesmo padrão de `(:Problem).fingerprint` (ADR-010 §3).
- ✅ **Vocabulário próprio**: `tool_used` / `entity_seen` / `category_chosen` são semanticamente específicos. Nada de `profile.last_cnpj` ou `profile.cfop_used`.
- ✅ **Mesma infra**: `BaseShortTermMemory` + `Consolidator` + `CacheWarmer` cobrem o novo tier sem código novo de orquestração. Só `_read_cache`, `_serialize_for_cache`, `_fold_from_log`, `_store_cache` no subclass.
- ✅ **Audit trail correto**: events `continuity.*` no EventLog permitem query "qual foi o último CFOP do user X em 30 dias" sem varrer o `profile`.

### Negativas

- ⚠️ **Mais um agente no World**: cada `(tenant, user)` ganha um terceiro agent_id (`profile:` + `continuity:`). Aceitável — são chaves Redis separadas e o `parse_agent_id` é O(prefixos).
- ⚠️ **Migração de dados existentes**: se já há `profile.preference_set` com campos de recência (`last_cnpj`, `last_cfop`), precisam ser migrados para `continuity.tool_used` / `continuity.category_chosen`. Mitigação: ADR recomenda **migration script** que lê o EventLog, classifica cada `preference_set` por nome de chave, e emite os eventos `continuity.*` correspondentes antes de cortar a leitura do `profile`. Idempotente em `(tenant, user, slot)`.
- ⚠️ **Sliding TTL precisa ser renovado a cada write**: se um write falhar parcialmente (pipeline interrompido), o TTL pode não ser renovado. Mitigação: o `expire` é a última instrução do pipeline transactional; se ela não roda, o write também não roda (atomicidade).
- ⚠️ **`cleared` é irreversível no cache**: o EventLog mantém o histórico, mas a `recency_suggest` deixa de funcionar até novo uso. Aceitável — é o comportamento desejado para LGPD.

### Mitigações

| Problema | Mitigação |
|----------|-----------|
| Migração de `profile` antigo com campos de recência | Script `scripts/migrate_profile_to_continuity.py` lê EventLog, classifica, emite `continuity.*`. Idempotente. Dry-run por padrão; `--commit` aplica. |
| TTL não renovado por falha de pipeline | `expire` é última instrução do pipeline transactional; atomicidade garante consistência. |
| Caller ainda escreve no `profile` o que devia ser `continuity` | Doc + lint warning: chaves começando com `last_` ou `recent_` no `profile` viram erro de convenção (a ser definido em ADR futuro). |
| Schema drift entre `continuity` e `business` | `continuity` é per-user, `business` é per-tenant agregação. Documentado em ARCHITECTURE §3.1. |

---

## 6. Roadmap de Implementação

1. **Fase 1** (esta sprint): este ADR + updates de
   `ADR-004` §2.1, `ADR-010` §3 (cross-reference), `ARCHITECTURE.md` §3.1,
   `memory/__init__.py` docstring.
2. **Fase 2**: `memory/continuity.py` (`ContinuityState`,
   `ContinuityManager`, `ContinuityEventType`,
   `_fold_continuity_events`); update `consolidation.py`
   (`MemoryKind` ganha `"continuity"`, `MemoryAgent.continuity`,
   `_MANAGER_REGISTRY`, `parse_agent_id`); update
   `cache_warmer.py` (`CacheRefreshKind` ganha `"continuity"`,
   `CacheWarmer.pump_once` nova branch); update `Projector`
   (`project_continuity`, `project_all`). Testes unitários
   paralelos aos de `profile`.
3. **Fase 3**: `recency_suggest(tenant, user, slot)` API +
   exemplo `10_continuity_recency.py` demonstrando agent que
   reabre e recebe sugestão do último CFOP. Testes de
   integração (Redis real, sliding TTL, `cleared`).
4. **Fase 4**: `scripts/migrate_profile_to_continuity.py`
   para tenants legados; lint warning para chaves
   `last_*` / `recent_*` em `profile`.

---

## 7. Referências

- [ADR-001: Arquitetura geral](./ADR-001-Arquitetura.md)
- [ADR-004: Memory Tiers, Tools e Projeção para FalkorDB](./ADR-004-Memory-Tools-Knowledge.md)
- [ADR-010: Memory Tier "business" — Solutions](./ADR-010-Memory-Business-Tier.md)
- [ADR-013: Semantic Routing GLiNER2](./../../fmh_agents/ADRs/ADR-013-Semantic-Routing-GLiNER2.md)
- [Redis Agent Builder: How agents work](https://redis.io/docs/latest/develop/ai/agent-builder/agent-concepts/)
