<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-005: Checkpoints Duráveis e `idempotency_key` em Tools

**Status:** Aceito
**Data:** 08 de junho de 2026
**Versão:** 2.1 (commit safety no dispatcher e nas tools)
**Autores:** Equipe de Arquitetura FMH
**Relacionado:** [ADR-001](./ADR-001-Arquitetura.md), [ADR-002](./ADR-002-Replay-Puro.md), [ADR-004](./ADR-004-Memory-Tools-Knowledge.md), [ADR-010](./ADR-010-Memory-Business-Tier.md)

---

## 1. Contexto

O `ReactiveDispatcher` (v2.0) mantém o **cursor de progresso por
agente em memória** (`self._cursors: dict[str, str]`). Esse
modelo tem dois problemas em produção:

  1. **Crash + restart**: o cursor evapora com o processo. O
     dispatcher re-executa todo o histórico do stream,
     re-dispachando eventos já processados. O `EventLog` deduplica
     os `.requested` / `.completed` que voltam ao stream, mas
     **side effects em tools externas** (PIX, NF-e transmitidas,
     HTTP POSTs) podem ser re-executados, gerando duplicações
     custosas (cobranças em dobro, retry de APIs de pagamento).

  2. **Crescimento do fleet**: cada novo agente exige um
     `track_agent` explícito ou um `SCAN` no boot. Em produção
     com 10k+ agentes ativos, o boot fica lento.

A solução combina **dois mecanismos complementares** — o
dispatcher persiste seu commit point no Redis (checkpoint), e
o `ToolInvoker` injeta uma `idempotency_key` estável em cada
chamada de tool, permitindo dedup de side effects externos.

---

## 2. Decisão

### 2.1 `ReactiveCheckpoint` — commit point durável

```python
@dataclass(frozen=True, slots=True)
class ReactiveCheckpoint:
    agent_id: str
    last_event_id: UUID         # lógico
    last_stream_id: str         # físico, âncora p/ XRANGE exclusivo
    confirmed_at: datetime
    state_hash: Optional[str] = None
```

Armazenado em **um único hash Redis** (`fmh:reactive:checkpoints`,
um field por agente). Escrito pelo dispatcher **após** o batch
ter sido commitado no EventLog.

**Invariantes**:

- Par `(last_event_id, last_stream_id)` salvo num único `HSET`
  (atômico em Redis).
- Próxima leitura usa `min="(<last_stream_id>"` (exclusivo).
  Sobrevive a `XTRIM MAXLEN`.
- Save é **post-commit do EventLog**. Janela residual entre
  `XADD` OK e `HSET` é submilissegundo.

### 2.2 `idempotency_key` em `Tool.invoke`

```python
@runtime_checkable
class Tool(Protocol):
    async def invoke(
        self, *, idempotency_key: str, **kwargs
    ) -> Result[Any, ToolError]: ...
```

O `ToolInvoker` injeta `idempotency_key=str(request.event_id)`
em **toda** chamada. A chave:

- É **estável** entre re-dispatches: mesmo `.requested` event_id
  → mesma chave.
- Permite dedup local na tool (cache `dict[idempotency_key, result]`)
  ou externo (passa adiante para a API de pagamento, que dedup'a).

### 2.3 Modos de operação

| Modo | Comportamento | Quando usar |
|------|---------------|-------------|
| `ReactiveDispatcher(log, systems=[...])` | Cursor in-memory, não sobrevive a restart | Testes, dev single-process |
| `ReactiveDispatcher(log, systems=[...], checkpoint_store=store)` | Checkpoint durável no Redis | **Produção** |

A escolha é explícita no construtor — não há fallback implícito
para evitar comportamento não-determinístico em produção.

### 2.4 Backward compatibility

- `ReactiveDispatcher` sem `checkpoint_store` preserva o
  comportamento legacy (cursor in-memory). Documentado e testado.
- `Tool.invoke` **agora exige** `idempotency_key` na assinatura.
  Tools legadas com `**kwargs` continuam funcionando (a chave é
  absorvida pelo `**kwargs`).
- `InvoiceIssueTool` e `InvoiceQueryTool` (em
  `fmh_agents`) já usavam `**kwargs` e portanto não
  precisaram de mudança.

---

## 3. Fluxo end-to-end

```
Reactive System emit "tool.bank.transfer.requested"
            ↓
    EventLog.append (XADD + SET NX idempotency)
            ↓
    ReactiveDispatcher lê evento, aplica sistema, emite resultado
            ↓
    ToolInvoker consome .requested, chama tool com idempotency_key
            ↓
    tool.invoke(idempotency_key=..., amount=100, to=acc-123)
            ↓
    [tool cacheia por idempotency_key se side-effect externo]
            ↓
    ToolInvoker emite .completed
            ↓
    ReactiveDispatcher persiste checkpoint (N=last_event_id)
```

Em **restart**:

```
ReactiveDispatcher (novo processo)
            ↓
    Carrega checkpoint de N=last_event_id do Redis
            ↓
    Próximo XRANGE: min="(<last_stream_id>"
            ↓
    Processa apenas eventos com stream_id > checkpoint
            ↓
    [se tool re-chamada: idempotency_key bate com cache]
```

---

## 4. Trade-offs

### Prós

- **Crash safety**: dispatcher sobrevive a restart sem
  re-processamento descontrolado.
- **Side-effect safety**: tools com dedup local ficam
  at-most-once para o caller externo.
- **Observabilidade**: `CheckpointStore.load_all()` expõe o
  estado de progresso de cada agente para dashboards.
- **Compatibilidade**: zero breaking change para tools legadas
  com `**kwargs`.
- **`state_hash` opcional**: detecta projection drift em
  mudanças de `project_default`.

### Contras

- **Nova dependência operacional**: requer Redis persistente
  (AOF) para não perder checkpoints em restart do Redis.
- **Janela residual**: submilissegundo entre `XADD` e `HSET`.
  Em disaster do Redis exatamente nesse intervalo, pode
  re-entregar. Mitigável com idempotency_key em tools.
- **Mais um conceito**: o time precisa entender checkpoint vs
  cursor. Documentado em `docs/checkpoints.md`.

### Alternativas consideradas

- **Cursor no próprio stream do agente (XADD com metadata)**:
  rejeitado — acopla o "commit point" ao conteúdo do stream,
  dificultando auditoria e métricas.
- **`XAUTOCLAIM` com consumer groups**: poderoso mas exige
  refator do modelo de ownership. Considerado para v2.2.
- **Apenas idempotency_key (sem checkpoint)**: cobre
  side-effects externos, mas o dispatcher ainda re-faz fold
  + dispatch do zero a cada restart. Caro em fleet grande.

---

## 5. Consequências

### Para o time de desenvolvimento

- Toda nova tool **deve** aceitar `idempotency_key` na
  assinatura.
- Tools com side-effects externos **devem** implementar
  dedup (cache local, Redis, ou passar adiante).
- O `ReactiveDispatcher` em produção **deve** receber um
  `CheckpointStore`. Não há fallback implícito.

### Para o time de infra

- Redis usado pelo FMH **deve** ter `appendonly yes` e
  `appendfsync everysec` (recomendado mínimo).
- Monitorar lag por agente via `CheckpointStore.load_all()`.

### Para a arquitetura

- O framework oferece **at-least-once** por default. A
  aplicação é responsável por mover para **at-most-once**
  via `idempotency_key` quando necessário.
- O `KnowledgeConsolidator` (ADR-010) **não exige checkpoint
  próprio**. A deduplicação é dupla:
  `(Problem {fingerprint}, Action {request_event_id})` via
  `MERGE` no FalkorDB + `event_id` determinístico no
  EventLog. Replay puro do log reconstrói os mesmos pares;
  o promoter resolve duplicação no grafo. Reuso do
  `ReactiveCheckpoint` aqui seria over-engineering — o
  promoter é write-only idempotente por construção.

---

## 6. Veja também

- [docs/checkpoints.md](../docs/checkpoints.md) — guia completo
- [ADR-002: Replay canônico](../ADRs/ADR-002-Replay-Puro.md) —
  porque o EventLog é a fonte de verdade
- [ADR-004 §2.3: Tools são Protocol no core](../ADRs/ADR-004-Memory-Tools-Knowledge.md#23-tools-são-protocol-no-core-com-resiliência)
- [ADR-010: Memory Tier "business"](../ADRs/ADR-010-Memory-Business-Tier.md) —
  sub-grafo de Solutions; usa MERGE idempotente em vez de
  checkpoint dedicado.
- Tests: `tests/integration/runner/test_checkpoint.py`
- Tests: `tests/unit/tools/test_invoker.py`
