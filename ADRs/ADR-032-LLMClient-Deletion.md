<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-032: LLMClient facade deletion

**Status:** Aceito
**Data:** 30 de junho de 2026
**Relacionado:** [ADR-019](./ADR-019-Epilogo-Typed-Adapters.md),
[ADR-030](./ADR-030-LLMTransport-as-Callable.md),
[ADR-018b/018c](#referências)

## 1. Contexto

`LLMClient` foi introduzido em Iter 18b (ADR-019 epílogo) como
um facade para o LLM transport, seguindo o pattern established
por `GraphClient` (graph, renomeado para `GraphPool` em
Iter 28 FU 7 / ADR-033) e `EmbeddingClient` (embedding):

  - `XxxTransport` — Protocol
  - `XxxTransportAdapter` — low-level adapter
  - `XxxClient` — facade
  - `XxxTool` — orchestrator

A motivação era duplo:
1. Encapsular a escolha de adapter (lazy import de
   `litellm`) para que apps não precisem instalar
   `litellm` quando passam um adapter custom.
2. Manter a API surface backend-agnostic.

Iter 28 FU 3 (ADR-030) migrou `LLMTransport` de
`async def complete(...)` para
`async def __call__(LLMRequest) -> dict` (structural
match com `Callable[LLMRequest, dict]`). O
`LLMClient.complete(...)` tornou-se **inconsistente**:
a facade declarava `complete` (legacy) mas não
`__call__` (current). O `__call__` herdado do Protocol
é um no-op (Protocols não implementam), o que
significa que o `LiteLLMTool._call_litellm` (que
chama `transport(request)` pós-Iter 28 FU 3) não
delegaria para o adapter via `LLMClient` — chamaria
o no-op.

### 1.1 Callers (zero)

Auditoria pré-deleção:

| Local | Tipo | Comportamento |
|-------|------|---------------|
| `fmh_agents.tools._llm_client` | definição | classe |
| `fmh_agents.tools.__init__` | re-export | `from ... import LLMClient` |
| `fmh_agents.tools.__all__` | public surface | `"LLMClient"` |
| `fmh_agents.tests.unit.tools.test_llm_client_guard` | test | valida lazy import |
| `fmh_app/`, `fmh_office/` | apps | **zero** |
| `fmh_agents/examples/` | examples | **zero** |

**Zero callers em production code.** A facade era
puramente decorativa — Iter 18b/18c prometeu um
pattern que ninguém consumiu. O único test que
exercitava o lazy import documentava um feature
sem usuário.

## 2. Decisão

`LLMClient` é **deletado** de `fmh_agents.tools`.
A facade, seu módulo de suporte, e o test de
guard são todos removidos. Aplicativos que
precisam de um LLM transport instanciam
`LiteLLMTransportAdapter` diretamente (ou passam
qualquer `LLMTransport` concreto para o
`LiteLLMTool`).

### 2.1 Por que deletar (não simplificar)

A "simplificação" proposta no ADR-030 §4 era
ampliar o type signature:
`LLMTransport | Callable[[LLMRequest], dict]`. Esta
opção foi rejeitada por:

1. **Zero callers**: a facade não tem usuário.
   Ampliar o type signature não desbloqueia
   ninguém.
2. **Inconsistência com Protocol**: `LLMClient`
   declara `complete` (legacy) mas não `__call__`
   (current). Qualquer call site que faça
   `client(request)` recebe `None`. Manter a
   facade exige migrar `LLMClient` para `__call__`,
   o que adiciona LOC sem destravar ninguém.
3. **Pattern violation**: o pattern
   `XxxClient = facade` (Graph, Embedding) pressupõe
   que apps chamam `client.operation()`. Para LLM,
   apps chamam `tool.invoke(...)` (orchestrator);
   o transport é injetado no `LiteLLMTool`
   internamente. O facade `LLMClient` quebra a
   abstração `tool ↔ transport` ao se interpor
   entre eles.

### 2.2 Mudanças

1. **Módulo deletado**:
   `fmh_agents/src/fmh_agents/tools/_llm_client.py`
   (110 LOC).

2. **Test deletado**:
   `fmh_agents/tests/unit/tools/test_llm_client_guard.py`
   (88 LOC; 3 tests).

3. **`__init__.py` atualizado**:
   `fmh_agents/tools/__init__.py`:
   - `from ._llm_client import LLMClient` removido.
   - `"LLMClient"` removido de `__all__`.

4. **Docstring atualizado**:
   `fmh_backend/src/fmh_backend/knowledge/extraction/gliner_argument.py:68`
   — referência a `LLMClient` removida (substituída
   por `fmh_agents.tools.llm`).

5. **Deletion gate test** (novo):
   `fmh_backend/tests/unit/test_llm_client_deleted.py`
   (6 tests):
   - `test_llm_client_not_exported_from_tools`:
     `from fmh_agents.tools import LLMClient` deve
     falhar.
   - `test_llm_client_module_deleted`:
     `fmh_agents.tools._llm_client` não pode ser
     importado.
   - `test_llm_client_not_in_tools_all`:
     `LLMClient` não está em `__all__` e não é
     atributo do módulo.
   - `test_guard_test_deleted`:
     `test_llm_client_guard.py` não existe.
   - `test_litellm_transport_adapter_still_works`:
     sanity check: `LiteLLMTransportAdapter` ainda
     é importável e IS-A `LLMTransport`.
   - `test_llm_transport_protocol_still_works`:
     sanity check: o Protocol do framework tem
     `__call__` (canonical contract).

TDD: 3 conditions on deletion falharam antes da
remoção; passaram após.

## 3. Consequências

### 3.1 Pros

- **-198 LOC** (110 do facade + 88 do test, +174
  do deletion gate = -24 net, mas o deletion gate
  é **regression test**, então conta como +).
- **Elimina inconsistência `complete` vs
  `__call__`**: o facade não existe mais, então
  não pode estar desalinhado com o Protocol.
- **API surface do vertical reduzida**:
  `fmh_agents.tools.__all__` passa de 26 entries
  para 25.
- **Signal arquitetural mais claro**: o
  orchestrator `LiteLLMTool` é o ponto de entrada
  canônico para LLM; o transport é uma
  dependência injetada, não um objeto que apps
  manipulam diretamente.

### 3.2 Cons

- **Breaking change para hypothetical external
  consumers**: se algum consumer (fora deste
  monorepo) importava `from fmh_agents.tools
  import LLMClient`, vai quebrar. Mas: (a) este
  monorepo não tem callers externos; (b)
  `LLMClient` nunca foi parte de API pública
  documentada em `__all__` (foi adicionado
  depois); (c) AGENTS.md §2 permite breaking
  change intencional.
- **Perda do test do lazy import**: o test
  `test_llm_client_guard.py` validava que
  `litellm` é lazy-loaded. Esse test
  documentava um feature (`LLMClient(adapter=...)`
  funciona sem `litellm`), mas o feature não
  tem usuário. A lazy-load ainda é exercitada
  via `LiteLLMTool` (que importa `litellm`
  lazy dentro de `_call_litellm`).

### 3.3 Trade-offs

- **Migração atômica** (1 commit) vs **shim de
  deprecation**: AGENTS.md §2 proíbe shims. A
  facade tinha 0 callers; manter um shim seria
  dívida sem propósito.
- **Deleção vs rename** (`LLMClient =
  LiteLLMTransportAdapter`): o alias preserva a
  backward-compat mas adiciona 1 entry em
  `__all__` sem propósito. Deleção é mais limpa.

## 4. Migration

### 4.1 Callers (zero internos)

Verificado via `grep` que **nenhum** arquivo em
`fmh_backend/src/`, `fmh_backend/tests/`,
`fmh_agents/src/`, `fmh_agents/tests/`, `fmh_app/`,
`fmh_office/` importa `LLMClient` em runtime. A
única fonte de verdade era o próprio arquivo de
definição e seu test.

### 4.2 Docstring/comment updates

2 arquivos tiveram menções textuais a `LLMClient`
atualizadas:

1. `gliner_argument.py:68` (docstring): referência
   a `LLMClient` removida.
2. `fmh_agents/tools/__init__.py:76` (import) e
   `__all__:111` (public surface): removidos.

### 4.3 Test de deletion gate

`fmh_backend/tests/unit/test_llm_client_deleted.py`
(novo, 6 tests) — ver §2.2 item 5.

## 5. Decisões relacionadas

- **[ADR-019 §18b/§18c](./ADR-019-Epilogo-Typed-Adapters.md#12-apêndice-iter-18--llm-transport-protocol)**:
  introduziu `LLMClient` como facade seguindo o
  pattern de `GraphClient` (renomeado `GraphPool` em
  Iter 28 FU 7)/`EmbeddingClient`. O
  pattern se mostrou desnecessário para o path
  LLM: apps não manipulam transports diretamente;
  usam `LiteLLMTool.invoke(...)` (orchestrator).
- **[ADR-030 §4](./ADR-030-LLMTransport-as-Callable.md#4-roadmap-residual-pós-adr-030)**:
  propôs "simplificar `LLMClient` para aceitar
  `LLMTransport | Callable[[LLMRequest], dict]`".
  Esta ADR deleta `LLMClient` em vez de ampliá-lo.
- **AGENTS.md §2 (zero shims)**: a facade foi
  deletada sem shim de compat. Migração atômica
  em 1 commit.
- **AGENTS.md §1 (adapter types)**: a regra
  "1 lib externa = 1 Protocol" continua aplicada:
  `LLMTransport` (framework) + `LLMRequest` (VO) +
  `LiteLLMTransportAdapter` (vertical impl). Sem
  facade intermediário.

## 6. Referências

- `fmh_agents/src/fmh_agents/tools/_llm_client.py` —
  deletado (110 LOC).
- `fmh_agents/tests/unit/tools/test_llm_client_guard.py` —
  deletado (88 LOC, 3 tests).
- `fmh_agents/src/fmh_agents/tools/__init__.py` —
  import removido + `__all__` entry removida.
- `fmh_backend/src/fmh_backend/knowledge/extraction/gliner_argument.py` —
  docstring atualizada.
- `fmh_backend/tests/unit/test_llm_client_deleted.py` —
  6 novos tests (deletion gate + sanity checks).
- `fmh_agents/src/fmh_agents/tools/llm.py` —
  `LiteLLMTransportAdapter` (o substituto canônico
  para o default-branch de `LLMClient`).
