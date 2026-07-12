<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-006: Separação Tool × Role

> [!WARNING]
> **ESTE ADR FOI SUBSTITUÍDO (SUPERSEDED)**
> O conceito de `Role` como um wrapper de comportamento síncrono para `Tool` foi substituído pelo modelo do [ADR-039](./ADR-039-Role-rethinking-and-intentions-routing.md).
> No novo design, a Role é puramente um componente de dados (`RoleComponent`) e a execução de intenções é resolvida de forma pura e assíncrona pelo `IntentResolutionSystem`.

**Status:** Superseded by [ADR-039](./ADR-039-Role-rethinking-and-intentions-routing.md)
**Data:** 08 de junho de 2026
**Versão:** 0.3.0
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** ADR-001, ADR-004, ADR-007

---

## 1. Contexto

O `fmh_agents` precisava de um caminho para chamar LLMs (OpenAI,
Anthropic, locais via Ollama, etc.) dentro do fluxo de eventos.
A tentação natural foi criar uma `LLMTool` por capability
(`PlannerTool`, `SummarizerTool`, `ClassifierTool`...). Isso
**escala mal**:

  - **N tools registradas** no `ToolRegistry` para o mesmo
    adapter físico.
  - **Configuração duplicada**: cada tool tem seu próprio
    modelo, rate limit, budget, fallback.
  - **Reuso ruim**: a mesma lógica de "falar com OpenAI" precisa
    ser reescrita em cada tool concreta.
  - **Testes inflados**: cada tool precisa de mock próprio.

A separação proposta é **Tool = I/O, Role = semântica**.

---

## 2. Decisão

### 2.1 Definições

**Tool** — uma capability de I/O side-effecting. Implementa o
`Tool` Protocol (`fmh_backend.tools.protocol.Tool`).

  - Conhece o "como": fala com HTTP, banco, API.
  - NÃO conhece o "porquê" do pedido.
  - Registrável no `ToolRegistry`.
  - Output: `Result[dict|Response, ToolError]` genérico.

**Role** — uma especialização semântica. É uma classe Python
ordinária, **não** é Tool.

  - Conhece o "porquê": prompt do domínio, schema de saída,
    regras de parsing.
  - Recebe uma Tool por injeção no construtor.
  - Output: `Result[TipoPydantic, ToolError]` tipado.

### 2.2 Estrutura

```
fmh_agents/
├── tools/
│   ├── llm.py             LiteLLMTool (1 capability: completar prompt)
│   └── ...
├── roles/
│   ├── planner.py         PlannerRole (usa LiteLLMTool, retorna Plan)
│   ├── summarizer.py      SummarizerRole (usa LiteLLMTool, retorna Summary)
│   └── ...
├── config/
│   ├── llm.py             LLMConfig, RateLimiter, CostBudget
│   └── ...
└── examples/              scripts de estudo
```

### 2.3 Composição

```python
# 1 Tool de I/O
llm = LiteLLMTool(default_model="gpt-4o-mini", rate_limiter=...)

# N Roles usando a mesma Tool
planner = PlannerRole(llm=llm)
summarizer = SummarizerRole(llm=llm)

# 1 system reativo (cola entre evento e Role)
async def plan_after_validation(world, event):
    if event.event_type != "nf.validated":
        return []
    r = await planner.plan(extract_task(event))
    if r.is_err():
        return [failure_event(...)]
    return [success_event(r.unwrap().model_dump())]
```

### 2.4 Por que Role não é Tool

| Razão | Consequência |
|-------|--------------|
| Role conhece schema de saída tipado | `PlannerRole` retorna `Plan` (pydantic), não `dict` |
| Role conhece o prompt do domínio | `SummarizerRole` injeta SYSTEM_PROMPT próprio |
| Role pode compor prompts/cache/dedup | Não cabe em uma Tool I/O genérica |
| Múltiplas roles compartilham 1 Tool | 1 rate limit, 1 budget, 1 fallback chain |

### 2.5 Como uma Role vira evento (opcional)

Se o caller quiser expor a role via EventLog, ela é envolvida
em um sistema reativo — **não** em uma Tool:

```python
async def request_plan(world, event):
    if event.event_type != "task.received":
        return []
    return [Event.domain_from(
        agent_id=event.agent_id,
        type="tool.llm.complete.requested",
        data={"purpose": "plan", "task": event.data["task"]},
        causation_id=event.event_id,
    )]
```

O consumidor desse `*.requested` é o `ToolInvoker` chamando a
`LiteLLMTool` com o `purpose` no data. **Mas isso adiciona um
discriminador no data**, o que é feio. Recomendação:
manter Roles como classes Python, e usar `LiteLLMTool` direto
em sistemas quando necessário.

### 2.6 Idempotency

Toda Role repassa `idempotency_key` à Tool. A Role pode
construir uma chave estável a partir dos inputs
(`hash(task)`) ou aceitar uma chave explícita do caller.

A Tool **não** dedupa — LiteLLM não tem cache server-side.
Quem cacheia é a Role (em memória, Redis, etc.) se quiser.

---

## 3. Trade-offs

### Prós

- **Reuso**: 1 Tool serve N roles. Trocar provedor/modelo
  é uma linha.
- **Configuração centralizada**: rate limit, budget, fallback
  vivem na Tool.
- **Testabilidade isolada**: Role testa com `FakeLLMTransport`,
  sem mock de LiteLLM.
- **Tipagem forte**: cada role retorna um tipo pydantic
  específico.
- **Composição limpa**: 1 tool no registry, N roles em
  variáveis Python.

### Contras

- **Mais um conceito**: o time precisa entender Tool × Role.
- **Idempotency é responsabilidade do caller**: a Tool não
  dedupa; quem chama precisa construir a chave.
- **Discriminador se virar evento**: se expor via EventLog,
  precisa de um `purpose` no data para a Tool saber o que
  fazer. Solução pragmática: manter como classe Python.

### Alternativas consideradas

- **N tools (PlannerTool, SummarizerTool)**: rejeitado —
  duplicação de config e código.
- **Role como Tool**: rejeitado — Tool é I/O, Role é
  semântica. Misturar infla ambas.
- **LLM como sistema reativo puro**: rejeitado — sistemas
  são puros, LLM é I/O. Misturar quebra o modelo.

---

## 4. Consequências

### Para o time

- Toda nova Tool concreta vai em `fmh_agents/tools/`.
- Toda nova role vai em `fmh_agents/roles/`.
- O framework (`fmh_backend`) não ganha nada de LLM — é
  decisão de aplicação.
- Tests de Role usam `FakeLLMTransport` (injetado via
  `transport=` na Tool).

### Para a arquitetura

- O framework oferece o **contrato** de Tool (ADR-004) e o
  helper de invocação (ToolInvoker). Não opina sobre o que
  é uma Tool concreta.
- Roles são puramente **conceito de aplicação**; não há
  `Role` Protocol no framework.

---

## 5. Veja também

- [ADR-007: LLM via LiteLLM](./ADR-007-LiteLLM-Adapter.md) —
  decisão do adapter concreto
- [ADR-004: Memory, Tools e Knowledge](../fmh_backend/ADRs/ADR-004-Memory-Tools-Knowledge.md) —
  Tool Protocol
- [ADR-005: Checkpoints e idempotency_key](../fmh_backend/ADRs/ADR-005-Checkpoints-Idempotency.md) —
  contrato de idempotency_key
- [fmh_agents/tools/llm.py](../../fmh_agents/src/fmh_agents/tools/llm.py)
- [fmh_agents/roles/](../../fmh_agents/src/fmh_agents/roles/)
