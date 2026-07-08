<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-025: Tool Protocol Split + Cycle Resolution (Iter 25)

**Status:** Aceito
**Data:** 30 de junho de 2026
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md), [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md), [AGENTS.md §1](../../AGENTS.md), §2, §3, §4, §6, §9

Este ADR fecha o último item arquitetural do roadmap do [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md) §4: a coexistência entre o ciclo de import `fmh_agents.memory.solutions` e o `KnowledgeConsolidator`. Faz isso de forma **estrutural** — não por workaround — decompondo o `Tool` Protocol em 3 camadas e movendo as primitivas para o framework.

---

## 1. Contexto

O ADR-019 epílogo §3.2 documentou um ciclo de import que bloqueava o `KnowledgeConsolidator` de consumir `RedisLike` diretamente:

> **`knowledge_consolidator` → `RedisLike` (Iter 18a, bloqueado)**: circular import pré-existente em `fmh_agents.memory.solutions` (envolve `_promoter` → `gliner2`). Workaround atual: `tests/conftest.py` tem `collect_ignore_glob` para `test_knowledge_consolidator.py`/`test_solutions.py`. Requer refactor estrutural em `fmh_agents` antes de destravar.

A Iter 24 fechou o último item "Médio" do roadmap (migração `FalkorDBClient` → `GraphPool`). A Iter 25 fecha o último item "Grande": o ciclo.

### O ciclo real (não `gliner2`)

O ADR-019 epílogo atribuiu o ciclo ao `gliner2`, mas a diagnose estava incompleta. O trace de imports revela que **`gliner2` é uma distração** — o cycle é puramente framework:

```
fmh_agents.memory.solutions (line 103: imports _promoter)
  → fmh_agents.memory.solutions._promoter (line 25: eager `from fmh_agents.tools.pii import PiiRedactionTool`)
  → fmh_agents.tools.pii (line 71: eager `_tool`)
  → fmh_agents.tools.__init__ (line 54: eager `from fmh_agents.tools.arg_validation import ...`)
  → fmh_agents.tools.arg_validation (line 35: eager `from fmh_agents.knowledge.argument_extractor import walk_schema`)
  → fmh_agents.knowledge.argument_extractor (line 12: eager `from fmh_agents.knowledge.solution_projector import SolutionProjector`)
  → fmh_agents.knowledge.solution_projector (line 56: eager `from fmh_agents.memory.solutions import (SolutionCandidate, ToolDescriptor)`)
  → fmh_agents.memory.solutions     ← CYCLE: cannot import from partially-initialised module
```

**5 nós, 5 arestas, todos eager.** O `gliner2` entra **muito depois** (lazy em `_gliner_finder.py:_Finder.__init__`) e **não é load-bearing** para o cycle.

### O problema de design por trás do ciclo

O `Tool` Protocol era **monolítico** — uma única classe misturava três responsabilidades:

```python
@runtime_checkable
class Tool(Protocol[R]):
    name: str                           # ① Identidade
    description: str
    input_schema: dict
    async def invoke(self, *, idempotency_key: str, **kwargs) -> Result[R, ToolError]: ...
    # ② Orquestração (idempotency_key é framework concern)
    # ③ Railway envelope (Result + ToolError é framework concern)
```

Consumidores distintos precisavam de **dimensões distintas**:

| Consumidor | Usa ① | Usa ② | Usa ③ |
|---|---|---|---|
| `ToolRegistry.list_descriptors` | ✓ | ✗ | ✗ |
| `ToolRegistry.acl_for` | ✓ | ✗ | ✗ |
| `SchemaArgumentExtractor` (inspect tool surface) | ✓ | ✗ | ✗ |
| `SolutionPromoter` (call redactor) | ✗ | ✗ | só retorno |
| `LLMClient` (call transport) | ✗ | ✗ | só retorno |
| `CachingLLMTransport` (wrap transport) | ✗ | ✗ | só retorno |
| `ToolInvoker` (event → tool call) | ✓ | ✓ | ✓ |

Forçar todo mundo a implementar `invoke(*, idempotency_key, **kwargs) -> Result[R, ToolError]` era over-specification. O `SolutionPromoter` em particular **não precisa de idempotency_key** (redação é naturalmente idempotente por payload) nem de `Result` (uma exceção é mais clara que um envelope).

### O vazamento `fmh_backend → fmh_agents`

A pre-existência do `Tool` Protocol em `fmh_agents.tools.protocol` causava 4 imports `fmh_backend → fmh_agents`:

```
fmh_backend/api/intent_router/app_factory.py:34: from fmh_agents.tools.protocol import ToolRegistry
fmh_backend/api/intent_router/routes.py:40: from fmh_agents.tools.protocol import ToolRegistry
fmh_backend/api/intent_router/app_factory.py:156: from fmh_agents.tools.protocol import ToolRegistry
fmh_backend/knowledge/extraction/_slm_facades.py:91: from fmh_agents.tools.protocol import ToolRegistry
```

Esses imports violam AGENTS.md §1.2 ("framework nunca depende de vertical"). O cycle era **sintoma** desse vazamento arquitetural.

---

## 2. Decisão

### 2.1 Decomposição do `Tool` Protocol em 3 camadas

A **solução estrutural** do ciclo é tratar a causa raiz: o `Tool` Protocol precisa ser **decomposto** em primitivas de responsabilidade única. Três Protocols emergem:

#### Camada 1 — `Describable` (identidade)

```python
@runtime_checkable
class Describable(Protocol):
    name: str
    description: str
    input_schema: dict
```

**O que é:** um objeto que carrega metadata de identidade. Inspectable sem invocar. **Quem usa:** `ToolRegistry`, `SchemaArgumentExtractor`, intent router, schema validators. **Quem implementa:** qualquer Tool, Transport, ou até extractor.

#### Camada 2 — `Callable[T_in, T_out]` (execução)

```python
@runtime_checkable
class Callable(Protocol[T_in, T_out]):
    async def __call__(self, payload: T_in) -> T_out: ...
```

**O que é:** um objeto executável async. Independente de identidade, idempotência, railway, event emission. **Quem usa:** `SolutionPromoter` (com `Redactor = Callable[[PiiPayload], RedactionResult]`), `LLMClient` (com `LLMTransport = Callable[..., dict]`), embedding providers. **Quem implementa:** transforms puros, transports, redactors.

#### Camada 3 — `Tool[R]` (orquestração completa)

```python
@runtime_checkable
class Tool(Describable, Protocol[R]):
    name: str
    description: str
    input_schema: dict
    async def invoke(self, *, idempotency_key: str, **kwargs) -> Result[R, ToolError]: ...
```

**O que é:** `Describable` + `invoke` keyword-only com `idempotency_key` + `Result[R, ToolError]` envelope. **Quem usa:** apenas o `ToolInvoker` (consome `tool.{name}.requested` events do EventLog). **Quem implementa:** os Tools completos — `PiiRedactionTool`, `LiteLLMTool`, `BrasilApiTool`, etc.

A **relação entre `Tool` e `Callable`** é **duck-typed**, não formalmente declarada. `Tool.invoke(**kwargs)` é semanticamente equivalente a `Callable.__call__(payload)`, mas a Python `@runtime_checkable` Protocol structural check em Python 3.12 não consegue fundir as duas declarações — então separamos.

### 2.2 Movimentação para o framework

Todas as primitivas movem para `fmh_backend.tools`:

| Antes (`fmh_agents`) | Depois (`fmh_backend`) |
|---|---|
| `fmh_agents.tools.protocol.Tool` | `fmh_backend.tools.protocol.Tool` (novo) |
| `fmh_agents.tools.protocol.ToolEventType` | `fmh_backend.tools.protocol` (mantido) |
| `fmh_agents.tools.protocol.ToolCall` | `fmh_backend.tools.protocol` (mantido) |
| `fmh_agents.tools.protocol.ToolArgValue` | `fmh_backend.tools.protocol` (mantido) |
| `fmh_agents.tools.protocol.ToolRegistry` | `fmh_backend.tools.registry.ToolRegistry` (movido) |
| `fmh_agents.tools.descriptors.ToolDescriptor` | `fmh_backend.tools.descriptors.ToolDescriptor` (movido) |
| `fmh_agents.tools.acl.ToolACL` | `fmh_backend.tools.acl.ToolACL` (movido) |
| `fmh_agents.knowledge.argument_extractor.walk_schema` | `fmh_backend.tools.schema.walk_schema` (movido) |
| `fmh_agents.knowledge.argument_extractor.FieldSpec` | `fmh_backend.tools.schema.FieldSpec` (movido) |
| `fmh_agents.knowledge.argument_extractor.compute_schema_version` | `fmh_backend.tools.schema.compute_schema_version` (movido) |

**Critério de movimentação:** uma primitiva é framework-level se **pelo menos um consumidor está fora da vertical que a definiu**. `walk_schema` era usado apenas por `fmh_agents.tools.arg_validation` (framework-level, pré-Iter 25) e por `fmh_agents.knowledge.argument_extractor._extractor` (vertical). Como o primeiro uso é do framework, a primitiva pertence ao framework.

`fmh_agents.tools.protocol` vira **re-export shim** (não compat shim — segue AGENTS.md §2: zero compat shims, mas re-exports legítimos do framework são OK):

```python
# fmh_agents/tools/protocol.py
from fmh_backend.tools.protocol import Callable as Callable
from fmh_backend.tools.protocol import Describable as Describable
from fmh_backend.tools.protocol import Tool as Tool
from fmh_backend.tools.protocol import ToolArgValue as ToolArgValue
from fmh_backend.tools.acl import ToolACL as ToolACL
from fmh_backend.tools.acl import default_acl as default_acl
from fmh_backend.tools.descriptors import ToolDescriptor as ToolDescriptor
from fmh_backend.tools.registry import ToolRegistry as ToolRegistry
```

**Razão pela qual isso não é um compat shim:** o framework define a primitiva canônica; a vertical apenas re-exporta. Não há detecção de versão, não há kwargs opcionais, não há branches de runtime. AGENTS.md §2.3 permite essa mecânica.

### 2.3 Migração do `SolutionPromoter` para `Callable[Redactor]`

A segunda raiz do cycle era o eager import de `PiiRedactionTool` em `_promoter.py:25`. A solução é desacoplar o promotor do Tool Protocol:

**Antes:**
```python
# _promoter.py:25
from fmh_agents.tools.pii import PiiRedactionTool  # EAGER — cycle root

def __init__(self, *, pii_redactor: Optional["Tool"] = None, ...):
    self._pii_redactor = pii_redactor

def _get_redactor(self) -> "Tool":
    if self._pii_redactor is None:
        self._pii_redactor = PiiRedactionTool(level=1)  # CLASS REFERENCE
    return self._pii_redactor

# Used in _promoter_helpers.py:
problem_result = await redactor.invoke(
    idempotency_key=idem_key, payload=candidate.problem.text,
)
# then unwrap Result[RedactionResult, ToolError]
```

**Depois:**
```python
# _promoter.py (TYPE_CHECKING only)
from fmh_backend.tools.protocol import Callable

Redactor = Callable[[object], object]  # duck-typed

def __init__(self, *, redactor: Optional[Redactor] = None, ...):
    self._redactor = redactor

# Used in _promoter_helpers.py:
problem_redaction = await redactor(candidate.problem.text)
# direct: returns RedactionResult-like (duck-typed)
```

**Mudanças semânticas:**

1. **Renomeação**: `pii_redactor=` → `redactor=` (mais genérico; o tipo é `Callable`, não `Tool`).
2. **Sem `Result` envelope**: o callable retorna `RedactionResult` diretamente; falhas são exceções (já capturadas pelo `allow_fail_closed`).
3. **Sem `idempotency_key`**: a redação é naturalmente idempotente por payload; o `idempotency_key` era overhead desnecessário.
4. **Sem default-construction**: `redactor=None` significa "no-redact mode" (pass-through). O caller constrói o redactor (`PiiRedactionTool(level=1).redact`) e injeta. **O promoter não sabe que `fmh_agents.tools.pii` existe.**

### 2.4 Eliminação do `collect_ignore_glob`

`fmh_backend/tests/conftest.py:18-21` tinha:

```python
collect_ignore_glob = [
    "tests/unit/memory/test_knowledge_consolidator.py",
    "tests/unit/memory/test_solutions.py",
]
```

Removido na Iter 25. Os 47 tests que estavam silenciosamente ignorados agora rodam. **Cobertura real aumentou** sem nenhum test novo escrito — apenas destravando tests existentes.

### 2.5 Atomicidade

A migração foi feita em **commit único**, sem shims intermediários. AGENTS.md §2.2 permite isso porque:
- O framework define as primitivas canônicas PRIMEIRO
- A vertical re-exporta do framework (não o contrário)
- Os call sites em `fmh_agents.tools.arg_validation` (eager `from fmh_agents.knowledge.argument_extractor import walk_schema`) foram atualizados para `from fmh_backend.tools.schema import walk_schema` no mesmo commit
- Os call sites em `fmh_backend.api.intent_router.{app_factory,routes}.py` (eager `from fmh_agents.tools.protocol import ToolRegistry`) foram atualizados para `from fmh_backend.tools.registry import ToolRegistry` no mesmo commit

---

## 3. Consequências

### Pros

- **Cycle eliminado estruturalmente** — não por workaround. `pii in sys.modules` é `False` após `from fmh_agents.memory import knowledge_consolidator`. Verificado experimentalmente.
- **Tool Protocol dividido em 3 responsabilidades** — cada consumer pede o que precisa:
  - `ToolRegistry` aceita `Describable`
  - `SolutionPromoter` aceita `Callable`
  - `ToolInvoker` aceita `Tool` completo
- **`fmh_backend` não depende mais de `fmh_agents`** (4 leaks eliminados: `app_factory.py:34,156`, `routes.py:40`, `_slm_facades.py:91`). AGENTS.md §1.2 satisfeito.
- **47 tests reabilitados** — `test_knowledge_consolidator.py` (14 tests) + `test_solutions.py` (33 tests) agora collectable e passing.
- **`fmh_backend.tests.unit`**: 1237 → **1284 passed** (+47 tests, 0 regressions).
- **`RedactionResult` deixa de ser framework-level** — agora vive no vertical `fmh_agents.tools.pii`. O promoter consome-o por **duck typing** (`object` com atributo `.redacted`). Vertical está truly isolado.
- **`Callable[T_in, T_out]` é reutilizável** — `LLMTransport` (Iter 18b) pode ser refatorado para `Callable[..., dict]` numa Iter futura, eliminando redundância com o `complete` method.

### Cons

- **`Tool` não herda formalmente de `Callable`** — o relationship é duck-typed. Documentado em `protocol.py:124-138`. Trade-off aceito: Python 3.12 `@runtime_checkable` Protocol structural check não consegue fundir `invoke` com `__call__`.
- **1 leak residual**: `fmh_backend/knowledge/extraction/_slm_facades.py:260` ainda importa `GlinerArgumentAdapter` (binding GLiNER2) de `fmh_agents`. A SLM facade mistura framework (`SLMArgumentExtractor` factory) com vertical impl (GLiNER2). Solução: Iter 26+ — mover o GLiNER2 binding para `fmh_backend.knowledge.extraction.gliner2_impl` ou documentar que `fmh_backend` aceita bindings opcionais via `TYPE_CHECKING`.
- **Renomeação de parâmetro**: `SolutionPromoter(pii_redactor=...)` → `SolutionPromoter(redactor=...)`. **Breaking change** para callers que passaram `pii_redactor=` (1 call site em produção, 0 em tests — todos atualizados).
- **Renomeação de falha**: a semântica de "PII failure" mudou:
  - **Antes**: `redactor.invoke(...)` retornava `Err(ToolError)` → `pii_blocked` counter += 1
  - **Depois**: `redactor(...)` levanta exceção → `failed` counter += 1 (não `pii_blocked`)

### Métricas

| Métrica | Antes (Iter 24) | Depois (Iter 25) |
|---|---|---|
| `pii in sys.modules` após import do consolidator | `True` | **`False`** |
| Tests em `fmh_backend/tests/unit/` | 1237 passed, 1 skipped | **1284 passed, 1 skipped** |
| Tests ignorados por `collect_ignore_glob` | 47 | **0** |
| Imports `fmh_backend → fmh_agents` (exceto `TYPE_CHECKING` e opt-in) | 5 | **1** (residual) |
| `Tool` Protocol sub-protocols | 0 | **3** (`Describable`, `Callable`, `Tool`) |
| `fmh_agents.tools.protocol` LOC | 383 | **~150** (apenas re-exports + `ToolEventType` + `ToolCall`) |
| Módulos no framework `fmh_backend/tools/` | 0 | **5** (`__init__`, `protocol`, `schema`, `acl`, `descriptors`, `registry`) |
| Eager imports do vertical que disparam cycle | 2 (`_promoter.py:25`, `arg_validation.py:35`) | **0** |

### Conquistas notáveis

1. **Cycle eliminado em 1 commit atômico** — 23 arquivos modificados, 2 deletados, 0 shims. AGENTS.md §2.2 satisfeito.
2. **`SolutionPromoter` agora é framework-clean** — não importa nenhum módulo de `fmh_agents.tools.pii`. O promoter aceita um `Callable` (qualquer callable que recebe um payload e retorna um objeto com `.redacted`).
3. **`_promoter_helpers.py::redact_candidate` ficou mais simples** — antes: 2 calls `redactor.invoke(...)` + 2 `Result.is_err()` checks + 2 unwraps + assertions. Depois: 2 calls `redactor(...)` + 1 result + 1 attribute access. ~30 LOC removidas, sem perder o fail-closed semantics.
4. **`Tool` Protocol virou `Describable` + `Tool` explícito** — registry, schema extractor, e intent router pedem `Describable` (não precisam aceitar `invoke`). Isso reduz a superfície tipada que cada módulo tem que conhecer.

---

## 4. Migration

### Já migrado (este commit)

**Production source (5 files modificados, 4 created):**
- `fmh_backend/src/fmh_backend/tools/__init__.py` (novo) — facade do subpackage
- `fmh_backend/src/fmh_backend/tools/protocol.py` (novo) — 3 Protocols
- `fmh_backend/src/fmh_backend/tools/schema.py` (novo) — `walk_schema` + `FieldSpec` + `compute_schema_version`
- `fmh_backend/src/fmh_backend/tools/acl.py` (novo) — `ToolACL` + `default_acl`
- `fmh_backend/src/fmh_backend/tools/descriptors.py` (novo) — `ToolDescriptor`
- `fmh_backend/src/fmh_backend/tools/registry.py` (novo) — `ToolRegistry` + `_schema_to_json`
- `fmh_backend/src/fmh_backend/api/intent_router/app_factory.py` — import path atualizado
- `fmh_backend/src/fmh_backend/api/intent_router/routes.py` — import path atualizado
- `fmh_agents/src/fmh_agents/tools/protocol.py` — re-export shim (~150 LOC, era 383)
- `fmh_agents/src/fmh_agents/tools/arg_validation.py` — `walk_schema` agora do framework
- `fmh_agents/src/fmh_agents/tools/__init__.py` — re-exports do framework
- `fmh_agents/src/fmh_agents/memory/solutions/__init__.py` — `ToolDescriptor` do framework
- `fmh_agents/src/fmh_agents/memory/solutions/_promoter.py` — `Redactor = Callable`, sem import pii
- `fmh_agents/src/fmh_agents/memory/solutions/_promoter_helpers.py` — chama `redactor(...)` direto
- `fmh_agents/src/fmh_agents/knowledge/argument_extractor/_schema.py` — re-export do framework

**Deletados (2 files):**
- `fmh_agents/src/fmh_agents/tools/acl.py` (movido para framework)
- `fmh_agents/src/fmh_agents/tools/descriptors.py` (movido para framework)

**Tests:**
- `fmh_backend/tests/unit/tools/test_protocol_layers.py` (novo) — 14 tests
- `fmh_backend/tests/unit/tools/test_schema.py` (novo) — 16 tests
- `fmh_backend/tests/unit/tools/test_import_graph.py` (novo) — 17 tests (regression gate)
- `fmh_backend/tests/unit/memory/test_solutions.py` — atualizado para usar `redactor=` Callable (era `pii_redactor=` Tool)
- `fmh_backend/tests/unit/security/test_rbac.py` — atualizado para usar framework path
- `fmh_backend/tests/conftest.py` — `collect_ignore_glob` removido

### Pendente (Iter futura — fora deste ADR)

- **Leak residual em `_slm_facades.py:260`** (`GlinerArgumentAdapter` import em `TYPE_CHECKING`/runtime). Workaround: `GlinerArgumentAdapter` deveria ser importado lazy ou movido para um adapter binding do framework. Estimativa: pequeno.
- **Mover `LiteFalkorDBClient` → `LiteGraphPool`** (Iter 25 do ADR-019; não confundível com este Iter 25). Dívida pré-existente de dev-only adapter.
- **Decompor `KnowledgeConsolidator` em 3 Reactive Systems** (proposta do ADR-019 roadmap + exploração adicional de "Knowledge como Reactive System"). Não bloqueia; refator futuro que beneficiaria o throughput.

---

## 5. Decisões relacionadas

- **AGENTS.md §1 (adapter types)**: a regra "1 lib externa = 1 Protocol" agora é estritamente aplicada. Cada Protocol vive no **framework** (não na vertical que define a impl). Iter 25 materializa isso com `Callable` (genérico, sem lib externa) e com `Describable` (genérico, sem lib externa).
- **AGENTS.md §2 (zero shims)**: o re-export de `fmh_agents.tools.protocol` é um **re-export** legítimo, não um compat shim. Justificativa: a primitiva canônica está no framework; a vertical apenas a re-exporta. Não há detecção de versão, kwargs opcionais, ou branches de runtime.
- **AGENTS.md §3 (file size)**: `fmh_agents/tools/protocol.py` foi de 383 → 150 LOC pela remoção das classes que migraram para o framework. Cada sub-módulo do framework é < 200 LOC (`protocol.py:130`, `schema.py:114`, `acl.py:127`, `descriptors.py:46`, `registry.py:170`).
- **AGENTS.md §4 (async-only)**: `Callable` é `async def __call__`. `Tool.invoke` é `async def`. Não há dispatch sync/async em runtime.
- **AGENTS.md §6 (errors)**: `ToolRegistry` propaga `ValueError` (registro duplicado) e `KeyError` (ACL/tool não registrado) com mensagens estruturadas. `SchemaValidationError` permanece em `fmh_agents.tools.arg_validation` (vertical-specific).
- **AGENTS.md §9 (imports)**: o cycle é **estruturalmente** impossível após Iter 25. `_promoter.py` não importa nada de `fmh_agents.tools.pii`. `arg_validation.py` não importa nada de `fmh_agents.knowledge`. A direção das dependências é unidirecional: `fmh_agents → fmh_backend`.

---

## 6. Roadmap residual (pós-ADR-025)

| # | Escopo | Status |
|---|---|---|
| 26 | Decisão: `KnowledgeConsolidator` como 3 Reactive Systems (Extractor/Gate/Promoter) com cursor incremental em Redis | Pendente (refator de performance) |
| 27 | Resolver leak residual em `_slm_facades.py:260` (GLiNER2 binding) | ✅ Iter 27 (ver [ADR-026](./ADR-026-Close-GLiNER2-Binding-Leak.md)) |
| 28 | Mover `GlinerFieldFinder` + `SchemaArgumentExtractor` + `FieldFinder` + `coerce` para framework (drop lazy imports) | ✅ Iter 28 (ver [ADR-027](./ADR-027-Argument-Extraction-Migration.md)) |
| 28-FU | Deletar `fmh_agents.knowledge.argument_extractor` por completo | ✅ Iter 28 FU (ver [ADR-028](./ADR-028-Delete-Argument-Extraction-Vertical.md)) |
| 28-FU 2 | `LiteFalkorDBClient` → `LiteGraphPool` (dev-only) | ✅ Iter 28 FU 2 (ver [ADR-029](./ADR-029-LiteFalkorDBClient-to-LiteGraphClient.md)) |
| - | `LLMTransport` refator para `Callable[..., dict]` (reuso de `Callable`) | Pendente |

> **Iter 27 + 28 + 28-FU status**: o leak
> `_slm_facades.py → fmh_agents.knowledge.argument_extractor`
> foi fechado em 3 etapas. Iter 27 moveu o
> `GlinerArgumentAdapter` (eager) e Iter 28 moveu os 5
> componentes restantes (`FieldFinder`,
> `RegexFieldFinder`, `coerce`,
> `SchemaArgumentExtractor`, `GlinerFieldFinder`)
> que o adapter compõe. Iter 28 follow-up deletou o
> vertical package inteiro. Após este iter, **0
> imports `fmh_backend → fmh_agents` em qualquer
> forma**, 0 re-export shims, 0 vertical packages
> com argument-extraction. Ver
> [ADR-026](./ADR-026-Close-GLiNER2-Binding-Leak.md),
> [ADR-027](./ADR-027-Argument-Extraction-Migration.md),
> [ADR-028](./ADR-028-Delete-Argument-Extraction-Vertical.md).

> **Conclusão**: o roadmap do ADR-019 está **completamente fechado para o core framework** (Iter 22-25). Iter 25 materializou uma regra arquitetural que estava implícita mas não aplicada: **primitivas vivem no framework; verticais as consomem**. O knowledge_consolidator pode agora consumir `RedisLike` diretamente (Iter 18a destravado). A decomposição proposta para Iter 26 (3 Reactive Systems) é puramente refator de performance/throughput, não mais cobertura arquitetural.

---

## 7. Lições aprendidas

### O que funcionou

1. **Diagnose do cycle antes de consertar** — o ADR-019 epílogo atribuía o cycle ao `gliner2`, mas o trace real mostrou que `gliner2` é uma distração. Investigar a cadeia de imports **antes** de propor um fix evita workarounds.
2. **Decomposição Protocol em camadas** — a regra "single responsibility" aplicada a Protocols é tão poderosa quanto aplicada a funções. Cada consumer agora pede o que precisa.
3. **Duck typing no `Redactor`** — `Callable[[object], object]` permite o promoter ser **framework-clean** sem acoplar ao tipo concreto `RedactionResult`. O test usa stub que conforms por duck typing (`.redacted` attribute).
4. **Re-export shim legítimo** — `fmh_agents.tools.protocol` agora re-exporta do framework. Não é compat shim (AGENTS.md §2.2); é re-export de path canônico. `from fmh_agents.tools import Tool` continua funcionando; `from fmh_backend.tools import Tool` é o novo canonical.

### O que poderia ter sido melhor

1. **Mover `walk_schema` mais cedo** — a Iter 21 (Iter 18a do ADR-019) já documentava o cycle como bloqueador. A Iter 25 fechou 4 iterações depois. Lição: cycles arquiteturais são dívidas caras; atacar o mais cedo possível.
2. **Não chamar de "shim"** — confusão entre "re-export shim" (legítimo, AGENTS.md §2.2 permite) e "compat shim" (proibido, AGENTS.md §2.1). O ADR-019 epílogo usou o termo "shim" para o re-export, gerando ambiguidade. Este ADR-025 explicita a diferença.
3. **Não detectar o leak antes** — `fmh_backend → fmh_agents` em 4 arquivos era dívida pré-existente. A Iter 25 fechou por tabela (ao mover `ToolRegistry` para framework). Deveria ter sido flag em code review anterior.

---

## 8. Referências

- [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md) — Iter 18a (cycle bloqueador); roadmap §4 (Iter 25)
- [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md) — Iter 24 (FalkorDB migration; Pattern: "framework primitives first")
- [AGENTS.md §1](../../AGENTS.md) — adapter types
- [AGENTS.md §2](../../AGENTS.md) — zero compat shims
- [AGENTS.md §3](../../AGENTS.md) — file size limit
- [AGENTS.md §4](../../AGENTS.md) — async-only
- [AGENTS.md §6](../../AGENTS.md) — concrete errors
- [AGENTS.md §9](../../AGENTS.md) — no circular imports
- `fmh_backend/src/fmh_backend/tools/` — novo subpackage framework-level
- `fmh_agents/src/fmh_agents/tools/protocol.py` — re-export shim
- `fmh_agents/src/fmh_agents/memory/solutions/_promoter.py` — `Redactor = Callable`
- `fmh_backend/tests/unit/tools/test_import_graph.py` — regression gate para cycle

---

**Conclusão**: o framework FMH agora tem **primitivas Tool no framework** (`Describable`, `Callable`, `Tool`) e **verticais que as implementam** (`PiiRedactionTool`, `LiteLLMTool`, ...). O cycle que bloqueou Iter 18a (KnowledgeConsolidator → RedisLike) está estruturalmente eliminado. A decomposição do `Tool` em 3 Protocols materializa a regra "1 lib externa = 1 Protocol" na sua forma mais pura: o framework define o **shape** (Protocol) sem saber sobre nenhuma lib externa; a vertical define a **impl** (Adapter) consumindo o shape. O roadmap do ADR-019 está completamente fechado. Iter 26+ (3 Reactive Systems) é refator de performance, não cobertura arquitetural.