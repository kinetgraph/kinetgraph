<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-009: Hierarquia Opcional de Tools (Tipo A / B / C)

**Status:** Aceito
**Data:** 09 de junho de 2026
**Versão:** 0.3.0
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** ADR-004, ADR-006, ADR-007, ADR-008

---

## 1. Contexto

O framework `Tool` Protocol (ADR-004) é genérico: qualquer
classe que implemente `async def invoke(*, idempotency_key,
**kwargs) -> Result` pode ser registrada no `ToolRegistry` e
consumida pelo `ToolInvoker`.

Na prática, ao modelar sistemas de negócio, **as Tools têm
diferentes níveis de especialização**:

  - Algumas são **transporte puro** — `HTTPRequestTool`,
    `LiteLLMTool`, `SQLExecuteTool`. Stateless, sem
    semântica de domínio.
  - Algumas são **capability de um sistema externo** —
    `HubSpotApiTool`, `SalesforceApiTool`. Encapsulam
    auth, rate limit, error handling, schema daquele
    sistema.
  - Algumas são **regras de negócio** — `ApplyCRMPricingRuleTool`,
    `SetupCRMPipelineTool`, `SyncContactsTool`. Têm
    vocabulário do domínio e usam uma capability (ou um
    transporte) por injeção.

A tentação natural foi impor uma **hierarquia rígida** de
3 camadas: Tipo A → Tipo B → Tipo C, sempre. Mas isso é
**overhead para casos simples**:

  - Um crawler que só faz GET não precisa de uma
    Capability intermediária — uma Tool que fala direto
    com httpx resolve.
  - Uma LLM não precisa de uma Capability — o prompt
    é a única coisa que distingue uma Role de outra, e
    isso pertence à Role.
  - Uma Tool de HubSpot com 1 caso de uso não precisa
    de uma Capability — duplicar a config em 2 sites é
    mais barato que criar uma classe extra.

A proposta é reconhecer que **a hierarquia é um
vocabulário útil, não uma regra arquitetural**.
O desenvolvedor escolhe quantas camadas usar conforme
a complexidade justificar.

---

## 2. Decisão

### 2.0 Relação com o framework

O `fmh_agents` é um **pacote opcional e adicional** ao
`fmh_backend`. A relação é assimétrica:

```
┌──────────────────────────────────────────────┐
│  fmh_backend (obrigatório)                   │
│  - Tool Protocol                              │
│  - ToolRegistry, ToolInvoker                  │
│  - EventLog, ReactiveDispatcher               │
│  - Memory, Resilience, etc.                   │
└──────────────────────────────────────────────┘
                    ▲
                    │ depende de
                    │
┌──────────────────────────────────────────────┐
│  fmh_agents (opcional)                        │
│  - LiteLLMTool (Tipo A — transporte LLM)      │
│  - CachingLLMTransport                        │
│  - Roles (Planner, Summarizer, Chat, ...)     │
│  - LLMConfig, RateLimiter, CostBudget        │
│  - (futuro) Capabilities de CRM/ERP          │
│  - (futuro) Tools de Domínio por contexto     │
└──────────────────────────────────────────────┘
```

**O `fmh_backend` não conhece a hierarquia Tipo A/B/C**.
Ela é **convenção do `fmh_agents`**. Aplicações que não
usam LLM não precisam do `fmh_agents` — podem usar o
`fmh_backend` direto e implementar Tools próprios.

A classificação Tipo A/B/C é um **vocabulário útil**
para discutir organização de código, mas **não é
hierarquia mandatória** — é o que o título deste ADR
diz: opcional.

### 2.1 Três tipos, não três camadas

Definimos **três tipos de Tool** que coexistem sem
hierarquia rígida:

| Tipo | Função | Exemplo |
|---|---|---|
| **Tipo A — Transporte** | Fala direto com o mundo externo. Stateless, sem semântica. | `HTTPRequestTool`, `LiteLLMTool`, `SQLExecuteTool` |
| **Tipo B — Capability** | Encapsula um sistema externo específico. Auth, rate limit, error mapping. | `HubSpotApiTool`, `SalesforceApiTool` |
| **Tipo C — Domínio** | Tem vocabulário e regras de negócio. Usa Tipo A ou B por injeção. | `ApplyCRMPricingRuleTool`, `SetupCRMPipelineTool` |

**Não há hierarquia mandatória**: uma Tool pode ser Tipo
A e usar httpx direto, ou Tipo C que usa Tipo A, ou
Tipo C que usa Tipo B que usa Tipo A. O que importa é
que cada Tool tenha **responsabilidade clara**.

### 2.2 Quando usar cada tipo

**Use apenas Tipo A quando:**
- A Tool tem 1 caso de uso simples.
- A config (auth, rate limit) é trivial.
- Não há reuso entre Tools.

Exemplo: `CrawlerTool` que faz GET e retorna texto.

**Adicione Tipo B quando:**
- 3+ Tools de domínio falam com o mesmo sistema
  externo.
- A config é não-trivial (OAuth, retries, error
  mapping customizado).
- Trocar o sistema externo (HubSpot → Salesforce)
  é um cenário real.

**Adicione Tipo C quando:**
- Há regras de negócio que merecem encapsulamento
  próprio.
- Você quer uma Tool chamada por um sistema reativo
  via EventLog.

Quase todo sistema não-trivial tem Tipo C. **Tipo B é
opcional** e justificado por reuso + complexidade de config.

### 2.3 Tipo B: Tool ou não-Tool?

**Tipo B pode ser Tool ou classe Python comum**:

- **Como Tool (registrada)**: o sistema emite
  `tool.hubspot.api.requested` com `data={"endpoint":
  ..., "method": ...}`. Útil quando a chamada ao
  HubSpot vem de um sistema reativo.

- **Como classe Python (não-registrada)**: outras
  Tools a usam por injeção. **Mais comum**. A
  Capability é um detalhe de implementação, não algo
  que o sistema reativo precisa conhecer.

**Recomendação**: Tipo B como classe Python, injetada
em Tipo C. Mantém o registry enxuto.

### 2.4 LLM é caso especial

A LLM **não precisa de Tipo B** na maioria dos casos. O
que distingue uma "Capability" de LLM de outra é o
**prompt** — e o prompt é responsabilidade da Role, não
de uma Capability intermediária.

```python
# Estrutura típica com LLM
llm = LiteLLMTool(default_model="gpt-4o-mini", transport=cache)

# Tipo C: Role que usa a LLM diretamente
class SalesOrderAnalyzer:
    def __init__(self, llm: LiteLLMTool):
        self._llm = llm
    
    async def analyze(self, deal: dict) -> Result[Analysis, ToolError]:
        prompt = SALES_ANALYZER_PROMPT.format(deal=deal)
        r = await self._llm.invoke(
            system=SALES_SYSTEM, user=prompt,
            response_format=Analysis.model_json_schema(),
        )
```

**Sem Tipo B para LLM**. O `LiteLLMTool` é Tipo A; a
Role/SalesOrderAnalyzer é Tipo C.

**Exceção**: se você quiser **múltiplas Capabilities de
LLM com configs diferentes** (ex: "LLM para análise
sentimental" vs "LLM para sumarização", cada uma com
modelo e rate limit próprios), aí sim, encapsule em
uma Capability:

```python
sentiment_llm = LLMCompleteCapability(
    transport=llm_transport,
    default_model="gpt-4o-mini",  # melhor para sentiment
    rate_limiter=RateLimiter(rpm=60),
)
summarization_llm = LLMCompleteCapability(
    transport=llm_transport,
    default_model="gpt-3.5-turbo",  # mais barato
    rate_limiter=RateLimiter(rpm=20),
)
```

Mas isso é **raro** — comece sem e adicione se a
necessidade aparecer.

### 2.5 Onde mora cada tipo

Recomendação de organização (informal, não obrigatória):

```
fmh_agents/
├── transport/           # Tipo A
│   ├── http.py
│   ├── llm.py
│   └── sql.py
│
├── capabilities/        # Tipo B (opcional, sob demanda)
│   ├── crm/
│   ├── erp/
│   └── bank/
│
├── domains/             # Tipo C
│   ├── sales/
│   ├── production/
│   └── logistics/
│
└── roles/               # Classes Python que viram Tipo C
    ├── planner.py
    ├── summarizer.py
    └── ...
```

Pastas vazias ou ausentes são OK. O framework não exige
a hierarquia — é só um guia de organização.

### 2.6 O framework não conhece essa classificação

O `Tool` Protocol é genérico. O `ToolRegistry` aceita
qualquer Tool. O `ToolInvoker` despacha sem saber o
"tipo" da Tool. **A classificação Tipo A/B/C é uma
convenção do `fmh_agents`, não parte do framework**.

Isso é importante: o framework não precisa mudar
nada. Aplicações adotam a hierarquia conforme
necessário.

---

## 3. Trade-offs

### Prós

- **Flexibilidade**: aplica 1, 2 ou 3 camadas conforme a
  complexidade. Sem overhead para casos simples.
- **Vocabulário claro**: times podem discutir "isso é
  Tipo A ou Tipo C?" sem ambiguidade.
- **Reuso opcional**: extrai Tipo B só quando o reuso
  justifica. Sem cerimônica.
- **Reorganização fácil**: começar com 1 camada,
  adicionar Tipo B depois, sem refactor.
- **Testabilidade por camada**: cada Tipo é mockado
  independentemente.

### Contras

- **Menos guidance**: dev novo pode não saber se deve
  criar Tipo B ou não. Mitigado por este ADR + docs.
- **Inconsistência entre projetos**: cada aplicação
  adota o que precisa. Sem "uma forma certa".
- **Risco de reuso acidental**: 5 Tools de domínio
  usando o mesmo httpx direto (em vez de uma
  Capability) é difícil de manter. Mitigado por code
  review.

### Alternativas consideradas

- **Hierarquia rígida de 3 camadas**: rejeitado —
  overhead para casos simples.
- **Sem classificação**: rejeitado — sem vocabulário,
  times divergem em convenções.
- **Tipo B obrigatório para LLM**: rejeitado — LLM é
  um caso especial em que o prompt é a única
  diferença, então a Role já é Tipo C efetiva.

---

## 4. Consequências

### Para o time

- Avaliar **por Tool concreta** se vale a pena criar
  uma Capability intermediária.
- Não há "default" de hierarquia — o código é guiado
  por clareza e reuso.
- Tests mockam pela borda (camada injetada), não pelo
  tipo.

### Para a arquitetura

- O framework (`fmh_backend`) **não** conhece Tipo A/B/C.
  É convenção do `fmh_agents`.
- A documentação (este ADR + README) é o veículo da
  convenção.

### Para a aplicação

- Decisão: "Vale extrair uma Capability?" é uma
  pergunta por Tool, não por projeto.
- Quando em dúvida: começar com Tipo A, extrair Tipo B
  quando aparecer o 3º Tool de domínio usando o mesmo
  sistema externo.

---

## 5. Veja também

- [fmh_agents README](../../README.md) — pacote
  opcional e adicional
- [ADR-006: Tool × Role separation](ADR-006-Tool-Role-Separation.md)
- [ADR-007: LLM via LiteLLM](ADR-007-LiteLLM-Adapter.md)
- [ADR-008: Caching LLM transport](ADR-008-Caching-Transport.md)
- [docs/tools.md](../../fmh_backend/docs/tools.md) — visão
  geral do Tool Protocol no framework
