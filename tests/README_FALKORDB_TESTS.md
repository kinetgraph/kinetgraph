<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Testes de Integração - FalkorDB Repository

## Visão Geral

Testes de integração para o **FalkorDBWorldRepository**, que implementa persistência do World ECS usando FalkorDB.

---

## Pré-requisitos

### 1. Instalar FalkorDB

```bash
# Opção 1: Instalar dependência opcional
uv sync --extra falkordb

# Opção 2: pip
pip install 'kntgraph[falkordb]'

# Opção 3: direto
pip install falkordb-py
```

### 2. Iniciar FalkorDB

```bash
# Docker (recomendado)
docker run -d -p 6379:6379 --name falkordb-test falkordb/falkordb

# Verificar se está rodando
docker ps | grep falkordb

# Logs
docker logs falkordb-test
```

### 3. Testar Conexão

```bash
# Redis CLI (FalkorDB usa protocolo Redis)
redis-cli PING
# Deve retornar: PONG

# Testar FalkorDB específico
redis-cli GRAPH.QUERY test "RETURN 1"
```

---

## Executar Testes

### Todos os Testes de Integração

```bash
# Requer FalkorDB rodando
pytest tests/integration/test_falkordb_repository.py -v
```

### Testes Específicos

```bash
# InMemoryWorldRepository (não requer FalkorDB)
pytest tests/integration/test_falkordb_repository.py::TestInMemoryWorldRepository -v

# FalkorDBWorldRepository (requer FalkorDB)
pytest tests/integration/test_falkordb_repository.py::TestFalkorDBWorldRepository -v

# World Integration
pytest tests/integration/test_falkordb_repository.py::TestWorldWithRepository -v

# Async Queries
pytest tests/integration/test_falkordb_repository.py::TestAsyncQueries -v

# Error Handling
pytest tests/integration/test_falkordb_repository.py::TestErrorHandling -v
```

### Teste Único

```bash
# Teste específico
pytest tests/integration/test_falkordb_repository.py::TestFalkorDBWorldRepository::test_save_and_get_agent -v

# Com output detalhado
pytest tests/integration/test_falkordb_repository.py::TestFalkorDBWorldRepository::test_save_and_get_agent -v -s

# Com coverage
pytest tests/integration/test_falkordb_repository.py --cov=kntgraph.infra.falkordb --cov-report=html
```

---

## Estrutura dos Testes

### Fixtures

| Fixture | Escopo | Descrição |
|---------|--------|-----------|
| `falkordb_repo` | function | Repository conectado, limpa após cada teste |
| `falkordb_repo_with_data` | function | Repository com 3 agentes de exemplo |
| `in_memory_repo` | function | InMemoryWorldRepository para testes sem FalkorDB |
| `falkordb_client` | function | Cliente FalkorDB raw para testes de baixo nível |

### Classes de Teste

| Classe | Descrição | Requer FalkorDB |
|--------|-----------|-----------------|
| `TestInMemoryWorldRepository` | Testes de memória | ❌ Não |
| `TestFalkorDBWorldRepository` | Testes de integração FalkorDB | ✅ Sim |
| `TestWorldWithRepository` | Integração World + Repository | ✅ Sim |
| `TestAsyncQueries` | Async queries (AND/OR) | ✅ Sim |
| `TestErrorHandling` | Tratamento de erros | ✅ Sim |

---

## Testes Implementados

### InMemoryWorldRepository (6 testes)

| Teste | Descrição |
|-------|-----------|
| `test_save_and_get_agent` | CRUD básico de agente |
| `test_get_nonexistent_agent` | Agente não existe (None) |
| `test_save_and_get_world` | Salvar/carregar world |
| `test_delete_agent` | Delete de agente |
| `test_query_agents_by_component_and` | Query AND |
| `test_query_agents_by_component_or` | Query OR |

### FalkorDBWorldRepository (11 testes)

| Teste | Descrição |
|-------|-----------|
| `test_save_and_get_agent` | CRUD no FalkorDB |
| `test_get_nonexistent_agent` | Agente não existe |
| `test_save_and_get_world` | World completo |
| `test_delete_agent` | Delete |
| `test_query_agents_by_component_and` | Query AND (grafo) |
| `test_query_agents_by_component_or` | Query OR (grafo) |
| `test_get_all_agents` | Listar todos |
| `test_serialization_deserialization` | Round-trip JSON |
| `test_deterministic_agent_id` | UUIDv5 idempotente |
| `test_update_agent` | Atualização (MERGE) |
| `test_query_with_complex_components` | Componentes complexos |

### World Integration (3 testes)

| Teste | Descrição |
|-------|-----------|
| `test_world_load_from_repository` | Carregar do FalkorDB |
| `test_world_save_to_repository` | Salvar no FalkorDB |
| `test_world_persists_across_instances` | Persistência entre instâncias |

### Async Queries (6 testes)

| Teste | Descrição |
|-------|-----------|
| `test_async_query_and` | Async AND iteration |
| `test_async_query_or` | Async OR iteration |
| `test_async_query_first` | Primeiro resultado |
| `test_async_query_count` | Contagem |
| `test_async_query_to_list` | Converter para lista |
| `test_async_query_is_empty` | Verificar se vazio |

### Error Handling (2 testes)

| Teste | Descrição |
|-------|-----------|
| `test_connection_failure` | Falha de conexão |
| `test_falkordb_not_available` | falkordb-py não instalado |

**Total:** 28 testes

---

## Dados de Teste

### Agentes Criados no Fixture

```python
# agent1: DocumentComponent + ClientContextComponent
agent1 = AgentState.create(
    agent_type="service",
    tenant_id="tenant_123",
    unique_key="doc_001",
    components={
        "document": DocumentComponent(document_id="doc_001", document_type="nota_fiscal"),
        "client": ClientContextComponent(client_id="client_456"),
    }
)

# agent2: DocumentComponent apenas
agent2 = AgentState.create(
    agent_type="service",
    tenant_id="tenant_123",
    unique_key="doc_002",
    components={
        "document": DocumentComponent(document_id="doc_002", document_type="recibo"),
    }
)

# agent3: TaskComponent apenas
agent3 = AgentState.create(
    agent_type="service",
    tenant_id="tenant_123",
    unique_key="task_001",
    components={
        "task": TaskComponent(task_id="task_001", priority="high"),
    }
)
```

### Queries de Teste

```python
# AND: DocumentComponent + ClientContextComponent
# Resultado esperado: 1 agente (agent1)
await repo.query_agents_by_component(DocumentComponent, ClientContextComponent)

# OR: DocumentComponent OU TaskComponent
# Resultado esperado: 3 agentes (todos)
await repo.query_agents_by_component_or(DocumentComponent, TaskComponent)
```

---

## Cleanup

### Após Testes

```bash
# O fixture limpa automaticamente após cada teste
# Mas para cleanup manual:

# Limpar grafo
redis-cli GRAPH.QUERY kntgraph_test_agents "MATCH (n) DETACH DELETE n"

# Deletar grafo
redis-cli GRAPH.DELETE kntgraph_test_agents
```

### Parar FalkorDB

```bash
# Parar container
docker stop falkordb-test

# Remover container
docker rm falkordb-test

# Ambos
docker stop falkordb-test && docker rm falkordb-test
```

---

## Troubleshooting

### Erro: "FalkorDB not available"

```bash
# Verificar se FalkorDB está rodando
docker ps | grep falkordb

# Se não estiver, iniciar
docker run -d -p 6379:6379 --name falkordb-test falkordb/falkordb

# Testar conexão
redis-cli PING
```

### Erro: "falkordb-py não instalado"

```bash
# Instalar
pip install falkordb-py

# Ou com uv
uv sync --extra falkordb
```

### Erro: "Connection refused"

```bash
# Verificar porta
netstat -tlnp | grep 6379

# Verificar logs do FalkorDB
docker logs falkordb-test

# Reiniciar FalkorDB
docker restart falkordb-test
```

### Testes Pulados (Skipped)

Se testes são pulados com "FalkorDB not available":

```bash
# Verificar se falkordb-py está instalado
python -c "from falkordb import FalkorDB; print('OK')"

# Se falhar, reinstalar
pip uninstall falkordb-py
pip install falkordb-py
```

---

## Coverage

### Gerar Relatório

```bash
# Coverage HTML
pytest tests/integration/test_falkordb_repository.py --cov=kntgraph.infra.falkordb --cov-report=html

# Abrir relatório
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

### Coverage Mínimo Esperado

| Módulo | Coverage Mínimo |
|--------|----------------|
| `repository.py` | 90% |
| `falkordb_repository.py` | 85% |
| `world.py` (novo código) | 80% |
| `query.py` (async queries) | 85% |

---

## Performance dos Testes

| Categoria | Tempo Médio |
|-----------|-------------|
| InMemoryWorldRepository | ~50ms total |
| FalkorDBWorldRepository | ~500ms total |
| World Integration | ~300ms total |
| Async Queries | ~400ms total |

**Tempo total estimado:** ~1.5 segundos

---

## Exemplo: Adicionando Novo Teste

```python
@pytest.mark.integration
class TestNewFeature:
    """Testes para nova feature."""
    
    @pytest.mark.asyncio
    async def test_new_feature(
        self,
        falkordb_repo_with_data: FalkorDBWorldRepository
    ):
        """Testa nova feature."""
        repo = falkordb_repo_with_data
        
        # Arrange
        world = await repo.get_world()
        
        # Act
        result = await repo.new_feature_method(...)
        
        # Assert
        assert result == expected
```

---

## CI/CD Integration

### GitHub Actions

```yaml
name: Tests

on: [push, pull_request]

jobs:
  test-falkordb:
    runs-on: ubuntu-latest
    
    services:
      falkordb:
        image: falkordb/falkordb
        ports:
          - 6379:6379
    
    steps:
      - uses: actions/checkout@v4
      
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      
      - name: Install dependencies
        run: |
          pip install uv
          uv sync --extra falkordb
      
      - name: Run FalkorDB tests
        run: |
          pytest tests/integration/test_falkordb_repository.py -v
```

---

## Referências

- [Test File](../../tests/integration/test_falkordb_repository.py)
- [Fixtures](../../tests/integration/conftest.py)
- [Implementation](../../src/kntgraph/infra/falkordb/)
- [Documentation](./FALKORDB_IMPLEMENTATION.md)
