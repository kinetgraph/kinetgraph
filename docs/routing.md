<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Semantic Routing (ADR-013)

Roteamento semântico de intent + extração de argumentos
para `Tool`s registradas no `ToolRegistry`, em dois
momentos desacoplados.

- **Momento 1 (intent):** texto do usuário → nome da Tool.
  Implementado como `SemanticRoutingRole` em `fmh_agents`.
- **Momento 2 (args):** texto + tool alvo → slots do
  `input_schema` populados. Hook opcional no
  `ToolInvoker` (`pre_invoke_args_extractor`).

O classificador default em ambos os momentos é
`Gliner*Classifier` (GLiNER2, opt-in, local). O design
separa **backend** (regex / GLiNER2 / LLM futuro) de
**orquestração** (a Role e o hook do invoker), o que
permite testes determinísticos sem GLiNER2 e troca
trivial de backend.

> **Pré-requisitos**
>
> - Redis Streams rodando (para `EventLog`).
> - `uv add 'kntgraph[gliner]'` para usar GLiNER2.
>   Sem o extra, a Role e o hook funcionam — o
>   classifier é injetado.

---

## 1. Visão geral

```
┌────────────────┐  user.message.received
│ IntentRouter   │──────────────────────────┐
│ (HTTP gateway) │                          ▼
└────────────────┘          ┌──────────────────────────────┐
                           │  SemanticRoutingRole         │
                           │  (Momento 1)                 │
                           │  - ToolRegistry (DI)         │
                           │  - IntentClassifier (DI)     │
                           │  - RoutingConfig (threshold) │
                           └──────┬──────────┬────────────┘
                                  │          │
                  confidence ≥   │          │  confidence <
                  threshold       │          │  threshold
                                  ▼          ▼
                  tool.{name}.       routing.unclassified
                  requested              (DLQ / LLM fallback)
                          │
                          ▼
              ┌──────────────────────────────┐
              │  ToolInvoker                │
              │  (Momento 2 acontece aqui)  │
              │  - pre_invoke_args_extractor│
              │  - merge + validate args    │
              └──────┬──────────┬────────────┘
                     │          │
        ok & valid   │          │  invalid args / extractor Err
                     ▼          ▼
            tool.{name}.    tool.{name}.
            completed       args_invalid
                            (DLQ)
```

**Por que dois momentos:**

- O schema de intent é fixo por turno (uma `intent` label,
  N classes). O schema de args muda por tool. Acoplar os
  dois economiza ~30-50ms mas amarra os modos e dificulta
  teste independente. Mantemos separados em V1.
- Threshold de M1 é global (configurável por deployment).
  Threshold de M2 é por-field (`field_threshold` no
  `SchemaArgumentExtractor`).
- O cache de labels (M1) invalida com `schema_version` =
  hash das `tool.name`s. O cache de args (M2) invalida
  com `schema_version` = hash do `input_schema` da tool.

---

## 2. Momento 1 — Intent

### 2.1 Componentes

| Componente | Local | Responsabilidade |
|---|---|---|
| `IntentClassifier` (Protocol) | `fmh_backend.knowledge.extraction.base` | `async classify(text, labels) -> Classification` |
| `GlinerIntentClassifier` | `fmh_backend.knowledge.extraction.gliner_intent` | Implementação GLiNER2 (eager load, `asyncio.to_thread`, parsing tolerante a v1.0/v1.5+) |
| `SemanticRoutingRole` | `fmh_agents.roles.semantic_router` | Orquestra: snapshot de labels, threshold, emissão de evento |
| `async_route_on_user_message` | `fmh_agents.roles.semantic_router` | Sistema reativo `user.message.received → tool.{name}.requested` ou `routing.unclassified` |
| `RoutingConfig` | mesmo arquivo | `threshold` (default 0.6), `top_k_candidates` (default 3), `from_env()` |

### 2.2 Fluxo

1. `IntentRouter` HTTP emite `user.message.received` com
   `data.text`.
2. O dispatcher chama `async_route_on_user_message(role, event)`.
3. A Role chama `classifier.classify(text, registry.names())`.
4. Aplica threshold:
   - **OK** → emite `tool.{name}.requested` com
     `data.args={}` (M2 vai preencher).
   - **Abaixo** → emite `routing.unclassified` com
     `data.text_hash` (sha256), threshold, top-3
     candidates.
5. `event_id` determinístico via `Event.domain_from`
   (`uuid5` sobre causation + agent + event_type + data).
   Replay dedup no `EventLog`.

### 2.3 Determinismo e idempotência

- `schema_version` = `sha256("|".join(sorted(labels)))[:16]`.
  Recalculado a cada instanciação. Usado como chave de
  cache: `(text, schema_version) → decision`.
- `event_id` determinístico: replay do mesmo
  `user.message.received` produz o mesmo `requested` /
  `unclassified` event id → `EventLog` dedup.
- PII hygiene: `routing.unclassified` carrega
  `sha256(text)`, **nunca** o texto. O texto vive no
  `user.message.received` original — gate PII
  (`PiiRedactionTool`, ADR-010) é responsabilidade do
  consumer do evento, não da Role.

### 2.4 Configuração

| Env var | Default | Significado |
|---|---|---|
| `FMH_ROUTING_THRESHOLD` | `0.6` | Mínimo top-1 score para rotear |
| `FMH_ROUTING_TOP_K_CANDIDATES` | `3` | Quantos top candidates no `routing.unclassified` |

---

## 3. Momento 2 — Argument extraction

### 3.1 Componentes

| Componente | Local | Responsabilidade |
|---|---|---|
| `ArgumentExtractor` (Protocol) | `fmh_backend.knowledge.extraction.base` | `async extract(text, tool_name) -> ArgExtraction` |
| `FieldFinder` (Protocol) | `fmh_backend.knowledge.extraction.argument_extractor` | `async find(text, field) -> (value, confidence) \| None` |
| `RegexFieldFinder` | mesmo arquivo | Backend regex (CNPJ, CPF, date, money, email) |
| `GlinerFieldFinder` | mesmo arquivo | Backend GLiNER2 (eager load, `asyncio.to_thread`) |
| `SchemaArgumentExtractor` | mesmo arquivo | Orquestrador genérico: walk_schema + find + coerce + threshold |
| `GlinerArgumentExtractor` | mesmo arquivo | Wrapper de conveniência (`SchemaArgumentExtractor` + `GlinerFieldFinder`) |
| `pre_invoke_args_extractor` (hook) | `fmh_backend.tools.invoker` | Parâmetro opcional no `ToolInvoker` |
| `validate_args` | `fmh_backend.tools.arg_validation` | Validação mínima (required + tipos primitivos) |
| `ToolEventType.args_invalid` | `fmh_backend.tools.protocol` | Novo tipo de evento para DLQ |

### 3.2 Fluxo

1. `ToolInvoker.handle_request_event(request)` recebe um
   `tool.{name}.requested`.
2. Se `pre_invoke_args_extractor` está setado E `data.text`
   existe:
   - `extracted = await extractor(text, tool_name)`
   - `merged = {**extracted.fields, **caller_args}` (chamador
     vence)
   - `merged.pop("text", None)` (não vai pro Tool)
   - `validate_args(merged, tool.input_schema)`
3. Se validação OK → `tool.invoke(idempotency_key=..., **merged)`.
4. Se validação falha (ou extractor Err) → emite
   `tool.{name}.args_invalid` com `missing`,
   `type_mismatches`, `unexpected`, `reason` no payload.
   Tool **não** é invocada.

### 3.3 Schema walker

`walk_schema` deriva `FieldSpec`s de um JSON-Schema object
schema:

```python
schema = {
    "type": "object",
    "properties": {
        "cnpj": {"type": "string", "format": "cnpj"},
        "valor": {"type": "number"},
        "qtd": {"type": "integer"},
        "ativo": {"type": "boolean"},  # V1: ignorado
        "tags": {"type": "array"},     # V1: ignorado
    },
    "required": ["cnpj", "valor"],
}
# → [
#     FieldSpec(name="cnpj", json_type="string", required=True, format="cnpj"),
#     FieldSpec(name="valor", json_type="number", required=True),
#     FieldSpec(name="qtd", json_type="integer", required=False),
# ]
```

**V1 limitations (conhecidas, ADR-013 §3):**

- Apenas escalares top-level (`string`, `number`, `integer`).
  `boolean`, `array`, `object` são ignorados.
- Sem `oneOf` / `anyOf` / `allOf`.
- Sem `pattern`, `minimum`, `maximum` (o Tool valida).
- Sem `$ref`.

A validação customizada em `arg_validation.py` cobre o
suficiente para o hook: required + tipos primitivos. Para
JSON-Schema completo, instale `jsonschema` e envolva o
módulo — a lógica de merge do invoker é agnóstica ao
validador.

### 3.4 Coerção

`SchemaArgumentExtractor` chama o backend (`FieldFinder`)
uma vez por field, recebe `(value, confidence)`, e
coage:

- `string` → `str(value).strip()` (vazio → drop)
- `integer` → `int(value)` se inteiro, senão drop.
  Rejeita `bool` (que é `int` em Python).
- `number` → `int` ou `float` (depende do input).

Datas (`format: date` / `date-time`) são mantidas como
`str` crua — o Tool valida downstream. Não reescrevemos
datas silenciosamente.

### 3.5 Edge cases

| Cenário | Comportamento |
|---|---|
| Tool sem `input_schema` (None / `{}`) | Hook roda, sem validação. `text` strippado. |
| Request sem `data.text` | Caminho legacy: hook não é chamado, valida `caller_args` direto. |
| Extractor Err (`tool not registered`, model crash) | `args_invalid` com `reason: "extractor_error: ..."`. Tool não invocada. |
| `missing required` após merge | `args_invalid` com `missing: [...]`. |
| `type_mismatch` | `args_invalid` com `type_mismatches: [{field, expected, got}, ...]`. |
| `unexpected` key | Reportado no payload mas **não bloqueia** o invoke (a Tool decide se aceita kwargs extras). |
| Replay do mesmo request | `idempotency_key` estável → Tool pode dedupar. `args_invalid` também é determinístico. |

### 3.6 Configuração

`GlinerArgumentExtractor(registry, model_name="gliner2-base", field_threshold=0.5)`.
`field_threshold` filtra campos com confiança abaixo. Para
configurar via env, faça seu próprio wrapper
(`FMH_ARG_THRESHOLD`, etc.) — o framework não dita.

---

## 4. Exemplo end-to-end

```python
import asyncio
from uuid import uuid4
from kntgraph.core.event import Event, CorrelationContext
from kntgraph.core.result import Ok
from kntgraph.knowledge.extraction import (
    GlinerIntentClassifier, GlinerArgumentExtractor,
)
from kntgraph.tools.invoker import ToolInvoker
from kntgraph.tools.protocol import ToolRegistry, Tool
from kntgraph.stream.event_log import EventLog
import redis.asyncio as aioredis

from kntgraph.agents.roles import (
    SemanticRoutingRole, RoutingConfig,
    async_route_on_user_message,
)


class EmitirNFeTool(Tool):
    name = "emitir_nfe"
    description = "Emite um documento fiscal via serviço externo"
    input_schema = {
        "type": "object",
        "properties": {
            "cnpj": {"type": "string", "format": "cnpj"},
            "valor": {"type": "number"},
        },
        "required": ["cnpj", "valor"],
    }

    async def invoke(self, *, idempotency_key, **kwargs):
        return Ok({"chave": f"nfe-{kwargs['cnpj']}", "valor": kwargs["valor"]})


async def main():
    # Setup
    redis = aioredis.from_url("redis://localhost:6379")
    log = EventLog(redis)
    registry = ToolRegistry()
    registry.register(EmitirNFeTool())

    # Momento 1: roteamento
    classifier = GlinerIntentClassifier(model_name="gliner2-base")
    router = SemanticRoutingRole(
        registry, classifier, config=RoutingConfig.from_env(),
    )

    # Momento 2: extração (passada ao ToolInvoker como hook)
    extractor = GlinerArgumentExtractor(registry, model_name="gliner2-base")

    async def hook(text: str, tool_name: str):
        return await extractor.extract(text, tool_name)

    invoker = ToolInvoker(
        log=log, registry=registry, pre_invoke_args_extractor=hook,
    )

    # Request do usuário
    request = Event.domain_from(
        agent_id="agent-1",
        type="user.message.received",
        data={"text": "Emitir NF-e para CNPJ 12.345.678/0001-90 no valor de R$ 1500,50"},
        correlation=CorrelationContext.new(),
    )

    # M1: role classifica
    events = await async_route_on_user_message(router, request)
    routed = events[0]  # tool.emitir_nfe.requested (ou routing.unclassified)
    print("M1 emitted:", routed.event_type, routed.data)

    # M2: invoker processa
    response = await invoker.handle_request_event(routed)
    print("M2 response:", response.unwrap().event_type, response.unwrap().data)

    await redis.aclose()


asyncio.run(main())
```

Output esperado (com o modelo treinado / fine-tuned):

```
M1 emitted: tool.emitir_nfe.requested {'args': {}, 'routing': {...}}
M2 response: tool.emitir_nfe.completed {'result': {'chave': 'nfe-12.345.678/0001-90', 'valor': 1500.5}, ...}
```

---

## 5. Telemetria

`structlog` em cada evento:

| Evento | Campos |
|---|---|
| `routing.decided` | `routing_target`, `routing_confidence`, `routing_latency_ms`, `routing_model`, `routing_schema_version`, `routing_outcome="decided"` |
| `routing.unclassified` | `routing_target=""`, `routing_confidence`, `routing_latency_ms`, `routing_outcome="unclassified"` |
| `routing.classify_failed` | `error`, `request_event_id` (apenas em classifier Err) |
| `routing.args_extracted` | `args_tool`, `args_extracted_fields`, `args_latency_ms` |
| `routing.args_invalid` | `args_tool`, `args_reason`, `args_missing`, `args_type_mismatches` |

Métricas de counter (futuro, ADR-014): `routing.decided`,
`routing.unclassified`, `routing.args_extracted`,
`routing.args_invalid`. Histogramas: latência M1, latência
M2, confidence M1.

---

## 6. Performance e limites

| Aspecto | Valor típico (CPU) | Em GPU |
|---|---|---|
| Latência M1 (1 texto) | 30-100ms | 5-15ms |
| Latência M2 (1 tool, 3 fields) | 50-200ms | 10-30ms |
| Cold start (modelo 100-300MB) | 1-3s | 1-3s |
| Memória em steady state | ~500MB | ~500MB |

**Mitigações futuras (fora do escopo V1):**

- Batching de M1 (GLiNER2 ganha 5-10× com batch de 8-16).
- Warmup explícito (`await role.warmup()` chama
  `classifier.classify("warmup", labels)` para pré-pagar
  o cold start).
- Cache LRU por `(text_hash, schema_version)`. A Role já
  expõe `schema_version` como chave; o caller implementa.

---

## 7. Testes

```bash
# M1 (Role + classifier)
pytest fmh_agents/tests/unit/roles/test_semantic_router.py -v

# M1 parsing (sem modelo)
pytest fmh_backend/tests/unit/knowledge/test_gliner_intent.py -v

# M2 (extractor + hook)
pytest fmh_backend/tests/unit/knowledge/test_argument_extractor.py -v
pytest fmh_backend/tests/unit/tools/test_invoker_args_extractor.py -v
```

Cobertura:
- 26 testes no M1 Role (snapshot de labels, threshold,
  PII hygiene, determinismo, sistema reativo, config).
- 15 testes no classifier parser (4 shapes GLiNER2
  toleradas, drop defensivo, threshold, sort).
- 23 testes no M2 extractor (walk_schema, coerce, regex,
  threshold, isolation de erros).
- 12 testes no hook (merge, args_invalid, retrocompat,
  replay idempotente).

Total M1+M2: **76 testes novos**, sem dependência de
GLiNER2 instalado.

---

## 8. Veja também

- [ADR-013](../fmh_agents/ADRs/ADR-013-Semantic-Routing-GLiNER2.md) — decisão completa
- [ADR-006](../fmh_agents/ADRs/ADR-006-Tool-Role-Separation.md) — Role × Tool
- [ADR-010](../fmh_backend/ADRs/ADR-010-Memory-Business-Tier.md) — PII gate (a ser consultado para o `user.message.received` original)
- [ADR-012](../fmh_backend/ADRs/ADR-012-IntentRouter-HTTP-Gateway.md) — origem do `user.message.received`
- [tools.md](./tools.md) — Tool Protocol, ToolInvoker, `args_invalid` event
- [graphrag.md](./graphrag.md) — projeção de tool calls no FalkorDB
- [dead_letter_queue.md](./dead_letter_queue.md) — DLQ consumindo `routing.unclassified` e `tool.{name}.args_invalid`
