<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

Aqui está a consolidação das nossas definições arquiteturais formatada como um **Architecture Decision Record (ADR)** formal, seguida por um guia pragmático de implementação para a sua equipe de engenharia.

---

# ADR 005: Arquitetura ECS Baseada em Arrow e Spark para Agentes Operadores de Processo

> [!WARNING]
> **ESTE ADR ESTÁ OBSOLETO**
> A proposta de arquitetura ECS baseada em Apache Spark, PyArrow e Polars foi abandonada/rejeitada.
> O framework adotou uma arquitetura orientada a eventos pura baseada em Redis Streams e replay determinístico,
> conforme definido no [ADR-001](./ADR-001-Arquitetura.md) e [ADR-002](./ADR-002-Replay-Puro.md).

## 1. Status

**Obsoleto / Rejeitado** (Substituído pelo ADR-001 v2.0 e pelo ADR-002-Replay-Puro.md)

## 2. Contexto (O Problema)

Nossa plataforma de "TI Lucrativa" precisa orquestrar Agentes de IA autônomos que operam processos de negócio complexos (ex: validação de CNPJs, análise de risco). Arquiteturas tradicionais acoplam o comportamento ao código do agente (Agent Bloat), geram gargalos de concorrência síncrona nos bancos de dados, e inflacionam o custo computacional (EBITDA) ao manter instâncias pesadas rodando ociosamente. Precisamos garantir um **P99 de resposta em milissegundos** na borda, mantendo a consistência de um **Grafo de Conhecimento (FalkorDB)** global.

## 3. Decisão (A Solução)

Adotaremos uma arquitetura Híbrida de Segregação (Síncrona vs. Assíncrona) combinando o padrão **ECS (Entity Component System)**, gerenciamento de memória colunar (**PyArrow/Polars**), e consolidação episódica via **Apache Spark 4.x (Project Feather)**.

### Princípios da Arquitetura:

1. **Agentes como Entidades (ECS):** Agentes são estritamente IDs (Entidades). Intenções, estado e integrações MCP são **Componentes** (dados). A lógica de negócio reside nos **Sistemas**.
2. **Memória Hot Colunar (Zero-Copy):** O estado de execução (Hot Layer) do K3s roda em memória usando `Polars/PyArrow` (Struct of Arrays) para garantir vetorização e alta performance, sem o imposto do Python GIL.
3. **Double Buffering (O Tick do Mundo):** Para lidar com a imutabilidade do Arrow, a mutação de estado ocorre via Buffers de Escrita a cada ciclo (*Tick*), consolidando uma nova tabela de leitura no fim do ciclo.
4. **Atomicidade de Borda (Transactional Outbox):** A comunicação da intenção do agente para a infraestrutura de dados ocorre via Script Lua no Redis, atualizando a *Short-Term Memory* e inserindo no *Redis Stream* de forma atômica.
5. **Consolidação Efêmera (Córtex Assíncrono):** O **Spark Feather** roda em containers de nó único de forma sob demanda (*Load-Driven*). Ele consome os lotes do stream, refina o contexto, e consolida os relacionamentos no **FalkorDB (Warm)** e os logs no **Apache Iceberg (Cold)**.
6. **Sinalização Reversa:** O Spark utiliza Redis Pub/Sub ou injeção de flags no Redis JSON para alertar a borda sobre anomalias ou atalhos cognitivos, sem bloquear o fluxo original.

## 4. Consequências

### Pontos Positivos (Ganhos de EBITDA e P99):

* **P99 Blindado:** O agente e o banco de grafos nunca competem síncronamente. A borda escreve em memória/Redis em milissegundos.
* **Escalabilidade Extrema:** A separação permite escalar os Pods de ECS (agentes) independentemente do motor analítico (Spark).
* **FinOps Otimizado:** O Spark só consome CPU/RAM quando há lote acumulado no Redis Stream.
* **Zero-Copy Serialization:** O tráfego de dados entre o K3s e o Spark usa o formato Apache Arrow ponta a ponta.

### Pontos Negativos (Riscos Assumidos):

* **Paradigm Shift:** A equipe de desenvolvimento precisará abandonar o paradigma de Orientação a Objetos para adoção do Data-Oriented Design (DoD) com ECS e Polars.
* **Consistência Eventual Consciente:** O Grafo (FalkorDB) terá um atraso (lag) proposital em relação à borda. O UX deve ser desenhado para tolerar essa janela.

---

# Guia de Implementação (Roadmap do Engenheiro)

Este guia divide a execução em três fases lógicas de construção.

## Fase 1: A Borda Síncrona (Hot Layer & ECS)

O objetivo aqui é garantir que o agente rode, "pense" e registre suas ações sem encostar na infraestrutura pesada.

1. **Definição de Componentes (PyArrow/Polars):**
* Crie os *DataFrames* base no Polars para representar os Componentes.
* Exemplo: `ComponenteIntencao (entity_id, active_intent, parameters)`, `ComponenteEstado (entity_id, status)`.


2. **Implementação do "Tick" Loop:**
* Crie o loop de controle do mundo.
* **Read Phase:** Os *Sistemas* leem o DataFrame imutável e geram arrays mutáveis (NumPy ou dicionários) com os deltas de alteração.
* **Write Phase:** O motor do ECS aplica os deltas, gerando um novo DataFrame Polars substituindo o estado anterior.


3. **O Script Lua (Transactional Outbox):**
* Escreva um arquivo `.lua` carregado no Redis via `SCRIPT LOAD`.
* O script deve aceitar chaves (`KEYS[1]` = Memória do Agente, `KEYS[2]` = Nome do Stream) e argumentos (Payload Arrow/JSON).
* Ele deve conter: `redis.call('JSON.SET', KEYS[1], '$', ARGV[1])` e `redis.call('XADD', KEYS[2], '*', 'payload', ARGV[1])`.



## Fase 2: O Motor Analítico (Analytical Layer)

O objetivo é consumir as intenções agrupadas sem gerar computação ociosa.

1. **Configuração do Ephemeral Spark Feather no K3s:**
* Crie um `CronJob` no Kubernetes ou utilize o **KEDA** configurado para escalar a partir de > 0 mensagens no grupo do Redis Stream.
* Utilize a imagem oficial do Spark 4.1.2 configurada com `--master "local[*]"` para ativar o Feather.


2. **Consumo Bounded do Redis Stream:**
* No script PySpark (Spark Connect), leia o último ID processado do Iceberg.
* Use a API do Spark para conectar no Redis e ler um lote limitado de registros (ex: `count=5000`).
* Desestruture o payload (lembre-se do benefício de estar em Arrow).



## Fase 3: Validação, Consolidação e Sinalização (Warm/Cold Layer)

Fechando o ciclo de processamento e enviando o feedback à borda.

1. **O Filtro Ontológico (UDF GLiNER-2):**
* Dentro do DataFrame do Spark, aplique uma transformação (`mapInArrow` ou UDF colunar) que passe o payload pelo GLiNER-2 para garantir que as entidades mencionadas não ferem as regras do negócio.


2. **Consolidação (Triple-Write Assíncrono):**
* **Iceberg (Imutável):** Grave o lote processado no seu catálogo (MinIO/Ceph). Adicione a coluna `metadata_redis_id` para salvar a marca d'água.
* **FalkorDB (Relacional):** Extraia os nós (ex: CNPJ, Sócios) e execute as `MERGE` queries na linguagem Cypher para atualizar as arestas do grafo corporativo em lote.


3. **A Sinalização Reversa (Feedback Loop):**
* Se o Spark encontrar uma anomalia (ex: Risco Bloqueante), adicione uma etapa de ação direta: envie um comando `PUBLISH` para um tópico do Redis (ex: `agent:control:alerts`).
* Garanta que os *Sistemas* no ECS da Fase 1 tenham um *Listener* (assinatura não-bloqueante) desse tópico. No próximo "Tick", o ECS atualiza o `ComponenteEstado` do agente afetado para `BLOCKED`.