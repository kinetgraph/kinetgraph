<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-010: Memory Tier "business" — Solutions como Memória de Longo Prazo de Tool Calls

**Status:** Aceito
**Data:** 10 de junho de 2026
**Versão:** 1.0
**Autores:** Equipe de Arquitetura FMH
**Relacionado a:** [ADR-001](./ADR-001-Arquitetura.md), [ADR-002](./ADR-002-Replay-Puro.md), [ADR-004](./ADR-004-Memory-Tools-Knowledge.md), [ADR-005](./ADR-005-Checkpoints-Idempotency.md), [ADR-014](./ADR-014-Continuity-Tier.md)

---

## 1. Contexto

O FMH v0.2.x tem dois tiers de memória funcionando em loop
de consolidação:

- **Session** (`memory/session.py`) — Redis JSON com TTL,
  cache read-through.
- **Profile** (`memory/profile.py`) — Redis Hash, sem TTL,
  cache read-through.

Ambos são agentes no EventLog e são atualizados pelo
`Consolidator` (cíclico, dentro do tick do `Runner`).

> **Nota**: A partir do ADR-014, o `profile` foi refocado
> para "preferências estáticas da PME" e o estado-de-uso
> recente migra para um novo tier `continuity` (Redis Hash,
> TTL sliding, PII hash-only). Solutions (`"business"`) é
> **ortogonal** a ambos — é agregação **cross-agent** no
> FalkorDB, não estado per-user. Veja
> [ADR-014](./ADR-014-Continuity-Tier.md) §2.2 para a
> separação e §6 para a justificativa.

O ADR-004 §2.4 já declara o **Knowledge tier** (FalkorDB)
como projeção secundária para "GraphRAG e (futuramente)
GNN/PyG". Mas o que está implementado (`FalkorDBProjector` +
`GraphRAGRetriever` em `knowledge/`) é uma **projeção one-shot
manual**, chamada por `project_all()` em cold start, **fora**
do loop de consolidação. O grafo FalkorDB não é atualizado
por agente que processa tool calls; é atualizado por um
operador que decide rodar o rebuild.

Consequências:

- O grafo FalkorDB vira uma fotografia desatualizada. Tool
  calls do dia-a-dia nunca chegam lá a menos que alguém
  dispare `project_all()`.
- Não há mecanismo de **reuso**: a aplicação rodou 500 NF-e
  hoje, e a 501ª não tem como saber "qual a última solução
  que funcionou para CNPJ X e CFOP Y".
- Não há **PII gate** — qualquer dado no EventLog pode ser
  projetado no grafo, mesmo se contiver dados sensíveis.
- Não há **man-in-the-loop** para promoções críticas (tools
  que movem dinheiro, ex: `bank.transfer`).

Este ADR registra a decisão de tratar o FalkorDB como um
**tier de memória vivo**, atualizado por um **consolidador
separado pós-tick**, e de modelar o conteúdo como
**Solutions reutilizáveis** ancoradas em tool calls
concluídas com sucesso.

---

## 2. Decisão

### 2.1 `MemoryKind` ganha `"business"`

```python
MemoryKind = Literal["session", "profile", "business"]
# ADR-014 expande para:
# MemoryKind = Literal["session", "profile", "continuity", "business"]
```

`"business"` é o discriminante da fila de consolidação para
o FalkorDB. O nome é curto, simétrico com `"session"` /
`"profile"`, e descreve **para que sink o candidato vai**, não
a operação que o produziu (a operação é uma tool call — já
descrita por `event_type` no EventLog).

`"business_solutions"` foi considerado e rejeitado por
redundância: o `MemoryKind` é o discriminante, "solutions" já
é a forma do conteúdo (paralelo a "session" descrevendo uma
sessão, não "session_data"). `"business_operations"`
também rejeitado por ser verboso para um `Literal` que
alimenta `match`/narrowing.

> **Ortocionalidade com `continuity`** (ADR-014): Solutions
> é agregação **cross-agent** no FalkorDB (pergunta "qual a
> solução típica para este `Problem` em todo o tenant?").
> `continuity` é estado **per-user** em Redis Hash (pergunta
> "qual foi o último CFOP usado por este user?"). Os dois
> coexistem sem ambiguidade: `continuity` alimenta o agente
> no momento da decisão; `business` alimenta o reuso entre
> agentes no momento da busca.

### 2.2 Sem `(:Solution)` no MVP — Design A

Nó `(:Solution)` (agregado de N Actions) é **adiado**. O MVP
trabalha com a tríade `(:Problem) -[:SOLVED_BY]-> (:Action) -[:ON_TOOL]-> (:Tool)`.
Vantagens:

- 1 nó a menos no schema.
- Reuso = mesma `(:Action)` aparecendo em N `(:Problem)`. Não
  precisa de `confidence` em outro nível.
- Workflows compostos (1 solução = 3 tool calls sequenciais)
  entram no F-sprint seguinte se ficar claro que são
  necessários.

`(:Solution)` pode ser reintroduzido depois como **agregado
opcional** sem breaking change — a aresta `(:Action)-
[:STEP_OF]->(:Solution)` é aditiva.

### 2.3 Sem evento `solution.applied` — inferir de `tool.*.completed`

Não há evento novo. O `SolutionExtractor` lê o `World`
filtrando `event_type matches "tool\\..+\\.(completed|failed)"`
e reconstrói `(Problem, Action, Outcome)` direto do que já
existe no EventLog. Isso preserva o princípio do ADR-001
§1 Princípio 3 (estado derivado, não duplicado) e mantém a
projeção reconstruível do zero a partir do log.

Idempotência: o write no FalkorDB usa `MERGE` em
`(:Action {request_event_id})` + `MERGE` em
`(:Problem {fingerprint})` separadamente, com `MATCH ...
MERGE (:Problem)-[:SOLVED_BY]->(:Action)` ligando os dois.
Replays produzem o mesmo grafo.

### 2.4 `KnowledgeConsolidator` separado, pós-tick, opt-in

`Consolidator` (existente) continua focado em **Redis cache
(Session + Profile)** — read-through, latência sub-ms, roda
no tick do `Runner` via `as_cyclic_system()`.

**Novo `KnowledgeConsolidator`** (paralelo) cuida
**exclusivamente do FalkorDB**:

- Roda em **coroutine própria**, fora do tick loop.
- `interval` configurável (env `KNT_KNOWLEDGE_INTERVAL_S`,
  default `10.0`).
- Lê o `World` (fold do EventLog), chama
  `SolutionExtractor.extract(world)`, publica no
  `SolutionPromotionBus`, dispara `SolutionPromoter.pump_once()`.
- **Opt-in por tenant**: a flag fica em
  `knt:tenant:{cnpj}:flags` (hash Redis) com campo
  `knowledge_enabled: "1"`. Default: desabilitado. Cold
  start sem FalkorDB = zero overhead.

Por que separar:

- **Custo diferente**: Redis write é sub-ms. FalkorDB MERGE
  com embedding é 10-100ms. Consolidar no tick dobra a
  latência do caminho crítico.
- **Falha diferente**: FalkorDB pode estar fora. Se cair, não
  pode parar o tick.
- **Reconfiguração diferente**: trocar `EmbeddingProvider`
  exige reprojetar FalkorDB. Trocar TTL de cache é trivial.
- **Teste diferente**: `Consolidator` testa com
  `redis_aioredis` mock. `KnowledgeConsolidator` testa com
  FalkorDB (skip se não disponível).

### 2.5 PII: 3 níveis, default nível 1, fail-closed

`PiiRedactionTool` mora em `fmh_backend/tools/pii.py` (core,
não `fmh_agents` — PII é transversal). Níveis:

| Nível | Mecanismo | Quando ativa | Custo | Cobre |
|------|-----------|--------------|-------|-------|
| **1 — Heurístico** | Regex PT/EN (CPF, CNPJ, e-mail, telefone, CEP, chave PIX) | **Sempre** | < 1ms | ~60% dos casos |
| **2 — GLiNER2 (NER)** | `gliner2` classifica com `labels=` do schema da tool | Opt-in, env `KNT_PII_LEVEL=2` | ~20ms | +25% (nomes, endereços) |
| **3 — GLiNER2 v1.5 (task `pii`)** | Classificação de PII dedicada | Opt-in, env `KNT_PII_LEVEL=3` | batch assíncrono | +15% residuais |

Lista de labels default shipped pelo framework:

```python
DEFAULT_PII_LABELS = (
    "cpf", "cnpj", "email", "telefone",
    "endereco", "nome_pessoa", "chave_pix",
    "cartao_credito",
)
```

Override por tenant: `knt:tenant:{cnpj}:pii_labels` (set
Redis).

**Política fail-closed**: se a `PiiRedactionTool` lançar
exceção, o `SolutionPromoter` **não grava** e emite
`pii.check_failed` event no EventLog com
`data = {candidate_id, reason, tool_name, request_event_id}`.
O DLQ pega o evento. Aplicação precisa de alerta.

`redact_at_promotion=True` por padrão no `SolutionPromoter`.
O `data` original do tool request **nunca** é gravado no
FalkorDB — ou grava redacted, ou nem chega lá.

### 2.6 Man-in-the-loop: dois limiares

**Threshold 1 — `confidence`**: bump cross-agent. Lógica
pura no `SolutionExtractor` (lê histórico do `World`):
se o par `(problem_fingerprint, action_params_fingerprint)`
foi visto em **N agentes diferentes** (N configurável, env
`KNT_SOLUTIONS_CONFIDENCE_BUMP_AGENTS`, default `2`),
`confidence++`. Sem LLM.

**Threshold 2 — `approval_list`**: por tool, por tenant.
Configuração em `knt:tenant:{cnpj}:approval_list` (set
Redis). Tools listadas **nunca** auto-promovem, mesmo com
confidence alta.

Candidatos que falham qualquer limiar vão para a **review
queue** (Redis Stream `knt:solutions:review`, TTL
`KNT_SOLUTIONS_REVIEW_TTL_S`, default `604800` = 7 dias).
Operador consome via API HTTP da aplicação (fora do escopo
do framework). Aprovação emite `solution.promoted` event no
EventLog, que o `SolutionPromoter` consome e grava no
FalkorDB. Rejeição emite `solution.rejected` e vai pro DLQ.

**Default do MVP**: threshold 1 = `confidence < 1` (ou
seja, tudo auto-promove, exceto approval list). Threshold 2
vazio. Review queue existe mas fica ociosa até configuração
do tenant.

---

## 3. Grafo (FalkorDB)

Por tenant (`fmh_tenant_{cnpj}`), o sub-grafo de Solutions
convive com o sub-grafo de Documentos já existente
(ADR-004 §2.4):

```
(:Agent   {agent_id, last_seen, tenant_id})
(:Document {id, agent_id, event_type, data_json, embedding: vec_f32})
(:ToolCall {id, tool, request_id, status, latency_ms, agent_id})
(:Entity  {name, type, embedding: vec_f32})      # F9+, ADR-004

# Solutions tier (este ADR):
(:Tool     {name, description, input_schema_json})
(:Problem  {fingerprint, embedding: vec_f32, tags_json})
(:Action   {request_event_id, params_fingerprint, params_json})
(:Outcome  {status, latency_ms, result_signature, error_message})

(a:Agent)-[:HAS_DOC]->(d:Document)
(d:Document)-[:MENTIONS]->(e:Entity)
(a:Agent)-[:CALLED]->(t:ToolCall)

(p:Problem)-[:SOLVED_BY {confidence, validated_count}]->(a:Action)
(p:Problem)-[:FAILED_WITH {confidence, validated_count}]->(a:Action)
(a:Action)-[:ON_TOOL]->(tool:Tool)
(a:Action)-[:PRODUCED]->(o:Outcome)
```

**Chaves naturais**:

- `(:Tool {name})` — populado por `ToolRegistry.list_tool_descriptors()` no boot do promoter. `MERGE` por `name`.
- `(:Problem {fingerprint})` — `fingerprint = sha256(json(data, sort_keys=True))[:16]`. Mesma entrada do EventLog = mesmo fingerprint.
- `(:Action {request_event_id})` — `request_event_id = str(event_id)` do `tool.X.requested` no EventLog. Estável por construção (uuid5 determinístico).
- `(:Outcome)` — sem chave própria. Ancorado pela aresta `(:Action)-[:PRODUCED]->(:Outcome)`. Outcome é read-only após criação.

**Índices vetoriais**:

- `(:Problem).embedding` — mesma dimensão do `EmbeddingProvider` do tenant. `CREATE VECTOR INDEX FOR (p:Problem) ON (p.embedding) OPTIONS {dimension: $dim, similarityFunction: 'cosine'}`.
- `(:Document).embedding` e `(:Entity).embedding` (F9+) — já documentados em ADR-004 §2.4 e `docs/graphrag.md` §3.

**Importante**: 1 dimensão de embedding por tenant, fixa no
deploy. Misturar dimensões no mesmo FalkorDB é possível
(índices separados por label) mas caro de manter.

### 3.1 PII e o grafo

`(:Problem).tags_json` é construído a partir do `data` do
tool event **após** passar pelo `PiiRedactionTool` no nível
configurado. Os labels default cobrem os PIIs mais comuns
em automação fiscal/ERP brasileira.

A `(:Action).params_json` armazena os parâmetros da tool
chamada **redacted**. A `(:Action).params_fingerprint`
(usado em `(:Problem)-[:SOLVED_BY]->(:Action)` MERGE) é
hash do payload redacted — duas chamadas semanticamente
iguais (mesmos campos, valores redacted idênticos)
colidem no fingerprint.

---

## 4. Pipeline Consolidator → 3 buses → 3 sinks (estendido a 4 pelo ADR-014)

```
                       ┌─────────────────────────────────┐
                       │       EventLog (Redis Streams)   │
                       │  (source of truth)               │
                       └────────────────┬─────────────────┘
                                        │
                               World.fold(events)
                                        │
                 ┌──────────────────────┼──────────────────────┐
                 ▼                      ▼                      ▼
        ┌────────────────┐     ┌────────────────┐     ┌────────────────┐
        │  Consolidator  │     │  Consolidator  │     │  Knowledge     │
        │  (Redis cache) │     │  (Redis cache) │     │  Consolidator  │
        │  cyclic, tick  │     │  cyclic, tick  │     │  post-tick     │
        └────────┬───────┘     └────────┬───────┘     └────────┬───────┘
                 │                      │                      │
                 ▼                      ▼                      ▼
        ┌────────────────┐     ┌────────────────┐     ┌────────────────┐
        │ CacheRefreshBus│     │ CacheRefreshBus│     │ SolutionPromo- │
        │  (session)     │     │  (profile)     │     │ tionBus        │
        └────────┬───────┘     └────────┬───────┘     └────────┬───────┘
                 │                      │                      │
                 ▼                      ▼                      ▼
        ┌────────────────┐     ┌────────────────┐     ┌────────────────┐
        │ CacheWarmer    │     │ CacheWarmer    │     │ SolutionPromot.│
        │ → Redis JSON   │     │ → Redis Hash   │     │ → FalkorDB     │
        └────────────────┘     └────────────────┘     └────────────────┘

(ADR-014 adiciona um quarto branch de Consolidator →
 CacheRefreshBus (continuity) → CacheWarmer → Redis Hash
 com TTL sliding. Veja ADR-014 §3.)
```

`Consolidator` e `CacheWarmer` são o código existente
(ADR-004). `KnowledgeConsolidator`, `SolutionPromotionBus`
e `SolutionPromoter` são novos. Os dois loops rodam em
coroutines paralelas; o `Consolidator` no tick do `Runner`
e o `KnowledgeConsolidator` em sua própria coroutine com
`interval` configurável.

---

## 5. Critérios de Aceitação

- [ ] `MemoryKind` aceita `"business"` e o `match` no Consolidator trata exaustivamente.
- [ ] `SolutionExtractor.extract(world)` é puro: mesma entrada → mesma saída. Testado sem Redis.
- [ ] `KnowledgeConsolidator.start()`/`stop()` funcionam como coroutines independentes; interval configurável.
- [ ] `KnowledgeConsolidator` é opt-in: desabilitado por default, flag em `knt:tenant:{cnpj}:flags`.
- [ ] `SolutionPromoter.upsert_solution` é idempotente: rodar 2x produz o mesmo grafo.
- [ ] `PiiRedactionTool` (nível 1) substitui CPF/CNPJ/e-mail/telefone por placeholders antes do MERGE.
- [ ] `PiiRedactionTool` falhando → `pii.check_failed` event no EventLog + DLQ. Nada grava.
- [ ] `find_solutions_by_problem(embedding, k=5)` retorna top-k por similaridade cosseno em `(:Problem).embedding`.
- [ ] `find_solutions_by_tool("invoice.issue", k=5)` retorna actions daquele tool, ordenadas por confidence.
- [ ] Confidence bump cross-agent: `SolutionExtractor` incrementa `confidence` quando o par é visto em N agentes diferentes.
- [ ] Approval list por tenant: tools listadas em `knt:tenant:{cnpj}:approval_list` vão pra review queue, nunca auto-promovem.
- [ ] GLiNER2 nível 2/3 são opcionais (deps opcionais `fmh-backend[gliner]`). Default = só nível 1.
- [ ] 1 dimensão de embedding por tenant; doc explícito em `graphrag.md` §3.

---

## 6. Consequências

### Positivas

- ✅ **Memória de longo prazo viva**: tool calls viram knowledge automaticamente, sem `project_all()` manual.
- ✅ **Reuso operacional**: agente novo consulta `find_solutions_by_problem` e tem os 3 tool events similares dos últimos 30 dias na mão.
- ✅ **PII gate**: nada chega no FalkorDB sem passar pelo `PiiRedactionTool` (default fail-closed).
- ✅ **Man-in-the-loop explícito**: tools críticas (banco, fiscais) podem exigir aprovação humana.
- ✅ **Caminho crítico preservado**: `KnowledgeConsolidator` não bloqueia o tick.
- ✅ **Recuperável**: FalkorDB é reconstruível rodando `KnowledgeConsolidator` num tenant recém-criado.

### Negativas

- ⚠️ **Mais uma dependência opcional**: `gliner2` no caminho de PII nível 2/3. Mitigação: extra opcional, default nível 1 sem GLiNER2.
- ⚠️ **Custo de embedding por tool event**: cada `tool.<name>.completed` vira embedding em `(:Problem)`. Mitigação: allow-list de tools (env `KNT_SOLUTIONS_TOOL_ALLOWLIST`).
- ⚠️ **Review queue acumula sem operador**: TTL de 7 dias esvazia por DLQ; sem SLA. Mitigação: doc recomenda integração com API HTTP de operação (fora do framework).
- ⚠️ **Confidence bump cross-agent exige fold caro**: o `SolutionExtractor` precisa ler o histórico. Mitigação: cursor `last_event_id` por agente; reuso cross-agent é O(history) por tick, mas amortizado pelo interval de 10s.

### Mitigações

| Problema | Mitigação |
|----------|-----------|
| FalkorDB indisponível | `KnowledgeConsolidator.start()` loga warning e segue sem escrever. EventLog é truth; graph é projeção. |
| Embedding caro | Allow-list por tool; filtrar por `event_type` antes de indexar. |
| PII falso negativo em regex | 3 níveis opt-in; LGPD compliance é responsabilidade do tenant configurar nível 2/3. |
| Review queue órfã | TTL + doc recomenda operador com endpoint HTTP próprio. |
| Schema FalkorDB colide entre tenants | 1 graph por tenant (`fmh_tenant_{cnpj}`) já garante isolamento. |

---

## 7. Roadmap de Implementação

1. **Fase 1** (esta sprint): este ADR + updates de
   `ADR-004`, `docs/graphrag.md`, `docs/consolidation.md`.
2. **Fase 2**: `memory/solutions.py` (value objects +
   `SolutionExtractor` puro + `SolutionPromotionBus` +
   `SolutionPromoter` skeleton); `knowledge/extraction/`
   (heurístico + GLiNER2 opt-in); `KnowledgeConsolidator`
   sem FalkorDB ainda. Testes unitários.
3. **Fase 3**: `SolutionProjector` (Cypher MERGE +
   `PiiRedactionTool`); `find_solutions_by_problem` /
   `find_solutions_by_tool`; `ToolRegistry.list_tool_descriptors()`.
   Testes de integração (FalkorDB).
4. **Fase 4**: métricas structlog, allow-list,
   confiança bump cross-agent, review queue,
   [`09_knowledge_consolidation.py`](../../fmh_agents/examples/09_knowledge_consolidation.py).

---

## 8. Referências

- [ADR-001: Arquitetura geral](./ADR-001-Arquitetura.md)
- [ADR-002: Replay canônico](./ADR-002-Replay-Puro.md)
- [ADR-004: Memory Tiers, Tools e Projeção para FalkorDB](./ADR-004-Memory-Tools-Knowledge.md)
- [ADR-005: Checkpoints e idempotency](./ADR-005-Checkpoints-Idempotency.md)
- [ADR-014: Memory Tier "continuity" — Estado-de-Uso Recente](./ADR-014-Continuity-Tier.md)
- [docs/graphrag.md](../docs/graphrag.md) — retrieve e modelo
- [docs/consolidation.md](../docs/consolidation.md) — loop de consolidação
- [Redis Agent Builder: How agents work](https://redis.io/docs/latest/develop/ai/agent-builder/agent-concepts/)
- [FalkorDB Vector Index](https://docs.falkordb.com/cypher/indexing/vector-index.html)
- [FalkorDB GraphRAG SDK](https://docs.falkordb.com/genai-tools/graphrag-sdk.html)
- [GLiNER2 (v1.5+)](https://github.com/urchade/GLiNER2) — classificação de PII
