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

## v0.14.0 — **close the gaps** (RELEASED 2026-08-26)

**Foco**: fechar todas as brechas críticas e regressões
que estavam em produção. v0.14 ships as a
security-and-correctness release.

### v0.14 items (final state)

| # | Item | ADR | Tipo | Status |
|---|---|---|---|---|
| 1 | `RuleBasedChatSystem._persona_for_view` corrigido — personas globs casam desde v0.9 | [ADR-061 §11.1](./ADRs/ADR-061-litellm-integration-review.md) | bug | **Merged (commit `9a50bec`)** |
| 2 | LiteLLM retry com backoff exponencial (não fallback chain) | [ADR-061 §6.2](./ADRs/ADR-061-litellm-integration-review.md) | regression | **Merged (commit `84cfd45`)** |
| 3 | WorkerManager ACL hook (gate 1) + `RoleComponent` (gate 2) | [ADR-061 §5](./ADRs/ADR-061-litellm-integration-review.md), [ADR-060 §3.0](./ADRs/ADR-060-fmh-office-v2-pillars.md) | security | **Merged (commit `8936b0c`)** |
| 4 | `_BaseRoleSystem._build_request_event` gate on `RoleComponent.allowed_tools` | [ADR-061 §5](./ADRs/ADR-061-litellm-integration-review.md) | security | **Merged (commit `8936b0c`)** |
| 5 | `SolutionLookupSystem` synthetic emission gated on `RoleComponent.allowed_tools` | [ADR-061 §11.4b](./ADRs/ADR-061-litellm-integration-review.md) | security | **Tracking → DEBT §2.29 (item #5)** |
| 6 | `correlation_id = uuid4()` → derivar de `event_id` (audit trail fix) | [ADR-065 §2.3](./ADRs/ADR-065-http-intake-event-driven-review.md) | audit bug | **Merged (commit `7810b2b`)** |
| 7 | SSE subscribe (`GET /agents/{agent_id}/events`) substitui long-poll `GET /status` | [ADR-065 §3.1](./ADRs/ADR-065-http-intake-event-driven-review.md) | UX fix | **Merged (commit `3e6e2a9`)** |
| 8 | Gateway removido do 404 em tool desconhecida (delegado ao dispatcher) | [ADR-065 §3.2](./ADRs/ADR-065-http-intake-event-driven-review.md) | architecture | **Merged (commit `0fd1d69`)** |
| 9 | `PrincipalLevel` enum introduzido ao lado do `Role`; migração aditiva | [ADR-060 §2.0](./ADRs/ADR-060-fmh-office-v2-pillars.md) | foundation | **Merged (commit `4391ffe`)** |
| 10 | `agents.role_systems/` re-organisation (split; one class per file) | [ADR-060 §6.5.3](./ADRs/ADR-060-fmh-office-v2-pillars.md) | cleanup | **Tracking → DEBT §2.31** |
| 11 | `SolutionPipeline` consolidation (5 sistemas → 1) | [ADR-060 §6.5.2](./ADRs/ADR-060-fmh-office-v2-pillars.md) | cleanup | **Tracking → DEBT §2.32** |
| 12 | `RoleComponent.SwitcherSystem` (gate 3) + `handoff_targets` | [ADR-060 §3.1](./ADRs/ADR-060-fmh-office-v2-pillars.md) | feature | **Tracking → DEBT §2.29 (item #12)** |
| 13 | Remove `_RateLimitLike` / `_AuthLike` shims | [ADR-061 §4.1](./ADRs/ADR-061-litellm-integration-review.md) | cleanup | **Merged (commit entre `84cfd45` e `1cbb396`)** |
| 14 | Per-call `drop_params=` (não mutar `litellm.drop_params` global) | [ADR-061 §4.3](./ADRs/ADR-061-litellm-integration-review.md) | fix | **Merged (commit `1cbb396`)** |
| 15 | `Role` enum emite `DeprecationWarning` (apontando para `PrincipalLevel`) | [ADR-060 §2.0](./ADRs/ADR-060-fmh-office-v2-pillars.md) | foundation | **Closed 2026-08-26: `Role` removido direto (commit `7392d1c`); sem warning cycle** |

### Bonus items (delivered alongside v0.14)

| Item | ADR | Status |
|---|---|---|
| `RoleComponent` em `core.components.role` (mirror Jinja template shape) | [ADR-039 + ADR-060 §3.0](./ADRs/ADR-060-fmh-office-v2-pillars.md) | **Merged (commit `8936b0c`)** |
| Three-Gate Model: gate 1 (WorkerManager ACL) + gate 2 (RoleComponent) + gate 3 (worker) | [ADR-060 §3.0](./ADRs/ADR-060-fmh-office-v2-pillars.md) | **Merged (commit `8936b0c`)** |
| `WorkerManager.register(tool_cls, acl=...)` com sentinel `_UNSET` + `acl_for(name)` | [ADR-066 §4.1](./ADRs/ADR-066-Single-Tool-Path.md) | **Merged (commit `f1ccc19`)** |
| `Event.producer_principal_id` (stamp from API layer, recover in Worker) | [ADR-066 §4.1](./ADRs/ADR-066-Single-Tool-Path.md) | **Merged (commit `f1ccc19`)** |
| ADR-066 v0.17: DeprecationWarning on `WorkerManager.register` (no acl=) and `ToolRegistry.__init__` | [ADR-066 §4.4](./ADRs/ADR-066-Single-Tool-Path.md) | **Merged (commit `c22a5e4`)** |
| ADR-066 v0.17: CLI scaffold flip (`cli/templates/dispatcher.py.jinja`) | [ADR-066 §3.1](./ADRs/ADR-066-Single-Tool-Path.md) | **Merged (commit `c22a5e4`)** |
| `WorldProjection` protocol + `MemoryHydrationProjection` in `runner.reactive_extensions` | [ADR-042 §6.1](./ADRs/ADR-042-hydration-pipeline.md) | **Merged (commit `4e383de`)** |
| Built-in memory hydration in `ReactiveDispatcher._fold_with_filter` (no opt-in) | [ADR-042 §6.1](./ADRs/ADR-042-hydration-pipeline.md) | **Merged (commit `4e383de`)** |
| `SessionComponent.session_id` honours `data.session_id` (with fallback) | [DEBT §2.33](./DEBT.md) | **Merged (commit `543e145`)** |
| `Role` enum removed; `PrincipalLevel` is the single RBAC enum | [ADR-060 §2.0](./ADRs/ADR-060-fmh-office-v2-pillars.md) | **Merged (commit `7392d1c`)** |

### Still open (moved to DEBT)

- **DEBT §2.28 v0.18**: `git rm` of `ToolRegistry`; update API factory to take `worker_manager=`; update examples (`examples/knt-cli/weather_platform`); migrate `knowledge/extraction/*` callers; remove the `DeprecationWarning` on `WorkerManager.register` no-`acl=` form (becomes a hard error).
- **DEBT §2.29 item #5**: `SolutionLookupSystem` synthetic emission gated on `RoleComponent.allowed_tools` (still pending; gate 2 was delivered in v0.14 but the `SolutionLookupSystem` consumer is not yet wired).
- **DEBT §2.29 item #12**: `RoleComponent.SwitcherSystem` (gate 3) + `handoff_targets` (deferred; multi-agent handoff demand has not surfaced).
- **DEBT §2.31**: `agents/role_systems/` re-organisation (one class per file; `_prompts.py` / `_schemas.py` split).
- **DEBT §2.32**: `SolutionPipeline` consolidation (~1338 LOC across 5 classes).

### Notas de release (delivered 2026-08-26)

- Mensagem no changelog: **"v0.14 ships as a security-and-correctness release. Breaking: `Role` enum removed; migrate to `PrincipalLevel`. Deprecations: `ToolRegistry`, `WorkerManager.register` no-`acl=` form."**
- Migration guide integrado no CHANGELOG (`## [0.14.0]`).
- A mudança em `agents/` requer `knt upgrade` (ADR-053) para projetos v0.13.

---

## Próxima minor (v0.15) — proposed

A próxima minor proposta após v0.14 é **v0.15 — finish
the Three-Gate Model and remove legacy Tool path**:

- **DEBT §2.28 v0.18**: `git rm` of `ToolRegistry`;
  update API factory to take `worker_manager=`;
  update examples; migrate `knowledge/extraction/*`
  callers; remove the `DeprecationWarning` on
  `WorkerManager.register` no-`acl=` form (becomes
  a hard error). This is the final step of ADR-066.
- **DEBT §2.29 item #5**: `SolutionLookupSystem`
  synthetic emission gated on
  `RoleComponent.allowed_tools`. Wire the gate 2
  consumer in `agents/role_systems/`.
- **DEBT §2.31**: `agents/role_systems/` re-organisation
  (one class per file; split `_prompts.py` /
  `_schemas.py`).
- **DEBT §2.32**: `SolutionPipeline` consolidation
  (~1338 LOC).

A janela deprecation foi respeitada: `ToolRegistry`
e o no-`acl=` form emitiram `DeprecationWarning`
em v0.14 (um minor cycle), e a remoção hard é
v0.15+.

---

## O que ficou fora (rationale)

| Item antes planejado | Decisão | Razão |
|---|---|---|
| `fmh_office.v2` scaffold | Removido | Não há vertical separada; conceitos viram building blocks |
| `fmh_office.v1` removal | Removido | Vertical não existe mais; nada para remover |
| WebSocket subscribe (`/events/ws`) | Tracking | Pode entrar em minor futura se aparecer demanda |
| Three-gate enforcement end-to-end | **Tracking → DEBT §2.29 (items #5, #12)** | SwitcherSystem (item 12) é building block; SolutionLookupSystem synthetic emission (item #5) é o resto |
| Approval flow stub | Tracking | Sem vertical, sem approval flow |
| `SolutionLookupSystem` deprecation | Tracking | Pode entrar com SolutionPipeline adoção |
| `LiteLLMTool` / `ToolInvoker` deprecation | **Resolvido v0.9.0** | ADR-043 acelerou a remoção |
| **Single Tool Path (ADR-066)** — `ToolRegistry` / `Tool` Protocol / `ToolDescriptor` removal | **v0.16 + v0.17 + v0.18** | v0.16 (commit `f1ccc19`) entregou ACL hook; v0.17 (commit `c22a5e4`) entregou DeprecationWarning + scaffold flip; v0.18 é o `git rm` final |
| Split `llm.py` | Tracking | Cosmetic; entra quando alguém pegar |
| `arg_validation` re-export removal | Tracking | Idem |
| `Capability` removal | Tracking | Idem |
| `SolutionProjector` removal | Tracking | Idem |
| `LLMResponse` typed envelope | Tracking | Idem |
| Cost budget gate | Tracking | Idem |
| `fmh_office.v1` removal | Tracking | Vertical não existe |
| `PrincipalLevel` canonical | **Merged (commit `4391ffe`)** | Foundation para tudo; entregue em v0.14 |
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