<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-013: Roteamento Semântico de Intent via GLiNER2

**Status:** Aceito
**Data:** 14 de junho de 2026 (M1 aceito 14/jun, M2 aceito 15/jun)
**Versão:** 0.6.0
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** ADR-006, ADR-009, ADR-010, ADR-012; `fmh_backend/.../knowledge/extraction/gliner.py`

---

## 1. Contexto

O `IntentRouter` HTTP (`fmh_backend/.../api/intent_router.py`) é o
gateway de entrada: recebe `user.message.received` e hoje exige que
o `target` (nome da tool) seja **explícito no body**. Isso funciona
para integrações máquina-a-máquina, mas exige que o chamador
(classificador externo, LLM cliente, ou humano) já tenha feito a
classificação de intent.

Em conversas com o usuário final, o texto chega em linguagem
natural. Para o sistema reativo despachar o evento à tool certa,
precisamos de um **classificador de intent zero-shot** que:

- Rode **local** (sem custo por chamada, sem rate limit externo).
- Seja **determinístico** para o mesmo input + labels.
- **Não** dependa de treinar/rotular dataset.
- Saiba mapear o texto em uma das `Tool` registradas no
  `ToolRegistry` (sem hard-code de catálogo).
- Tenha **fallback explícito** quando a confiança é baixa
  (fail-closed, não "chuta" uma tool).

GLiNER2 (`gliner2>=0.1.0`, resolvido em `uv.lock` como `1.3.1`) já
é usado no framework para extração de entidades em
`fmh_backend/.../knowledge/extraction/gliner.py`, atrás do
`GlinerEntityExtractor` (Template-Method). O
`PiiRedactionTool` (`fmh_backend/.../tools/pii.py:221`) já consome
qualquer subclasse de `GlinerEntityExtractor` por DI. A peça que
falta é um classificador de intent que reusa essa mesma abstração.

A tentativa anterior — `fmh_agents/.../tools/semantic_invoker.py`
— foi um stub quebrado: imports comentados (B1), import top-level
de dep opcional (B2), assinatura que violava o `Tool` Protocol
(B3), classe sem `name/description/input_schema` (B4), e na prática
não invocava tool nenhuma (B5). Está documentado na avaliação
interna que originou este ADR. **Este ADR o substitui.**

---

## 2. Decisão

Adotamos um pipeline em **dois momentos** desacoplados:

### 2.1 Momento 1 — Roteamento semântico (intent only)

Um **`SemanticRoutingRole`** (classe Python, **não** Tool, conforme
ADR-006) classifica o texto do usuário em um `target_tool` do
`ToolRegistry`. Saída: evento `tool.{name}.requested` (path feliz)
ou `routing.unclassified` (abaixo do threshold).

**Componentes:**

| Componente | Local | Responsabilidade |
|---|---|---|
| `SemanticRoutingRole` | `fmh_agents/src/fmh_agents/roles/semantic_router.py` | Orquestra: monta schema GLiNER2 a partir do registry, chama classifier, decide threshold, emite evento |
| `GlinerIntentClassifier` | `fmh_backend/.../knowledge/extraction/gliner_intent.py` | Subclasse de `GlinerEntityExtractor`. Implementa `_run_model` chamando `await asyncio.to_thread(model.extract, text, schema)` |
| `RoutingDecision` (pydantic) | mesmo arquivo do Role | `target_tool: str`, `confidence: float`, `candidates: list[CandidateScore]`, `schema_version: int` |
| `IntentClassifier` (Protocol) | `fmh_backend/.../knowledge/extraction/base.py` | `async classify(text, labels) -> Result[Classification, ToolError]` — desacopla Role de GLiNER2 |

**Critério de unclassified:** `confidence < threshold`, onde
`threshold` é configurável via env `FMH_ROUTING_THRESHOLD` (default
`0.6`). Configurabilidade é importante porque o operador pode
querer ser mais conservador (0.7) se houver um consumer LLM
confiável no DLQ, ou mais permissivo (0.5) se as descrições das
tools forem muito boas.

**Determinismo do `event_id`:** o Role produz
`event_id = uuid5(NAMESPACE, sha256(text + schema_version))`,
inspirado em `api/intent_router.py:56-86`. Isso permite que
replays sejam deduplicados pelo `ToolInvoker` (que já dedupa por
`event_id`).

**Labels geradas dinamicamente:** na instanciação do Role, lê
`registry.list_descriptors()` e gera um schema GLiNER2 com **uma
label** (`intent`) e **classes = `[tool.name for tool in
descriptors]`**. Recalculado a cada instanciação (decisão de
simplicidade). Vantagens: zero hard-code, evolui com o registry,
auditoria simples (o `schema_version` é hash do `list_descriptors`).

### 2.2 Momento 2 — Extração de argumentos (schema da tool alvo)

Um **`SemanticArgumentExtractor`** popula os slots do
`input_schema` da tool escolhida a partir do mesmo texto. Roda
**dentro do `ToolInvoker`**, antes da chamada à Tool.

**Componentes:**

| Componente | Local | Responsabilidade |
|---|---|---|
| `SemanticArgumentExtractor` | `fmh_backend/.../knowledge/extraction/argument_extractor.py` | Recebe `ToolRegistry`. `extract(text, target_tool) -> Result[dict, ToolError]` |
| `pre_invoke_args_extractor` (hook) | `fmh_backend/.../tools/invoker.py` | Parâmetro opcional no `ToolInvoker`. Se setado, é invocado antes de `tool.invoke(**merged_args)` |
| `ArgExtraction` (pydantic) | mesmo arquivo do extractor | `tool_name`, `fields: dict`, `confidences: dict[str, float]`, `schema_version` |

**Schema GLiNER2 por tool:** o `input_schema` (JSON-Schema) é
lido do `ToolRegistry`. Para cada propriedade:

- `type: string` → `entities("nome_do_campo", dtype="str")`
- `type: number`/`integer` → `entities("nome_do_campo", dtype="num")`
- `type: string, format: date` → `entities("nome_do_campo", dtype="str")` (validação de formato é pós-extração)
- `type: boolean` → **não extrai** (intencional: boolean é decisão
  do usuário, não um span a identificar — fallback para LLM se
  necessário em iteração futura)

**Política de merge:** `merged = {**extracted_fields,
**request.data["args"]}`. Princípio da menor surpresa: argumentos
explícitos do chamador (e.g. `idempotency_key`, `correlation_id`,
`args` em chamada programática) **não** são sobrescritos pelo
extractor. O extractor só preenche o que falta.

**Validação pós-merge:** o dict final é validado contra
`tool.input_schema` (JSON-Schema). Se falhar → emite
`tool.{name}.args_invalid` para DLQ em vez de invocar com payload
parcial. Idempotência: o `idempotency_key` do request original é
repassado.

**Por que não fazer as duas extrações num único forward pass:**
schema de intent é fixo por turno (uma `intent` label, N classes);
schema de args muda por tool. Acoplar os dois economiza ~30-50ms
mas amarra os modos. Para V1, manter separado é mais simples de
testar, instrumentar e cachear independentemente.

### 2.3 Eventos no barramento

| Evento | Emissor | Consumidor |
|---|---|---|
| `user.message.received` | `IntentRouter` HTTP | `SemanticRoutingRole` (M1) |
| `tool.{name}.requested` | `SemanticRoutingRole` | `ToolInvoker` (M2 acontece aqui) |
| `routing.unclassified` | `SemanticRoutingRole` | DLQ + consumer LLM (futuro) |
| `tool.{name}.args_invalid` | `ToolInvoker` | DLQ |
| `tool.{name}.completed` / `failed` | `ToolInvoker` (já existe) | Auditoria / resposta |

### 2.4 Onde mora cada peça

```
fmh_agents/
└── src/fmh_agents/roles/
    └── semantic_router.py        # SemanticRoutingRole + sistema reativo

fmh_backend/
└── src/fmh_backend/knowledge/extraction/
    ├── base.py                   # + IntentClassifier Protocol
    ├── gliner.py                 # (já existe) GlinerEntityExtractor base
    ├── gliner_intent.py          # GlinerIntentClassifier (M1)
    └── argument_extractor.py     # SemanticArgumentExtractor (M2)
└── src/fmh_backend/tools/
    └── invoker.py                # + hook pre_invoke_args_extractor
```

M1 em `fmh_agents` (camada de composição, Role) e M2 em
`fmh_backend` (extractor + integração ao invoker). M1 depende de
M2 apenas via tipos, não em runtime.

### 2.5 Telemetria

`structlog` em cada decisão:
```
log.bind(
    routing_target=...,
    routing_confidence=...,
    routing_latency_ms=...,
    routing_model=...,
    routing_schema_version=...,
    routing_threshold=...,
    routing_outcome="decided"|"unclassified"|"args_invalid",
)
```

Métricas mínimas (futuro, ADR-014): contadores
`routing.decided`, `routing.unclassified`, `routing.args_extracted`,
`routing.args_invalid`, histogramas de latência e confiança.

---

## 3. Trade-offs

### Prós

- **Zero-shot**: adiciona uma Tool nova no registry e ela
  automaticamente vira uma classe candidata. Sem treinar, sem
  rotular, sem deploy coordenado.
- **Local**: sem custo por chamada, sem rate limit externo,
  execução offline. Importante para o domínio fiscal/ERP onde
  compliance e latência importam.
- **Determinístico**: mesmo input + mesmo modelo + mesmas labels
  → mesmo `target_tool`. Facilita replay e teste.
- **Fail-closed explícito**: confidence baixa → `routing.unclassified`
  → DLQ. Nunca "chuta" uma tool que não foi pedida.
- **Composição limpa**: Role reusa `GlinerEntityExtractor`, que
  já é Template-Method testado (`tests/unit/knowledge/test_extraction.py`).
  `ToolInvoker` ganha um hook opcional, sem breaking change.
- **Schema dinâmico**: `input_schema` da tool é a fonte de
  verdade para M2. Tool evolui → args evoluem.

### Contras

- **Cold start**: GLiNER2 é um transformer (centenas de MB). Primeiro
  request após boot paga ~1-3s. Mitigável com warmup no startup
  do Role (pre-load opcional via `await role.warmup()`).
- **Latência por turno**: ~30-100ms em CPU para M1, mais M2 (depende
  do nº de campos). Em GPU cai para ~5-15ms. Mitigável com batching
  futuro (fora do escopo V1).
- **Modelo precisa estar disponível**: depende do deploy ter
  baixado o modelo (HuggingFace, local path, etc.). Opt-in via
  extra `[gliner]`.
- **GLiNER2 ainda em movimento**: API pode mudar entre minor
  versions. Mitigado pelo padrão Template-Method — só
  `gliner_intent.py` e `argument_extractor.py` conhecem a API.
- **Threshold precisa ser calibrado**: 0.6 é chute informado.
  Recomendado rodar avaliação com corpus real (ADR-014 futuro).
- **Sem extração de listas/arrays**: V1 só lida com campos
  escalares. Arrays/objetos aninhados ficam para iteração
  seguinte.

### Alternativas consideradas

- **LLM-only via LiteLLMTool**: rejeitado — adiciona custo por
  chamada, latência variável, não-determinístico, dependência de
  provedor externo. Bom como **fallback** (consumer LLM no DLQ
  para `routing.unclassified`), ruim como caminho primário.
- **Regex + payload-key matching**: rejeitado — reusaria
  `HeuristicEntityExtractor`, mas classificar intent por regex é
  frágil e não generaliza.
- **Semantic search no FalkorDB**: rejeitado para V1 — depende
  de Knowledge Graph populado, e a busca semântica resolve "qual
  documento é sobre isso", não "qual tool deve rodar". Pode ser
  útil **depois** do roteamento (para enriquecer args com
  contexto).
- **Embeddings + kNN**: rejeitado — exigiria treinar/embarcar
  intents rotuladas, custo de setup alto, e GLiNER2 com classes
  dinâmicas resolve o mesmo problema mais simples.
- **Tool única de roteamento no registry**: rejeitado — viola
  ADR-006 (Role ≠ Tool) e reintroduziria o stub quebrado de
  `semantic_invoker.py`.
- **M1 + M2 num único forward pass GLiNER2**: rejeitado para V1
  — economiza ~30-50ms mas amarra os modos e dificulta teste
  independente. Rever se profiling mostrar que vale.

---

## 4. Consequências

### Para o time

- Remover `fmh_agents/src/fmh_agents/tools/semantic_invoker.py`
  (substituído por `roles/semantic_router.py`).
- Adicionar `fmh_agents/ADRs/ADR-013` (este documento).
- Adicionar testes em `fmh_backend/tests/unit/knowledge/test_argument_extractor.py`
  e `fmh_agents/tests/unit/roles/test_semantic_router.py` usando
  fake `IntentClassifier` (mesmo padrão de `FakeLLMTransport`
  do ADR-008).
- Configurar `FMH_ROUTING_THRESHOLD` no `.env` por ambiente.
- Calibrar threshold com corpus real (ADR-014 proposto).

### Para a arquitetura

- O `Tool` Protocol não muda. O `ToolInvoker` ganha **um**
  parâmetro opcional novo (`pre_invoke_args_extractor`), retrocompatível.
- O `EventLog` ganha 2 tipos novos: `routing.unclassified` e
  `tool.{name}.args_invalid` (este último é uma especialização de
  falha de tool, mas com semântica distinta para roteamento de DLQ).
- O framework passa a oferecer uma **facade de roteamento
  semântico opt-in** (assim como hoje oferece GLiNER2 opt-in via
  extra `[gliner]`). Apps que não quiserem semântica continuam
  usando `IntentRouter` HTTP com `target` explícito.

### Para a aplicação

- Aplicação que quiser roteamento semântico instancia
  `SemanticRoutingRole`, registra o sistema reativo
  `route_on_user_message` no `ReactiveDispatcher`, e — se quiser
  extração de args — passa `pre_invoke_args_extractor` ao
  construir o `ToolInvoker`.
- Aplicação que não quiser: zero impacto.

---

## 5. Veja também

- [ADR-006: Tool × Role separation](ADR-006-Tool-Role-Separation.md) —
  justifica Role ≠ Tool e o padrão de injeção.
- [ADR-009: Tool Tiers A/B/C](ADR-009-Tool-Tiers.md) — convenção
  de organização do `fmh_agents`.
- [ADR-010: Memory Business Tier](../fmh_backend/ADRs/ADR-010-Memory-Business-Tier.md) —
  precede este ADR: já documenta GLiNER2 como mecanismo de
  extração em PII / knowledge graph.
- [ADR-012: IntentRouter HTTP Gateway](../fmh_backend/ADRs/ADR-012-IntentRouter-HTTP-Gateway.md) —
  origem do `user.message.received` que alimenta o M1.
- `fmh_backend/src/fmh_backend/knowledge/extraction/gliner.py` —
  base `GlinerEntityExtractor` (Template-Method) reusada por M1.
- `fmh_backend/src/fmh_backend/tools/invoker.py:79-150` —
  `ToolInvoker` que ganha o hook de M2.
- `fmh_backend/src/fmh_backend/api/intent_router.py:56-86` —
  origem do padrão de `event_id` determinístico via UUID5.
