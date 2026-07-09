<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Checkpoints e Idempotência

A integração entre o `ReactiveDispatcher` e ferramentas externas
possui um requisito de **at-least-once delivery** que o framework
garante via dois mecanismos complementares:

  1. **Checkpoints duráveis** — salvam o commit point do
     dispatcher no Redis, permitindo restart sem re-dispatch.
  2. **`idempotency_key` em Tools** — toda tool recebe uma chave
     estável baseada no `event_id` da request, permitindo dedup
     de side effects externos.

Este documento cobre o modelo, as invariantes e como configurar
em produção.

> **Pré-requisitos**
>
> - Redis Streams rodando (mesma instância do EventLog).
> - `pip install 'fmh-backend'` (já vem com tudo).

---

## 1. O problema: at-least-once vs at-most-once

O `ReactiveDispatcher` processa eventos em batches:

```
[read N eventos] → [aplica sistemas reativos] → [emite M eventos] → [checkpoint]
```

Se o processo **morre entre o `read` e o `checkpoint`**, o
próximo boot precisa decidir:

  - **Reprocessar do início** → re-entrega eventos, side effects
    em tools podem duplicar (PIX cobrado 2x, NF transmitida 2x).
  - **Confiar no que estava em memória** → perdeu tudo.

A solução: **checkpoint durável salvo APÓS o commit do batch no
EventLog**. Janela de re-entrega residual: submilissegundo, entre
o `XADD` retornar OK e o `HSET` do checkpoint concluir.

---

## 2. `ReactiveCheckpoint` — o commit point

```python
# src/kntgraph/infra/checkpoint.py
@dataclass(frozen=True, slots=True)
class ReactiveCheckpoint:
    agent_id: str
    last_event_id: UUID         # lógico, imutável
    last_stream_id: str         # físico, âncora p/ XRANGE
    confirmed_at: datetime
    state_hash: Optional[str] = None  # opcional
```

| Campo | Significado |
|-------|-------------|
| `last_event_id` | UUID determinístico do último evento cujos side effects são duráveis. |
| `last_stream_id` | `<ms>-<seq>` do Redis. Usado como `min="(<last_stream_id>"` para leitura exclusiva. |
| `confirmed_at` | Timestamp UTC do save. Útil para SLOs e dashboards. |
| `state_hash` | Opcional. Hash determinístico do World pós-fold. Detecta projection drift. |

### Invariantes

1. **Atomicidade do par**: `last_event_id` e `last_stream_id`
   são salvos num único `HSET`, nunca separados.
2. **Exclusividade do cursor**: o próximo read usa
   `min="(<last_stream_id>"` (com parêntese). Sobrevive a
   `XTRIM MAXLEN`.
3. **Save é post-commit**: o `HSET` ocorre **depois** do
   `XADD` do EventLog retornar OK. A janela residual é
   "entre o OK do Redis e o HSET" — submilissegundo.

---

## 3. `CheckpointStore` — Redis hash

Todos os checkpoints vivem num único hash:

```
knt:reactive:checkpoints
  ├── "a-1" → {"last_event_id": "...", "last_stream_id": "1-0", ...}
  ├── "b-2" → {"last_event_id": "...", "last_stream_id": "1-0", ...}
  └── ...
```

### API

```python
from kntgraph.infra.checkpoint import (
    CheckpointStore, ReactiveCheckpoint,
)

store = CheckpointStore(redis)

# Save (chamado pelo dispatcher após commit)
await store.save(ReactiveCheckpoint(
    agent_id="a-1",
    last_event_id=event.event_id,
    last_stream_id="1700000000000-0",
    confirmed_at=datetime.now(timezone.utc),
))

# Load (chamado pelo dispatcher no bootstrap)
cp = await store.load("a-1")
if cp is None:
    # agente nunca visto ou reset
    ...

# Diagnostics
all_cps = await store.load_all()  # dict[agent_id, ReactiveCheckpoint]

# Recovery
await store.clear("a-1")           # remove um
await store.clear_all()            # wipe geral (testes / emergência)
```

---

## 4. Configurando o `ReactiveDispatcher`

```python
import redis.asyncio as aioredis
from kntgraph.infra.checkpoint import CheckpointStore
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog

redis = aioredis.from_url("redis://localhost:6379")
log = EventLog(redis)
store = CheckpointStore(redis)

dispatcher = ReactiveDispatcher(
    log,
    reactive_systems=[validate_doc, emit_completion],
    poll_interval=0.5,
    checkpoint_store=store,   # ← habilita checkpoints duráveis
)

await dispatcher.start()
```

### Sem `CheckpointStore` (legado)

```python
dispatcher = ReactiveDispatcher(log, reactive_systems=[...])
```

Comportamento legacy: cursors in-memory, **não sobrevivem a
restart**. Útil para testes e single-process dev. **Não use em
produção.**

---

## 5. `idempotency_key` em Tools

O EventLog deduplica eventos (mesmo `event_id` = no-op). Mas
side effects **fora** do EventLog (HTTP, DB, payment gateway)
não são cobertos. Para esses, o `ToolInvoker` injeta uma chave
estável:

```python
# Toda tool recebe:
await tool.invoke(
    idempotency_key="<UUID do .requested>",
    **request_data,
)
```

A chave é `str(request.event_id)` — estável entre
re-dispatches.

### Implementando uma tool não-idempotente

```python
from kntgraph.core.result import Ok, Err, ToolError

class BankTransferTool:
    name = "bank.transfer"
    description = "PIX transfer (must dedupe by idempotency_key)"
    input_schema = {...}

    def __init__(self, gateway):
        self._gateway = gateway
        # Cache local keyed by idempotency_key. Em produção,
        # use Redis com TTL ou um KV store externo.
        self._seen: dict[str, dict] = {}

    async def invoke(
        self, *, idempotency_key: str, amount: int, to: str
    ):
        if idempotency_key in self._seen:
            # Já processado — retornar resultado cacheado.
            return Ok({"status": "duplicate", "transfer": self._seen[idempotency_key]})

        result = await self._gateway.transfer(
            idempotency_key=idempotency_key,  # ← passa adiante
            amount=amount,
            destination=to,
        )
        self._seen[idempotency_key] = result
        return Ok({"status": "ok", "transfer": result})
```

### Tools naturalmente idempotentes

Tools read-only (consultas, lookups) podem ignorar
`idempotency_key`. Mas **devem aceitá-la** na assinatura para
uniformidade:

```python
class InvoiceQueryTool:
    async def invoke(self, *, idempotency_key: str, document_id: str):
        # idempotency_key é ignorada — a query é naturalmente safe.
        return Ok(await self._invoice_api.query(document_id))
```

---

## 6. Crash safety — fluxos garantidos

### Restart limpo (processo morto normalmente)

```
Boot 1: processa eventos N=1..10, salva checkpoint=N=10
[crash]
Boot 2: lê checkpoint, processa apenas N=11..15
```

✓ Sem re-entrega.

### Crash entre XADD e HSET (micro-janela)

```
Boot 1: processa N=10, XADD do .completed OK, [crash antes do HSET]
Boot 2: lê checkpoint antigo (N=9), reprocessa N=10
        → EventLog dedup do .completed (mesmo event_id)
        → tool vê idempotency_key já usada, dedupe via cache
```

✓ At-most-once no EventLog. At-most-once na tool com dedup.

### Crash durante fold do EventLog (desconexão Redis)

```
Boot 1: xrange em andamento, [Redis desconnect]
Boot 2: lê checkpoint, xrange do checkpoint → vazio (rede caiu
        antes de qualquer dado novo). Tenta de novo no próximo tick.
```

✓ Idempotente. Sem perda.

### Redis perde checkpoints (sem AOF)

Se o Redis é configurado sem persistência, checkpoints podem
sumir em restart do Redis. O dispatcher vai reprocessar do
início — **o EventLog dedup, mas tools com side effects externos
podem duplicar sem idempotency_key**.

Mitigação: use Redis com `appendonly yes` + `appendfsync everysec`
(recomendado) ou mais forte.

---

## 7. Monitoramento

### Lag por agente

```python
from datetime import datetime, timezone

cps = await store.load_all()
for agent_id, cp in cps.items():
    lag = (datetime.now(timezone.utc) - cp.confirmed_at).total_seconds()
    print(f"{agent_id}: lag = {lag:.1f}s, last = {cp.last_stream_id}")
```

### Detectar projection drift via `state_hash`

```python
import hashlib
import json

# No dispatcher, ao salvar:
state_hash = hashlib.sha256(
    json.dumps(world.to_map(), sort_keys=True, default=str).encode()
).hexdigest()

await store.save(ReactiveCheckpoint(
    agent_id=agent_id,
    last_event_id=event.event_id,
    last_stream_id=stream_id,
    confirmed_at=utcnow(),
    state_hash=state_hash,
))

# Em diagnóstico:
expected_hash = cp.state_hash
current_hash = ... # recalcular
if expected_hash != current_hash:
    alert("projection_drift", agent_id=agent_id)
```

---

## 8. Migração do legado

Se você já tem um dispatcher rodando **sem** `CheckpointStore`:

1. **Não há migração de dados**: o primeiro boot com
   `CheckpointStore` vai popular os checkpoints a partir do
   estado in-memory (que será descartado de qualquer forma
   no restart).
2. **O dispatcher precisa processar o backlog**: a primeira
   invocação com store vai criar checkpoints em `N=atual`.
3. **Cuidado com tools não-idempotentes ativas**: se há
   `tool.{name}.requested` pendentes no momento da migração,
   o dispatcher pode re-entregá-los. Garanta que toda tool
   externa honra `idempotency_key` **antes** de habilitar.

---

## 9. Veja também

- [tools.md](./tools.md) §3 — `idempotency_key` no `Tool` Protocol
- [resilience.md](./resilience.md) — circuit breaker para tools
- [ReactiveDispatcher](../../src/kntgraph/runner/reactive.py)
- [CheckpointStore](../../src/kntgraph/infra/checkpoint.py)
- [ToolInvoker](../../src/kntgraph/tools/invoker.py)
- Tests: `tests/integration/runner/test_checkpoint.py`
- Tests: `tests/unit/tools/test_invoker.py`
