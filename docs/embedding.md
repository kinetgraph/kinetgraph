<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Embedding Providers

O FMH expõe um [`EmbeddingProvider`](../../src/fmh_backend/knowledge/embedding/provider.py)
**plugável** que transforma texto em vetor de dimensão fixa.
O grafo FalkorDB armazena esses vetores como propriedade
`embedding: vec_f32` em nós `Document` (e futuramente `Entity`)
e o `GraphRAGRetriever` faz busca vetorial via
`vec.cosineDistance`.

Este documento cobre os providers disponíveis, como escolher
a dimensão ao criar o índice em FalkorDB, e como integrar
com GraphRAG multilíngue.

---

## 1. Protocol

```python
class EmbeddingProvider(Protocol):
    dimension: int

    async def embed(self, text: str) -> list[float]: ...
    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]: ...
    async def close(self) -> None: ...
```

`dimension` é atributo de classe — o índice vetorial em
FalkorDB precisa ser criado com o **mesmo valor**. Provider
e índice **nunca** podem estar fora de sincronia.

---

## 2. Providers disponíveis

| Provider | Modelo | Dimensão | Dependência | Uso |
|----------|--------|----------|-------------|-----|
| `HashEmbeddingProvider` | — (SHA-256) | 256 (config.) | nenhuma | testes, CI, desenvolvimento offline |
| `EmbeddingClient` | `paraphrase-multilingual` (default) | 768 (config.) | `ollama` (extra opcional) | **GraphRAG multilíngue** (PT/EN/ES/…) |
| `EmbeddingClient` | `nomic-embed-text` | 768 | `ollama` | inglês-only, alta qualidade |
| `EmbeddingClient` | `mxbai-embed-large` | 1024 | `ollama` | inglês-only, melhor recall |
| `OpenAIEmbeddingProvider`¹ | `text-embedding-3-small` | 1536 | `openai` | produção se Ollama não disponível |

¹ Não implementado neste repo — o Protocol está pronto;
basta uma classe com a mesma assinatura.

### 2.1 `HashEmbeddingProvider`

Determinístico e sem dependências. Vetores derivados de
SHA-256, **não semanticamente significativos**. Use apenas
em testes ou quando você precisa de um vetor estável
qualquer.

```python
from kntgraph.knowledge.embedding.provider import (
    HashEmbeddingProvider,
)

provider = HashEmbeddingProvider()
v = await provider.embed("qualquer texto")  # 256 floats
```

### 2.2 `EmbeddingClient` (default: `paraphrase-multilingual`)

Facade para **GraphRAG multilíngue**. Delega para um
`OllamaEmbeddingAdapter` (lazy, requer `kntgraph[ollama]`)
que usa Ollama rodando local ou em rede e o modelo
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
(distribuído pela comunidade Ollama como tag
`paraphrase-multilingual`).

- **50+ idiomas**, incluindo português brasileiro.
- **768 dimensões**.
- Modelo pequeno (~470 MB), cabe em CPU.
- Mesma dimensionalidade que `nomic-embed-text` — pode
  trocar sem refazer o índice se o grafo FalkorDB já tiver
  `dim=768`.

> **Por que `EmbeddingClient` e não `OllamaEmbeddingProvider`?**
> O nome antigo expunha o backend concreto na classe,
> quebrando a API backend-agnostic. O facade decide
> internamente qual adapter usar (hoje: Ollama; amanhã:
> OpenAI, HuggingFace, etc) sem mudar o call site.

#### Instalação

```bash
# 1. Adicionar a dependência opcional
uv add 'kntgraph[ollama]'

# 2. Instalar e iniciar o servidor Ollama (fora deste repo)
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &

# 3. Baixar o modelo padrão
ollama pull paraphrase-multilingual

# (opcional) outros modelos úteis
ollama pull nomic-embed-text
ollama pull mxbai-embed-large
```

#### Uso básico

```python
from kntgraph.knowledge.embedding import (
    EmbeddingClient,
)

client = EmbeddingClient()
# model="paraphrase-multilingual", host=None (default localhost:11434), dimension=768
v = await client.embed("NF-e emitida para o cliente XPTO")
assert len(v) == 768
```

#### Customização

```python
client = EmbeddingClient(
    model="nomic-embed-text",     # outro modelo Ollama
    host="http://10.0.0.5:11434", # servidor remoto
    dimension=768,                # ajuste se o modelo exigir
)
```

#### Injeção de adapter (testes)

```python
from kntgraph.knowledge.embedding import (
    EmbeddingClient,
    EmbeddingProvider,
)

class FakeAdapter(EmbeddingProvider):
    dimension = 768
    async def embed(self, text: str) -> list[float]:
        return [0.1] * 768
    async def embed_batch(self, texts):
        return [[0.1] * 768 for _ in texts]
    async def close(self) -> None: ...

client = EmbeddingClient(adapter=FakeAdapter())
```

#### Tratamento de erros

- Se o pacote `ollama` não estiver instalado: `RuntimeError`
  com mensagem instruindo a instalar `kntgraph[ollama]`.
- Se o servidor Ollama estiver offline: o cliente
  `ollama.Client` levanta `ConnectionError`.
- Se a dimensão retornada pelo modelo não bater com
  `dimension`: `ValueError` ("embedding dimension mismatch").

---

## 3. Integrando com GraphRAG

```python
from kntgraph.knowledge.embedding import (
    EmbeddingClient,
)
from kntgraph.infra.graph import GraphPool
from kntgraph.knowledge.graphrag.retriever import (
    GraphRAGRetriever,
)

graph = GraphPool(host="localhost", port=6379)
embedding = EmbeddingClient()  # 768d
retriever = GraphRAGRetriever(
    client=graph,
    embedding=embedding,
    tenant_id="12.345.678/0001-90",
)

results = await retriever.retrieve(
    "notas fiscais de saída do último trimestre", k=5
)
for r in results:
    print(r.doc_id, r.score, r.data)
```

> **Importante:** o índice vetorial em FalkorDB precisa
> estar criado com `dim=768` (default do
> `EmbeddingClient`). Veja a seção 4.

---

## 4. Índice vetorial em FalkorDB

A dimensão do índice **tem que** casar com
`provider.dimension`. Ao provisionar o grafo do tenant:

```cypher
CREATE VECTOR INDEX FOR (d:Document) ON (d.embedding)
OPTIONS {dim: 768, similarityFunction: 'cosine'}
```

Substitua `768` pela dimensão do provider em uso. Para
OpenAI `text-embedding-3-small`, use `1536`.

---

## 5. Testes

```bash
# Unitários do provider (sem rede, sem Ollama rodando)
uv run --package kntgraph pytest \
    fmh_backend/tests/unit/knowledge/test_embedding.py -v
```

Os testes do `EmbeddingClient` usam um fake adapter
injetado — **não precisam** de Ollama real rodando.

---

## 6. Extras opcionais do `pyproject.toml`

```toml
[project.optional-dependencies]
falkordb = ["falkordb>=1.6.1"]
ollama   = ["ollama>=0.4.0"]   # ← para EmbeddingClient (Ollama backend)
```

Instalação combinada:

```bash
uv add 'kntgraph[falkordb,ollama]'
```

---

## 7. Veja também

- [ADR-004: Memory Tiers, Tools e Knowledge](../ADRs/ADR-004-Memory-Tools-Knowledge.md)
- [GraphRAG retriever](../../src/fmh_backend/knowledge/graphrag/retriever.py)
- [Embedding provider](../../src/fmh_backend/knowledge/embedding/provider.py)
- [Exemplo de projeção FalkorDB](../../../fmh_agents/examples/08_falkordb_projection.py)
