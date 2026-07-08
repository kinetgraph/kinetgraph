<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-029: Migrate `LiteFalkorDBClient` → `LiteGraphClient` (Iter 28 FU 2) → `LiteGraphPool` (Iter 28 FU 7)

**Status:** Aceito
**Data:** 30 de junho de 2026
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md), [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md), [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md), [ADR-026](./ADR-026-Close-GLiNER2-Binding-Leak.md), [ADR-027](./ADR-027-Argument-Extraction-Migration.md), [ADR-028](./ADR-028-Delete-Argument-Extraction-Vertical.md), [ADR-033](./ADR-033-GraphPool-Reorg.md), [AGENTS.md §1](../../AGENTS.md), §2

Este ADR fecha o último item dev-only do roadmap aberto pelo [ADR-024 §2.4](./ADR-024-FalkorDBClient-GraphClient-Migration.md#24-o-litefalkordbclient-permanece) e referenciado pelo [ADR-025 §6 (Roadmap residual)](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md#6-roadmap-residual-pós-adr-025). O `LiteFalkorDBClient` é migrado para `LiteGraphClient` (Iter 28 FU 2; renomeado para `LiteGraphPool` em Iter 28 FU 7 / ADR-033) — o canonical dev-only client.

---

## 1. Contexto

O `LiteFalkorDBClient` (em `fmh_backend.infra.falkordblite_adapter`)
é um **adapter dev-only** que delega para `falkordblite` /
`redislite` (embedded Redis + FalkorDB num único wheel Python).
Ele existe para demos, CI, e dev work sem Docker.

O [ADR-024 §2.4](./ADR-024-FalkorDBClient-GraphClient-Migration.md#24-o-litefalkordbclient-permanece) documentou que o `LiteFalkorDBClient` foi **mantido** em Iter 24 porque:

1. **Não viola AGENTS.md §1**: o adapter em si é um
   `GraphLike` Protocol, encapsula `falkordb` internamente.
2. **Sem acoplamento estrutural ao vertical**: o
   `LiteFalkorDBClient` é uma dependência dev-only.

O ADR-024 também documentou a **dívida** que ficou:

> `LiteFalkorDBClient` → `LiteGraphPool`: o adapter
> dev-only em `infra/falkordblite_adapter.py` continua
> retornando `GraphLike` (sync). Um futuro
> `LiteGraphPool.graph()` retornaria `GraphAdapter`
> (async, wrapping `falkordblite.AsyncFalkorDB`).
> Estimativa: pequeno.

Iter 24 fechou o leak production (`FalkorDBClient` →
`GraphPool`). Este ADR fecha o paralelo dev-only
(`LiteFalkorDBClient` → `LiteGraphPool`).

### Por que importa

`GraphLike` foi explicitamente marcado como
**deprecated** em Iter 24:

> The Protocol `GraphLike` (in `core/_typing.py`) is
> maintained and its docstring was updated to
> reflect that it serves only the `LiteFalkorDBClient`
> (sync). The framework production operates via
> `GraphAdapter`.

O `LiteFalkorDBClient` é o **único** consumidor de
`GraphLike`. Quando ele migrar para `GraphAdapter`,
`GraphLike` fica sem consumidores — e pode ser
deletado num follow-up.

---

## 2. Decisão

### 2.1 Estratégia: dev-only migration atômica

A migração é puramente dev-only — o `LiteGraphPool`
só roda em `redislite`-equipped environments (CI, dev).
Nenhum test de produção é tocado.

Padrão Iter 24:
- `GraphPool` production migra `FalkorDBClient` →
  `GraphPool`, retorna `GraphAdapter`.
- `LiteGraphPool` dev-only: mesma migração, retorna
  `GraphAdapter`.

Mesma API pública (`connect` / `graph` / `close`),
mesma assinatura de `__init__`, mas o retorno de
`graph()` muda de `GraphLike` (sync Protocol,
deprecated) para `GraphAdapter` (async Protocol,
canonical).

### 2.2 Mudanças atômicas

**Framework (novo + edição):**
- `fmh_backend/src/fmh_backend/infra/graph/_lite_pool.py`
  (novo em Iter 28 FU 7; era `infra/lite_graph_client.py`
  no Iter 28 FU 2) — `LiteGraphPool` (cliente dev-only) +
  `LiteGraphAdapter` (o `GraphAdapter` retornado por
  `graph()`).
- `fmh_backend/src/fmh_backend/infra/__init__.py` —
  re-exporta `LiteGraphPool` e `LiteGraphAdapter`.
  `LiteFalkorDBClient` removido do `__all__`.
- `fmh_backend/src/fmh_backend/infra/falkordblite_adapter.py`
  (deletado) — substituído por `lite_graph_client.py`
  (Iter 28 FU 2; Iter 28 FU 7 renomeou o path para
  `infra/graph/_lite_pool.py`).

**Tests:**
- `fmh_backend/tests/unit/infra/test_lite_graph_client.py`
  (novo, Iter 28 FU 2) — 15 tests: API parity, lazy connect,
  `GraphAdapter` shape, query via `asyncio.to_thread`,
  exception wrapping. *(Iter 28 FU 7: o arquivo
  manteve o nome `test_lite_graph_client.py`
  para evitar re-criação; conteúdo atualizado para
  os novos nomes `LiteGraphPool`, `LiteGraphAdapter`.)*

### 2.3 `GraphAdapter` retornado por `LiteGraphPool.graph()`

O `LiteGraphAdapter` (em `infra/graph/_lite_pool.py`;
era `lite_graph_client.py` no Iter 28 FU 2):

- É um `GraphAdapter` (Protocol do framework).
- Implementa `async def query(cypher, *, params)` —
  offloads para `asyncio.to_thread` (o `falkordblite` é
  sync; a integração com o event loop é via thread).
- Converte o native `QueryResult` (list + list) para
  `GraphQueryResult` (tuple of tuples + tuple) na
  boundary.
- Wraps native exceptions em `GraphError` (per AGENTS.md
  §6).

### 2.4 `GraphLike` permanece (1 caller restante)

`GraphLike` (em `core/_typing.py`) é mantido. Seu
docstring será atualizado para indicar que agora é
**truely unused** (caller `LiteFalkorDBClient` foi
deletado). Um Iter futura deleta `GraphLike` por
completo.

### 2.5 Compat

Zero compat shim. AGENTS.md §2.2:
- O framework define `LiteGraphPool` (canonical).
- A vertical `fmh_agents` (e `fmh_office`, `fmh_app`)
  **não tem** importador de `LiteFalkorDBClient` ou
  `falkordblite_adapter` (verificado via `grep -rn`).
- Deleção atômica é segura: 0 callers em produção.

---

## 3. Consequências

### Pros

- **`GraphAdapter` é agora o único Protocol público**
  para clientes FalkorDB. `GraphLike` (deprecated) só
  existe por compat; um Iter futura deleta-o.
- **Dev-only parity** — `LiteGraphPool` tem a mesma
  API pública que `GraphPool`. Quem troca Docker →
  `falkordblite` em dev não precisa aprender uma API
  diferente.
- **`asyncio.to_thread` no adapter** — o `falkordblite`
  é sync; o framework é async. A integração via thread
  mantém o event loop responsivo (mesma técnica que
  o `LiteGraphAdapter.query` documenta explicitamente
  no docstring).
- **Exception wrapping** — native errors do
  `falkordblite` viram `GraphError` (per AGENTS.md §6,
  concrete error types). Os projectors e o
  `SolutionProjector` já esperam `GraphError`; o
  dev-only path agora segue o mesmo contrato.

### Cons

- **Adapter sync por design** — `falkordblite` não tem
  API async. O `asyncio.to_thread` resolve mas adiciona
  overhead de thread hop. Para dev/CI isso é aceitável.
- **Mock no test do `to_thread`** — o test
  `test_query_runs_in_thread` valida que a query
  executa **fora** do event loop (catches
  `RuntimeError: no running event loop` se a função
  rodasse inline). Funciona, mas é um pouco
  frágil se o Python mudar o comportamento default de
  `get_running_loop`. Mitigação: o comentário do
  test documenta a intenção.
- **`GraphLike` ainda existe** — dívida residual.
  Iter futura deleta-o (1 file, ~10 LOC + ~30 LOC
  de comentário).

### Métricas

| Métrica | Antes (Iter 28) | Depois (Iter 28 FU 2) | Depois (Iter 28 FU 7) |
|---|---|---|---|
| `fmh_backend/infra/falkordblite_adapter.py` | 138 LOC | **0** (deletado) | 0 |
| `fmh_backend/infra/lite_graph_client.py` | 0 | **~270 LOC** (novo) | **0** (deletado em Iter 28 FU 7; movido para `infra/graph/_lite_pool.py`) |
| `fmh_backend/infra/graph/_lite_pool.py` | 0 | 0 | **~290 LOC** (Iter 28 FU 7) |
| Imports de `LiteFalkorDBClient` em produção | 0 | **0** | 0 |
| Imports de `falkordblite_adapter` em produção | 0 | **0** |
| `LiteFalkorDBClient` em `__all__` de `infra/` | sim | **não** |
| `GraphLike` callers | 1 (`LiteFalkorDBClient`) | **0** (deprecated, but kept) |
| `fmh_backend/tests/unit/` total | 1316 passed | **1331 passed** (+15) |

### Conquistas notáveis

1. **Dev-only path é agora 100% `GraphAdapter`** — o
   `LiteGraphPool.graph()` retorna `GraphAdapter`
   (Protocol canonical), não `GraphLike` (deprecated).
2. **`GraphLike` sem callers** — o Protocol sync
   está oficialmente sem consumidores. A deleção é
   puramente housekeeping (1 file + 30 LOC de docs).
3. **API parity `GraphPool` ↔ `LiteGraphPool`** —
   mesmo constructor, mesmos métodos. O swap
   Docker → `falkordblite` é trivial.

---

## 4. Migration

### Já migrado (este commit)

**Framework (novo + edição + deleção):**
- `fmh_backend/src/fmh_backend/infra/lite_graph_client.py`
  (novo, ~270 LOC, Iter 28 FU 2) — `LiteGraphPool` +
  `LiteGraphAdapter`. *(Iter 28 FU 7: path renomeado
  para `infra/graph/_lite_pool.py` para seguir o
  pattern `infra.graph/`; classe permanece
  `LiteGraphPool`.)*
- `fmh_backend/src/fmh_backend/infra/__init__.py` —
  re-exports os 2 novos; `LiteFalkorDBClient` removido.
- `fmh_backend/src/fmh_backend/infra/falkordblite_adapter.py`
  (deletado, 138 LOC) — substituído pelo novo.

**Tests:**
- `fmh_backend/tests/unit/infra/test_lite_graph_client.py`
  (novo) — 15 tests cobrindo:
    - `TestLiteGraphPool` (8 tests): API surface,
      `db_path` handling, lazy connect, idempotência,
      `ImportError` quando `redislite` ausente, `graph()`
      retorna `GraphAdapter`, triggers connect.
    - `TestLiteGraphAdapterQuery` (3 tests): query via
      `asyncio.to_thread`, off-event-loop, exception
      wrapping em `GraphError`.
    - `TestLiteGraphPoolPublicSurface` (2 tests):
      métodos públicos, signature do `__init__`.

### Pendente (Iter futura)

- **Deletar `GraphLike` Protocol** (em
  `core/_typing.py:140`). Sem callers (este iter
  deleta o último). O Protocol é ~10 LOC + 30 LOC
  de comentário. Estimativa: trivial.
- **Atualizar o docstring de `GraphLike`** para
  indicar "no callers" (após a deleção do Protocol
  ser confirmada).
- **`fmh_backend.infra` review** — depois de Iter 28
  + 28 FU + 28 FU 2, o package `infra/` é puramente
  framework. Pode ser que Iter 29+ delete subpackages
  que se tornaram vazios.

---

## 5. Decisões relacionadas

- **AGENTS.md §1 (adapter types)**: a regra "1 lib
  externa = 1 Protocol" é estritamente aplicada para
  FalkorDB em ambos os paths: production (`GraphPool`
  + `GraphAdapter`) e dev (`LiteGraphPool` +
  `GraphAdapter`). Iter 28 FU 2 fecha o último caso
  onde um path usava um Protocol diferente
  (`GraphLike`).
- **AGENTS.md §2 (zero shims)**: o legacy
  `LiteFalkorDBClient` é deletado (não há shim). 0
  callers em produção; 0 compat layer.
- **AGENTS.md §6 (concrete errors)**: `GraphError`
  agora é o único error type do path FalkorDB
  (production + dev). O `LiteGraphAdapter` wrappa
  native exceptions no mesmo tipo.
- **ADR-024 §2.4**: a dívida documentada em Iter 24
  ("`LiteFalkorDBClient` → `LiteGraphPool`") é
  fechada. O roadmap está completo.
- **ADR-025 §6 (Roadmap)**: o item "`LiteFalkorDBClient`
  → `LiteGraphPool`" é marcado ✅.

---

## 6. Lições aprendidas

### O que funcionou

1. **Padrão replicável** — a migração `FalkorDBClient`
  → `GraphPool` (Iter 24) e `LiteFalkorDBClient` →
  `LiteGraphPool` (Iter 28 FU 2) seguem o mesmo
  template: novo módulo, mesmo construtor, retorna
  `GraphAdapter`. Os tests são quase idênticos
  estruturalmente.
2. **Test de deletion gate** — `test_close_terminates
  _embedded_server` valida que `close()` é idempotente
  e chama `FalkorDB.close()`. Cobre o cenário
  principal de uso (CI teardown).
3. **`asyncio.to_thread` explícito** — a docstring do
  `LiteGraphAdapter.query` documenta **por que**
  thread (o `falkordblite` é sync) e o test
  `test_query_runs_in_thread` valida que a chamada
  ocorre **fora** do event loop. Comentário + test =
  o invariante não é só convenção.

### O que poderia ter sido melhor

1. **Fazer esta migração em Iter 24** — Iter 24
  fechou o leak production mas deixou o dev-only
  path com `GraphLike`. A dívida foi paga 1 iter
  depois. Lição: ao migrar `FalkorDBClient` para
  `GraphPool`, **migrar `LiteFalkorDBClient`
  também**. Mesmo padrão, mesmo commit, mesmo test.
2. **Atualizar `GraphLike` docstring imediatamente** —
  o Protocol está sem callers (este iter). O
  docstring ainda diz "serves the LiteFalkorDBClient"
  (factual no momento em que foi escrito, mas agora
  desatualizado). Iter futura: atualizar para
  "deprecated, no callers" antes de deletar.

---

## 7. Referências

- [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md) — Iter 10-13 (GraphAdapter Protocol)
- [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md) — Pattern: production migration
- [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md) — Pattern: framework primitives
- [ADR-026](./ADR-026-Close-GLiNER2-Binding-Leak.md) — Pattern: lazy import elimination
- [ADR-027](./ADR-027-Argument-Extraction-Migration.md) — Pattern: atomic batch migration
- [ADR-028](./ADR-028-Delete-Argument-Extraction-Vertical.md) — Pattern: vertical deletion
- [AGENTS.md §1](../../AGENTS.md) — adapter types
- [AGENTS.md §2](../../AGENTS.md) — zero shims
- [AGENTS.md §6](../../AGENTS.md) — concrete errors
- `fmh_backend/src/fmh_backend/infra/graph/_lite_pool.py` — Iter 28 FU 2 (era `infra/lite_graph_client.py`); Iter 28 FU 7 (movido para `infra/graph/`)
- `fmh_backend/tests/unit/infra/test_lite_graph_client.py` — 15 tests

---

**Conclusão**: o framework FMH agora tem **0 imports
`fmh_backend → fmh_agents` em qualquer forma** (Iter 28
FU confirmou) E **`GraphAdapter` é o único Protocol público
para FalkorDB** (este iter fecha o último path que ainda
usava `GraphLike`). O roadmap do ADR-019 (Iter 22-25)
+ ADR-024 (Iter 24) + ADR-025 (Iter 25) + ADR-026 (Iter
27) + ADR-027 (Iter 28) + ADR-028 (Iter 28 FU) + este
ADR-029 (Iter 28 FU 2) está **fechado em 100%** para o
core framework. Iter 26+ (3 Reactive Systems para
Knowledge) é refator de performance, não cobertura
arquitetural.

---

## 7. Apêndice: Iter 28 FU 7 — Path migration

Ver [ADR-033](./ADR-033-GraphPool-Reorg.md) §2.1
para os detalhes completos. Resumo:

- `infra/lite_graph_client.py` → `infra/graph/_lite_pool.py`
  (Iter 28 FU 7).
- `LiteGraphPool` permanece como nome de classe
  (sem renomeação no Iter 28 FU 7; apenas path muda
  para seguir o pattern `infra.graph/`).
- O `__init__` de `infra/` foi atualizado:
  `from .graph._lite_pool import LiteGraphAdapter, LiteGraphPool`.

Os call sites em apps/tests foram atualizados
atomicamente no Iter 28 FU 7 (mesmo commit que moveu
`GraphClient` → `GraphPool`). Zero shims.