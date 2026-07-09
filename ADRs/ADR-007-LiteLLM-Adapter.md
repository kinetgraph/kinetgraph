<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-007: LLM Tool via LiteLLM

**Status:** Aceito
**Data:** 08 de junho de 2026
**Versão:** 0.3.0
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** ADR-006

---

## 1. Contexto

O `fmh_agents` precisava de uma Tool para chamar LLMs. As
opções consideradas:

  - **OpenAI SDK direto** (`openai>=1.0`)
  - **Anthropic SDK** (`anthropic>=0.30`)
  - **Google Generative AI** (`google-generativeai`)
  - **Ollama local** (`ollama>=0.4`)
  - **LiteLLM** (`litellm>=1.50`)

Os requisitos do projeto:

  1. **Múltiplos provedores**: cloud (OpenAI, Anthropic,
     Google) e local (Ollama).
  2. **Mesma interface**: código de aplicação não muda
     quando troca de provedor.
  3. **Fallback chain**: se o primário falhar, tenta o
     próximo (essencial para reduzir downtime).
  4. **Structured output**: JSON schema para outputs
     tipados.
  5. **Sem lock-in**: não queremos reescrever cada Role
     se trocarmos de provedor.

---

## 2. Decisão

Adotamos **LiteLLM** como adapter único, com uma `LiteLLMTool`
genérica que expõe a interface `complete(system, user, ...) ->
LLMResponse`.

### 2.1 Por que LiteLLM

| Alternativa | Prós | Contras |
|-------------|------|---------|
| OpenAI SDK | oficial, simples | só OpenAI |
| Anthropic SDK | oficial, simples | só Anthropic |
| Cada SDK direto | controle total | N×código, N×testes, N×fallback |
| **LiteLLM** | **1 interface, 100+ provedores, fallback, structured output** | dep externa pesada, abstrai features específicas |

LiteLLM dá:
  - Mesma assinatura para OpenAI/Anthropic/Google/Mistral/Ollama.
  - `acompletion()` async nativo.
  - `completion_cost()` para extrair custo.
  - `drop_params=True` para ignorar params não suportados.
  - `response_format` para structured output (JSON schema).

### 2.2 Forma da Tool

```python
class LiteLLMTool:
    name = "llm.complete"
    
    async def invoke(
        self, *, idempotency_key: str,
        system: str, user: str,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: Optional[dict] = None,
        stream: bool = False,
        **kwargs,
    ) -> Result[LLMResponse, ToolError]:
        ...
```

`LLMResponse` carrega `text`, `usage`, `model`, `latency_ms`,
`cost_usd`. É o suficiente para Roles parsearem e para
observabilidade.

### 2.3 Configuração via `LLMConfig`

```python
@dataclass(frozen=True)
class LLMConfig:
    default_model: str = "gpt-4o-mini"
    fallback_models: tuple[str, ...] = ()
    rate_limit_rpm: Optional[int] = 60
    cost_budget_per_hour_usd: Optional[float] = 2.0
    timeout_s: float = 30.0
    drop_unsupported_params: bool = True
```

`from_env()` lê `FMH_LLM_DEFAULT_MODEL`, `FMH_LLM_FALLBACK_MODELS`
(CSV), `FMH_LLM_RATE_LIMIT_RPM`, etc.

### 2.4 Fallback chain

A Tool mantém uma lista `models = [primary] + fallback_models`.
Para cada modelo:

  1. Tenta `acompletion` com `asyncio.wait_for(timeout)`.
  2. Se sucesso, retorna `Ok(LLMResponse)`.
  3. Se `RateLimitError` (ou `_RateLimitLike`), continua.
  4. Se `AuthenticationError` (ou `_AuthLike`), **aborta** —
     auth não é recuperável via fallback.
  5. Se timeout ou outro erro, registra `last_err` e continua.

Se nenhum modelo funcionar, retorna `Err(ToolError("..."))`.

### 2.5 Rate limit e Cost budget

Implementados como wrappers async no `config/llm.py`:

  - `RateLimiter(rpm)`: sliding window com `deque[timestamps]`.
  - `CostBudget(per_hour_usd)`: sliding window com `deque[(ts, cost)]`.

Ambos são checados **antes** da chamada. Se recusarem,
retornam `Err(ToolError("rate_limited"))` ou
`Err(ToolError("budget_exhausted"))` sem chamar o provider.

### 2.6 Transport abstraction

A chamada real ao LiteLLM fica atrás de um protocolo
`LLMTransport`:

```python
class LLMTransport:
    async def complete(self, *, model, messages, ...) -> dict: ...
```

A Tool tem um `LiteLLMTransport` default (chama `litellm.acompletion`).
Tests injetam um `FakeLLMTransport` (em
`fmh_agents/tests/unit/_fake_transport.py`).

Isso evita:
  - Mock global do LiteLLM.
  - Dependência de rede nos tests.
  - Acoplamento entre Tool e a lib.

### 2.7 Idempotency

A Tool aceita `idempotency_key=str(request.event_id)` (do
framework, ADR-005), mas **não** dedupa. LiteLLM não tem
cache server-side. Roles são responsáveis por:

  - Construir uma `idempotency_key` estável do input
    (ex: `hashlib.sha256(task).hexdigest()[:32]`).
  - Cachear o resultado se quiserem at-most-once.

---

## 3. Trade-offs

### Prós

- **1 dep, 100+ provedores** — troca de provedor é 1 linha.
- **Fallback chain** out-of-the-box.
- **Structured output** via `response_format` (pydantic-friendly).
- **Cost extraction** via `litellm.completion_cost`.
- **Async-first** (`acompletion`).
- **Testabilidade** via transport injetável.

### Contras

- **LiteLLM é uma dep pesada** (~50MB) com muitas
  transitive deps. Mitigável: declared como
  `[llm]` extra em `pyproject.toml`, não dep base.
- **Custo computacional**: cada chamada passa por uma
  camada de adapter. Em alta frequência, isso importa.
- **Recursos de provider não-uniformes**: structured output
  é suportado por OpenAI e Anthropic (parcialmente), mas
  não por todos. `drop_params=True` ajuda, mas
  comportamento pode variar.
- **`completion_cost` pode falhar** para modelos novos
  ou locais. A Tool lida com isso retornando `cost_usd=None`.

### Alternativas consideradas

- **Cada SDK direto**: rejeitado por explosão combinatorial
  de código de adapter.
- **LangChain**: rejeitado por ser opinionated e trazer
  abstrações que conflitam com o modelo puro do FMH.
- **Haystack**: rejeitado por ser focado em pipelines,
  não em tools.
- **Sem framework, usar SDKs**: rejeitado por duplicação.

---

## 4. Consequências

### Para o time

- Toda nova role usa `LiteLLMTool` (injetada) — não chama
  LiteLLM diretamente.
- Configuração do LLM vive em `LLMConfig`, lido de env
  (`FMH_LLM_*`).
- Tests de role usam `FakeLLMTransport`. Sem rede.
- Em produção, `LiteLLMTool` é registrado no `ToolRegistry`
  para que o `ToolInvoker` possa chamá-lo se algum sistema
  emitir `tool.llm.complete.requested`.

### Para a arquitetura

- O framework não conhece LiteLLM. O contrato `Tool` é
  genérico.
- Roles são puramente Python. Podem ser usadas standalone
  (sem EventLog) ou via sistemas reativos.
- A abstração `LLMTransport` permite trocar LiteLLM por
  outro adapter (ex: um mock de testcontainer) sem
  reescrever Roles.

### Para DevOps

- `OPENAI_API_KEY` (ou `ANTHROPIC_API_KEY`, etc) no env.
- Rate limit e cost budget configuráveis via env
  (`FMH_LLM_RATE_LIMIT_RPM`, `FMH_LLM_COST_BUDGET_USD`).
- LiteLLM aceita base URL customizada para provedores
  self-hosted (vLLM, TGI, etc).

---

## 5. Veja também

- [ADR-006: Tool × Role](./ADR-006-Tool-Role-Separation.md) —
  separação conceitual
- [fmh_agents/tools/llm.py](../../fmh_agents/src/fmh_agents/tools/llm.py)
- [fmh_agents/config/llm.py](../../fmh_agents/src/fmh_agents/config/llm.py)
- [fmh_agents/roles/](../../fmh_agents/src/fmh_agents/roles/)
- [examples/](../../fmh_agents/examples/)
