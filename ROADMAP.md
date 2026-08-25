# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Kinetgraph roadmap — próximo release.

Este arquivo lista o que entra em **v0.14.0** (próxima
minor planejada após v0.13.0). Decisões de release
mais distantes estão fora de escopo até v0.14 fechar.

Convenções
----------

- **In scope**: ADRs que já decidem o que entra;
  mudanças que o release inclui.
- **Out of scope**: dívidas em `DEBT.md` que
  acompanha o release mas não é headline feature.
- **Status do ADR**: `Proposed` / `Accepted` /
  `Implemented`. Apenas ADRs com status `Accepted` ou
  `Implemented` entram no scope; `Proposed` fica em
  "Planejado, aguardando aceitação".

Princípio de priorização
------------------------

**Valor agregado = fechar brechas/bugs críticos**
(latent bug, security gap, regression). Cleanup
estrutural segue a frente — não vale atrasar um fix
crítico para esperar um cleanup.

Direção: prospecção. O registro histórico está em
`CHANGELOG.md`; tech debt operacional em `DEBT.md`.

---

## Decisão de escopo (2026-08-26)

A vertical `fmh_office` (ADR-015) **não é mais
tratada como vertical separada**. Os conceitos que
ela introduziu (multi-role handoff, deterministic
step advancement, team composition) vivem como
**building blocks** dentro de `agents/memory/` e
`agents/memory/role_systems/`. Os componentes
seguem disponíveis; o ` fmh_office.v2` scaffold e o
flag `--vertical=fmh-office` foram **removidos do
roadmap**.

Esta decisão reduz o roadmap a **v0.14 (fix-first) +
um item de deprecation de v0.15** (a janela do
`AGENTS.md` §7). Minors adicionais (v0.16, v1.0)
não estão planejadas no momento.

---

## v0.14.0 — **close the gaps**

**Foco**: fechar todas as brechas críticas e regressões
que estão em produção hoje. Operators em produção
recebem proteção imediata.

### In scope (brecha — fix-first)

| # | Item | ADR | Tipo | Status | Owner |
|---|---|---|---|---|---|
| 1 | `RuleBasedChatSystem._persona_for_view` corrigido — personas globs casam desde v0.9 | [ADR-061 §11.1](./ADRs/ADR-061-litellm-integration-review.md) | bug | Merged | — |
| 2 | LiteLLM fallback chain no `LiteLLMToolWorker.invoke` (regressão do ADR-043) | [ADR-061 §6.2](./ADRs/ADR-061-litellm-integration-review.md) | regression | Merged | — |

> **Implementação efetiva:** `with_timeout_and_retry` + `BackoffPolicy(retry_on=(LLMRateLimitError, asyncio.TimeoutError))` (commit `84cfd45`). **Retry do mesmo model com backoff exponencial** (não fallback entre models). O `LLMConfig.fallback_models` continua sendo carregado mas não consumido; fica como **Tracking** até alguém implementar (o `with_fallback_chain` do toolkit não diferencia `LLMAuthError`/`LLMRateLimitError`, e estender o toolkit expandia escopo). A regressão do ADR-043 (rate-limit virava `Err` imediato) está fechada; o cenário "primary cai 429 → retry com backoff → sucesso" é coberto pelos 6 testes em `TestInvokeRetryPolicy`.
| 3 | `chat_llm` registrado em `default_acl()` | [ADR-061 §5](./ADRs/ADR-061-litellm-integration-review.md) | security | Not started | — |
| 4 | `ChatRoleSystem` gate on `RoleComponent.allowed_tools` para `chat_llm` | [ADR-061 §5](./ADRs/ADR-061-litellm-integration-review.md) | security | Not started | — |
| 5 | `SolutionLookupSystem` synthetic emission gated on `RoleComponent.allowed_tools` | [ADR-061 §11.4b](./ADRs/ADR-061-litellm-integration-review.md) | security | Not started | — |
| 6 | `correlation_id = uuid4()` → derivar de `event_id` (audit trail fix) | [ADR-065 §2.3](./ADRs/ADR-065-http-intake-event-driven-review.md) | audit bug | Not started | — |
| 7 | SSE subscribe (`GET /agents/{agent_id}/events`) substitui long-poll `GET /status` | [ADR-065 §3.1](./ADRs/ADR-065-http-intake-event-driven-review.md) | UX fix | Not started | — |
| 8 | Gateway removido do 404 em tool desconhecida (delegado ao dispatcher) | [ADR-065 §3.2](./ADRs/ADR-065-http-intake-event-driven-review.md) | architecture | Not started | — |

### In scope (preparação — habilita próximos minors)

| # | Item | ADR | Tipo | Status | Owner |
|---|---|---|---|---|---|
| 9 | `PrincipalLevel` enum introduzido **ao lado** do `Role` enum | [ADR-060 §2.0](./ADRs/ADR-060-fmh-office-v2-pillars.md) | foundation | Not started | — |
| 10 | `agents.role_systems/` re-organisation (split `_prompts.py`; one class per file) | [ADR-060 §6.5.3](./ADRs/ADR-060-fmh-office-v2-pillars.md) | cleanup | Not started | — |
| 11 | `SolutionPipeline` consolidation (4 sistemas → 1) | [ADR-060 §6.5.2](./ADRs/ADR-060-fmh-office-v2-pillars.md) | cleanup | Not started | — |
| 12 | `RoleComponent.SwitcherSystem` (gate 3) + `handoff_targets` campo | [ADR-060 §3.1](./ADRs/ADR-060-fmh-office-v2-pillars.md) | feature | Not started | — |

### In scope (cleanup leve — sem regressão)

| # | Item | ADR | Tipo | Status | Owner |
|---|---|---|---|---|---|
| 13 | Remove `_RateLimitLike` / `_AuthLike` shims | [ADR-061 §4.1](./ADRs/ADR-061-litellm-integration-review.md) | cleanup | Not started | — |
| 14 | Per-call `drop_params=` (não mutar `litellm.drop_params` global) | [ADR-061 §4.3](./ADRs/ADR-061-litellm-integration-review.md) | fix | Not started | — |

### Out of scope (tracking)

- **`fmh_office` como vertical separada** foi descartada em
  2026-08-26 — os conceitos vivem como building blocks
  em `agents/memory/`. Decisão registrada acima.
- **Demais minors (v0.15+)** não estão planejadas no
  momento. A deprecation do `Role` enum (item abaixo)
  é o **único** trabalho entre ciclos planejado; o
  restante entra quando aparecer demanda concreta.

### Notas de release

- Migration guide em `docs/migration_0.13_to_0.14.md`.
- A mudança em `agents/` requer `knt upgrade` (ADR-053) para projetos v0.13.
- Mensagem no changelog: **"v0.14 ships as a security-and-correctness release; eight items are bug-fix or regression-fix."**

---

## Próximo item entre ciclos (sem minor planejada)

A janela deprecation de uma minor (`AGENTS.md` §7)
exige que o item seja deprecated **antes** do release
que o remove. Como v0.14 introduz `PrincipalLevel`
ao lado de `Role`, a **deprecation** do `Role` enum
precisa acontecer **antes** de qualquer release que o
remova. Esse item fica registrado aqui para garantir
a janela:

| # | Item | ADR | Status | Owner |
|---|---|---|---|---|
| 15 | `security.principal.Role` enum emite `DeprecationWarning` (apontando para `PrincipalLevel`) | [ADR-060 §2.0](./ADRs/ADR-060-fmh-office-v2-pillars.md) | Not started | — |

**Quando executar:** em qualquer release que toque
o módulo `security.principal` (mesmo que não seja uma
minor planejada); ou antes do release que remove o
enum. A regra do `AGENTS.md` §7 é de uma minor cycle
de deprecation — não pode ser pulada.

---

## O que ficou fora (rationale)

| Item antes planejado | Decisão | Razão |
|---|---|---|
| `fmh_office.v2` scaffold | Removido | Não há vertical separada; conceitos viram building blocks |
| `fmh_office.v1` removal | Removido | Vertical não existe mais; nada para remover |
| WebSocket subscribe (`/events/ws`) | Tracking | Pode entrar em minor futura se aparecer demanda |
| Three-gate enforcement end-to-end | Tracking | SwitcherSystem (item 12) é building block; enforcement full é follow-up |
| Approval flow stub | Tracking | Sem vertical, sem approval flow |
| `SolutionLookupSystem` deprecation | Tracking | Pode entrar com SolutionPipeline adoção |
| `LiteLLMTool` / `ToolInvoker` deprecation | Tracking | Itens framework-level; entram quando alguém pegar |
| Split `llm.py` | Tracking | Cosmetic; entra quando alguém pegar |
| `arg_validation` re-export removal | Tracking | Idem |
| `Capability` removal | Tracking | Idem |
| `SolutionProjector` removal | Tracking | Idem |
| `LLMResponse` typed envelope | Tracking | Idem |
| Cost budget gate | Tracking | Idem |
| `fmh_office.v1` removal | Tracking | Vertical não existe |
| `PrincipalLevel` canonical | **Incluído como item 9** | Foundation para tudo; entra em v0.14 |
| `WorldCheckpoint` benchmark | Tracking | Pode entrar em patch v0.14.x |
| Cross-region restore, encryption-at-rest, etc. (ADR-057/058 §open) | Tracking | Aguarda decisão de infra/platform team |

Cada item marcado como **Tracking** pode ser
promovido a `In scope` em uma minor futura. O critério
de promoção é **valor**: a mesma lógica do v0.14
(fechar brechas) se aplica.

---

## Dependencies (DAG)

v0.14 é **self-contained**: nenhum item bloqueia
outro dentro da minor. Os 8 fixes de brecha são
independentes entre si (todos rodam no mesmo
critical path: `LiteLLMToolWorker.invoke`, gateway,
`SolutionLookupSystem.__call__`, `RuleBasedChatSystem.__call__`).

A item 9 (`PrincipalLevel` introduzido) é pré-requisito
para o item 15 (`Role` enum deprecation); este
ROADMAP mantém ambos porque a janela `AGENTS.md` §7
exige coexistência de uma minor.

```
v0.14 items 1-14 (independentes)
       │
       ├── item 9 (PrincipalLevel introduced)
       │            │
       │            └── item 15 (Role enum DeprecationWarning)  [next minor]
       │
       └── item 12 (SwitcherSystem) — building block; pode ser usado em minor futura sem vertical
```

---

## What this roadmap is **not**

- **Não é compromisso de release.** Datas e conteúdo podem mudar conforme PRs são mergeados e ADRs aceitos. O `CHANGELOG.md` é o registro autoritativo do que entrou; este arquivo é a **direção pretendida**.
- **Não substitui `DEBT.md`.** Dívida operacional (refactor, lint, doc drift) vive lá; este arquivo lista **features e fixes**.
- **Não detalha cada ADR.** Cada entrada aponta para o ADR canônico que decide. Releitura do ADR é o caminho autoritativo.
- **Não planeja minors distantes.** Após v0.14, o roadmap reabre quando aparecer demanda concreta. **Tracking** items são o backlog; promovê-los para `In scope` requer uma decisão de minor (com rationale de valor).

## Open questions (rolling)

1. **v0.14.1 patch com `WorldCheckpoint` benchmark?** Pode entrar como patch se o item 14 mostrar que o default precisa flip. **Tracking:** owner = core team.
2. **Promoção de items Tracking para minor futura.** Cada item em **O que ficou fora** pode ser promovido quando aparecer demanda. **Tracking:** cada item individualmente.
3. **Naming do `SolutionPipeline` API pública** (`agents.memory.solutions.pipeline.SolutionPipeline` vs namespace mais curto). **Tracking:** quando o item 11 for implementado.
4. **WebSocket subscribe** (item que estava em v0.15 original). Útil se dashboards / monitoring aparece. **Tracking:** quando aparecer demanda concreta.
5. **Three-gate enforcement end-to-end** (idem). Útil para auditoria; pode entrar como minor dedicada. **Tracking:** quando aparecer demanda de auditoria formal.
6. **`project_memory` composition in production dispatcher** (bug latente). **Status:** ✅ **resolved 2026-08-26** (commits `9a50bec` + `f33d4f5`). O `_fold_with_filter` em `src/kntgraph/runner/_folding.py` agora chama `_project_memory_into_world(world, new_events)` entre o default fold e o overlay tool, com storage sync via `clone_with_entity`. Os 5 role systems LLM-backed (`ChatRoleSystem`, `PlannerRoleSystem`, `SummarizerRoleSystem`, `PersonalizedRoleSystem`, e persona-via-`RoleComponent`) **voltam a funcionar em produção**. **Testes integrados** adicionados em `tests/unit/runner/test_runner_split_modules.py::TestFoldWithFilter` (3 novos testes: `test_memory_projection_composes_into_world`, `test_memory_projection_preserves_storage_for_replay`, `test_memory_projection_passes_through_when_no_memory_events`). **Reintrodução dos 15 testes de role systems**: ainda pendente (precisam ser reescritos agora que o bug está corrigido — o `_persona_for_view` corrigido em 2026-08-26 também entra nesses testes). **Tracking:** items 1 e 11-15 do roadmap v0.14 cobrem a reintrodução.

---

## How to update this file

### Ciclo de vida de um item

```
Not started  ──▶  In progress  ──▶  In review  ──▶  Merged  ──▶  Released
   │                │                │              │             │
   │                │                │              │             └── release cortado; item
   │                │                │              │                 movido pro CHANGELOG.md
   │                │                │              └── PR merged; feature branch
   │                │                │                  em main mas não released
   │                │                └── PR aberto; waiting on review
   │                └── PR aberto OR local work in progress
   └── item identificado; sem trabalho ainda
```

**Quem atualiza:** quem pegou o item (Owner). Se o
Owner muda, transfere o item junto (atualiza a coluna).

**Quando atualizar:** em cada commit que afete o
status (PR aberto, PR mergeado, release cortado). Não
atualizar em cada commit trivial do item — manter o
git log fino.

### Regras

1. Nova feature/fix entra como item em uma minor (ou em "Próximo item entre ciclos") com referência ao ADR.
2. Mudança de ADR status (`Proposed` →`Accepted` →`Implemented`) reflete aqui; não é o mesmo que o Status do item (operacional).
3. Status do item avança: `Not started` → `In progress` → `In review` → `Merged` → `Released`.
4. Release cortado: o item é removido deste arquivo e entra no `CHANGELOG.md`.
5. ADR afetado revertera ou accepted: atualize o status do item e o ADR aqui.
6. Mudança de estratégia de minor (ex: v0.14 → split): atualize este arquivo + ADR afetado.
7. **Re-priorização por valor** (este arquivo é vivo): se um item de brecha crítica aparecer fora das minors atuais, **adiantar é permitido**.
8. **Mudança de escopo (2026-08-26):** se a decisão "fmh_office não é vertical separada" reverter, reabra ADR-015 como vertical. Não é decisão irreversível — é pragmática.

## Como retomar trabalho após parar

Este arquivo é a **fonte de verdade do que está em
andamento**. Para retomar:

1. **Filtre por `Status`:** todos os itens `In progress` ou `In review` estão ativos. `Not started` é backlog.
2. **Filtre por `Owner`:** o que está com você? Os outros Owners são responsabilidade deles.
3. **Filtre por minor:** o que vai entrar na próxima release? O que está marcado como **Tracking** é backlog; promover para `In scope` requer uma decisão de minor.

**Critério "parar e retomar":** você pode parar a
qualquer momento desde que (a) o item esteja em
`In progress` com Owner atribuído, (b) o ADR que
decide esteja `Accepted` ou `Implemented`. Sem Owner
no item, o trabalho não pode ser retomado por outra
pessoa — atribua antes de parar.

**Detecção de item parado:** se um item está em
`In progress` por mais de uma minor sem avançar de
status, ele está **estagnado**. Reveja: o Owner
ainda está na equipe? O ADR mudou? O item foi
superado por outro? Decida entre (a) retomar, (b)
re-atribuir, (c) cancelar (remove do ROADMAP e
mover para DEBT.md com rationale).