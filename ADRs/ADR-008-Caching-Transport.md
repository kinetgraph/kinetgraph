<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-008: Caching LLM Transport

**Status:** Aceito
**Data:** 09 de junho de 2026
**Versão:** 0.3.0
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** ADR-006, ADR-007

---

## 1. Contexto

O `LiteLLMTool` aceita `idempotency_key` (do framework,
ADR-005) mas **não** dedupe — LiteLLM não tem cache
server-side. Para fechar a janela entre o framework
(checkpoint durável) e o provider externo, precisamos de
um cache na borda do `LiteLLMTool`.

Cenários onde o cache faz diferença:

  - **Replay de uma Role** após restart: o sistema
    re-emite as mesmas `tool.llm.complete.requested`
    events com o mesmo `event_id` → mesmo `idempotency_key`.
  - **Re-dispatch** do mesmo `*.requested` por um
    sistema reativo: idempotência via EventLog
    deduplica o append, mas o sistema ainda re-roda a
    Role. Sem cache, a LLM é chamada de novo.
  - **Testes**: rodar o mesmo test N vezes com
    `temperature=0` deveria ser grátis após a primeira.
  - **Desenvolvimento local**: iterar sem queimar
    budget da cloud.

A solução é um **decorator transport**: o
`CachingLLMTransport` envolve outro transport, memoiza
respostas por `idempotency_key`, e delega miss ao inner.

---

## 2. Decisão

### 2.1 Camada de cache: transport, não tool

A abstração `LLMTransport` (ADR-007) já separa I/O do
resto. Cachear no transport tem três vantagens:

  - **Transparente**: a `LiteLLMTool` não muda.
  - **Componível**: cache → metrics transport → inner
    (chains).
  - **Testável**: substituir o inner por um
    `FakeLLMTransport` para validar que o cache hit não
    chama o fake.

### 2.2 Chave do cache

`(idempotency_key, model, response_format)` — tuple
serializada. Razão para incluir `model` e
`response_format`:

  - Mesmo `idempotency_key` em chamadas diferentes pode
    apontar para modelos diferentes (overrides per-call).
  - `response_format` muda o schema de saída, e outputs
    diferentes devem ser armazenados separadamente.

Temperatura e outros params **não** entram na chave
(deliberadamente). Consequência: usar `temperature=0`
em produção para garantir determinismo entre calls com
mesmo key. Para `temperature > 0`, a Role não deve
usar caching.

### 2.3 Storage

  - **Default**: `dict` in-process.
  - **Backend opcional**: qualquer objeto com
    `__getitem__` / `__setitem__` / `__contains__`.
    Para Redis-backed shared cache, basta passar o
    client Redis.
  - **Locking**: in-process é O(1) por GIL. Para
    backends distribuídos, o backend precisa fornecer
    seu próprio lock (ou aceitar race em miss-then-fill,
    que é benigno).

### 2.4 TTL

Opcional. Lazy expiration: a entrada é checada no
read, não há background sweep. Bom o suficiente para
casos típicos (cache efêmero, dev).

Para TTL agressivo (segundos) com milhões de entries,
sugere-se um backend com eviction (Redis com LRU).

### 2.5 Erros

Erros **não** são cacheados. A próxima call com mesma
key re-tenta. Racional: erros são transientes (rate
limit, timeout) — cachear envenenaria a key.

A métrica `errors` é incrementada para observabilidade
— callers podem detectar "poison keys" (keys que
sempre falham).

### 2.6 Métricas

`hits`, `misses`, `stores`, `errors`, `size` — expostos
via `cache.metrics: dict`. Útil para dashboards e
para detectar cenários anormais (ex: `misses=1000`
e `hits=0` significa que o cache não está sendo útil,
talvez idempotency_keys estão mal-formadas).

### 2.7 Invalidação

```python
await cache.invalidate("k1")  # drop entries for k1
await cache.clear()            # drop all
```

Para invalidar **por modelo** (ex: depois de um
redeploy do Ollama), itera sobre `cache.metrics` ou
chama `clear()` se o cache é local.

### 2.8 Clock injetável

O cache aceita `time_fn` para testes. Default é
`time.monotonic`. Tests passam um mock que avança
manualmente.

---

## 3. Trade-offs

### Prós

- **At-most-once real** para chamadas determinísticas
  (temperature=0).
- **Transparente** — não muda contrato da Tool nem
  dos Roles.
- **Composível** — pode encadear com outros transports
  (metrics, logging, retry).
- **Testável** — `FakeLLMTransport` no inner + cache
  cobre os 4 caminhos (miss, hit, expired, error).
- **Métricas embutidas** para SRE.

### Contras

- **In-process default não escala** horizontalmente
  (cada processo tem seu cache). Mitigação: Redis
  backend.
- **Memória cresce** sem TTL ou eviction. Mitigação:
  TTL + maxsize (LRU).
- **Determinismo requerido** (temperature=0). Para
  outputs criativos, cache pode dar respostas
  repetidas para prompts parecidos mas não idênticos.
- **Não cacheia erros** — comportamento correto, mas
  significa que cada retry de um erro genuíno custa
  uma chamada real.

### Alternativas consideradas

- **Cache server-side LiteLLM**: existe mas é
  instável e não-portável entre providers.
- **Cache na Role**: cada Role teria que implementar
  caching, com N×código.
- **Cache no framework core**: genérico demais, não
  tem conhecimento de `idempotency_key` semântico.

---

## 4. Consequências

### Para o time

- Usar `temperature=0` em produção quando caching é
  desejado.
- Monitorar `cache.metrics["hits"] / (hits+misses)`
  como hit-rate. < 30% indica problema.
- Em testes, `FakeLLMTransport` continua sendo a
  abstração primária; o `CachingLLMTransport` é um
  decorator opcional para validar comportamento de
  cache.

### Para a arquitetura

- O framework (`fmh_backend`) **não** conhece o cache.
  Vive em `fmh_agents/tools/cache.py` — decisão de
  aplicação, não de framework.
- O contrato `LLMTransport` é estável: o cache é
  apenas mais uma implementação. Chains
  `cache → metrics → inner` são possíveis.

### Para DevOps

- Em produção com múltiplos processos, considere
  Redis backend para hit-rate global.
- Em produção com Ollama local, o cache é quase
  sempre um win (mesmo prompt com `temperature=0` dá
  mesmo output, mas a chamada tem latência de I/O).
- Em produção com API paga, o cache reduz custo
  **diretamente**. Monitore spend antes/depois.

---

## 5. Veja também

- [ADR-006: Tool × Role](./ADR-006-Tool-Role-Separation.md)
- [ADR-007: LLM via LiteLLM](./ADR-007-LiteLLM-Adapter.md)
- [fmh_agents/tools/cache.py](../../fmh_agents/src/fmh_agents/tools/cache.py)
- [examples/07_caching_transport.py](../../fmh_agents/examples/07_caching_transport.py)
- Tests: `fmh_agents/tests/unit/tools/test_cache.py`
