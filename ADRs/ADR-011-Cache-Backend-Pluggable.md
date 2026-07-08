<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-011: Cache Backend Plugável (InMemory LRU + Redis) e Reescrito do `CachingLLMTransport`

**Status:** Aceito
**Data:** 11 de junho de 2026
**Versão:** 0.4.0
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** [ADR-008](./ADR-008-Caching-Transport.md), [fmh_backend/docs/analysis/fmh_agents_consistency.md](../../fmh_backend/docs/analysis/fmh_agents_consistency.md)

---

## 1. Contexto

O `CachingLLMTransport` (ADR-008) introduziu o decorator
pattern que fecha a janela entre o `idempotency_key` do
framework e o LiteLLM sem cache server-side. A
implementação Fase 4 tinha **duas dívidas técnicas** que
a análise de consistência (`fmh_agents_consistency.md`)
listou como não-bloqueantes:

1. **Backend in-memory apenas**: o transporte
   aceitava um `backend: dict | None` e armazenava
   entries em dict. Em produção com múltiplos processos,
   cada worker tinha seu próprio cache → chamadas
   duplicadas e estado divergente.

2. **Sem LRU**: o dict crescia sem limite. Uma
   workload de longa duração (ex: agente de fechamento
   mensal) podia esgotar a memória.

Além disso, o usuário pediu **LRU como estratégia de
eviction** durante a discussão (em vez de FIFO, LFU ou
nenhuma).

---

## 2. Decisão

Reescrever o `CachingLLMTransport` e o pacote
`fmh_agents/tools/cache.py` em torno de **3 primitivos**:

1. **`AsyncCacheStorage` Protocol** — interface async
   com 3 métodos (`get`, `set`, `delete`).
2. **`InMemoryCacheStorage`** — implementação default
   com **`OrderedDict` + LRU** (maxsize opcional).
3. **`RedisCacheStorage`** — implementação async para
   multi-processo, com `HSET + EXPIRE` em pipeline.

### 2.1 Protocol `AsyncCacheStorage`

```python
@runtime_checkable
class AsyncCacheStorage(Protocol):
    async def get(self, key: str) -> Optional[_CacheEntry]: ...
    async def set(self, key: str, entry: _CacheEntry) -> None: ...
    async def delete(self, key: str) -> None: ...
```

`Protocol` (não ABC) para duck typing — qualquer objeto
com esses 3 métodos satisfaz o contrato. `@runtime_checkable`
permite `isinstance(obj, AsyncCacheStorage)` em testes.

### 2.2 `InMemoryCacheStorage` — LRU via `OrderedDict`

```python
class InMemoryCacheStorage:
    def __init__(self, maxsize: int = 1024) -> None: ...

    async def get(self, key):
        # LRU: hit promotes to most-recently-used.
        entry = self._store[key]
        self._store.move_to_end(key)
        return entry

    async def set(self, key, entry):
        if key in self._store:
            self._store.move_to_end(key)
            self._store[key] = entry
        else:
            self._store[key] = entry
            if maxsize > 0 and len(self._store) > maxsize:
                self._store.popitem(last=False)  # LRU evict
                self.evictions += 1
```

**Decisões**:

- **`OrderedDict.move_to_end` + `popitem(last=False)`**: O(1)
  por op. `last=False` remove o **primeiro** item (LRU).
- **`maxsize=0` ou negativo** = sem LRU (dict cresce).
  Validado no construtor.
- **`maxsize=1024`** default. Cobre workloads típicos
  sem gastar memória. Configurável pelo caller.
- **Lock `asyncio.Lock`**: serializa get/set/delete.
  Em Python single-threaded, o overhead é desprezível;
  em multi-threaded, garante visibilidade.

### 2.3 `RedisCacheStorage` — multi-processo

```python
class RedisCacheStorage:
    def __init__(self, redis_client, *, prefix, ttl_s=None, maxsize=None): ...

    async def set(self, key, entry):
        pipe = self._redis.pipeline()
        pipe.hset(rkey, mapping=_encode_entry(entry))
        if self._ttl_s is not None and self._ttl_s > 0:
            pipe.expire(rkey, int(self._ttl_s))
        await pipe.execute()
```

**Decisões**:

- **`HSET` + `EXPIRE` em pipeline**: 1 round-trip ao Redis.
- **Encoding**: `_CacheEntry` → hash (não string JSON)
  para permitir `HGET` de campos individuais em
  métricas scrapers (ex: ler só `cost_usd`).
- **TTL obrigatório** se passado. Sem TTL, o admin
  precisa `clear()` para liberar espaço.
- **Eviction no Redis server-side**: o LRU local do
  `RedisCacheStorage` **não** faz eviction at-capacity
  (cada `set` é uma escrita atômica). A eviction real
  é feita pelo Redis via `maxmemory_policy=allkeys-lru`
  (recomendado em produção). O parâmetro `maxsize` é
  só um **hint** exposto em `metrics` para correlação
  com `maxmemory` do servidor.

### 2.4 `CachingLLMTransport` — simplificado

**Removido**: parâmetro `backend: dict | None` (back-compat
Fase 2). O transporte agora aceita apenas
`storage: AsyncCacheStorage | None`. Default é
`InMemoryCacheStorage()`.

**Adicionado**:
- `storage.metrics` é exposto no `metrics` do transporte
  (composição — `cache.metrics["evictions"]` se
  InMemory, `cache.metrics["size"]` em ambos).
- O `_size` interno do transporte é mantido (contador
  local; mais barato que consultar o storage).

### 2.5 `invalidate` e `clear` opcionais

O Protocol `AsyncCacheStorage` exige apenas `get/set/delete`.
`invalidate` e `clear` são **extension methods**: o
transporte chama via `getattr(..., default=None)`. O
`RedisCacheStorage` implementa `clear` (via `SCAN +
UNLINK` no prefixo); o `InMemoryCacheStorage` expõe
`clear` via `dict.clear` direto (sem scan).

---

## 3. Trade-offs

### Prós

- **LRU = memória limitada**: o storage não cresce
  sem bound. `maxsize=1024` cobre 99% dos workloads.
- **Multi-processo**: o Redis é a única opção
  coordenar estado entre workers. O LRU no
  RedisCacheStorage é declarado como "hint" — o
  server-side policy é o limite real.
- **Async-first**: o Protocol é async. Backends
  distribuídos (Redis, Memcached) encaixam sem adapter.
- **Protocol, não ABC**: duck typing. Aplicações plugam
  backends próprios (LRU-K, RocksDB, S3) sem herança.

### Contras

- **`InMemoryCacheStorage` ainda é single-process**:
  o dict é local ao processo. Para hit-rate global,
  Redis.
- **LRU é O(1) mas não é LFU**: a LLM hot key
  (mesmo prompt chamado 1000x/min) pode ser evictada
  se outras 1024 keys forem tocadas no meio. Aceitável
  em prática; LFU seria mais complexo (Counter + heap).
- **Encoding do RedisCacheStorage é manual** (JSON
  + 6 fields). Para schema evolutivo, MsgPack ou
  Protobuf seria melhor. Para Fase 5, JSON é
  suficiente e debugável.

### Alternativas consideradas

- **LFU (Least Frequently Used)**: rejeitado —
  requer Counter + heap, mais complexo que LRU para
  benefício marginal (LLM workloads são dominados
  por hot keys, mas o `idempotency_key` estável já
  reduz o churn).
- **TTI (Time-to-Idle)**: rejeitado — o `ttl_s` já
  cobre isso. TTI separado seria complicar.
- **Async LRU genérico (cachetools)**: rejeitado —
  dep externa desnecessária; o código é ~30 LOC.
- **Manter `backend: dict` para back-compat**: rejeitado
  — o usuário liberou ("pode trocar"). Simplificar.

---

## 4. Consequências

### Para o time

- Toda Role que usa `CachingLLMTransport` em
  produção multi-processo deve passar
  `RedisCacheStorage(redis, ttl_s=...)`.
- O default `InMemoryCacheStorage(maxsize=1024)` cobre
  testes e single-worker. Não há razão para mudar
  exceto em produção.
- Métricas expostas em `cache.metrics`:
  `size`, `maxsize`, `evictions` (in-memory) ou `size`,
  `prefix`, `ttl_s` (Redis). Útil para dashboards.

### Para a arquitetura

- O Protocol `AsyncCacheStorage` é estável. Backends
  novos (Memcached, S3, Redis Cluster) são uma
  classe nova de ~50 LOC.
- O transporte não precisa mudar para suportar
  backends novos. Composição.
- O `LiteLLMTool` continua agnóstico: aceita
  qualquer transport.

### Para DevOps

- Para Redis em produção, configurar
  `maxmemory` e `maxmemory-policy=allkeys-lru` (ou
  `volatile-lru` para só evictar keys com TTL).
- Monitorar `cache.metrics["evictions"]` (in-memory)
  ou `cache.metrics["size"]` vs `maxsize` (Redis).
- TTL default no transporte = `None` (never expire).
  Recomendado em produção: `ttl_s=3600` (1h) para
  bounded cache.

---

## 5. Veja também

- [ADR-008: Caching LLM transport](./ADR-008-Caching-Transport.md) —
  decisão original do decorator
- [fmh_backend/docs/analysis/fmh_agents_consistency.md](../../fmh_backend/docs/analysis/fmh_agents_consistency.md) —
  dívida técnica listada em §6
- [fmh_agents/tools/cache.py](../../fmh_agents/src/fmh_agents/tools/cache.py) —
  implementação
