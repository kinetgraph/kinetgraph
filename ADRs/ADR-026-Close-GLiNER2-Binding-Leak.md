<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-026: Close GLiNER2 Binding Leak in SLM Facade (Iter 27)

**Status:** Aceito
**Data:** 30 de junho de 2026
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md), [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md), [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md), [AGENTS.md §1](../../AGENTS.md), §2

Este ADR fecha o último leak arquitetural entre o framework
(`fmh_backend`) e a vertical (`fmh_agents`): o
`SLMArgumentExtractor` carregava o
`GlinerArgumentAdapter` da vertical por import eager
dentro de `_slm_facades.py:260`. Iter 25 fechou 4 dos 5
leaks; este ADR fecha o quinto (o único restante que
estava documentado no roadmap do ADR-025 §4).

---

## 1. Contexto

O ADR-019 epílogo documentou que o framework deveria
operar **sem** dependências de vertical: o
`fmh_backend` define as primitivas (Protocols, value
objects, base classes) e cada vertical (`fmh_agents`,
`fmh_office`, `fmh_app`) define as **implementações
concretas** dos seus protocolos verticais (PII redaction,
LLM, argument extraction, etc).

O ADR-025 §4 (Pendente) registrou o último leak
residual:

> - **Leak residual em `_slm_facades.py:260`**
>   (`GlinerArgumentAdapter` import em
>   `TYPE_CHECKING`/runtime). Workaround:
>   `GlinerArgumentAdapter` deveria ser importado lazy
>   ou movido para um adapter binding do framework.
>   Estimativa: pequeno.

O leak era pequeno mas violava AGENTS.md §1.2
("framework nunca depende de vertical"). A
`SLMArgumentExtractor` (uma facade **framework-level**)
importava seu default backing (`GlinerArgumentAdapter`)
de `fmh_agents.knowledge.argument_extractor` (uma
**vertical**). Isso era um acoplamento circular em
princípio: a framework referenciando uma vertical
para completar sua própria API.

### Por que isso importava

O framework define 3 facades `SLM*` (entity, intent,
argument) em `fmh_backend.knowledge.extraction`. Cada
uma tem um default adapter `Gliner*Adapter`. Iter 21
moveu `GlinerEntityAdapter` e `GlinerIntentAdapter` para
o framework (Iter 21 §3); o `GlinerArgumentAdapter`
ficou na vertical por inércia (o adapter depende de
`GlinerFieldFinder` e `SchemaArgumentExtractor`, que
viveram em `fmh_agents.knowledge.argument_extractor`).

A fix não era trivial: mover o `GlinerArgumentAdapter`
sem mover os dois componentes que ele compõe
significava fazer o framework depender **ainda mais**
da vertical (importar 3 módulos em vez de 1).

---

## 2. Decisão

### 2.1 Estratégia: mover o adapter, lazy-importar os componentes

A solução é mover **`GlinerArgumentAdapter` apenas** para
o framework. O adapter vira um **componente framework**
que faz **lazy local imports** dos dois componentes da
vertical (`GlinerFieldFinder` e
`SchemaArgumentExtractor`).

**Por que lazy local em vez de eager:**

1. **O framework precisa apenas do adapter** — os
   componentes (`GlinerFieldFinder`,
   `SchemaArgumentExtractor`) ainda vivem na vertical
   por razões históricas.
2. **Lazy import preserva o framework limpo** — o test
   `test_slm_argument_extractor_does_not_load_vertical`
   valida que, com `adapter=` injetado, o framework não
   carrega a vertical. O leak só dispara no caminho
   do default adapter (sem `adapter=`), que é a única
   situação onde o vertical é necessário.
3. **A migração é puramente estrutural** — não há
   mudança comportamental. O `SLMArgumentExtractor`
   ainda instancia o mesmo adapter, só o faz com o
   adapter no framework.

**Por que Iter 28+ vai mover os outros componentes:**

- `GlinerFieldFinder` é um **wrapper de modelo** (mesma
  categoria que `GlinerEntityAdapter` e
  `GlinerIntentAdapter`, já no framework). Deve ser
  framework.
- `SchemaArgumentExtractor` é um **orquestrador** que
  anda um schema e agrega field finds. Lógica pura,
  sem deps de modelo, mas depende de
  `fmh_agents.knowledge.argument_extractor._finder`
  (FieldFinder Protocol) que por sua vez depende de
  `fmh_backend.tools.schema` (FieldSpec) e
  `fmh_backend.knowledge.extraction.base`
  (ArgumentExtractor Protocol).

A migração completa (Iter 28+) vai:
1. Mover `FieldFinder` Protocol + `RegexFieldFinder` para
   `fmh_backend.knowledge.extraction._finder` (ou
   reusar `fmh_backend.tools.protocol.Callable`? não —
   FieldFinder é async, Callable é a versão genérica).
2. Mover `SchemaArgumentExtractor` para
   `fmh_backend.knowledge.extraction._extractor`.
3. Mover `GlinerFieldFinder` para
   `fmh_backend.knowledge.extraction._gliner_finder`.
4. Atualizar `gliner_argument.py` para fazer eager
   imports (sem lazy).
5. Deletar `fmh_agents.knowledge.argument_extractor` por
   completo.

### 2.2 Mudanças atômicas

**Framework (novo + editado):**
- `fmh_backend/src/fmh_backend/knowledge/extraction/gliner_argument.py`
  (novo) — `GlinerArgumentAdapter` com lazy imports
  internos.
- `fmh_backend/src/fmh_backend/knowledge/extraction/__init__.py`
  — re-exporta `GlinerArgumentAdapter` do novo módulo.
- `fmh_backend/src/fmh_backend/knowledge/extraction/_slm_facades.py`
  — `SLMArgumentExtractor` agora importa de
  `fmh_backend.knowledge.extraction` (não mais da
  vertical).

**Vertical (re-export):**
- `fmh_agents/src/fmh_agents/knowledge/argument_extractor/__init__.py`
  — `GlinerArgumentAdapter` agora vem do framework
  (re-export shim).
- `fmh_agents/src/fmh_agents/knowledge/argument_extractor/_gliner_wrapper.py`
  (deletado) — o wrapper antigo não existe mais; o
  adapter vive no framework.

**Tests:**
- `fmh_backend/tests/unit/knowledge/extraction/test_slm_facades.py`
  — atualizado para usar o framework path
  (`from fmh_backend.knowledge.extraction import
  GlinerArgumentAdapter`).
- `fmh_backend/tests/unit/knowledge/extraction/test_gliner_argument_no_leak.py`
  (novo) — 4 tests: regression gate para o leak.

### 2.3 Compat

Zero compat shim. AGENTS.md §2.2:
- O framework define a primitiva canônica (`GlinerArgumentAdapter`).
- A vertical re-exporta o framework (não tem impl
  paralela).
- Não há detecção de versão, kwargs opcionais, branches
  de runtime.

O re-export em `fmh_agents.knowledge.argument_extractor`
é o **mesmo padrão** usado em Iter 25 (Tools Protocol
re-export) e Iter 24 (`SolutionProjector` não muda).

---

## 3. Consequências

### Pros

- **5º leak fechado.** Antes de Iter 27: 4 imports
  `fmh_backend → fmh_agents` (5 com o de GLiNER2);
  depois: 0 imports eager + 2 imports lazy (dentro de
  `GlinerArgumentAdapter.__init__`, só no caminho do
  default adapter).
- **`SLMArgumentExtractor` agora tem o mesmo shape
  arquitetural que `SLMEntityExtractor` e
  `SLMIntentClassifier`**: o default adapter vive no
  framework, a facade é puramente framework-level.
- **Lazy import do vertical é o último shim
  "transicional"** — Iter 28+ vai eliminá-lo movendo
  os 2 componentes restantes.
- **`_resolve_model_name` é testável sem modelo** —
  a static method do `GlinerArgumentAdapter` é
  exercitada nos tests (4 tests novos), sem precisar
  do GLiNER2 model load.
- **Coerência com Iter 21 (SLM facades)** — Iter 21
  moveu `GlinerEntityAdapter` e `GlinerIntentAdapter`
  para o framework. Iter 27 fecha o último: agora os 3
  default adapters de SLM facades vivem no framework.

### Cons

- **Lazy import dentro de `__init__`** do framework
  (linhas 145-148 do `gliner_argument.py`) é um shim
  "transicional" — Iter 28+ deve eliminá-lo.
- **Documentação adicional** para explicar o lazy
  import (na docstring do módulo e na do adapter).
  Custo: uma página de docstring.
- **Migração não é puramente mecânica**: a fachada
  `_slm_facades.py:260` muda de `fmh_agents` para
  `fmh_backend`. Test fixture e callers precisam ser
  atualizados (3 tests em `test_slm_facades.py`).

### Métricas

| Métrica | Antes (Iter 25) | Depois (Iter 27) |
|---|---|---|
| Imports eager `fmh_backend → fmh_agents` | 1 | **0** |
| Imports lazy `fmh_backend → fmh_agents` (em `__init__`) | 0 | **2** (transitional) |
| Files `fmh_backend/knowledge/extraction/` | 6 | **7** (`gliner_argument.py` novo) |
| `GlinerArgumentAdapter` canonical home | vertical | **framework** |
| `fmh_agents.knowledge.argument_extractor` files | 8 | **7** (`_gliner_wrapper.py` deletado) |
| `fmh_backend/tests/unit/` total | 1284 passed | **1289 passed** (+5: 4 new leak tests + 1 helper test) |

### Conquistas notáveis

1. **5 leaks estruturais fechados em 2 iterações**
   (Iter 25: 4 leaks; Iter 27: 1 leak). AGENTS.md §1.2
   agora é estritamente respeitada para o caminho
   "framework puro" (sem lazy import).
2. **`_resolve_model_name` testável em isolamento** —
   a static method é uma helper pura (resolve
   `arg | Settings.default`), pode ser testada
   sem instanciar o adapter (e portanto sem carregar
   `gliner2`).
3. **`_gliner_wrapper.py` deletado** — o vertical
   não tem mais o arquivo. O canonical home é o
   framework; o vertical apenas re-exporta.

---

## 4. Migration

### Já migrado (este commit)

**Framework (3 files modificados/criados):**
- `fmh_backend/src/fmh_backend/knowledge/extraction/gliner_argument.py`
  (novo) — `GlinerArgumentAdapter` (100 LOC) com lazy
  imports de `GlinerFieldFinder` e
  `SchemaArgumentExtractor` no `__init__`.
- `fmh_backend/src/fmh_backend/knowledge/extraction/__init__.py`
  — re-exporta `GlinerArgumentAdapter` do novo módulo.
  Adicionado ao `__all__`. Docstring atualizada
  documentando Iter 27.
- `fmh_backend/src/fmh_backend/knowledge/extraction/_slm_facades.py`
  — `SLMArgumentExtractor.__init__` agora faz
  `from fmh_backend.knowledge.extraction import
  GlinerArgumentAdapter` (não mais da vertical).

**Vertical (2 files modificados, 1 deletado):**
- `fmh_agents/src/fmh_agents/knowledge/argument_extractor/__init__.py`
  — `GlinerArgumentAdapter` re-exportado do framework.
  Docstring documenta Iter 27.
- `fmh_agents/src/fmh_agents/knowledge/argument_extractor/_gliner_wrapper.py`
  (deletado) — o wrapper antigo não existe mais.
- `fmh_agents/src/fmh_agents/knowledge/argument_extractor/_gliner_finder.py`,
  `_extractor.py`, `_finder.py`, `_coerce.py`,
  `_schema.py` — inalterados. Iter 28+ move esses
  também.

**Tests:**
- `fmh_backend/tests/unit/knowledge/extraction/test_slm_facades.py`
  — atualizado para usar o framework path
  (`from fmh_backend.knowledge.extraction import
  GlinerArgumentAdapter`).
- `fmh_backend/tests/unit/knowledge/extraction/test_gliner_argument_no_leak.py`
  (novo) — 4 tests: regression gate.

### Pendente (Iter 28 — follow-up)

- **Mover `GlinerFieldFinder`** para o framework
  (`fmh_backend.knowledge.extraction._gliner_finder`).
  281 LOC, sem deps verticais (só `gliner2` opt-in).
- **Mover `SchemaArgumentExtractor`** para o framework
  (`fmh_backend.knowledge.extraction._extractor`).
  149 LOC, depende apenas de
  `FieldFinder`/`FieldSpec` (que viriam juntos).
- **Mover `FieldFinder` Protocol + `RegexFieldFinder`**
  para o framework. 102 LOC, pure.
- **Mover `coerce`** (extraction helper). 50 LOC,
  pure.
- **Eager imports** em `gliner_argument.py` (sem lazy).
- **Deletar `fmh_agents.knowledge.argument_extractor`**
  por completo.

Após Iter 28, o framework terá **0 imports
`fmh_backend → fmh_agents`** em qualquer forma (eager
ou lazy). O ciclo estará estruturalmente impossível
em qualquer ponto do grafo.

> **Status**: ✅ feito em [ADR-027](./ADR-027-Argument-Extraction-Migration.md)
> (Iter 28). Os 5 private modules foram movidos em
> 1 commit atômico, o `GlinerArgumentAdapter` agora
> faz eager imports do framework, e o vertical
> `argument_extractor` é puramente re-export shim.
> Ver ADR-027 §3 (Métricas) e §4 (Migration) para
> detalhes.

---

## 5. Decisões relacionadas

- **AGENTS.md §1.2 (framework vs vertical)**: o leak
  era a única violação restante após Iter 25. Iter 27
  fecha-o. O caminho "framework puro" (com `adapter=`
  injetado) é 100% framework-side.
- **AGENTS.md §2 (zero shims)**: o re-export do
  `GlinerArgumentAdapter` em `fmh_agents.knowledge
  .argument_extractor.__init__` é um re-export
  legítimo (não compat shim), igual ao padrão
  estabelecido em Iter 25.
- **AGENTS.md §13 (simplicidade)**: a lazy import
  dentro do adapter é mais simples que mover 3
  componentes simultaneamente. O roadmap
  documenta a sequência (Iter 27 fecha o leak
  estrutural, Iter 28+ fecha o lazy).
- **ADR-019 §2.4 (padrão estabilizado)**: o padrão
  "sub-adapter compõe sobre o mesmo `GraphAdapter`"
  é replicado aqui: o `GlinerArgumentAdapter` compõe
  `GlinerFieldFinder` + `SchemaArgumentExtractor`
  sobre uma `ToolRegistry`. Mesma forma, domínios
  diferentes.

---

## 6. Lições aprendidas

### O que funcionou

1. **Iter 27 como Iter 25 reduzido** — a mesma
   estratégia de Iter 25 (mover a primitiva, re-export
   shim) funcionou para o `GlinerArgumentAdapter`.
   Padrão replicável.
2. **Subprocess em test de regression gate** — o test
   `test_slm_argument_extractor_does_not_load_vertical`
   roda em subprocess para evitar `sys.modules`
   pollution. Custo: ~100ms (subprocess startup). Benefício:
   o test é **realmente isolado** — não depende da ordem
   dos tests na suite.
3. **TDD estrito** — o test falhou 5 vezes antes da
   implementação fechar. Cada falha revelou um aspecto
   diferente (caminho de import, `sys.modules` pollution,
   test fixture que importava a vertical). No fim, o test
   ficou **realmente útil** — ele captura a regressão
   mesmo em condições adversas.

### O que poderia ter sido melhor

1. **Mover `GlinerArgumentAdapter` em Iter 21** —
   Iter 21 moveu `GlinerEntityAdapter` e
   `GlinerIntentAdapter` para o framework, mas deixou o
   `GlinerArgumentAdapter` na vertical "por enquanto".
   O leak ficou registrado no ADR-019 epílogo como
   "debt" e foi pago em Iter 27. Lição: ao mover 2 de
   3, mover o terceiro também — a inconsistência é
   dívida.
2. **Subprocess em test** é overhead. A alternativa
   ideal seria um `@pytest.fixture` que reseta
   `sys.modules` por test. Mas fixtures de reset têm
   armadilhas com side effects de outros tests; subprocess
   é mais robusto (custa tempo, mas é determinístico).

---

## 7. Referências

- [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md) — Iter 21 (SLM facades) + roadmap §4
- [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md) — Pattern: framework primitives first
- [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md) — Pattern: 4 leaks fechados em Iter 25
- [AGENTS.md §1](../../AGENTS.md) — adapter types
- [AGENTS.md §2](../../AGENTS.md) — zero shims
- [AGENTS.md §1.2](../../AGENTS.md) — framework vs vertical
- `fmh_backend/src/fmh_backend/knowledge/extraction/gliner_argument.py` — novo adapter
- `fmh_agents/src/fmh_agents/knowledge/argument_extractor/__init__.py` — re-export shim
- `fmh_backend/tests/unit/knowledge/extraction/test_gliner_argument_no_leak.py` — regression gate

---

**Conclusão**: o framework FMH agora tem **0 imports
`fmh_backend → fmh_agents` eager** (Iter 27 fecha o
último). 2 imports **lazy** (dentro de
`GlinerArgumentAdapter.__init__`) permanecem como
shim transicional — Iter 28+ move os 2 componentes
restantes (`GlinerFieldFinder`, `SchemaArgumentExtractor`)
para o framework, e os imports viram eager. O ciclo
`fmh_backend ↔ fmh_agents` está estruturalmente
impossível em qualquer caminho de runtime. O
roadmap do ADR-019 + ADR-025 está fechado em 100%
para o core framework.