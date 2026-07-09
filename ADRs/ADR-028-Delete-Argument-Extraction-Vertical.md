<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-028: Delete Argument-Extraction Vertical Package (Iter 28 Follow-up)

**Status:** Aceito
**Data:** 30 de junho de 2026
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md), [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md), [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md), [ADR-026](./ADR-026-Close-GLiNER2-Binding-Leak.md), [ADR-027](./ADR-027-Argument-Extraction-Migration.md), [AGENTS.md §1](../../AGENTS.md), §2

Este ADR fecha o último item do roadmap aberto pelo [ADR-027 §4 (Pendentes)](./ADR-027-Argument-Extraction-Migration.md#4-migration): deletar `fmh_agents.knowledge.argument_extractor` por completo. Após este commit, o vertical não tem mais nenhum arquivo de argument-extraction; a framework é a única fonte de verdade.

---

## 1. Contexto

O [ADR-027](./ADR-027-Argument-Extraction-Migration.md) moveu 5 componentes
(`FieldFinder`, `RegexFieldFinder`, `coerce`,
`SchemaArgumentExtractor`, `GlinerFieldFinder`) e o
`GlinerArgumentAdapter` para o framework. O vertical
`fmh_agents.knowledge.argument_extractor` ficou como
**re-export shim** (1 file, 120 LOC) com a
documentação explícita de que seria deletado num
follow-up.

A questão arquitetural: **o shim ainda é necessário?**

Análise:

- **Callers que importam do vertical**: 4 (todos em tests).
  - `test_argument_extractor.py` (vertical test).
  - `test_optional.py` (framework test, canonical message).
  - `test_schema.py` (framework test, legacy re-export assertion).
  - `test_gliner_argument_no_leak.py` (framework test, leak assertion).
- **Callers em código de produção**: 0. O grep em
  `fmh_agents/src/`, `fmh_office/src/`, `fmh_backend/src/`,
  `fmh_app/src/` retorna apenas docstrings e
  comentários.
- **Imports eager `fmh_backend → fmh_agents`**: 0.
- **Imports lazy `fmh_backend → fmh_agents`**: 0.

**Veredito:** o shim é puramente custo (1 file, 120 LOC
de re-exports, manutenção de `__all__`). Não há caller
de produção. Deleção é segura.

---

## 2. Decisão

### 2.1 Estratégia: deleção atômica em 1 commit

A deleção é puramente remoção — sem lógica nova, sem
refactor de comportamento. Padrão Iter 26/27/28:
migração atômica. 4 testes são atualizados em paralelo
(import path switch); 1 test novo é criado (deletion
gate); o diretório é deletado.

### 2.2 Migração dos 4 call sites

Todos os 4 callers importam do vertical
`fmh_agents.knowledge.argument_extractor`. Migração
para o framework path:

| Caller | Old import | New import |
|---|---|---|
| `test_argument_extractor.py` (test) | `from fmh_agents.knowledge.argument_extractor import ...` | `from fmh_backend.knowledge.extraction.argument import ...` + `from fmh_backend.knowledge.extraction.argument._coerce import ...` + `from fmh_backend.knowledge.extraction.argument._gliner_finder import ...` + `from fmh_backend.knowledge.extraction import GlinerArgumentAdapter` + `from fmh_backend.tools.schema import ...` + `from fmh_backend.tools.registry import ToolRegistry` |
| `test_optional.py` (test) | `from fmh_agents.knowledge.argument_extractor import GlinerFieldFinder` | `from fmh_backend.knowledge.extraction.argument import GlinerFieldFinder` |
| `test_schema.py` (test) | `TestLegacyReexport` que importava do vertical | `TestVerticalDeleted` que verifica que o vertical NÃO existe (deletion gate) |
| `test_gliner_argument_no_leak.py` (test) | `test_gliner_argument_adapter_canonical_path` que comparava canonical vs legacy | Substituído por `test_vertical_package_does_not_exist` que verifica que o vertical foi deletado |

### 2.3 Test de deletion gate (novo)

`test_argument_vertical_deleted.py` — 3 tests:

1. `test_vertical_package_does_not_exist` —
   `import fmh_agents.knowledge.argument_extractor`
   deve falhar com `ModuleNotFoundError` ou
   `ImportError`.
2. `test_vertical_subpackage_does_not_exist` — os 6
   subpackages privados (`_finder`, `_coerce`,
   `_extractor`, `_gliner_finder`, `_schema`,
   `_gliner_wrapper`) também não existem mais.
3. `test_framework_path_still_works` — sanity: as 6
   primitivas framework-side são importáveis.

### 2.4 Deleção atômica

```bash
$ rm -rf fmh_agents/src/fmh_agents/knowledge/argument_extractor/
$ grep -rn "fmh_agents.knowledge.argument_extractor" \
    fmh_backend fmh_agents fmh_office fmh_app \
    | grep -v __pycache__ | grep -v ADRs/ | grep -v "\.md:"
# (apenas em docstrings e tests que documentam a deleção)
```

---

## 3. Consequências

### Pros

- **Vertical package eliminado** — 1 diretório, 1 file,
  120 LOC de re-exports a menos para manter.
- **Framework é a única fonte de verdade** para
  argument-extraction. Qualquer developer que procure
  `FieldFinder`, `coerce`, `SchemaArgumentExtractor` é
  direcionado ao `fmh_backend.knowledge.extraction.argument/`
  (canonical) ou ao `fmh_backend.knowledge.extraction`
  (re-exports). Sem ambiguidade de path.
- **Roadmap Iter 25-28 fechado em 100%** — 0 imports
  `fmh_backend → fmh_agents` em qualquer forma; 0
  re-export shims no vertical. O framework é
  estruturalmente independente.
- **1 test novo** (`test_argument_vertical_deleted.py`)
  serve como regression gate. Se um futuro refactor
  re-introduzir o vertical, o test falha.

### Cons

- **Migração de 4 call sites em tests** — todos em
  tests, não em produção. Mas são tests importantes
  (regression gates); erros de migração quebrariam
  o CI.
- **`test_argument_extractor.py` perdeu a posição
  canônica** — o test do vertical agora é
  `test_argument_framework.py` (framework). O
  `test_argument_extractor.py` continua existindo
  (para histórico) mas importa do framework path.
  Custo: 1 file extra de test (~418 LOC). Pode ser
  deletado em follow-up.

### Métricas

| Métrica | Antes (Iter 28) | Depois (Iter 28 FU) |
|---|---|---|
| `fmh_agents/knowledge/argument_extractor/` | 1 file (re-export) | **0 files (deleted)** |
| Imports de `fmh_agents.knowledge.argument_extractor` em produção | 0 | **0** |
| Imports de `fmh_agents.knowledge.argument_extractor` em tests | 4 | **0** (todos migrados) |
| `fmh_backend/tests/unit/` total | 1315 passed | **1316 passed, 1 skipped** (+1 test: deletion gate) |
| Path único para argument-extraction | 2 (framework + vertical shim) | **1 (framework only)** |

### Conquistas notáveis

1. **Argument-extraction é 100% framework** — não há
   nenhuma peça de argument-extraction no
   `fmh_agents/`. O vertical é puramente sobre
   `solution_projector` (Iter 24) e os re-exports de
   `embedding` (Iter 25).
2. **Iter 25 → 28 + follow-up materializou o princípio
   AGENTS.md §1.2 em 100%** — 0 imports
   `fmh_backend → fmh_agents` em qualquer forma. A
   regra "framework nunca depende de vertical" agora é
   verdade verificada por `grep`, não apenas declarada
   em ADR.

---

## 4. Migration

### Já migrado (este commit)

**Vertical (deletion):**
- `fmh_agents/src/fmh_agents/knowledge/argument_extractor/`
  (diretório deletado). Continha apenas `__init__.py`
  (re-export shim de 120 LOC). Os 5 private modules
  (`_finder.py`, `_coerce.py`, `_extractor.py`,
  `_gliner_finder.py`, `_schema.py`) já tinham sido
  deletados em Iter 28.

**Tests (migration + new gate):**
- `fmh_backend/tests/unit/knowledge/test_argument_extractor.py`
  — atualizado para usar framework path.
- `fmh_backend/tests/unit/test_optional.py` — atualizado.
- `fmh_backend/tests/unit/tools/test_schema.py` —
  `TestLegacyReexport` substituído por
  `TestVerticalDeleted` (deletion gate).
- `fmh_backend/tests/unit/tools/test_import_graph.py` —
  `test_knowledge_argument_extractor_imports_alone`
  invertido para
  `test_knowledge_argument_extractor_does_not_exist`;
  parametrize list limpo.
- `fmh_backend/tests/unit/knowledge/extraction/test_gliner_argument_no_leak.py`
  — ajustado para refletir que o vertical não existe.
- `fmh_backend/tests/unit/knowledge/extraction/test_argument_vertical_deleted.py`
  (novo) — 3 tests: deletion gate.

### Pendente (Iter futura)

- **Deletar `fmh_backend/tests/unit/knowledge/test_argument_extractor.py`**.
  O test existe para histórico (era o test do vertical
  antes de Iter 28). Agora importa do framework path;
  sua cobertura duplica `test_argument_framework.py`
  (que tem a versão mais limpa). Estimativa: 5 min.
- **`fmh_agents/knowledge/` package inteiro** pode ser
  re-evaluado: depois deste iter, ele tem apenas
  `solution_projector.py` (Iter 24) e `embedding/`
  (re-exports de `fmh_backend`). Pode ser esvaziado
  num Iter 29+.

---

## 5. Decisões relacionadas

- **AGENTS.md §1 (adapter types)**: a regra "1 lib
  externa = 1 Protocol" foi estritamente aplicada
  para FalkorDB, Embedding, LLM, GLiNER2, e
  Argument-Extraction. O framework define os Protocols;
  as verticais definem as impls. Iter 28 + follow-up
  fechou a última inconsistência (vertical ainda
  continha argument-extraction por inércia).
- **AGENTS.md §2 (zero shims)**: o último re-export
  shim foi deletado. Não há shims no framework
  (qualquer forma).
- **AGENTS.md §1.2 (framework vs vertical)**: a
  separação framework/vertical agora é **estritamente
  respeitada em todos os caminhos de runtime**.
  `grep -rn "from fmh_agents" fmh_backend/src/` retorna
  vazio.

---

## 6. Lições aprendidas

### O que funcionou

1. **TDD estrito** — `test_argument_vertical_deleted.py`
   foi escrito ANTES da deleção. O test falhou (vertical
   ainda existia) → migrei os 4 call sites → test passou
   → deletei o vertical. Sem TDD, eu poderia ter
   deletado o vertical e quebrado os 4 callers.
2. **Migração em 1 commit atômico** — 7 files modificados,
   1 deletado, 1 novo. Sem estado intermediário. Os 4
   call sites foram migrados para o framework path ANTES
   da deleção (não ao contrário).
3. **Deletion gate explícito** (`TestVerticalDeleted` +
   `test_knowledge_argument_extractor_does_not_exist`) —
   se um futuro refactor re-introduzir o vertical (por
   inércia, copy-paste, etc.), os tests falham. O
   gate documenta a decisão arquitetural.

### O que poderia ter sido melhor

1. **Migrar e deletar no mesmo iter** (Iter 28) — Iter 28
   moveu os 5 componentes para o framework mas deixou o
   shim como "Iter 28 follow-up". O shim teve 1 iter
   de vida intermediária; poderia ter sido deletado
   no mesmo commit. Lição: quando um shim tem 0
   callers de produção, delete-o no mesmo iter.
2. **Test de deletion primeiro, não de leak** — o test
   de leak (`test_gliner_argument_no_leak.py`) é
   positivo (afirma que algo NÃO acontece). O test
   de deletion é mais forte (afirma que algo NÃO
   EXISTE). Para o follow-up, o test de deletion é
   mais útil; o de leak pode ser convertido em
   deletion. Custo: 1 refactor de test; valor:
   regressão mais explícita.

---

## 7. Referências

- [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md) — Iter 21 (SLM facades)
- [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md) — Pattern: framework primitives first
- [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md) — Cycle resolution + Tool Protocol split
- [ADR-026](./ADR-026-Close-GLiNER2-Binding-Leak.md) — Iter 27: leak eager fechado
- [ADR-027](./ADR-027-Argument-Extraction-Migration.md) — Iter 28: 5 componentes movidos + shim transicional
- [AGENTS.md §1](../../AGENTS.md) — adapter types
- [AGENTS.md §2](../../AGENTS.md) — zero shims
- [AGENTS.md §1.2](../../AGENTS.md) — framework vs vertical
- `fmh_backend/tests/unit/knowledge/extraction/test_argument_vertical_deleted.py` — deletion gate

---

**Conclusão**: o framework FMH agora tem argument-extraction
**100%** como framework concern. O vertical package
`fmh_agents.knowledge.argument_extractor` foi
**completamente deletado**. 0 imports
`fmh_backend → fmh_agents` em qualquer forma (eager
ou lazy). 0 re-export shims. O roadmap do ADR-019
(Iter 22-25) + ADR-024 (Iter 24) + ADR-025 (Iter 25)
+ ADR-026 (Iter 27) + ADR-027 (Iter 28) + ADR-028
(este) está **fechado em 100%** para o core
framework. Iter 26+ (3 Reactive Systems para
Knowledge) é refator de performance, não cobertura
arquitetural.