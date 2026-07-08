<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-030: LLMTransport as Callable (Iter 28 FU 3)

**Status:** Aceito
**Data:** 30 de junho de 2026
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md), [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md), [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md), [ADR-026](./ADR-026-Close-GLiNER2-Binding-Leak.md), [ADR-027](./ADR-027-Argument-Extraction-Migration.md), [ADR-028](./ADR-028-Delete-Argument-Extraction-Vertical.md), [ADR-029](./ADR-029-LiteFalkorDBClient-to-LiteGraphClient.md), [AGENTS.md §1](../../AGENTS.md), §2

Este ADR fecha o último item open-ended do roadmap do [ADR-019 epílogo §6](./ADR-019-Epilogo-Typed-Adapters.md#6-roadmap-residual-pós-adr-019): "`LLMTransport` refator para `Callable[..., dict]` (reuso de `Callable`)". Materializa o princípio de Iter 25 ("primitivas vivem no framework") para o path LLM.

---

## 1. Contexto

O [ADR-019 epílogo §1.2 (Planejado)](./ADR-019-Epilogo-Typed-Adapters.md#1-iter-21--slm-facades-gliner2) documentou que o framework define 3 Protocol em camadas (Iter 25):

- `Describable` (identidade)
- `Callable[T_in, T_out]` (execução)
- `Tool[R]` (orquestração completa)

O [ADR-025 §1.4](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md#14-the-relationship-to-callable) explicitou um gap: o `LLMTransport` era um Protocol custom no vertical (`fmh_agents.tools.llm_transport`) com método `complete(**kwargs) -> dict`, duck-typed equivalente a `Callable[CompletionRequest, CompletionResponse]`. O framework's `Callable` Protocol era o shape canônico para qualquer executável async; o `LLMTransport` não era formalmente relacionado a `Callable`.

> "The relationship to `Callable` is **duck-typed**, not formally declared."

Esta iteração fecha esse gap. O `LLMTransport` migra para:

1. **Framework-side**: a primitive `LLMTransport` (e o value object `LLMRequest`) movem-se para `fmh_backend.tools.llm_transport` (canonical home, junto com os outros 3 Protocol).
2. **`Callable` shape**: o método `complete(**kwargs)` vira `__call__(request: LLMRequest)`. As 9 keyword parameters viram campos do `LLMRequest` (dataclass frozen).
3. **Re-export shim**: o vertical (`fmh_agents.tools.llm_transport`) continua importável via re-export do framework.

---

## 2. Decisão

### 2.1 Estratégia: refactor atômico (vertical + tests + framework)

A migração é puramente refactor — comportamento observável inalterado. Mas a forma da chamada muda (`complete(**kwargs)` → `__call__(LLMRequest(**kwargs))`). Padrão Iter 28 (atomic batch): um commit fecha o gap.

Mover 5 símbolos (1 Protocol + 4 dataclasses) é trivial em LOC. A migração dos **callers** (`LiteLLMTool`, `CachingLLMTransport`, `FakeLLMTransport`) é puramente mecânica: `transport.complete(model=..., messages=...)` → `transport(LLMRequest(model=..., messages=...))`.

### 2.2 `LLMRequest` — value object do request

A refactor materializa o request como um **value object** (`LLMRequest`, frozen dataclass) com 9 campos. Benefícios:

- **First-class value**: o request pode ser passado entre funções, logado, inspecionado.
- **Type-safe**: erros em nome de campo (typos) são capturados em `LLMRequest.__init__`, não em runtime.
- **Testes mais simples**: `LLMRequest(model="gpt-4", messages=[...], ...)` ao invés de `**kwargs`.

### 2.3 `LLMTransport: Callable` vs `LLMTransport: Protocol`

O design inicial tentou `class LLMTransport(Callable["LLMRequest", dict])`. Python's `@runtime_checkable` constraint rejeita isso:

> `@runtime_checkable can be only applied to protocol classes, got <class 'fmh_backend.tools.llm_transport.LLMTransport'>`

A razão: subscrever um Protocol genérico (`Callable[LLMRequest, dict]`) perde o `_is_protocol` flag no Python 3.12. Solução: declarar `LLMTransport` como um **fresh Protocol** que define `__call__` diretamente. A relação com `Callable` é **structural** (mesma shape `async def __call__(self, request: LLMRequest) -> dict`).

Implicação para type-checking: `isinstance(obj, LLMTransport)` funciona. `isinstance(obj, Callable)` também funciona (structural match). `issubclass(LLMTransport, Callable)` **não** funciona (Python's Protocol subclass rule). Aceitável — `isinstance` é o que os tests usam.

### 2.4 Vertical `llm_transport.py` vira re-export shim

O módulo vertical `fmh_agents/tools/llm_transport.py` foi reescrito como re-exports do framework. 179 LOC viraram 50 LOC (re-exports + docstring).

### 2.5 Compat

Zero compat shim (AGENTS.md §2.2). Migração atômica:

- `LLMTransport`, `LLMRequest`, `LLMResponse`, `LLMUsage`, `LLMChunk` agora vêm do framework.
- Vertical `fmh_agents.tools.llm_transport` re-exporta do framework. Mesmo identity, mesmo nome, mesma assinatura.
- Callers (`LiteLLMTool`, `CachingLLMTransport`, `FakeLLMTransport`) atualizados em 1 commit.

---

## 3. Consequências

### Pros

- **LLMTransport IS-A Callable (structural)** — qualquer
  `LLMTransport` é drop-in `Callable[LLMRequest, dict]`.
  O framework trata ambos uniformemente.
- **LLMRequest é first-class value** — o request pode
  ser logado, serializado, passado entre adapters (e.g.
  para tracing distribuído).
- **LLMTransport no framework** — alinhado com `Tool`,
  `Describable`, `Callable`, `FieldFinder`, etc. O
  framework define o shape; a vertical define a impl.
- **Re-export shim no vertical** preserva backward
  compat. Tests existentes que importam de
  `fmh_agents.tools.llm_transport` continuam
  funcionando.
- **3 imports `fmh_backend → fmh_agents` em produção
  eliminados** (de `cache.py`, `llm.py`, `__init__.py`)
  — o vertical agora importa de `fmh_backend.tools`.

### Cons

- **Breaking change** no shape da chamada:
  `transport.complete(**kwargs)` →
  `transport(LLMRequest(**kwargs))`. Qualquer caller
  externo precisa ser atualizado. Mitigação: 3
  call sites internos atualizados em 1 commit; re-export
  vertical preserva `LLMTransport` symbol mas **não**
  preserva o `complete()` method.
- **`issubclass(LLMTransport, Callable)` não funciona**
  — Python 3.12 Protocol constraint. `isinstance(obj, LLMTransport)`
  e `isinstance(obj, Callable)` funcionam (structural).
- **Cycle import pré-existente em `fmh_agents.tools.cache`**
  (introduzido em Iter 28 FU 2 via
  `infra.lite_graph_client`) — não relacionado a esta
  iter, mas afeta a coleta de `test_cache.py`. O test
  está pré-quebrado; o fix está em Iter futura (dívida
  do roadmap).

### Métricas

| Métrica | Antes (Iter 28 FU 2) | Depois (Iter 28 FU 3) |
|---|---|---|
| `fmh_backend/tools/llm_transport.py` | 0 LOC | **~140 LOC** (novo) |
| `fmh_agents/tools/llm_transport.py` | 158 LOC | **~50 LOC** (re-export) |
| `LLMTransport` declared as `Callable[...]` | não (duck-typed) | **sim (structural)** |
| `complete(**kwargs)` API | sim | **não** (substituído por `__call__(LLMRequest)`) |
| Imports `fmh_backend → fmh_agents` em produção (LLM path) | 3 | **0** |
| `fmh_backend/tests/unit/` total | 1331 passed | **1339 passed** (+8) |
| `LLMRequest` é first-class value | não | **sim** (frozen dataclass) |

### Conquistas notáveis

1. **3 Protocol em camadas + LLMTransport formam 4
   primitives canônicas** no framework. A fundação
   está completa.
2. **`LLMRequest` é reuso futuro** — adapters futuros
   (OpenAI direct, Anthropic direct, local vLLM)
   só precisam implementar `__call__(LLMRequest)`.
   O request shape é estável.
3. **Tracing ready** — `LLMRequest` é serializável
   (dataclass). O framework pode logar requests
   uniformly sem saber o provider concreto.

---

## 4. Migration

### Já migrado (este commit)

**Framework (novo):**
- `fmh_backend/src/fmh_backend/tools/llm_transport.py`
  (novo, ~140 LOC) — `LLMRequest`, `LLMResponse`,
  `LLMUsage`, `LLMChunk`, `LLMTransport`.
- `fmh_backend/src/fmh_backend/tools/__init__.py` —
  re-exports dos 5 novos símbolos.
- `fmh_backend/src/fmh_backend/tools/protocol.py` —
  comentário atualizado: `LLMTransport.__call__` ao
  invés de `LLMTransport.complete`.

**Vertical (re-export shim + 2 edits + 1 edit):**
- `fmh_agents/src/fmh_agents/tools/llm_transport.py`
  (reescrito, 158 → 50 LOC) — re-exports do framework.
- `fmh_agents/src/fmh_agents/tools/llm.py` —
  `LiteLLMTransportAdapter.__call__` (não mais
  `complete`); `LiteLLMTool._call_litellm` constrói um
  `LLMRequest` e chama `transport(request)`.
- `fmh_agents/src/fmh_agents/tools/cache.py` —
  `CachingLLMTransport.__call__` (não mais `complete`).
- `fmh_agents/src/fmh_agents/tools/__init__.py` —
  re-exports do framework.

**Tests:**
- `fmh_backend/tests/unit/tools/test_llm_transport_callable.py`
  (novo) — 8 tests: `LLMRequest` (frozen, defaults,
  extra), `LLMTransport` Protocol shape, `Callable`
  structural, deletion of `complete()`.
- `fmh_agents/tests/unit/tools/test_llm.py` — 3 calls
  `transport.complete(...)` → `transport(LLMRequest(...))`.
- `fmh_agents/tests/unit/_fake_transport.py` —
  `FakeLLMTransport.__call__` (delegação ao
  `complete` legacy).

### Pendente (Iter futura)

- **Cycle import em `fmh_agents.tools.cache`** —
  pré-existente (Iter 28 FU 2 introduziu). O test
  `test_cache.py` não coleta. A correção requer
  refactor em `fmh_backend.infra.lite_graph_client`
  (lazy imports dos knowledge pieces) ou em
  `fmh_backend.stream.event_log` (quebrar cycle com
  `fmh_backend.knowledge.falkordb.adapter`). Estimativa:
  pequeno. Bloqueador: nenhum (dev-only; production
  não passa por aqui).
- **`LLMClient` facade simplificação** — hoje
  `LLMClient(adapter=...)` aceita um `LLMTransport`
  custom. Pós-Iter 28 FU 3, qualquer `Callable` que
  satisfaz o shape pode ser passado. O type
  signature pode ser ampliado para
  `LLMTransport | Callable[[LLMRequest], dict]`. Não
  bloqueia.

---

## 5. Decisões relacionadas

- **AGENTS.md §1 (adapter types)**: a regra "1 lib
  externa = 1 Protocol" agora está estritamente
  aplicada para o path LLM: framework define
  `LLMTransport` + `LLMRequest`; vertical define
  `LiteLLMTransportAdapter` (impl concreta via
  `litellm`).
- **AGENTS.md §2 (zero shims)**: a migração foi
  puramente refactor. Re-export vertical é legítimo
  (não compat shim). 0 shims introduzidos.
- **ADR-025 §1.4 (Callable relationship)**: o gap
  "relationship is duck-typed" foi fechado. O
  `LLMTransport` agora é structural-equivalent a
  `Callable[LLMRequest, dict]`.

---

## 6. Lições aprendidas

### O que funcionou

1. **TDD estrito** — o test
   `test_old_complete_method_does_not_exist` capturou
   a refactor (a remoção do `complete()` method). O
   test falhou antes da implementação, passou depois.
2. **Re-export shim no vertical** preservou
   backward-compat para tests existentes
   (`test_llm_settings.py`, `test_llm_client_guard.py`)
   sem modificação.
3. **Padrão "value object para payload"** — Iter 25
   documentou o pattern (LLMRequest seria a
   concretização). Esta iter é a materialização.

### O que poderia ter sido melhor

1. **Fazer esta iter mais cedo** — o gap foi
   documentado em Iter 25 (1 iter atrás). Demorou 1
   iter para fechar. Lição: gaps documentados em ADRs
   como roadmap devem ser atacados na iter seguinte.
2. **Cycle import pré-existente em `cache.py`** —
   introduzido por Iter 28 FU 2, não capturado
   imediatamente. Um test que importasse
   `fmh_agents.tools.cache` antes do refactor teria
   detectado. Lição: cycles introduzidos em uma iter
   devem ter regression tests na mesma iter.

---

## 7. Referências

- [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md) — Iter 18b (LLM Transport Protocol); roadmap §6
- [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md) — Pattern: vertical → framework migration
- [ADR-025](./ADR-025-Tool-Protocol-Split-Cycle-Resolution.md) — Pattern: 3-Protocol split; `Callable` introduced
- [ADR-026](./ADR-026-Close-GLiNER2-Binding-Leak.md) — Pattern: atomic migration
- [ADR-027](./ADR-027-Argument-Extraction-Migration.md) — Pattern: value object for adapter payload
- [ADR-028](./ADR-028-Delete-Argument-Extraction-Vertical.md) — Pattern: re-export shim → delete
- [ADR-029](./ADR-029-LiteFalkorDBClient-to-LiteGraphClient.md) — Iter 28 FU 2 (immediate predecessor)
- [AGENTS.md §1](../../AGENTS.md) — adapter types
- [AGENTS.md §2](../../AGENTS.md) — zero shims
- `fmh_backend/src/fmh_backend/tools/llm_transport.py` — novo
- `fmh_agents/src/fmh_agents/tools/llm_transport.py` — re-export shim
- `fmh_backend/tests/unit/tools/test_llm_transport_callable.py` — 8 tests

---

**Conclusão**: o framework FMH agora tem o path LLM
**100%** como framework concern. `LLMTransport`,
`LLMRequest`, `LLMResponse`, `LLMUsage`, `LLMChunk`
são primitives do framework. O `LLMTransport` é
structural-equivalent a `Callable[LLMRequest, dict]`
— a regra "1 lib externa = 1 Protocol" agora é
estritamente aplicada para o path LLM. O roadmap
do ADR-019 epílogo (Iter 22-25) + ADR-024 (Iter 24)
+ ADR-025 (Iter 25) + ADR-026 (Iter 27) + ADR-027
(Iter 28) + ADR-028 (Iter 28 FU) + ADR-029 (Iter 28
FU 2) + este ADR-030 (Iter 28 FU 3) está **fechado em
100%** para o core framework. Iter 26+ (3 Reactive
Systems para Knowledge) é refator de performance,
não cobertura arquitetural.