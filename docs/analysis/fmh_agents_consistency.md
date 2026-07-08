<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Análise de Consistência: Princípios do Framework × `fmh_agents`

**Data:** 11 de junho de 2026
**Escopo:** cruzar os 6 princípios invioláveis de
`fmh_backend` (ADR-001 §2.1) com a implementação real de
`fmh_agents` (LiteLLMTool, 4 Roles, CachingLLMTransport,
LLMConfig). Mapear **consistência** (princípios respeitados),
**tensões** (princípios respeitados mas com nuances) e
**extensões** (o que `fmh_agents` adiciona além do
framework).

**Conclusão upfront:** `fmh_agents` é uma extensão
**perfeitamente consistente** com o framework. As tensões
encontradas são **trade-offs legítimos** documentados em
ADR, não violações. O pacote demonstra que o framework
oferece **espaço de manobra suficiente** para a camada de
aplicação sem precisar de patches no core.

---

## 1. Princípios do `fmh_backend` (referência)

Do [ADR-001 §2.1](../ADRs/ADR-001-Arquitetura.md):

| # | Princípio | Implicação |
|---|-----------|------------|
| 1 | **Imutabilidade total no core** | `Event`, `World`, `AgentView` são frozen. |
| 2 | **Core funcional** | Sistemas são funções `World → list[Event]`. Side effects via adaptadores. |
| 3 | **Event Sourcing estrito** | Redis Streams é a fonte, World é projeção. |
| 4 | **Async-First** | Todo I/O é non-blocking. Workers são stateless. |
| 5 | **Fail Fast** | Circuit breaker, timeout, DLQ. |
| 6 | **Determinismo** | `event_id = uuid5(causation, type, payload)`. Replay idempotente. |

Complementos relevantes (outros ADRs):

- **ADR-004 §2.3**: Tools = Protocol no core, adapters na
  borda. Resiliência mora no adapter.
- **ADR-005**: `idempotency_key` injetado pelo `ToolInvoker`
  — at-least-once → at-most-once para side effects.
- **ADR-006 §2.6**: Roles passam `idempotency_key` à Tool;
  a Tool **não** dedupa (LiteLLM não tem cache
  server-side); a Role é responsável por cachear se
  quiser.

---

## 2. Mapeamento Princípio × `fmh_agents`

### 2.1 Princípio 1 — Imutabilidade no core

**Veredito: ✅ respeitado (e reforçado)**

`fmh_agents` **não toca no core**. Os value objects do
`fmh_agents` são frozen:

- `LLMResponse`, `LLMUsage`, `LLMChunk` — `@dataclass(frozen=True, slots=True)`.
- `Plan`, `PlanStep`, `Summary`, `ChatReply` — Pydantic
  `BaseModel` (imutável por default).
- `LLMConfig` — `@dataclass(frozen=True)` com `__post_init__`
  de validação.
- `LiteLLMTool` carrega estado (model, rate limiter, etc.)
  mas é **singleton-like** (configurado uma vez) — mutação
  não é exposta como API pública.

**Reforço**: a separação Tool × Role (ADR-006) **depende**
da imutabilidade. Roles retornam `Result[T, ToolError]`
imutável; LiteLLMTool retorna `Result[LLMResponse,
ToolError]`. Toda a comunicação é por valor. Sem aliases.

### 2.2 Princípio 2 — Core funcional

**Veredito: ✅ respeitado (com nota)**

`fmh_agents` não implementa `CyclicSystem` nem
`ReactiveSystem` — **propositalmente**. Roles são
**classes Python standalone**, não sistemas puros do
framework. O ADR-006 §2.5 recomenda mantê-las como
classes, **não** como `tool.llm.complete.requested` events:

> "Recomendação: manter Roles como classes Python, e usar
> `LiteLLMTool` direto em sistemas quando necessário."

Quando o caller quiser integrar com o framework, ele
escreve um **sistema reativo** (camada de aplicação) que
invoca a Role. A Role em si é "pure-ish": chama a Tool
(com `await`), parseia a saída, retorna `Result`. Sem
acesso a `World`, sem side effects além de chamar a Tool.

**Nota**: a Role **não** é uma função pura (ela faz I/O
via LiteLLMTool), mas é um **adaptador funcional**: dados
mesmos inputs → mesma chamada à Tool → mesma resposta
(quando `temperature=0`). A determinismo é delegado à
Tool, e a Tool o garante via `idempotency_key` + cache
(opcional).

**Tensão encontrada (não-violação)**: a Role mantém
estado (`_model`, `_temperature`, `_max_tokens`). É
configuração, não estado mutável de runtime. Coerente
com o "sistemas stateless" do princípio 4.

### 2.3 Princípio 3 — Event Sourcing estrito

**Veredito: ✅ respeitado**

`fmh_agents` **não escreve no EventLog**. As Roles não
emitem eventos. O `ChatRole.reply()` exemplo (chat.py) tem
um docstring que diz "the caller is responsible for
`append_message` after a successful reply" — ou seja, o
caller integra com o EventLog. A Role não faz isso.

O exemplo 04 (`04_reactive_system_with_llm.py`) mostra
**como** integrar: um sistema reativo puro (que **usa** o
framework) chama a Role (que **usa** a Tool). Três
camadas com responsabilidades distintas:

```
System (puro, framework) → Role (semântico, fmh_agents) → Tool (I/O, fmh_backend)
```

A Role fica no **meio**: nem pura (faz I/O) nem
side-effecting (não emite eventos). Isso é coerente com
ADR-006 §2.5: "Roles são puramente Python. Podem ser
usadas standalone (sem EventLog) ou via sistemas reativos."

### 2.4 Princípio 4 — Async-First

**Veredito: ✅ respeitado exemplarmente**

`fmh_agents` é **100% async** na superfície de I/O:

- `LiteLLMTool.invoke` é `async def`.
- `LiteLLMTool.astream` é `async def` retornando `AsyncIterator`.
- `PlannerRole.plan`, `SummarizerRole.summarize`, `ChatRole.reply`,
  `PersonalizedRole.respond` — todos `async def`.
- `LiteLLMTransport.complete` é `async def`.
- `CachingLLMTransport.complete` é `async def`.
- `RateLimiter.allow` é `async def` (sliding window com
  `asyncio.Lock`).
- `CostBudget.can_spend` / `charge` são `async def`.

Uso de `asyncio.wait_for(timeout)` para timeout, conforme
ADR-001 §2.1 (Fail Fast). O `_astream_litellm_inner` é um
async generator bem desenhado, com timeout **per-chunk**
(porque `asyncio.wait_for` não funciona sobre iteradores).

Único uso de sync: `load_env()` em `config/llm.py` (lê
arquivo `.env` na boot, I/O de arquivo, não há versão
async razoável).

### 2.5 Princípio 5 — Fail Fast

**Veredito: ✅ respeitado (e enriquecido)**

`fmh_agents` implementa 5 caminhos de falha explícitos
na `LiteLLMTool.invoke`:

1. **`stream=True` em `invoke()`** → `Err(ToolError("use
   astream()..."))`. Erro de uso detectado cedo.
2. **Rate limit excedido** → `Err(ToolError("rate_limited"))`
   **antes** da chamada HTTP.
3. **Cost budget excedido** → `Err(ToolError("budget_exhausted"))`
   **antes** da chamada HTTP.
4. **Auth error** (`_AuthLike`) → **aborta** fallback
   chain (auth não é recuperável).
5. **Rate limit / timeout / outros** → continua
   fallback chain.
6. **Todos os modelos falham** → `Err(last_err)`.

O fallback chain é uma **camada extra de resiliência** que
o framework não obriga mas que `fmh_agents` recomenda.
O `timeout_s` é aplicado via `asyncio.wait_for` (per-call,
não global — operador escolhe).

`CachingLLMTransport` adiciona mais um caminho: **erros
não são cacheados** (decisão correta: erros são transientes,
cachear envenenaria a key).

**Falta (não-violação)**: `fmh_agents` não tem DLQ nem
circuit breaker. Por quê? Porque `LiteLLMTool` é um
**adapter** (ADR-001 §4.7: "sistemas puros não usam
circuit breaker. Adaptadores que fazem I/O (LLM, API
externa, banco) usam"). O **adapter concreto** de cada
aplicação pode wrappar `LiteLLMTool` com circuit breaker
— a abstração está no framework (`fmh_backend.resilience`).
A decisão de **não** wrappear por default é: LiteLLM já
faz fallback entre provedores, e o `CachingLLMTransport`
reduz chamadas. O caller decide se precisa de mais.

### 2.6 Princípio 6 — Determinismo

**Veredito: ✅ respeitado (com responsabilidade compartilhada)**

`fmh_agents` **não** gera `event_id` (isso é do framework).
Mas honra o contrato de `idempotency_key`:

- Toda Role constrói um `idempotency_key` estável via
  `hashlib.sha256(task|context).hexdigest()[:32]`.
- O `idempotency_key` é propagado para `LiteLLMTool.invoke`.
- A Tool **não** dedupa (LiteLLM não tem cache server-side);
  passa adiante para o `transport`.
- O `CachingLLMTransport` (opcional) **dedupa** por
  `(idempotency_key, model, response_format)`.

Para determinismo estrito (replay seguro), o ADR-008 §2.2
recomenda **`temperature=0` em produção** quando caching é
desejado. O `CachingLLMTransport` **não** inclui
`temperature` na chave — temperatura fora de 0 é
responsabilidade do caller.

**Tensão encontrada (resolvida)**: o LiteLLM raw não é
determinístico (servidor do provider tem nondeterminism).
A solução é a **combinação** `temperature=0` +
`CachingLLMTransport` + `idempotency_key` estável. Não é
violação; é composição de primitivos.

### 2.7 Princípio ADR-004 (Tools são Protocol no core)

**Veredito: ✅ respeitado**

`LiteLLMTool` herda de `Tool` (em `tools/protocol.py`).
Implementa exatamente o contrato:

- `name: str` (classe attr: `"llm.complete"`)
- `description: str`
- `input_schema: dict` (JSON-schema válido)
- `async def invoke(*, idempotency_key, **kwargs) -> Result[Any, ToolError]`

A `LiteLLMTool` é registrável no `ToolRegistry` e consumível
pelo `ToolInvoker`. Nada no `fmh_backend` precisou mudar.

**Nota sobre `astream`**: `LiteLLMTool` **estende** o
Protocol com `astream` (async iterator). É duck typing:
o Protocol exige `invoke`, mas não proíbe métodos
adicionais. O `ToolInvoker` chama só `invoke`; `astream` é
para callers diretos (Roles, sistemas).

### 2.8 Princípio ADR-005 (`idempotency_key` em Tools)

**Veredito: ✅ respeitado (com nuance)**

`LiteLLMTool.invoke` aceita `idempotency_key` como
**keyword obrigatório** (assinatura) e o propaga para o
`transport.complete(..., idempotency_key=...)`. O
`ToolInvoker` consegue chamar com `idempotency_key=
str(event_id)` sem quebrar.

**Nuance**: a Tool **não** usa `idempotency_key` para
deduplicar (LiteLLM não tem cache server-side). A chave
atravessa a fronteira até o `CachingLLMTransport`, que
**sim** deduplica. Documentado em ADR-007 §2.7.

### 2.9 Princípio ADR-006 (Tool × Role)

**Veredito: ✅ respeitado exemplarmente — é o próprio ADR**

`PlannerRole`, `SummarizerRole`, `ChatRole`,
`PersonalizedRole` são **classes Python** (não Tools).
Recebem `LiteLLMTool` por injeção. Output é `Result[T,
ToolError]` com `T` tipado (`Plan`, `Summary`, `ChatReply`,
`str`). Cada uma tem:
- `SYSTEM_PROMPT` próprio.
- Schema pydantic de output.
- Lógica de parsing específica (`parse_model_json` tolera
  markdown fences, comum em modelos locais como gemma).
- Hash de `idempotency_key` estável baseado em inputs.

A separação é **exemplar**: 1 `LiteLLMTool` registrada,
N Roles. Trocar provedor é 1 linha (passar outra
`LiteLLMTool` para a Role).

---

## 3. Extensões que `fmh_agents` adiciona

`fmh_agents` não só respeita o framework — **adiciona 5
extensões** que o framework não tem (e não precisa ter,
por design):

### 3.1 LLMTransport — contrato compartilhado entre framework e cliente

O framework (`fmh_backend.tools.llm_transport`) define
`LLMTransport`, `LLMResponse`, `LLMUsage`, `LLMChunk` como
**contratos genéricos de I/O LLM**. Eles são o "shape
mínimo" que qualquer cliente LLM precisa expor: `complete
(...)` async retornando um dict estilo-LiteLLM, e os
dataclasses para uso e chunk de streaming.

**Por que está no framework e não no cliente**:
`LLMTransport` é uma fronteira de I/O, não uma decisão
de cliente. LiteLLM é uma escolha do `fmh_agents` (ADR-007);
qualquer outro cliente pode implementar `LLMTransport`
com OpenAI SDK, Anthropic SDK, Ollama direto, etc. Movendo
o Protocol para `fmh_backend.tools`, o framework oferece o
contrato e cada vertical implementa a sua estratégia.

**Razão histórica**: o Protocol vivia no `fmh_agents`
porque era o único consumidor. Quando a consistência
começou a ser revisitada, ficou claro que a segunda
vertical teria que duplicar o Protocol — sinal de que
a abstração pertence ao framework. Movido em jun/2026
junto com ADR-011 (cache plugável).

**Cliente concreto** (`fmh_agents.tools.llm`):
- `LiteLLMTransport`: a chamada real ao `litellm.acompletion`.
- `LiteLLMTool`: a Tool que orquestra fallback chain, rate
  limit, circuit breaker, cost budget, streaming.
- `CachingLLMTransport`: decorator que memoiza por
  `idempotency_key` (ADR-011).

### 3.2 CachingLLMTransport — decorator transparente

Implementa **at-most-once** para chamadas determinísticas
(ADR-008). É o que fecha a janela entre o
`idempotency_key` aceito e o LiteLLM sem cache.

**Razão**: o framework garante replay determinístico
(event_id = uuid5), mas a LLM tem nondeterminism. O
CachingLLMTransport é o patch ortogonal que re-introduz
determinismo na chamada de I/O.

**Insight arquitetural**: o cache mora no **transport**, não
na Tool, não na Role. Decorator pattern — composição
livre, sem alterar a Tool.

### 3.3 Hierarquia opcional Tipo A/B/C (ADR-009)

`fmh_agents` introduz um **vocabulário de organização**
(Tipo A — Transporte, Tipo B — Capability, Tipo C —
Domínio) que o framework não conhece. O `Tool` Protocol é
genérico; a classificação é convenção de aplicação.

`LiteLLMTool` é **explicitamente Tipo A**. As Roles
(`PlannerRole` etc.) são **Tipo C** "efetivas" (não
registradas como Tool, mas semanticamente são Tipos C que
usam um Tipo A por injeção).

**Razão**: o framework diz "Tool é I/O"; `fmh_agents` diz
"Tool tem 3 níveis de especialização, mas só o Tipo A é
obrigatório para I/O puro". O vocabulário é um guia de
organização de código, não uma hierarquia mandatória.

### 3.4 LLMConfig + RateLimiter + CostBudget

`fmh_agents` adiciona 3 primitivos de configuração que o
framework não tem:

- `LLMConfig.from_env()` lê `FMH_LLM_*` (env vars
  específicas de LLM).
- `RateLimiter` é sliding-window com `asyncio.Lock`.
- `CostBudget` é sliding-window com cobrança
  **post-call** (usa `litellm.completion_cost`).

Esses são **adapter-side** (ADR-001 §4.7: resiliência
mora no adapter). O framework tem `resilience/`
(circuit breaker, retry, etc.) mas **não** tem rate
limiter / cost budget — são domínio-específicos de LLM.

### 3.5 JSON-schema structured output

`LiteLLMTool` aceita `response_format: dict` (JSON schema)
e o passa ao LiteLLM. Roles definem `model_json_schema()`
dos seus Pydantic models (e.g. `Plan.model_json_schema()`)
e usam `parse_model_json` para extrair JSON de markdown
fences (comum em modelos locais).

**Razão**: a fronteira Tool×Role precisa de **contrato
de saída tipado**. JSON schema é o vocabulário neutro
(provider-agnostic) que LiteLLM sabe passar para qualquer
provider que suporte structured output.

---

## 4. Tensões Encontradas (e Resoluções)

| # | Tensão | Resolução |
|---|--------|------------|
| 1 | LiteLLM raw é não-determinístico (viola Princípio 6 indiretamente) | Composição `temperature=0` + `CachingLLMTransport` + `idempotency_key` estável. Documentado em ADR-007 §2.7. |
| 2 | LiteLLMTool **não** deduplica por `idempotency_key` (não usa a chave para nada) | Aceito por design (LiteLLM não tem cache server-side); a chave é "tunneled" para o transport que cacheia. |
| 3 | Role não é "pura" (faz I/O) | Aceito — Role é "adaptador funcional", não sistema. O framework define sistema como puro, Role é conceito da camada de aplicação. |
| 4 | `idempotency_key` em streaming (`astream`) não é aplicada por chamada, é per-chunk | Aceito — `asyncio.wait_for` não funciona sobre `AsyncIterator`. Timeout aproximado é a única opção. |
| 5 | Sem DLQ / circuit breaker na `LiteLLMTool` | Decisão de design — LiteLLM já faz fallback. Aplicação pluga circuit breaker do framework (`fmh_backend.resilience`) se quiser. |
| 6 | Configuração LiteLLM tem 3 retries implícitos em algumas paths, mas `LiteLLMTool` não tem política de retry explícita | Idem — caller configura retry do framework ou aceita o fallback chain. |

**Nenhuma é violação.** Todas são trade-offs legítimos
documentados.

---

## 5. O que `fmh_agents` **não tem** (e isso é coerente)

Para fechar o ciclo, listo o que **não** está em
`fmh_agents` (e o framework garante via outro caminho):

- **Sem DOM**: `LiteLLMTool` lê env, não XML/YAML. O
  framework também não tem DOM; é tudo dataclass imutável.
- **Sem async generators próprios** (exceto o `astream`
  único): o framework tem `AsyncIterator` (em vários
  lugares), `fmh_agents` reusa.
- **Sem eventos de domínio**: `fmh_agents` não emite
  `Event`; quem emite é o sistema reativo da aplicação.
- **Sem persistência própria**: `CachingLLMTransport`
  é in-memory. Redis-backed é plug-in opcional.
- **Sem UI**: o framework tem o retriever (`GraphRAGRetriever`)
  e o cliente HTTP-style; `fmh_agents` é puramente
  server-side.

---

## 6. Veredito Final

`fmh_agents` é uma **extensão exemplarmente consistente**
do `fmh_backend`:

| Critério | Avaliação |
|----------|------------|
| Respeita os 6 princípios do ADR-001 | ✅ sim (com tensões documentadas) |
| Implementa Tool Protocol sem hacks | ✅ exato |
| Honra `idempotency_key` no boundary | ✅ sim (propaga, não dedupa) |
| Async-first em toda a superfície pública | ✅ 100% |
| Fail Fast em todos os caminhos de erro | ✅ 6 caminhos explícitos na Tool |
| Determinismo (com composição) | ✅ via `idempotency_key` + cache + `temperature=0` |
| Separação Tool × Role (ADR-006) | ✅ 4 Roles, 1 Tool compartilhada |
| Hierarquia Tipo A/B/C (ADR-009) | ✅ vocabulário documentado, sem overhead |
| Não toca no core do framework | ✅ zero modificações em `fmh_backend` (mas contribui de volta: `LLMTransport` migrou para `fmh_backend.tools` — sinal de simbiose) |

**Recomendação**: o design de `fmh_agents` pode servir de
**modelo** para futuras camadas de aplicação
(`fmh_financial`, `fmh_logistics` etc.). Os 5 patterns
identificados (LLMTransport, CachingDecorator, JSON-schema
structured output, env-driven config, Role-as-class) são
**replicáveis**.

**Dívida técnica**: ambas as pendências detectadas
anteriormente foram quitadas em jun/2026:

- ✅ **Cache backend plugável** (ADR-011): `AsyncCacheStorage`
  Protocol com `InMemoryCacheStorage` (LRU via `OrderedDict`,
  `maxsize=1024` default) e `RedisCacheStorage` (HSET+EXPIRE
  pipeline, `scan_iter` para clear). 36 testes unitários.
- ✅ **Circuit breaker opcional** na `LiteLLMTool`:
  `LiteLLMTool(..., circuit_breaker=...)` aceita um
  `CircuitBreakerLike` Protocol compatível com `pybreaker`/
  `circuitbreaker`. 5 testes unitários.

**Padrão emergente** (vale registrar para próximas
verticais): abstrações que começam no cliente e acabam
promovidas ao framework quando uma segunda vertical
aparece. O `LLMTransport` seguiu exatamente esse caminho.

---

## 7. Conclusão Executiva

O framework `fmh_backend` define um **contrato** (Tool,
Event, World, idempotency_key). O pacote `fmh_agents` é
uma **implementação de referência** que:

1. **Honra todos os 6 princípios** (com nuances
   documentadas).
2. **Adiciona 5 extensões** ortogonais (LLMTransport,
   CachingDecorator, Tipo A/B/C, LLMConfig+RateLimiter+
   CostBudget, JSON-schema output).
3. **Não toca no core** — todas as modificações seriam
   no `fmh_backend` se algum princípio fosse violado, e
   nenhuma foi necessária.
4. **Documenta trade-offs** explicitamente em ADRs
   (006, 007, 008, 009).

**A arquitetura está validada**: o framework tem espaço
de manobra suficiente para a camada de aplicação
desenvolver features ricas (LLM com cache e fallback)
sem comprometer os princípios. Isso é o que diferencia
um framework opinativo de uma biblioteca utilitária —
e o FMH acertou.
