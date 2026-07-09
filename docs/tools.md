<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Tools

`Tool` é a forma padronizada que sistemas do FMH usam para
invocar capacidades side-effectful (fiscal authority, banco, ERP,
HTTP, etc.). A ideia central: **sistemas puros emitem um
evento, um adapter com resiliência executa a chamada e
emite o resultado**. O sistema que pediu nunca chama
I/O diretamente.

> **Sobre Tools concretas**: o `fmh_backend` define o
> **protocolo** de Tool e o `ToolInvoker` helper, mas
> **não traz Tools concretas**. Essas vivem em pacotes
> adicionais:
>
> - [fmh_agents](../../fmh_agents/) — `LiteLLMTool`,
>   `CachingLLMTransport`, Roles semânticas.
> - Pacotes externos hipotéticos — `fmh_crm_hubspot`,
>   `fmh_bank_itau`, etc.
>
> Aplicações que não precisam de LLM/CRM/ERP podem
> implementar Tools próprias e registrar direto no
> `ToolRegistry` deste framework. Veja
> [ADR-009](../../fmh_agents/ADRs/ADR-009-Tool-Tiers.md)
> para a convenção **opcional** de tipos de Tool
> (Tipo A — transporte, Tipo B — capability, Tipo C —
> domínio).

Este documento cobre:

1. O `Tool` Protocol e o `ToolRegistry`.
2. O fluxo via EventLog (`requested` → `completed` / `failed` / `args_invalid`).
3. O `ToolInvoker` (helper para adapters).
4. Como escrever uma tool real e onde plugar resiliência.
5. O hook M2 (`pre_invoke_args_extractor`) para extração
   semântica de argumentos.
6. Como o `FalkorDBProjector` indexa tool calls.

> **Pré-requisitos**
>
> - Redis Streams rodando.
> - `pip install 'kntgraph[falkordb]'` somente se quiser
>   grafo de tool calls.

---

## 1. O `Tool` Protocol

```python
# fmh_backend/src/fmh_backend/tools/protocol.py
class Tool(Protocol):
    name: str            # "invoice.issue" (provider.action)
    description: str     # humano-legível, usado em prompts
    input_schema: dict   # JSON-schema-like (opcional)

    async def invoke(self, **kwargs) -> Result[Any, ToolError]: ...
```

- Sem estado: o framework trata a tool como opaca, lê
  `name` / `description` / `input_schema` e chama `invoke`.
- O retorno é `Result[Any, ToolError]`: nunca levanta
  exceções de negócio — empacote em `Err(ToolError(...))`.
- Idempotência: se a chamada for repetível com segurança,
  documente e implemente (e.g. `invoice.query` é safe-retry,
  `bank.transfer` não é).

### Convenção de nomes

`provider.action` em `lower_snake_case`:

| Tool | Significa |
|------|-----------|
| `invoice.issue`  | Emite um documento fiscal via serviço externo |
| `invoice.query`  | Consulta status de um documento emitido |
| `erp.create_invoice` | Cria fatura no ERP |
| `bank.get_balance` | Saldo da conta |
| `bank.transfer` | PIX/TED (NÃO idempotente — cuidado) |

---

## 2. ToolRegistry

```python
from kntgraph.tools.protocol import ToolRegistry, Tool

registry = ToolRegistry()
registry.register(invoice_issue_tool)
registry.register(erp_create_invoice_tool)

registry.names()           # ["invoice.issue", "erp.create_invoice"]
registry.get("invoice.issue")  # Tool
"invoice.issue" in registry    # True
```

- Simples: `dict[str, Tool]`. Sem hot-reload nem discovery.
- Se a aplicação tiver tools por tenant, faça um registry
  por tenant (F9+).

---

## 3. O fluxo via EventLog

```
+---------------------+       +---------------------+
| Sistema (puro)      |       | Adapter (ToolInvoker|
| World → list[Event] |       |  + circuit breaker) |
+----------+----------+       +----------+----------+
           |                             |
           | 1. emite                    |
            |    "tool.invoice.issue     |
            v                             |
+---------------------+                  |
|  EventLog           |  2. adapter       |
|  (Redis Streams)    |     lê            |
+----------+----------+------------------+
            ^                             |
            | 3. emite                    |
            |    "tool.invoice.issue     |
           |     .completed" | ".failed" |
           +-----------------------------+
                     | 4. sistema reativo
                       consome o resultado
```

Convenção de `event_type` (helper `ToolEventType`):

| Etapa | event_type |
|-------|------------|
| Sistema pede | `tool.<name>.requested` |
| Tool retornou Ok | `tool.<name>.completed` |
| Tool retornou Err | `tool.<name>.failed` |
| Args inválidos (M2 hook) | `tool.<name>.args_invalid` |

O `data` do evento `.completed`:

```python
{
    "request_id": "<event_id do .requested>",
    "tool": "<name>",
    "result": <valor retornado pelo invoke>,
    "latency_ms": 12.3,
}
```

O `data` do `.failed`:

```python
{
    "request_id": "<event_id do .requested>",
    "tool": "<name>",
    "error": "mensagem",
    "latency_ms": 9.8,  # opcional
}
```

### `.args_invalid` (M2 hook)

Emitido pelo `ToolInvoker` quando o hook
`pre_invoke_args_extractor` está configurado e os args
merged (do chamador + do extractor) não validam contra
`tool.input_schema`. A Tool **não** é invocada — o
evento vai para a DLQ.

```python
{
    "request_id": "<event_id do .requested>",
    "tool": "<name>",
    "reason": "missing required: ['valor']; type mismatches: [...]",
    "missing": ["valor"],
    "type_mismatches": [
        {"field": "valor", "expected": "number", "got": "str"}
    ],
    "unexpected": ["junk"],
    "latency_ms": 1.2,  # opcional
}
```

Detalhe completo em
[routing.md §3](./routing.md#momento-2--argument-extraction).

### Idempotência

- O `event_id` do `.requested` é determinístico
  (`causation_id` + tool + args) → o EventLog dedupe.
- O adapter é invocado **no máximo uma vez** por request.
- O `.completed` / `.failed` também tem `event_id`
  determinístico: re-invocar produz o mesmo resultado.

### `idempotency_key` (at-least-once → at-most-once)

O EventLog cobre dedup **dentro do Redis**. Mas side effects
externos (PIX, HTTP, DB) ficam de fora. Para esses, o
`ToolInvoker` injeta uma chave estável em **toda** chamada:

```python
async def invoke(self, *, idempotency_key: str, **kwargs):
    # idempotency_key = str(request.event_id)
    # Estável entre re-dispatches: dispatcher restart
    # → mesmo .requested event_id → mesma chave
    ...
```

**Tools com side effects externos** (bank, payment) **DEVEM**
implementar dedup por `idempotency_key`. Tools read-only podem
ignorar, mas devem aceitar o parâmetro.

Veja [checkpoints.md §5](./checkpoints.md#5-idempotency_key-em-tools)
para a discussão completa e exemplos de implementação
(bank transfer com dedup, contratos de crash safety).

---

## 4. Escrevendo uma tool

```python
import httpx
from kntgraph.core.result import Ok, Err, ToolError
from kntgraph.tools.protocol import Tool

class InvoiceIssueTool:
    name = "invoice.issue"
    description = "Emite um documento fiscal via serviço externo."
    input_schema = {
        "type": "object",
        "required": ["xml_b64", "tp_amb"],
        "properties": {
            "xml_b64":  {"type": "string"},
            "tp_amb":   {"type": "integer", "enum": [1, 2]},
        },
    }

    def __init__(self, endpoint: str, timeout: float = 30.0):
        self._endpoint = endpoint
        self._timeout = timeout

    async def invoke(self, **kwargs) -> Ok | Err:
        xml_b64 = kwargs.get("xml_b64")
        tp_amb = kwargs.get("tp_amb")
        if not xml_b64 or tp_amb not in (1, 2):
            return Err(ToolError("invalid_args"))
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as cli:
                r = await cli.post(
                    self._endpoint,
                    json={"xml": xml_b64, "tpAmb": tp_amb},
                )
            if r.status_code != 200:
                return Err(ToolError(f"invoice.issue http {r.status_code}: {r.text}"))
            return Ok(r.json())
        except httpx.TimeoutException:
            return Err(ToolError("invoice.issue_timeout"))
        except Exception as e:
            return Err(ToolError(f"invoice.issue_unexpected: {e!r}"))
```

Regras práticas:

1. **Nunca levante exceção de negócio** — use `Err(ToolError)`.
   Exceções são para bugs; falhas esperadas viram `Err`.
2. **Timeouts curtos** — defina em todas as chamadas de rede.
3. **Documente idempotência** — afeta retry e circuit breaker.
4. **Pense em PII/secrets** — não logue o `data` inteiro,
   use `structlog` com campos específicos.

---

## 5. `ToolInvoker` (adapter helper)

O `ToolInvoker` é o adapter de referência: lê
`.requested`, chama a tool, escreve `.completed`/`.failed`.
Em produção, envolva-o com circuit breaker / retry / DLQ
(veja [resilience.md](./resilience.md)).

```python
import redis.asyncio as aioredis
from kntgraph.stream.event_log import EventLog
from kntgraph.tools.protocol import ToolRegistry
from kntgraph.tools.invoker import ToolInvoker

redis = aioredis.from_url("redis://localhost:6379")
log = EventLog(redis)

registry = ToolRegistry()
registry.register(InvoiceIssueTool(endpoint="https://..."))

invoker = ToolInvoker(log=log, registry=registry)

# Loop do adapter (em produção: scheduler / daemon)
async def adapter_loop():
    while True:
        handled = await invoker.run_once(agent_id="nf-001")
        if handled == 0:
            await asyncio.sleep(0.1)
```

### `handle_request_event` (avançado)

Se você precisa de mais controle (ex: agrupar requests,
debouncing), chame `handle_request_event(request)`
diretamente. Ele retorna `Ok(Event)` (response event) ou
`Err(Exception)`.

```python
result = await invoker.handle_request_event(request_event)
if result.is_ok():
    print("respondeu com:", result.unwrap().event_id)
else:
    print("falhou ao emitir response:", result.err_value())
```

---

## 6. Argument extraction (M2 hook, ADR-013)

O `ToolInvoker` aceita um hook opcional
`pre_invoke_args_extractor` que preenche `args` a partir
do texto do request antes de invocar a Tool. É a peça
M2 do [routing semântico](./routing.md) — Momento 1
(classificar intent) emite o `tool.{name}.requested`;
Momento 2 (este hook) extrai os slots do
`input_schema` da tool.

```python
from kntgraph.tools.invoker import ToolInvoker
from kntgraph.knowledge.extraction import GlinerArgumentExtractor

extractor = GlinerArgumentExtractor(registry, model_name="gliner2-base")

async def hook(text: str, tool_name: str):
    return await extractor.extract(text, tool_name)

invoker = ToolInvoker(
    log=log, registry=registry, pre_invoke_args_extractor=hook,
)
```

**Contrato do hook:**

```python
PreInvokeArgsExtractor = Callable[
    [str, str],  # (text, tool_name)
    Awaitable[Result[ArgExtraction, ToolError]],
]
```

**Comportamento:**

- `caller_args` no request (`data.args`) **vence** o
  extractor. O extractor preenche os buracos.
- `data.text` é strippado do payload antes de invocar
  (o Tool não o declara).
- O dict merged é validado contra `tool.input_schema`
  (V1 subset: required + tipos primitivos). Em falha →
  `tool.{name}.args_invalid` (Tool não invocada).
- Sem hook: comportamento legacy + bônus de validação.
- Sem `data.text` no request: hook não é chamado;
  valida `caller_args` direto.

**Backends plugáveis:**

| Backend | Quando usar |
|---|---|
| `RegexFieldFinder` (sem GLiNER2) | CNPJ/CPF/data/email/money conhecidos |
| `GlinerFieldFinder` (opt-in) | Campos arbitrários, zero-shot |
| `FieldFinder` custom | LLMs, RAG, etc. |

**Não use M2 para:**

- Boolean decisions ("o usuário quer ativar X?") — M2
  só lida com escalares. V2 adiciona boolean/array.
- Listas de objetos aninhados ("itens da NF-e") — fora
  do escopo V1.

Detalhe, contratos, edge cases, performance, e exemplo
end-to-end em [routing.md](./routing.md#momento-2--argument-extraction).

---

## 7. Resiliência

O `ToolInvoker` **não** traz circuit breaker ou retry
embutidos — é um helper. Em produção:

1. Envolva `tool.invoke(**kwargs)` com o circuit breaker do
   `fmh_backend.resilience` (veja [resilience.md](./resilience.md)).
2. Adicione retry idempotente (apenas para tools marcadas
   como safe-retry).
3. Timeouts por tool, não globais.
4. Em falha catastrófica (circuit aberto): o sistema
   **continua funcionando** — basta o `.failed` ser
   emitido. Sistemas puros downstream decidem o que fazer
   (replay, dead-letter, fallback).

```python
from kntgraph.resilience.circuit_breaker import CircuitBreaker
from kntgraph.resilience.retry import retry

breaker = CircuitBreaker(failure_threshold=5, recovery_time_s=30)

@retry(max_attempts=3, backoff="exponential")
async def safe_invoke(tool, **kwargs):
    return await breaker.call(tool.invoke, **kwargs)
```

---

## 8. Tool calls no grafo (FalkorDB)

O `FalkorDBProjector` (veja [graphrag.md](./graphrag.md))
indexa **toda chamada de tool** completada ou falhada:

```cypher
(t:ToolCall {
    id, tool, request_id, status, latency_ms, agent_id
})
(a:Agent)-[:CALLED]->(t)
```

Consultas úteis:

```cypher
// Tools mais chamadas nas últimas 24h
MATCH (a:Agent)-[:CALLED]->(t:ToolCall)
WHERE t.status = 'completed'
  AND t.latency_ms > 1000
RETURN t.tool, count(*) AS n
ORDER BY n DESC
LIMIT 10;
```

```cypher
// Taxa de falha por tool
MATCH (a:Agent)-[:CALLED]->(t:ToolCall)
RETURN t.tool,
       sum(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS fails,
       count(*) AS total
ORDER BY fails DESC;
```

---

## 9. Testes

```bash
uv run --package kntgraph pytest \
    fmh_backend/tests/unit/tools/ -v
```

Os testes usam tools fake (sem I/O real). Para testar
tools que falam HTTP, use `httpx.MockTransport` ou
`aioresponses` — o adapter em si já está coberto.

---

## 10. Veja também

- [routing.md](./routing.md) — semantic routing (M1 intent + M2 args)
- [graphrag.md](./graphrag.md) — projeção de tool calls no FalkorDB.
- [resilience.md](./resilience.md) — circuit breaker, retry, timeout.
- [checkpoints.md](./checkpoints.md) — `idempotency_key` em tools
  e checkpoints duráveis do dispatcher.
- [ADR-004 §2.3: Tools são Protocol no core](../ADRs/ADR-004-Memory-Tools-Knowledge.md#23-tools-são-protocol-no-core-com-resiliência)
- [ADR-013](../../fmh_agents/ADRs/ADR-013-Semantic-Routing-GLiNER2.md) —
  decisão completa do semantic routing
- [fmh_agents](../../fmh_agents/) — pacote opcional com
  `LiteLLMTool` e Roles
- [ADR-009](../../fmh_agents/ADRs/ADR-009-Tool-Tiers.md) —
  hierarquia opcional de Tools (Tipo A/B/C)
- [Tool Protocol](../../src/fmh_backend/tools/protocol.py)
- [Tool Invoker](../../src/fmh_backend/tools/invoker.py)
- [Core Result (Railway)](../../src/fmh_backend/core/result.py)
