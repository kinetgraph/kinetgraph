<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-027: Argument Extraction Migration to Framework (Iter 28)

**Status:** Aceito (obsoleto em parte pelo ADR-028, que removeu o shim de compatibilidade legado do vertical fmh_agents)
**Data:** 30 de junho de 2026
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md), [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md), [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md), [ADR-026](./ADR-026-Close-GLiNER2-Binding-Leak.md), [AGENTS.md §1](../../AGENTS.md), §2

Este ADR fecha o roadmap aberto pelo [ADR-026 §4 (Pendentes)](./ADR-026-Close-GLiNER2-Binding-Leak.md#4-migration): mover os 2 componentes restantes (`GlinerFieldFinder`, `SchemaArgumentExtractor` + helpers) que o `GlinerArgumentAdapter` ainda importava lazy da vertical. Após Iter 28, **0 imports `fmh_backend → fmh_agents` em qualquer forma** (eager ou lazy).

---

## 1. Contexto

O ADR-026 fechou o leak eager
(`_slm_facades.py:260` → `fmh_agents.knowledge.argument_extractor`)
mas deixou 2 imports **lazy** dentro de
`GlinerArgumentAdapter.__init__` (linhas 145-148
do `gliner_argument.py`):

```python
from fmh_agents.knowledge.argument_extractor import (
    GlinerFieldFinder,
)
from fmh_agents.knowledge.argument_extractor import (
    SchemaArgumentExtractor,
)
```

Esses 2 componentes viviam na vertical por razões
históricas (Iter 21 moveu `GlinerEntityAdapter` e
`GlinerIntentAdapter`, mas Iter 21 não moveu o
`GlinerArgumentAdapter` nem seus componentes
compostos — Iter 27 fechou só o adapter).

A questão arquitetural: esses componentes são
**framework-level** (lógica pura, sem deps de
vertical) ou **vertical** (têm acoplamento forte com
algo de `fmh_agents`)?

Análise:

- **`FieldFinder` Protocol + `RegexFieldFinder`** —
  pure logic (regex contra texto). 102 LOC. **Framework.**
- **`coerce`** — pure logic (raw value → JSON-Schema
  type). 69 LOC. **Framework.**
- **`SchemaArgumentExtractor`** — pure logic (walks a
  schema, aggregates field finds, coerces). 149 LOC.
  **Framework.**
- **`GlinerFieldFinder`** — wraps GLiNER2 model (a
  third-party opt-in dep). 281 LOC, dos quais 130 LOC
  são helpers de match-extraction (`field_o`,
  `extract_first`, `match_to_value`). **Framework**
  (mesma categoria que `GlinerEntityAdapter` e
  `GlinerIntentAdapter`).
- **Helpers de match-extraction** (`field_o`,
  `extract_first`, `match_to_value`) — pure logic que
  tolera múltiplas shapes de GLiNER2. **Framework.**

**Veredito:** todos os 5 componentes são
framework-level. A única razão de viverem na vertical
era inércia (estavam lá antes do split Iter 25/27
acontecer).

---

## 2. Decisão

### 2.1 Estratégia: migração atômica em batch

Diferente de Iter 25 (que moveu uma primitiva por vez
para evitar regressão) e Iter 27 (que moveu o
`GlinerArgumentAdapter` com lazy imports), Iter 28
move **5 arquivos** em 1 commit atômico. A razão:

- Os 5 componentes são **mutuamente dependentes**:
  `SchemaArgumentExtractor` depende de `coerce` e
  `FieldFinder`; `GlinerFieldFinder` depende de
  `FieldFinder`; `GlinerArgumentAdapter` depende de
  `GlinerFieldFinder` e `SchemaArgumentExtractor`.
- Mover 1-2-3 e parar deixaria o framework em estado
  intermediário (parcialmente framework, parcialmente
  vertical) — exatamente o que Iter 27 documentou
  como "shim transicional".
- Os tests existentes (`test_argument_extractor.py`
  e `test_gliner_finder.py` no vertical; `test_extraction.py`
  no framework) cobrem todas as componentes. Eles
  detectam qualquer regressão na migração atômica.

### 2.2 Estrutura do subpackage `argument/`

A framework ganha um subpackage `argument/` com 4
módulos:

```
fmh_backend/knowledge/extraction/argument/
├── __init__.py           # public re-exports
├── _finder.py            # FieldFinder Protocol + RegexFieldFinder
├── _coerce.py            # coerce() helper
├── _extractor.py         # SchemaArgumentExtractor (orchestrator)
└── _gliner_finder.py     # GlinerFieldFinder + match helpers
```

E o `GlinerArgumentAdapter` (em `gliner_argument.py`)
faz **eager imports** desses módulos (não mais lazy
shims do vertical).

### 2.3 Vertical vira re-export shim completo

`fmh_agents/knowledge/argument_extractor/__init__.py`
agora é puramente re-exports do framework. Os 5
arquivos private (`_finder.py`, `_coerce.py`,
`_extractor.py`, `_gliner_finder.py`, `_schema.py`)
foram **deletados** da vertical (eram 595 LOC).

A vertical existe apenas para **backward compat**
(callers que importam de `fmh_agents.knowledge
.argument_extractor` continuam funcionando via
re-export). Um follow-up commit pode deletar a
vertical inteira.

### 2.4 Compat

Zero compat shim. AGENTS.md §2.2:
- O framework define as primitivas canônicas.
- A vertical re-exporta do framework.
- Não há detecção de versão, kwargs opcionais, branches.

Re-export legítimo (mesmo padrão de Iter 25 Tools
Protocol, Iter 24 `SolutionProjector`, Iter 27
`GlinerArgumentAdapter`).

---

## 3. Consequências

### Pros

- **0 imports `fmh_backend → fmh_agents`** em qualquer
  forma (eager ou lazy). AGENTS.md §1.2 estritamente
  respeitada em todos os caminhos de runtime do
  framework.
- **Argument-extraction é 100% framework** — a
  framework expõe `FieldFinder`, `RegexFieldFinder`,
  `GlinerFieldFinder`, `coerce`, `SchemaArgumentExtractor`,
  `GlinerArgumentAdapter` (todos) como API pública.
  Verticais podem compor essas primitivas sem herdar
  de vertical alguma.
- **`SLMArgumentExtractor` agora tem o shape
  arquitetural exato** que `SLMEntityExtractor` e
  `SLMIntentClassifier` já tinham (Iter 21). Os 3
  default adapters de SLM facades vivem no framework.
- **5 private modules da vertical deletados** (595 LOC).
  O vertical package agora existe só como re-export
  shim (~120 LOC).
- **Lazy import shim documentado no ADR-026 §4
  eliminado**. A "transitional debt" de Iter 27 foi
  paga em 1 iter.

### Cons

- **Migração atômica em batch é arriscada** — qualquer
  regressão em qualquer das 5 componentes quebra os
  tests do framework. Mitigação: tests cobrem todas as
  componentes; tests existentes em `test_extraction.py`
  e `test_argument_extractor.py` (vertical) detectam
  regressões.
- **Re-export shim na vertical é dívida** — o
  `fmh_agents.knowledge.argument_extractor` package
  existe só para backward compat. Pode ser deletado
  num follow-up. Custo: 120 LOC de re-export, 1 file
  a mais para manter.
- **`CoercedValue` symbol removal** — o
  `__init__.py` do framework extraction não re-exporta
  `CoercedValue` (era usado internamente). É tipo
  privado do `coerce()`; callers externos não devem
  usar. (Linter flag detecta se voltar a ser importado.)

### Métricas

| Métrica | Antes (Iter 27) | Depois (Iter 28) |
|---|---|---|
| Imports eager `fmh_backend → fmh_agents` | 0 | **0** |
| Imports lazy `fmh_backend → fmh_agents` | 2 (em `gliner_argument.py:__init__`) | **0** |
| Files em `fmh_backend/knowledge/extraction/` | 7 | **8** (+1 subpackage) |
| Files em `fmh_backend/knowledge/extraction/argument/` | 0 | **5** (novo subpackage) |
| Files em `fmh_agents/knowledge/argument_extractor/` | 7 (5 private + __init__ + _gliner_wrapper já deletado) | **1** (só `__init__.py` re-export) |
| Private modules da vertical | 5 | **0** (deletados) |
| Total `fmh_backend/tests/unit/` tests | 1289 passed | **1315 passed** (+26) |

### Conquistas notáveis

1. **5 imports verticais eliminados em 1 commit
   atômico** — o `GlinerArgumentAdapter` agora é
   puramente framework (eager imports do
   subpackage `argument/`).
2. **Argument-extraction é framework-agnostic** —
   qualquer vertical pode consumir `FieldFinder` /
   `coerce` / `SchemaArgumentExtractor` /
   `GlinerFieldFinder` sem passar pelo
   `fmh_agents.knowledge.argument_extractor`. O
   pacote vertical se torna um consumidor opcional
   (re-export), não um requisito.
3. **0 leaks `fmh_backend → fmh_agents`** — verificado
   via `grep -rn "from fmh_agents" fmh_backend/src/`
   retornou vazio. AGENTS.md §1.2 estritamente
   respeitada.

---

## 4. Migration

### Já migrado (este commit)

**Framework (novo subpackage + edits):**
- `fmh_backend/knowledge/extraction/argument/__init__.py`
  (novo) — public re-exports.
- `fmh_backend/knowledge/extraction/argument/_finder.py`
  (novo) — `FieldFinder` Protocol + `RegexFieldFinder`.
- `fmh_backend/knowledge/extraction/argument/_coerce.py`
  (novo) — `coerce` helper.
- `fmh_backend/knowledge/extraction/argument/_extractor.py`
  (novo) — `SchemaArgumentExtractor` orchestrator.
- `fmh_backend/knowledge/extraction/argument/_gliner_finder.py`
  (novo) — `GlinerFieldFinder` + match-extraction
  helpers (`field_o`, `extract_first`, `match_to_value`).
- `fmh_backend/knowledge/extraction/gliner_argument.py`
  — eager imports do subpackage `argument/` (não mais
  lazy do vertical).
- `fmh_backend/knowledge/extraction/__init__.py` —
  re-exports dos 10 novos símbolos do subpackage
  `argument/`.

**Vertical (re-export shim + 5 deletions):**
- `fmh_agents/knowledge/argument_extractor/__init__.py`
  — puramente re-exports do framework.
- `fmh_agents/knowledge/argument_extractor/_finder.py`
  (deletado) — movido para o framework.
- `fmh_agents/knowledge/argument_extractor/_coerce.py`
  (deletado) — movido para o framework.
- `fmh_agents/knowledge/argument_extractor/_extractor.py`
  (deletado) — movido para o framework.
- `fmh_agents/knowledge/argument_extractor/_gliner_finder.py`
  (deletado) — movido para o framework.
- `fmh_agents/knowledge/argument_extractor/_schema.py`
  (deletado) — Iter 25 já tinha movido
  `walk_schema`/`FieldSpec`/`compute_schema_version`
  para `fmh_backend/tools/schema.py`. O re-export
  ficou aqui; agora desaparece.

**Tests:**
- `fmh_backend/tests/unit/knowledge/extraction/test_argument_framework.py`
  (novo) — 22 tests: cobre `FieldFinder` Protocol,
  `RegexFieldFinder`, `coerce`, `SchemaArgumentExtractor`,
  `GlinerFieldFinder`, `GlinerArgumentAdapter`, e a
  invariante "framework has 0 imports `fmh_agents`".
- `fmh_backend/tests/unit/knowledge/extraction/test_gliner_argument_no_leak.py`
  — ainda passa (4 tests); o subprocess continua
  validando que o caminho com `adapter=` injetado
  não carrega o vertical.

### Pendente (Iter futura)

- **Deletar `fmh_agents.knowledge.argument_extractor`**
  por completo. O pacote inteiro agora é re-export;
  deletá-lo requer:
    1. Buscar todos os imports do vertical
       (`from fmh_agents.knowledge.argument_extractor import ...`).
    2. Atualizar para o framework path.
    3. Deletar o diretório.
  Estimativa: pequeno (o `grep` deve retornar ~10
  call sites).
- **`fmh_agents/knowledge` package inteiro** pode ser
  re-evaluado: ele tem `argument_extractor` (re-export
  shim), `solution_projector` (Iter 24 migrou o
  client), `intent_classifier.py` (?)... Pode ser que
  Iter 29+ delete `fmh_agents.knowledge` por completo.

> **Status**: ✅ feito em [ADR-028](./ADR-028-Delete-Argument-Extraction-Vertical.md)
> (Iter 28 follow-up). O vertical package foi
> completamente deletado. 4 call sites de tests
> migrados para o framework path. 1 deletion gate
> (`test_argument_vertical_deleted.py`) adicionado
> para regressão futura. Ver ADR-028 §3 (Métricas) e
> §4 (Migration) para detalhes.

---

## 5. Decisões relacionadas

- **AGENTS.md §1.2 (framework vs vertical)**: o último
  caminho `fmh_backend → fmh_agents` foi fechado. O
  framework é estruturalmente independente do vertical
  em qualquer caminho de runtime.
- **AGENTS.md §2 (zero shims)**: o re-export do vertical
  é legítimo (mesmo padrão de Iter 25/27). Nenhum
  compat shim introduzido.
- **ADR-019 §2.4 (padrão estabilizado)**: o padrão
  "sub-adapter compõe sobre o mesmo `GraphAdapter`"
  (backend axis) é complementado pelo padrão Iter 28
  "framework primitives → vertical impls" (lifecycle
  axis). As duas dimensões são ortogonais.
- **ADR-025 §1 (decomposição do Tool Protocol)**: o
  argumento de Iter 25 — "primitivas vivem no
  framework; verticais as implementam" — agora se
  aplica a TODA a knowledge stack (extraction, graph,
  memory). Iter 28 é a materialização final.

---

## 6. Lições aprendidas

### O que funcionou

1. **Migração atômica em batch** foi a decisão certa
   para Iter 28. Os 5 componentes são mutuamente
   dependentes; mover 1-2-3 e parar teria deixado
   estado intermediário. Os tests cobriram todos os
   5; a migração atômica pegou todas as regressões
   (que não houve) numa só execução.
2. **Tests do framework precedem implementação** —
   `test_argument_framework.py` falhou 1 vez
   (`test_default_adapter_is_gliner` patchava um path
   que mudou de `require_optional` para `__init__` da
   adapter). O test foi ajustado para a nova
   arquitetura (patch no `__init__` da adapter, não
   no `require_optional`). 0 regressões.
3. **Re-export shim no vertical** preservou
   backward compat. Tests no vertical
   (`test_argument_extractor.py` e
   `test_gliner_finder.py`) continuam passando sem
   modificação.

### O que poderia ter sido melhor

1. **Mover os 5 componentes em Iter 21** — Iter 21
   moveu `GlinerEntityAdapter` e `GlinerIntentAdapter`
   para o framework mas deixou `GlinerArgumentAdapter`
   + 5 componentes na vertical. O roadmap
   explicitou que seria pago em Iter 27/28; foram 2
   iterações para fechar totalmente. Lição: ao mover
   2 de 3, mover o terceiro também.
2. **Tests de "framework não carrega vertical" são
   frágeis** — o test `test_slm_argument_extractor
   _does_not_load_vertical` precisou de subprocess
   para ser robusto contra `sys.modules` pollution.
   Custo: ~100ms por test (subprocess startup).
   Alternativa: marker pytest que reseta `sys.modules`
   por test. Mas fixtures de reset têm armadilhas.

---

## 7. Referências

- [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md) — Iter 21 (SLM facades) + roadmap §4
- [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md) — Pattern: framework primitives first
- [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md) — Pattern: 4 leaks fechados em Iter 25
- [ADR-026](./ADR-026-Close-GLiNER2-Binding-Leak.md) — Iter 27 fecha leak eager; Iter 28 fecha lazy
- [AGENTS.md §1](../../AGENTS.md) — adapter types
- [AGENTS.md §2](../../AGENTS.md) — zero shims
- [AGENTS.md §1.2](../../AGENTS.md) — framework vs vertical
- `fmh_backend/src/fmh_backend/knowledge/extraction/argument/` — novo subpackage framework
- `fmh_agents/src/fmh_agents/knowledge/argument_extractor/__init__.py` — re-export shim
- `fmh_backend/tests/unit/knowledge/extraction/test_argument_framework.py` — 22 tests novos

---

**Conclusão**: o framework FMH agora tem **0 imports
`fmh_backend → fmh_agents` em qualquer forma** (eager
ou lazy). O caminho "framework puro" (com `adapter=`
injetado ou com default adapter) é 100% framework-side.
O cycle `fmh_backend ↔ fmh_agents` está
**estruturalmente impossível** em qualquer ponto do
grafo. O roadmap do ADR-019/024/025/026 está fechado
para o core framework — Iter 28 é a materialização
final do princípio "primitivas vivem no framework;
verticais as implementam".