<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-019: Adapter Redis — encapsulamento tipado por sub-responsabilidade

**Status:** Aceito
**Data:** 28 de junho de 2026
**Relacionado:** [AGENTS.md](../AGENTS.md) §1, §3, §6; ADR-005 (Checkpoints); ADR-016 (Event Signing)

## 1. Contexto

Antes desta iteração, o framework `fmh_backend` tinha **11 módulos** que importavam `redis.asyncio` diretamente como dependência top-level:

- `stream/event_log/store.py`, `infra/checkpoint.py`, `infra/idempotency.py`
- `events/dlq/store.py`, `events/dlq/actions.py`
- `memory/{base,session,profile,continuity/manager}.py`
- `api/auth.py`

O tipo `redis.asyncio.Redis` vazava pela base de código como se fosse um detalhe interno. O `EventLog.append` era um **god method** com ~140 LOC e complexidade ciclomática ~11 — misturando 4 estágios: validação, signature, idempotência (3 fases em 1) e dispatch de resilience.

A AGENTS.md §1 prescreve: "toda entrada vinda de biblioteca externa deve ser encapsulada em um adapter type definido dentro do framework". A regra nunca havia sido aplicada ao Redis.

## 2. Decisão

### 2.1 Estrutura do adapter

Criar `fmh_backend/src/fmh_backend/infra/redis/` com sub-pacotes por sub-responsabilidade:

```
infra/redis/
├── __init__.py            # API pública
├── _client.py             # Protocol RedisLike (fronteira tipada)
├── _codec.py              # bytes↔str (decode_value, decode_dict, decode_int_dict)
├── _pool.py               # RedisPool + create_redis_pool
├── _errors.py             # RedisUnavailableError, IdempotencyConflict
├── _factory.py            # create_event_log_storage (settings-driven)
└── _event_log/            # sub-adapter: EventLog
    ├── __init__.py
    ├── _adapter.py        # Protocol EventLogStorage + RedisEventLogAdapter
    ├── _keys.py           # AGENT_STREAM_KEY, EVENT_ID_INDEX, SCAN_PATTERN
    └── _idempotency.py    # 3 fases isoladas: _check_phase, _claim_phase, _finalize_phase
```

Iteração 1 cobre apenas o sub-adapter `_event_log/`. Os shards `_memory/`, `_dlq/`, `_auth/`, `_checkpoint/` seguem em iterações separadas.

### 2.2 Princípio "1 tecnologia = 1 módulo adapter"

`redis.asyncio` agora só importa em **dois lugares**:

1. `infra/redis/_pool.py` — lazy dentro de `RedisPool.from_settings` (factory).
2. `infra/redis/_client.py` — bloco `TYPE_CHECKING`.

Todo o resto consome `RedisLike` (Protocol) ou `EventLogStorage` (Protocol).

### 2.3 Decomposição do god method

`EventLog.append` foi decomposto em:

| Antes | Depois |
|---|---|
| `EventLog.append` (~140 LOC, CC~11) | `EventLog._preflight` (CC=5) + `EventLog.append` (CC=2) + `_do_storage_call` (closure) |
| `claim_event_id_slot` (~65 LOC, 3 fases em 1) | `_check_phase` + `_claim_phase` + `_finalize_phase` + orchestrator |
| `EventLog.__init__` mistura storage + resilience + signature | `EventLog.__init__` (storage only) + dispatch layer isolado em `_dispatch` |
| Codec/keys espalhados (`stream/event_log/store.py`) | Centralizados em `infra/redis/_event_log/_keys.py` e `_codec.py` |

### 2.4 Back-compat (decisão intencional, AGENTS.md §2)

Os arquivos legados (`infra/redis.py`, `infra/redis_codec.py`, `infra/idempotency.py`) foram **convertidos em shims 1-linha** que re-exportam dos novos módulos. Call sites externos não quebram.

O `EventLog.__init__` aceita três formas (ordem de resolução):

1. `storage=` (nova API, recomendado)
2. `redis_client=` kwarg (legado, deprecated)
3. `EventLog(client)` posicional (legado, detectado por duck typing vs `EventLogStorage`)

A heurística duck-type distingue: `EventLogStorage` tem `read_latest`, `stream_len`, `list_agents`; um Redis cru tem `xadd`, `xrange` mas não estes.

### 2.5 Gate de complexidade (radon)

Adotar `radon>=6.0` como dev dependency. Pipeline CI local em `scripts/ci.py` (script PEP 723 executável via `uv run scripts/ci.py`).

Gates:
- **CC ≤ 10** (grade B do radon) — hard fail quando excedido
- **MI ≥ 20** (grade A do radon) — hard fail quando abaixo
- **No regression** vs `.radon-baseline.json` — hard fail quando algum bloco piora

Política: **baseline + regressão**. God methods pré-existentes são tolerados até refator intencional (atualizar baseline via `--update-baseline`). Novas introduções de god methods são rejeitadas.

## 3. Consequências

### Pros

- **Encapsulamento total**: substituir `redis.asyncio` (por exemplo, por `pyredis-v2` ou outro backend) requer mudar apenas `_pool.py`.
- **Testabilidade**: `EventLog` testável sem Redis real — basta injetar uma `EventLogStorage` fake.
- **Decomposição dos god methods**: `EventLog.append` caiu de CC=11 → CC=2; `claim_event_id_slot` agora tem 4 funções testáveis independentemente.
- **Gate automático**: regressões de complexidade são bloqueadas antes do merge.
- **Sem breaking change**: 0 call sites externos quebraram (verificado em `fmh_app`, `fmh_office`, `fmh_agents`).

### Cons

- **Indireção**: 1 chamada extra por operação de I/O (delegação EventLog → storage → client). Custo negligível (~microsegundos).
- **Heurística de duck typing**: o `EventLog.__init__` faz um check estrutural para distinguir storage vs redis client. Falsos positivos possíveis se uma classe mock implementar ambos os shapes.
- **Shims deprecated**: 3 arquivos legados viraram shims — devem ser removidos em release futura.

### Métricas observadas (após iteração 1)

| Métrica | Antes | Depois |
|---|---|---|
| Imports `redis.asyncio` em framework | 11 | 2 (só no adapter) |
| LOC `EventLog.append` | ~140 | ~6 |
| CC `EventLog.append` | ~11 | 2 |
| CC `claim_event_id_slot` (orquestrador) | ~8 | ~3 |
| Tests passando | 870 | 912 |
| Tests RED do adapter | 0 | 32 (todos verdes após GREEN) |

## 4. Migration

### Call sites atualizados (mesmo commit)

- `fmh_backend/src/fmh_backend/stream/event_log/store.py` — `EventLog` refatorado para orquestrador fino; storage injetado.
- `fmh_backend/src/fmh_backend/stream/event_log/__init__.py` — re-exporta `AGENT_STREAM_KEY`/`EVENT_ID_INDEX`/`claim_event_id_slot` do novo local para back-compat.

### Call sites ainda na API legada (próximas iterações)

- `fmh_app/src/fmh_app/app_runner.py:205` — `EventLog(redis_client)`
- `fmh_app/src/fmh_app/mvp/http.py:296` — `EventLog(redis_client)`
- `fmh_office/src/fmh_office/mvp/pedido.py:223` — `EventLog(redis_client)`
- `fmh_office/src/fmh_office/mvp/http.py:296` — `EventLog(redis_client)`

Estes continuam funcionando via back-compat shim. Devem ser migrados para `storage=` antes de remover os shims.

### Próximas iterações

| # | Escopo | Estimativa |
|---|---|---|
| 2 | `_memory/` sub-adapter (Session, Profile, Continuity) | Médio |
| 3 | `_dlq/` sub-adapter (DLQStorage + idempotency HASH-based) | Médio |
| 4 | `_auth/` sub-adapter (APIKeyStorage, parte Redis) | Pequeno |
| 5 | `_checkpoint/` sub-adapter (CheckpointStorage) | Pequeno |
| 6 | Remover shims legados; atualizar call sites | Pequeno |

## 5. Decisões relacionadas

- **AGENTS.md §1**: "zero `Any`, zero `object` no framework" — estendido para "1 lib externa = 1 adapter Protocol".
- **AGENTS.md §2**: "compat shims apenas quando há outros consumidores; aqui somos single-workspace, mas rolling out merece transição suave".
- **AGENTS.md §6**: tipagem concreta de erros — `RedisUnavailableError`, `IdempotencyConflict` substituem catches genéricos de `Exception`.

## 6. Referências

- [AGENTS.md §1](../AGENTS.md) — adapter types
- [AGENTS.md §3 — god modules](../../AGENTS.md) — files > 500 LOC devem ser divididos
- [scripts/ci.py](../../scripts/ci.py) — pipeline CI local
- [infra/redis/_client.py](../src/fmh_backend/infra/redis/_client.py) — Protocol RedisLike
- [infra/redis/_event_log/_adapter.py](../src/fmh_backend/infra/redis/_event_log/_adapter.py) — Protocol EventLogStorage
- [infra/redis/_event_log/_idempotency.py](../src/fmh_backend/infra/redis/_event_log/_idempotency.py) — 3 fases isoladas
- [stream/event_log/store.py](../src/fmh_backend/stream/event_log/store.py) — EventLog refatorado (orquestrador fino)