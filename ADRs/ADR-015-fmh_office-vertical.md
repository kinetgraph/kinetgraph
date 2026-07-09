<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-015 — Vertical `fmh_office`: framework de times de papéis para automação de backoffice de PME

| | |
|---|---|
| **Status** | Proposed |
| **Date** | 2026-06-22 |
| **Deciders** | @adriano |
| **Supersedes** | — |
| **Related** | ADR-001 (Arquitetura geral), ADR-006 (Tool × Role), ADR-012 (IntentRouter), ADR-013 (Roteamento semântico), `ARCHITECTURE_PROPOSAL.md` §7 (BPM sketch) |

---

## 1. Contexto

PMEs brasileiras operam backoffice com times pequenos (1-5 pessoas) que
orquestram manualmente fluxos como **pedido → faturamento → cobrança**,
**onboarding de funcionário**, **contas a pagar**, **contas a receber**.
Esses fluxos são:

- **Repetitivos** — mesma sequência 10-100x/dia.
- **Baseados em regras** — "se cliente inadimplente, bloqueia pedido";
  "se NF-e > R$ 10k, exige aprovação do gerente".
- **Conhecidos** — o passo-a-passo é claro; o gargalo é execução
  repetível, não decisão.
- **Multi-ator** — vendedor, estoquista, financeiro, gerente. Cada um
  com permissões e SLAs próprios.

O **FMH framework** já entrega a infraestrutura canônica: event
sourcing, idempotência, tools/roles, memory tiers, knowledge layer
(FalkorDB), HTTP gateway, LLM via LiteLLM. O que **não** entrega, e
que esta ADR preenche, é a camada de **orquestração de processo de
negócio de longa duração com time de papéis pré-configurados**.

Hoje, a única "vertical" de referência (`fmh_app`) resolve um problema
single-intent reativo (lookup CNPJ por cidade/UF). É o exemplo do
**como**, não o **porquê**: o framework precisa de uma vertical que
demonstre processos multi-step, com regras, time, e human-in-the-loop
opcional — casos de uso reais para PMEs.

---

## 2. Problema

Construir uma vertical `fmh_office/` que:

1. Permita ao **desenvolvedor** declarar, em YAML/JSON, **regras de
   negócio** e um **time de papéis** (vendendor, estoquista,
   financeiro, gerente), sem código Python para casos simples.
2. Permita ao **operador da PME** instanciar **execuções de processo**
   (um "pedido novo", "um candidato novo") e acompanhar via
   dashboard/HTTP.
3. Cada papel é **um agente FMH** (event sourcing, idempotente,
   reativo), apoiado por LLM/SLM quando precisa de julgamento
   (classificar email, redigir resposta, extrair campos) e
   deterministic quando é regra pura.
4. O **engine** é event-driven: cada step do processo é um evento,
   transições via `ReactiveSystem` que lê `BusinessProcessState`
   do `AgentView`. Aproveita o que o framework já dá.
5. **Knowledge layer** aprende (FalkorDB) padrões de execução
   passados — qual caminho o pedido tipicamente segue, quais
   exceções recorrentes.

### 2.1 Casos de uso alvo (MVP)

| Caso | Steps | Papéis | Regras |
|---|---|---|---|
| **Pedido → Faturamento → Cobrança** | Receber → Verificar estoque → Gerar invoice → Enviar boleto → Confirmar pgto | Atendente, Estoquista, Financeiro | "se inadimplente, bloqueia"; "se valor > R$ 5k, exige aprovação gerente" |
| Onboarding de funcionário | Receber candidato → Conferir docs → Agendar entrevista → Contratar | RH, Gerente | "se cargo CLT, valida CTPS digital" |
| Contas a pagar | Receber NF-e → Conferir com pedido → Lançar → Agendar pgto | Financeiro, Gerente | "se fornecedor novo, exige 1ª aprovação" |

**MVP escolhe o primeiro caso** (Pedido → Faturamento → Cobrança) por
ser:

- I/O-bound (integra com múltiplos sistemas externos opcionais)
- Tem **regras** (não é só roteamento)
- Tem **multi-ator** (3 papéis distintos)
- Cabe num **MVP pequeno** (4-5 events, 1 processo)
- Cobre o pattern completo para os outros casos virarem templates

### 2.2 Não-objetivos (v1)

- **Não é um ERP**. Não tem contabilidade, fiscal, folha complexa.
- **Não é BPMN**. Não visa compliance com notação OMG. É uma DSL
  interna mínima, declarativa em YAML/JSON.
- **Não substitui o framework** — é uma vertical; depende de
  `fmh_backend` e `fmh_agents`, não ao contrário.
- **Não tem UI própria** v1. O `IntentRouter` HTTP é a interface.
- **Não tem multi-tenant** v1. Single-tenant por deployment.

---

## 3. Requisitos (do desenvolvedor e do operador da PME)

### 3.1 Do desenvolvedor

- Declarar regras em YAML/JSON: `"quando campo X == Y, então step Z"`.
- Declarar papéis: `atendente`, `financeiro`, etc. Cada papel aponta
  para um `Role` pré-configurado no catálogo.
- Instanciar a execução: submeter evento `process.pedido.started`
  com `data` (cliente, itens, valor).
- Acompanhar: `GET /agents/{agent_id}/events/{event_id}/status` (já
  existe via IntentRouter).

### 3.2 Do operador

- Disparar execução via HTTP ou UI externa (Futuro v2).
- Acompanhar via dashboard. v1: status polling + FalkorDB query.
- Cancelar uma execução: emite `process.{id}.cancelled`.

### 3.3 Do sistema

- **Eventual consistency**: o engine não bloqueia — cada step
  acontece quando o evento anterior é processado.
- **Durabilidade**: tudo no `EventLog`; replay reconstrói o estado.
- **Idempotência**: reentregas (HTTP retry, EventLog replay) não
  duplicam steps.
- **Observabilidade**: cada step é um evento; `correlation_id`
  atravessa o processo inteiro.
- **Auditabilidade**: PII level 1 (CPF/CNPJ) hasheado no FalkorDB;
  raw só no `EventLog`.

---

## 4. Decisão de design

### 4.1 Estrutura de pastas

```
fmh_office/
├── pyproject.toml
├── src/fmh_office/
│   ├── __init__.py
│   ├── catalog/
│   │   ├── __init__.py
│   │   ├── roles.py                # AtendenteRole, EstoquistaRole, FinanceiroRole, GerenteRole
│   │   └── registry.py             # RoleRegistry (paralelo ao ToolRegistry)
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── process.py              # ProcessModel (YAML loader), ProcessStep, ProcessRoleBinding
│   │   ├── state.py                # BusinessProcessState, ProcessStatus, ProcessEventType
│   │   ├── systems.py              # ProcessStarterSystem, StepAdvancerSystem, TerminalHandlerSystem
│   │   └── rules.py                # RuleEvaluator (YAML → predicate), GuardComponent
│   ├── loader/
│   │   ├── __init__.py
│   │   └── yaml_loader.py          # Carrega processos+regras de YAML/JSON; compila para ProcessModel
│   ├── mvp/
│   │   ├── __init__.py
│   │   ├── pedido.py               # ProcessModel do MVP (Pedido → Faturamento → Cobrança)
│   │   ├── rules.yml               # Regras do MVP em YAML
│   │   └── integration.py          # AppRunner wiring para o MVP
│   └── tests/
│       ├── unit/
│       │   ├── test_engine.py
│       │   ├── test_rules.py
│       │   ├── test_catalog.py
│       │   └── test_mvp.py
│       └── integration/
│           ├── test_pedido_flow.py
│           └── test_yaml_loader.py
├── examples/
│   └── pedido.yml                  # Exemplo declarativo do processo
├── docs/
│   ├── getting_started.md
│   ├── dsl_reference.md
│   └── adrs/
│       └── ADR-001-fmh_office.md   # mirror do ADR-015 (canônico fica em fmh_backend)
└── README.md
```

### 4.2 Modelo de processo (YAML/JSON)

```yaml
# exemplo: examples/pedido.yml
process:
  id: pedido_faturamento_cobranca
  version: 1
  description: |
    Recebe um pedido, verifica estoque, gera invoice,
    envia boleto, confirma pagamento.
  entry_event: process.pedido.started

steps:
  - id: verify_estoque
    role: estoquista
    trigger: process.pedido.started
    action: tools.estoque.check
    rules:
      - when: data.quantidade_total > data.estoque_disponivel
        then: goto step=cancel_for_estoque
    emits:
      success: estoque.verified
      failure: estoque.failed

  - id: generate_invoice
    role: financeiro
    trigger: estoque.verified
    action: tools.financeiro.issue_invoice
    rules:
      - when: data.valor_total > 5000
        then: require_approval role=gerente
        else: continue
    emits:
      success: invoice.issued
      approval_granted: invoice.approved
      failure: invoice.failed

  - id: send_boleto
    role: financeiro
    trigger: invoice.approved
    action: tools.financeiro.send_boleto
    emits:
      success: boleto.sent
      failure: boleto.failed

  - id: confirm_payment
    role: financeiro
    trigger: boleto.sent
    action: tools.financeiro.check_payment
    timeout: 7d
    emits:
      success: process.pedido.completed
      timeout: process.pedido.failed

  - id: cancel_for_estoque
    role: atendente
    trigger: estoque.failed
    action: tools.atendente.notify_client
    emits:
      success: process.pedido.cancelled
```

### 4.3 Shape do "time de trabalho"

Conceito: **Team** = um mapeamento `role_name → Role instance` +
um `ProcessModel` (passos + regras).

```python
# Carregado em runtime
team = load_team_from_yaml("examples/pedido.yml")
# Equivale a:
team = Team(
    name="pedido_faturamento_cobranca",
    process=ProcessModel.from_yaml(...),
    roles={
        "atendente":   AtendenteRole(inference=llm, ...),
        "estoquista":  EstoquistaRole(io=estoque_tool, ...),
        "financeiro":  FinanceiroRole(io=financeiro_tool, ...),
        "gerente":     GerenteRole(inference=llm, ...),
    },
    rules=RuleSet.from_yaml(...),
)
```

A `Team` é uma `dataclass` simples (não uma framework primitive).
Ela **constrói** os `ReactiveSystem`s certos e os registra no
`ReactiveDispatcher`. Estado de execução = `BusinessProcessState`
componente no `AgentView`.

### 4.4 Engine event-driven

Cada step do `ProcessModel` mapeia para um trio:
- **trigger** (event_type de entrada)
- **role** (qual Role executa)
- **emits** (event_type de saída, por outcome)

Um único `StepAdvancerSystem` (genérico) lida com **todos** os
steps: lê o `BusinessProcessState`, vê se há um step pendente
matching o trigger, executa via o `role` apropriado, atualiza o
state, emite o evento de saída.

**Pseudocódigo** (essência):

```python
class StepAdvancerSystem:
    def __init__(self, team: Team):
        self.team = team
        self.process = team.process

    async def __call__(self, world, event):
        for step in self.process.steps_matching(event.event_type):
            process_state = world.get_agent(event.agent_id)
                .get_component(BusinessProcessState)
            if step.id in process_state.completed_steps:
                continue  # idempotente: replay-skip
            role = self.team.roles[step.role]
            result = await role.execute(event.data, step)
            if result.is_err():
                return [emit_failure(event, result.err_value())]
            if step.rules:
                decision = self.team.rules.evaluate(step.rules, result)
                if decision.goto:
                    return [emit_redirect(event, decision)]
                if decision.require_approval:
                    return [emit_approval_request(event)]
            process_state.completed_steps.add(step.id)
            return [emit_step_done(event, step, result)]
        return []
```

**Vantagens**:
- Engine é **1 system**; steps são **dados** (YAML), não código.
- Replay = `EventLog.read(agent_id)` + fold. Idempotente.
- Adicionar um step novo = edit YAML. Sem Python.

### 4.5 Regras como dados, não código

`RuleSet.evaluate(rules, data)` é o coração. v1 implementa:

```yaml
- when: data.quantidade_total > data.estoque_disponivel
  then: goto step=cancel_for_estoque
- when: data.valor_total > 5000
  then: require_approval role=gerente
- when: data.cliente.status == "inadimplente"
  then: block
```

DSL mínima: comparadores (`==`, `!=`, `>`, `<`, `>=`, `<=`),
operadores lógicos (`and`, `or`, `not`), caminhos dot-path
(`data.field.subfield`), constantes.

**Implementação v1**: um mini-parser JSONLogic-like (~200 linhas).
**Não usa eval()**; a DSL é restrita (whitelist de operadores).
**Não usa LLM** para decidir regras — regras são determinísticas;
LLM é só para extrair/classificar dados de input.

### 4.6 Catálogo de Roles pré-configuradas

`fmh_office.catalog.roles` exporta um conjunto base de Roles
"atendente", "estoquista", "financeiro", "gerente", "rh". Cada uma:

- Tem prompt + schema embutidos (português).
- Implementa `Role.execute(data, step) -> Result[dict, ToolError]`.
- Pode chamar uma `Tool` (HTTP/DB) e/ou um `Capability` (LLM).
- Tem um `LLMDispatcher`-style fallback (LLM se o Tool falhar
  com erro recuperável).

O desenvolvedor instancia:
```python
team = Team(
    process=ProcessModel.from_yaml("pedido.yml"),
    roles={
        "atendente":   AtendenteRole(inference=llm_tool),
        "estoquista":  EstoquistaRole(io=estoque_tool, inference=llm_tool),
        "financeiro":  FinanceiroRole(io=financeiro_tool, inference=llm_tool),
        "gerente":     GerenteRole(inference=llm_tool),
    },
)
```

Customização: o dev pode **subclassar** qualquer Role e
**re-registrar** no `RoleRegistry`. Mantém o pattern do `ToolRegistry`.

### 4.7 Knowledge layer (FalkorDB)

Após cada execução, um `ProcessLearnerSystem` (cyclic) analisa
o `EventLog` e atualiza o grafo:

- `(:Process {id, name, version})` — tipo do processo
- `(:ProcessExecution {id, started_at, completed_at, outcome})` — instância
- `(:Step {id, role})` — passo
- `(:Transition {from_step, to_step, condition, count})` — aresta estatística

Permite queries tipo "qual step tem mais exceções?", "qual a
taxa de cancelamento por cliente?", "qual approval role
mais aciona?".

PII (CPF, CNPJ, nome) é **hasheado** antes de ir pro FalkorDB
(reusa `fmh_app/knowledge/pii.py::hash_cnpj`).

---

## 5. Alternativas consideradas

### 5.1 Process engine tradicional (BPMN/Camunda/Temporal)

- **Pro**: Maduros, SLA, escalação, compensation built-in.
- **Contra**: Adicionam dependência externa pesada; o framework
  já tem event sourcing — duplicar com Temporal seria retrabalhar.
  PMEs não querem operar um cluster Temporal.
- **Veredito**: rejeitado. FMH já tem o suficiente; um YAML+events
  é mais leve e mantém o framework canônico.

### 5.2 Plan-driven executor (executa `PlannerRole.plan`)

- **Pro**: Reusa o `Plan` model que já existe em `fmh_agents`.
- **Contra**: Plan é LLM-emitted, não declarativo. Mistura
  planejamento com execução. YAGNI — Plan é bom para
  classificar intenção, não para declarar processo.
- **Veredito**: rejeitado. Processos de negócio são declarativos
  por natureza; LLM-emitted plan é certo para tarefas, não BPM.

### 5.3 Tudo LLM (CrewAI / AutoGen / LangGraph)

- **Pro**: Marketing de "agentes autônomos".
- **Contra**: Regras de negócio determinísticas viram prompt —
  frágil, caro, não-auditável. CrewAI/AutoGen são frameworks
  concorrentes, não extensões.
- **Veredito**: rejeitado. LLM onde precisa julgar; regras
  determinísticas onde precisa garantir.

### 5.4 DSL custom estilo BPMN (XML/YAML BPMN-like)

- **Pro**: Padronização.
- **Contra**: BPMN é overkill para PMEs. Notação não traz valor
  de runtime; só traz custo de aprendizado.
- **Veredito**: rejeitado. DSL mínima YAML/JSON é suficiente.

---

## 6. Consequências

### 6.1 Positivas

- **Reuso massivo** de `fmh_backend` (EventLog, Tool/Role,
  ReactiveSystem, FalkorDB projection, HTTP gateway, DLQ, resilience,
  memory tiers).
- **Reuso de `fmh_agents`** (LiteLLMTool, LLMDispatcher pattern,
  semantic routing).
- **PME não-técnica** pode customizar processos editando YAML.
- **Dev tem framework-grade infra** sem reinventar process engine.
- **Audit + replay** são grátis (event sourcing).
- **LLM onde precisa**; regras onde precisa garantir.

### 6.2 Negativas

- **DSL é vertical-locked** (YAML/JSON do fmh_office). Dev que
  sair da PME não pode levar. Aceitável: é o ponto da vertical.
- **Engine é opinativo**: 1 step = 1 role. Casos com multi-role
  concurrent no mesmo step viram "step composto" (futuro).
- **Long-running com SLA** (v1) usa `timeout` no YAML; SLA real
  (escalation) é v2.
- **Sem UI** v1. Operador usa HTTP/dashboard externo.

### 6.3 Riscos

- **Risco 1 — DSL cresce descontrolada**. Mitigação: começar
  com 4-5 operadores (`==`, `>`, `<`, `in`, `not`); avaliar
  depois de MVP.
- **Risco 2 — Regras viram Turing-complete**. Mitigação: sem
  `eval`, sem loops, sem recursão. Whitelist.
- **Risco 3 — LLM em loop infinito**. Mitigação: `max_steps`
  no `BusinessProcessState`; vira `process.failed` se exceder.
- **Risco 4 — ProcessState fica grande**. Mitigação: state é
  component, não event payload; EventLog guarda só deltas.

---

## 7. Roadmap (após aceite)

| PR | Entrega | Estimativa |
|---|---|---|
| 0 | Esqueleto `fmh_office/` + `pyproject.toml` + CI | 0.5 dia |
| 1 | `RuleEvaluator` + tests (DSL mínima) | 1 dia |
| 2 | `ProcessModel` + YAML loader + tests | 1 dia |
| 3 | `BusinessProcessState` + `ProcessEventType` + Component | 0.5 dia |
| 4 | `StepAdvancerSystem` + tests com EventLog fake | 1 dia |
| 5 | `AtendenteRole`, `EstoquistaRole`, `FinanceiroRole`, `GerenteRole` (catalog mínimo) | 2 dias |
| 6 | MVP `pedido.yml` + integration + app runner | 2 dias |
| 7 | `ProcessLearnerSystem` + FalkorDB projection | 1 dia |
| 8 | HTTP gateway via IntentRouter + status polling | 0.5 dia |
| 9 | Docs (`getting_started`, `dsl_reference`) | 1 dia |
| 10 | Live test + PROGRESS.md + adrs mirror | 0.5 dia |

**Total**: ~10 dias úteis. **MVP rodando end-to-end em 2 semanas.**

---

## 8. Métricas de sucesso

- **Dev pode declarar um processo novo em YAML sem tocar Python**.
  Test: criar `examples/onboarding.yml` + carregar → rodar
  via `AppRunner`. Cobre o requisito 3.1.
- **Replay de uma execução completa reproduz o estado**. Test:
  append 5 events ao EventLog, fold, assert `BusinessProcessState`
  terminal correto.
- **RuleEvaluator** rejeita DSL maliciosa (eval, import, recursão).
- **Live**: executar MVP com LLM real + FalkorDB e ver
  `process.pedido.completed` em <2min.

---

## 9. Open questions (a resolver durante implementação)

1. **Aprovação humana**: como modelar "esperar gerente aprovar
   por horas"? v1: `process.{id}.awaiting_approval` event
   externo. v2: SLA + escalation.
2. **Sub-processos**: v1 não suporta; v2 se faz sentido (provavelmente sim).
3. **Versionamento de processo**: executar v1 e v2 do mesmo processo
   simultaneamente. v1: `process_version` no event; v2: routing.
4. **Test framework para regras**: como um dev testa regras sem
   rodar o processo inteiro? v1: `RuleSet.evaluate(rules, data)`
   puro, testável isolado.

---

## 10. Decisão

Aprovada. Começar pelo PR 0 (esqueleto) e PR 1+2 (DSL + loader) em
paralelo. PR 3+4 (state + engine) dependem desses. PR 5+6 (roles
+ MVP) dependem do engine. PR 7+ (knowledge + docs) é polish.

A próxima vertical depois desta é candidata a ser **`fmh_clinic`**
(prontuário + agenda) ou **`fmh_logistics`** (rota + entrega) —
mas é especulação; primeiro `fmh_office` precisa mostrar que
o pattern funciona.
