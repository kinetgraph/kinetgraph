<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-004: Memory Tiers, Tools e Projeção para FalkorDB

**Status:** Aceito
**Data:** 06 de junho de 2026
**Versão:** 1.2 (incorpora ADR-014: tier de Continuity)
**Autores:** Equipe de Arquitetura FMH
**Relacionado a:** [ADR-001](./ADR-001-Arquitetura.md), [ADR-002](./ADR-002-Replay-Puro.md), [ADR-003](./ADR-003-Ciclo-Dual.md), [ADR-010](./ADR-010-Memory-Business-Tier.md), [ADR-014](./ADR-014-Continuity-Tier.md)

---

## 1. Contexto

O FMH v2.0 (F0–F7) tem um event log puro em Redis Streams e
uma vertical PME (NF-e, fechamento mensal) funcional. Faltam
três capacidades para cobrir o caso de uso declarado —
"automação de processos de PMEs brasileiras":

1. **Memória de longo prazo** para sessão, perfil e
   preferências (atualmente inexistente).
2. **Tools** — forma padronizada para sistemas invocarem
   funções externas (fiscal authority, banco, ERP).
3. **Projeção FalkorDB** — grafo de conhecimento para
   GraphRAG e (futuramente) GNN/PyG.

Este ADR registra as decisões para o F8.

---

## 2. Decisão

### 2.1 Memória tem 4 tiers de cache/projeção, cada um com estrutura Redis/FalkorDB distinta

| Tier | Caso de uso | Estrutura | Chave | Vida útil |
|------|-------------|-----------|-------|-----------|
| **Working** | ECS (World, AgentView) | In-memory | — | Tick (volátil) |
| **Session** | Conversa, working memory | Redis JSON com TTL | `knt:session:{session_id}` | min–horas |
| **Profile** | Preferências estáticas da PME / usuário | Redis Hash | `knt:profile:{tenant_id}:{user_id}` | meses–anos |
| **Continuity** | Estado-de-uso recente (última tool, último cliente, último CFOP) | Redis Hash com TTL sliding | `knt:continuity:{tenant_id}:{user_id}` | dias–semanas (sliding) |
| **Knowledge** (Documentos) | Busca semântica livre em eventos indexados | FalkorDB — sub-grafo `(:Document)` | `knt:tenant:{cnpj}` (graph) | permanente |
| **Knowledge** (Solutions) | Reuso de tool calls bem-sucedidas | FalkorDB — sub-grafo `(:Problem)-[:SOLVED_BY]->(:Action)-[:ON_TOOL]->(:Tool)` | `knt:tenant:{cnpj}` (graph) | permanente |
| **Event log** | Source of truth (TUDO) | Redis Streams per-agent | `knt:agents:{id}:events` | permanente (trim) |

**Separação `profile` vs `continuity`** (ADR-014):
`profile` modela "o que a PME é" (regime tributário, tier SLA,
e-mail de NF-e, idioma). `continuity` modela "o que a PME estava
fazendo" (última tool usada, último CNPJ de cliente, último
CFOP escolhido). Os dois cohabitam o mesmo `(tenant_id, user_id)`
mas têm lifecycles, auditabilidade e gates de PII distintos.
Critério prático: campo que muda em resposta a uma tool call →
`continuity`; campo configurado explicitamente pelo usuário →
`profile`. Veja [ADR-014](./ADR-014-Continuity-Tier.md) para a
decisão completa, vocabulário de eventos e política de TTL
sliding.

Os dois sub-grafos do Knowledge tier compartilham o mesmo
graph por tenant (`fmh_tenant_{cnpj}`) e a mesma dimensão
de embedding, mas têm labels separadas e lifecycles
diferentes. Veja [ADR-010](./ADR-010-Memory-Business-Tier.md)
para o sub-grafo de Solutions; este ADR cobre o sub-grafo
de Documentos.

**Princípio-chave**: o EventLog é a **única fonte da verdade**.
Os outros tiers são **projeções cacheáveis e reconstruíveis**:

- Session e Profile são agentes no EventLog. O `World.fold`
  os reconstrói. O Hash/JSON no Redis é um **cache** que o
  sistema escreve após a fold, para acesso O(1) sem replay.
- FalkorDB é um **grafo de conhecimento** populado por
  consolidatores (um por sub-grafo). O sub-grafo de
  Documentos é atualizado pelo `FalkorDBProjector`
  existente; o sub-grafo de Solutions é atualizado pelo
  `KnowledgeConsolidator` (ADR-010 §2.4) em coroutine
  separada pós-tick. Ambos podem ser reconstruídos do zero
  a partir do log.
- Vector embeddings são computados por um *embedding
  provider* plugável e armazenados como propriedade
  `embedding: vec_f32` em nós FalkorDB. Mesma dimensão por
  tenant; 1 índice vetorial por label.

### 2.2 Session, Profile e Continuity são **agentes** (modelo coerente)

```python
# agent_id = "session:550e8400-e29b-41d4-a716-446655440000"
# Eventos: session.started, session.message, session.ended
# State: dict[str, Any] (contexto da conversa)

# agent_id = "profile:12.345.678/0001-90:user-001"
# Eventos: profile.preference_changed, profile.tier_changed
# State: dict[str, str] (preferências estáticas chave-valor)

# agent_id = "continuity:12.345.678/0001-90:user-001"
# Eventos: continuity.tool_used, continuity.entity_seen,
#          continuity.category_chosen, continuity.cleared
# State: dict[str, str] com prefixos (tool:, entity:, last:)
```

**Implicações**:

- Replay puro reconstrói session, profile e continuity a partir do log.
- Audit trail completo: "quem mudou a preferência X, quando
  e por quê" é uma query de correlação no EventLog; "qual foi
  o último CFOP usado pelo user Y" é uma query idem em
  `continuity.category_chosen`.
- O mesmo idempotency index do EventLog evita duplicação
  (ex: dois adapters emitindo `profile.tier_changed` para o
  mesmo `user` no mesmo tick = 1 evento; dois tools emitindo
  `continuity.tool_used` para a mesma `params_fingerprint`
  colapsam em 1).
- TTL em `knt:session:{id}` é **cosmético** — a verdade está
  no stream. TTL apenas libera memória no Redis. O TTL sliding
  de `knt:continuity:{tenant}:{user}` segue a mesma filosofia
  (libera memória, não é fonte da verdade).

### 2.3 Tools são **Protocol** no core, com resiliência

```python
# core/tools.py
class Tool(Protocol):
    """A pure description of a side-effecting capability."""

    name: str
    description: str
    input_schema: dict  # JSON schema for the LLM

    async def invoke(self, **kwargs) -> Result[Any, ToolError]:
        """Adapter-owned implementation. Idempotent if possible."""
        ...


# Como o sistema chama uma tool:
# 1. Sistema emite "tool.invoice.issue.requested" com payload
# 2. Adapter (com circuit breaker) consome, chama o serviço externo
# 3. Adapter emite "tool.invoice.issue.completed" ou ".failed"
# 4. Sistema reativo reage ao completed/failed
```

Vantagens:

- Sistema puro continua puro (não toca o serviço externo direto)
- Circuit breaker, retry, bulkhead ficam no **adapter**
- Idempotência: tool que pode falhar parcialmente é
  re-apelável via reprocess do DLQ
- LLM pode escolher tools (futuro): o `input_schema` é
  introspectável e serializável para o prompt

### 2.4 FalkorDB é projeção secundária, com multi-tenant isolation

**Layout**:

```
$ SELECT GRAPH fmh_tenant_{cnpj}
  # Sub-grafo de Documentos (este ADR):
  (:Agent {agent_id, agent_type, last_consolidated_tick})
  (:Component {slot, data_json})
  (:Document {content, embedding: vec_f32, ...})
  (:Entity {name, type, embedding: vec_f32})    # F9+
  (:ToolCall {tool, input, output, status, latency_ms})

  (a:Agent)-[:HAS_COMPONENT]->(c:Component)
  (a:Agent)-[:EMITTED]->(d:Document)
  (d:Document)-[:MENTIONS]->(ent:Entity)
  (ent1)-[:RELATED_TO]->(ent2)
  (a:Agent)-[:CALLED]->(t:ToolCall)

  # Sub-grafo de Solutions (ADR-010):
  (:Tool     {name, description, input_schema_json})
  (:Problem  {fingerprint, embedding: vec_f32, tags_json})
  (:Action   {request_event_id, params_fingerprint, params_json})
  (:Outcome  {status, latency_ms, result_signature, error_message})

  (p:Problem)-[:SOLVED_BY {confidence, validated_count}]->(a:Action)
  (p:Problem)-[:FAILED_WITH {confidence, validated_count}]->(a:Action)
  (a:Action)-[:ON_TOOL]->(tool:Tool)
  (a:Action)-[:PRODUCED]->(o:Outcome)
```

O sub-grafo de Solutions é gerenciado pelo
`KnowledgeConsolidator` (coroutine pós-tick) e alimentado
pelo `SolutionPromoter`. Veja
[ADR-010](./ADR-010-Memory-Business-Tier.md) para a
semântica completa de `(Problem, Action, Outcome, Tool)`.
$ SELECT GRAPH fmh_tenant_{cnpj}
  (:Agent {agent_id, agent_type, last_consolidated_tick})
  (:Component {slot, data_json})
  (:Document {content, embedding: vec_f32, ...})
  (:Entity {name, type, embedding: vec_f32})
  (:ToolCall {tool, input, output, status, latency_ms})

  (a:Agent)-[:HAS_COMPONENT]->(c:Component)
  (a:Agent)-[:EMITTED]->(e:Document)
  (d:Document)-[:MENTIONS]->(ent:Entity)
  (ent1)-[:RELATED_TO]->(ent2)
  (a:Agent)-[:CALLED]->(t:ToolCall)
```

**Estratégia de população**:

- **Projection system cíclico** (`projection_to_graph`):
  - Lê o EventLog (cíclico ou via CDC)
  - Para cada agente novo/alterado, faz `MERGE` no FalkorDB
  - Para cada documento novo, computa embedding e faz
    `MERGE` com a propriedade `embedding`
- FalkorDB é **reconstruível** rodando o projection replay
- Multi-tenant: cada tenant tem seu próprio graph
  (`fmh_tenant_{cnpj}`) — isolamento total

**Por que FalkorDB e não Redis Vector Set**:

- GraphRAG é o caso de uso principal. FalkorDB permite
  combinar `vector_distance` + `MATCH` em uma única query
  Cypher. Redis Vector Set exigiria orquestração cliente.
- GNN/PyG (futuro): FalkorDB exporta o grafo, PyG ingere.
- Já é a tecnologia escolhida na v1 do projeto e tem
  **vector index nativo**.

**Trade-off conhecido**: FalkorDB é uma dependência
operacional a mais (segunda porta Redis-compat). Para o MVP
do F8 isso é aceitável; podemos mover a projeção para Redis
8 Vector Set se a operação ficar muito cara.

---

## 3. Estrutura de Diretórios (F8 + ADR-010 + ADR-014)

```
fmh_backend/src/fmh_backend/
├── memory/                       # F8.1 + ADR-010 + ADR-014
│   ├── base.py                   # BaseShortTermMemory (cache read-through)
│   ├── session.py                # SessionManager
│   ├── profile.py                # ProfileManager (preferências estáticas)
│   ├── continuity.py             # ADR-014: ContinuityManager
│   │                             # (estado-de-uso recente, TTL sliding,
│   │                             #  PII hash-only, LGPD cleared)
│   ├── consolidation.py          # session/profile/continuity cache ↔ EventLog
│   ├── cache_warmer.py           # Sink adapter (Redis), 3 kinds
│   ├── solutions.py              # ADR-010: Problem/Action/Outcome,
│   │                             # SolutionExtractor, SolutionPromoter,
│   │                             # SolutionPromotionBus, PiiRedactionTool glue
│   └── knowledge_consolidator.py # ADR-010: post-tick orchestrator
│                                 # (FalkorDB sink, opt-in per tenant)
│
├── tools/                        # F8.2 + ADR-010
│   ├── protocol.py               # Tool Protocol
│   ├── registry.py               # ToolRegistry + list_tool_descriptors (ADR-010)
│   ├── invoker.py                # ToolInvoker
│   └── pii.py                    # ADR-010: PiiRedactionTool (3 níveis)
│
└── knowledge/                    # F8.3 + ADR-010
    ├── falkordb/
    │   ├── client.py             # FalkorDB connection
    │   ├── schema.py             # Tenant graph schema
    │   ├── adapter.py            # Event → Document/ToolCall projection
    │   └── solution_projector.py # ADR-010: Event → Problem/Action/Outcome
    ├── embedding/
    │   └── provider.py           # EmbeddingProvider Protocol
    ├── extraction/               # ADR-010: entity/feature extraction
    │   ├── base.py               # EntityExtractor Protocol
    │   ├── heuristic.py          # HeuristicEntityExtractor (default)
    │   └── gliner.py             # GlinerEntityExtractor (opt-in)
    └── graphrag/
        └── retriever.py          # vector_search (legado) +
                                  # find_solutions_by_problem (ADR-010) +
                                  # find_solutions_by_tool (ADR-010)
```

`fmh_agents/` ganha:

```
fmh_agents/src/fmh_agents/
└── tools/                        # Domain tools (fiscal, ERP, banco)
    ├── invoice_issue.py
    ├── erp.py
    └── bank.py
```

---

## 4. Ordem de Implementação

1. **F8.1 Memory** (✓ entregue): session + profile como
   agents no EventLog + cache Redis (Hash, JSON com TTL).
2. **F8.2 Tools** (✓ entregue): Protocol + Registry + 1 tool
   real mockada. Cenário: invoice → tool externa → response.
3. **F8.3 Knowledge** (✓ entregue parcial): `FalkorDBClient`,
   `FalkorDBProjector` (Document/ToolCall), `GraphRAGRetriever`
   (vetorial puro). Sub-grafo de Documentos funcional.
4. **ADR-010 Solutions** (próxima): sub-grafo de Solutions
   (Problem/Action/Outcome/Tool), `KnowledgeConsolidator`
   pós-tick, `PiiRedactionTool` 3 níveis, man-in-the-loop
   via review queue. Veja [ADR-010](./ADR-010-Memory-Business-Tier.md)
   para o roadmap em 4 fases (docs → puro → adapters → polish).
5. **ADR-014 Continuity** (próxima): tier de estado-de-uso
   recente, separado de `profile`. TTL sliding, PII hash-only,
   LGPD `continuity.cleared`. Veja
   [ADR-014](./ADR-014-Continuity-Tier.md) para o roadmap em
   4 fases (docs → manager + bus → API `recency_suggest` →
   migração de tenants legados).

---

## 5. Critérios de Aceitação

- [ ] Session/Profile/Continuity são agentes (mesmo `World.fold`,
      mesma idempotência, mesmo replay puro).
- [ ] `knt:session:{id}` é Redis JSON com TTL ≤ 24h.
- [ ] `knt:profile:{tenant}:{user}` é Redis Hash, sem TTL
      obrigatório.
- [ ] `knt:continuity:{tenant}:{user}` é Redis Hash com TTL
      sliding (renovado a cada write).
- [ ] `continuity.entity_seen` armazena apenas `value_hash`;
      `value` raw nunca chega no EventLog do `continuity` agent.
- [ ] `continuity.cleared` zera o estado projetado no cache;
      histórico no EventLog permanece intocado.
- [ ] Tool Protocol vive no `core`, adapters no
      `fmh_agents/tools/`.
- [ ] Tool calls passam por circuit breaker.
- [ ] FalkorDB tem 1 graph por tenant.
- [ ] Projection system reconstrói FalkorDB a partir do
      EventLog do zero (idempotente).
- [ ] Embedding provider é plugável; testes rodam com
      provider fake (sem rede).

---

## 6. Consequências

### Positivas

- ✅ **Modelo coerente**: session e profile seguem as
  mesmas regras que o resto (event-sourced, idempotente,
  auditado).
- ✅ **GraphRAG nativo**: queries híbridas vetor + grafo em
  uma só Cypher.
- ✅ **Multi-tenant isolado**: segurança, performance
  previsível.
- ✅ **Tools formalizadas**: caminho padrão para I/O
  externo com resiliência.
- ✅ **Plugar embeddings**: testável sem rede; produção
  escolhe provider.

### Negativas

- ⚠️ **Mais uma dependência operacional**: FalkorDB tem
  que estar rodando para queries de conhecimento. Mitigação:
  EventLog é a verdade; FalkorDB é cache; sistemas puros
  nunca dependem dele.
- ⚠️ **Embedding em todo evento com documento**: pode
  ficar caro. Mitigação: projection é assíncrono e separado;
  escolher o que indexar é decisão da aplicação.
- ⚠️ **Tool registry central**: se a aplicação tem
  ferramentas por tenant, precisa de partição. Aceitável
  para o MVP.
- ⚠️ **`profile` sobrecarga config + recência** (resolvido
  por ADR-014): mistura de "o que a PME é" e "o que a PME
  estava fazendo" no mesmo agente. Mitigação: ADR-014 separa
  em `profile` (estático) + `continuity` (recente, sliding TTL,
  PII hash-only, LGPD `cleared`).

### Mitigações

| Problema | Mitigação |
|---------|-----------|
| FalkorDB indisponível | EventLog é truth; sistemas puros seguem |
| Embedding caro | Projection é assíncrono; filtrar por `event_type` antes de indexar |
| Tool registry global | Namespace por tenant (futuro) |
| `profile` mistura config e recência | ADR-014: separar em `profile` + `continuity` |

---

## 7. Referências

- [ADR-001: Arquitetura geral](./ADR-001-Arquitetura.md)
- [ADR-002: Replay canônico](./ADR-002-Replay-Puro.md)
- [ADR-003: Ciclo dual](./ADR-003-Ciclo-Dual.md)
- [ADR-010: Memory Tier "business" — Solutions](./ADR-010-Memory-Business-Tier.md)
- [ADR-014: Memory Tier "continuity" — Estado-de-Uso Recente](./ADR-014-Continuity-Tier.md)
- [Redis Agent Builder: How agents work](https://redis.io/docs/latest/develop/ai/agent-builder/agent-concepts/)
- [FalkorDB Vector Index](https://docs.falkordb.com/cypher/indexing/vector-index.html)
- [FalkorDB GraphRAG SDK](https://docs.falkordb.com/genai-tools/graphrag-sdk.html)
