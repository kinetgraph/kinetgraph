<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-031: GraphLike Protocol deletion

**Status:** Aceito
**Data:** 30 de junho de 2026
**Relacionado:** [ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md),
[ADR-029](./ADR-029-LiteFalkorDBClient-to-LiteGraphClient.md),
[ADR-019](./ADR-019-Epilogo-Typed-Adapters.md)

## 1. Contexto

O Protocol `GraphLike` foi introduzido em Iter 24 (ver [ADR-024 §2.5](./ADR-024-FalkorDBClient-GraphClient-Migration.md#25-graphlike-protocol-preservado))
como o shape sync do handle FalkorDB retornado por
`LiteFalkorDBClient` (dev-only). Em Iter 24, o Protocol
`GraphAdapter` (async) foi introduzido como o shape canônico
para o framework production; `GraphLike` coexistia como uma
relíquia sync, com a nota explícita de que seria deletado em
uma iter futura.

Iter 28 follow-up 2 (ver [ADR-029](./ADR-029-LiteFalkorDBClient-to-LiteGraphClient.md))
migrou o dev-only client para `LiteGraphClient` (renomeado
`LiteGraphPool` em Iter 28 FU 7, ver
[ADR-033](./ADR-033-GraphPool-Reorg.md)), que retorna
`GraphAdapter` (mesma shape que `GraphPool` produção).
A partir desta migração, `GraphLike` ficou sem consumidores:

- `LiteFalkorDBClient` foi deletado (Iter 28 FU 2).
- O framework production consome `GraphAdapter` (via `GraphPool`).
- O dev-only consome `GraphAdapter` (via `LiteGraphPool`).

`GraphLike` virou um Protocol órfão.

### 1.1 O que `GraphLike` definia

```python
# fmh_backend/src/fmh_backend/core/_typing.py (pré-Iter 28 FU 4)
class GraphLike(Protocol):
    """Shape of a FalkorDB Graph handle (sync only)."""

    def query(self, cypher: str, params: Mapping[str, ValidatorInput] | None = None): ...
```

Diferenças em relação a `GraphAdapter`:

- `GraphLike.query` é **sync**; `GraphAdapter.query` é
  **async** com return type `GraphQueryResult`.
- `GraphLike` aceita `params: Mapping[str, ValidatorInput]`;
  `GraphAdapter` aceita `params: dict[str, object]`.
- `GraphLike` retorna duck-typed (sem Protocol type hint
  no return); `GraphAdapter` retorna `GraphQueryResult`
  (estrutura imutável explícita).

## 2. Decisão

`GraphLike` é **deletado** de `fmh_backend.core._typing`.
O Protocol sync-only não tem consumidores; sua existência
viola AGENTS.md §3 (zero código morto) e adiciona
superfície de API pública que pode ser mal-usada por
novos adapters sync.

### 2.1 Mudanças

1. **Definição deletada** de `core/_typing.py`:
   - Classe `GraphLike` (linhas 140-148 do arquivo
     pré-Iter 28 FU 4) removida.
   - Section docstring "FalkorDB Graph handle (sync flavour
     — legacy Lite)" removida.
   - Entrada `"GraphLike"` em `__all__` removida.

2. **Import órfão removido**: `from collections.abc import
   Mapping` era usado apenas em `GraphLike.query`; sem
   consumidores, removido também.

3. **Docstring do módulo** (`core/_typing.py:37-42`): a
   bullet de `GraphLike` removida. O docstring agora
   lista apenas os Protocols canônicos: `OpaqueKey`,
   `RouterApp`, `Dependable`, `HTTPExceptionLike`,
   `HeaderParam`, e o `ValidatorInput` data type.

4. **Docstring comment OpaqueHandleT** (`_typing.py:97`):
   referência a `GraphLike` substituída por `LiteGraphPool`
   (o cliente concreto que materializa o handle opaco;
   nome `LiteGraphClient` no Iter 28 FU 2, renomeado
   `LiteGraphPool` em Iter 28 FU 7).

5. **Comentários em `infra/lite_graph_client.py`** (Iter 28 FU 4;
   renomeado `infra/graph/_lite_pool.py` em Iter 28 FU 7): 4
   referências a `GraphLike` em docstrings removidas. O
   novo `LiteGraphPool` retorna `GraphAdapter`; não há
   motivo para mencionar o Protocol deletado.

6. **Comentário em `infra/__init__.py`**: referência
   histórica a `GraphLike` removida. O import de
   `LiteGraphPool` permanece.

7. **`test_lite_graph_client.py`**: 2 docstring references
   removidas (linhas 11, 107 do arquivo pré-Iter 28 FU 4).

8. **`.radon-baseline.json`**: entry `class:GraphLike` removida
   (radon CC=2; não há mais classe para medir).

### 2.2 Não-deleção deliberada: `fmh_app.GraphLike`

`fmh_app/src/fmh_app/knowledge/client.py:56` define um
**outro** `GraphLike` — local ao app, **async** (não sync),
e com caller ativo (`fmh_app/knowledge/projection.py:161`).
Esse não é o mesmo símbolo e não é afetado por esta iteração.
É um Protocol app-layer válido: define a shape mínima
que o projector de knowledge precisa de um graph handle.

A coexistência de nomes idênticos em módulos diferentes
é comum em Python (mesmo princípio de `JsonValue`:
framework tem um, app pode ter outro). O framework
deleta o seu; o app mantém o seu (com caller ativo).

### 2.3 Por que não há shim de compatibilidade

AGENTS.md §2 proíbe shims de compatibilidade. Não há
external consumer (apenas internal); o Protocol era
interno, deletável sem aviso. Migração atômica em 1
commit: 1 definition removed + 1 `__all__` entry removed +
1 import removed + 6 docstring/comment updates.

## 3. Consequências

### 3.1 Pros

- **`_typing.py` mais enxuto**: 22 linhas removidas
  (definição + section comment + `__all__` entry +
  import). Module LOC: 211 → 180.
- **API pública do framework reduzida**: `GraphLike` não
  aparece em `core._typing.__all__`; quem procurar pelo
  shape sync recebe `ImportError` explícito (fail-closed,
  AGENTS.md §6).
- **Sinal arquitetural mais claro**: a forma de graph
  handle do framework é **uma** (`GraphAdapter`).
  O fato de `GraphLike` ter coexistido com `GraphAdapter`
  por 4 iterações (Iter 24 → Iter 28 FU 4) deu abertura
  a bugs sutis (sync vs async dispatch).

### 3.2 Cons

- **Tests que ainda rodam com pre-Iter 24 imports**:
  se algum caller downstream (fora deste monorepo)
  tinha `from fmh_backend.core._typing import GraphLike`,
  vai quebrar. Mas: (a) este monorepo não tem callers
  externos; (b) o Protocol nunca foi parte de API pública
  documentada; (c) AGENTS.md §2 permite breaking change
  intencional.
- **Atributo `Mapping` removido do módulo**: callers que
  importassem `Mapping` de `core._typing` (mesmo que
  semanticamente errado) também quebram. Verificação:
  zero callers no monorepo (grep mostra apenas o
  `from collections.abc import Mapping` interno).

### 3.3 Trade-offs

- **Migração atômica** (1 commit) vs **shim deprecado
  com warning**: o shim adicionaria branch de runtime
  e mascararia a remoção. AGENTS.md §2 proíbe shims.
  Decidido por migração atômica.

## 4. Migration

### 4.1 Callers (zero internos)

Verificado via `grep` que **nenhum** arquivo em
`fmh_backend/src/`, `fmh_backend/tests/`, `fmh_agents/src/`,
`fmh_agents/tests/`, `fmh_app/src/`, `fmh_app/tests/`, ou
`fmh_office/src/` importa `GraphLike` de
`fmh_backend.core._typing`. A única fonte de verdade
era o próprio arquivo de definição.

### 4.2 Docstring/comment updates

6 arquivos tiveram menções textuais a `GraphLike`
atualizadas:

1. `core/_typing.py:37-42` (docstring módulo) — bullet
   `GraphLike` removida.
2. `core/_typing.py:97` (comment `OpaqueHandleT`) —
   referência substituída por `LiteGraphPool`.
3. `infra/lite_graph_client.py` (4 docstrings; Iter 28 FU 7
   renomeou para `infra/graph/_lite_pool.py`) —
   menções a `GraphLike` removidas.
4. `infra/__init__.py:7-13` (comment sobre
   `LiteGraphPool`; Iter 28 FU 7 atualizou para
   apontar ao novo path) — referência histórica a
   `GraphLike` removida.
5. `tests/unit/infra/test_lite_graph_client.py`
   (2 docstrings) — referências removidas.

### 4.3 Test de deletion gate

`fmh_backend/tests/unit/core/test_graph_like_deleted.py`
(novo, 6 tests):

- `test_graph_like_not_exported_from_core_typing` —
  `from fmh_backend.core._typing import GraphLike` deve
  falhar com `ImportError`.
- `test_graph_like_attribute_missing_from_module` —
  `hasattr(_typing, "GraphLike")` é `False`.
- `test_graph_like_not_in_typing_all` — `"GraphLike"`
  não está em `__all__`.
- `test_mapping_import_removed_too` — `"Mapping"` não
  está em `dir(_typing)`.
- `test_graph_adapter_still_works` — sanity check:
  `GraphAdapter` ainda é importável de
  `fmh_backend.knowledge.graph`.
- `test_lite_graph_client_still_works` — sanity check:
  `LiteGraphAdapter` ainda herda de `GraphAdapter`.

TDD: tests escritos **antes** da remoção, falharam
(4 conditions on deleted `GraphLike`), passaram
após a remoção (6/6 verdes).

## 5. Decisões relacionadas

- **[ADR-024](./ADR-024-FalkorDBClient-GraphClient-Migration.md)**:
  introduziu `GraphLike` como Protocol sync-only para
  o legado `LiteFalkorDBClient`. Note que ADR-024 já
  previa a deleção: "O Protocol `GraphLike` (em
  `core/_typing.py`) é mantido e seu docstring foi
  atualizado para refletir que ele serve apenas ao
  `LiteFalkorDBClient` (sync). ... `GraphLike` Protocol
  está sem callers (candidato a ...)".

- **[ADR-029](./ADR-029-LiteFalkorDBClient-to-LiteGraphClient.md)**:
  Iter 28 FU 2 migrou o dev-only client para
  `LiteGraphPool` que retorna `GraphAdapter`. ADR-029
  §5 lista como follow-up: "Deletar `GraphLike` Protocol
  (em `core/_typing.py`)".

- **[ADR-019](./ADR-019-Epilogo-Typed-Adapters.md) §14**:
  apêndice sobre Iter 27+28 documenta o roadmap até
  `LLMTransport: Callable`. ADR-031 fecha o último
  item arquitetural residual do roadmap do ADR-019.

- **AGENTS.md §1**: zero `Any`/`object` no framework.
  `GraphLike.query` aceitava `params: Mapping[str, ValidatorInput]`
  (não `Any`), mas seu caller sync-only
  (`LiteFalkorDBClient.query`) retornava duck-typed.
  `GraphAdapter.query` é totalmente tipado
  (`async def query -> GraphQueryResult`). Deleção de
  `GraphLike` fortalece a fronteira de tipos.

- **AGENTS.md §4**: async é pilar. `GraphLike` era
  sync-only (anomalia em um framework async-first).
  Deleção corrige a anomalia.

## 6. Referências

- `fmh_backend/src/fmh_backend/core/_typing.py` —
  definição removida.
- `fmh_backend/src/fmh_backend/infra/lite_graph_client.py` —
  4 docstring references removidas.
- `fmh_backend/src/fmh_backend/infra/__init__.py` —
  1 comment removido.
- `fmh_backend/tests/unit/infra/test_lite_graph_client.py` —
  2 docstring references removidas.
- `fmh_backend/tests/unit/core/test_graph_like_deleted.py` —
  6 novos tests (deletion gate + sanity checks).
- `.radon-baseline.json` — entry `class:GraphLike` removida.
- `fmh_app/src/fmh_app/knowledge/client.py:56` — `GraphLike`
  **local** ao app (async, caller ativo). Não relacionado
  a esta iteração.
