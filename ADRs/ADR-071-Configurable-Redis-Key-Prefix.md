<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-071: Suporte a Prefixo Configurável de Chaves Redis (Namespace Scoping)

- **Status:** Aceito
- **Data:** 2026-09-18
- **Autor:** kinetgraph architecture team
- **Relacionado:**
  - [ADR-005](./ADR-005-Checkpoints-Idempotency.md) — Checkpoints e Idempotência
  - [ADR-019](./ADR-019-Redis-Adapter-Typing.md) — Adapter Redis & Encapsulamento Tipado
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — Tool Worker Pattern
  - [ADR-068](./ADR-068-idle-redis-traffic-and-eventlog-subscribe.md) — Pub/Sub e Streams Redis

---

## 1. Contexto e Problema

O framework Kinetgraph (`kntgraph`) utiliza internamente a namespace nativa `knt:` para a gravação de chaves de estado e controle no Redis:

- **Event Log:** `knt:agents:{agent_id}:events` e `knt:eventids:{event_id}`
- **Fila de Tools:** `knt:tools:{tool_name}:queue`
- **Checkpoints do World:** `knt:world:{agent_id}` e `knt:reactive:checkpoints`
- **Dead Letter Queue (DLQ):** `knt:dlq:events`, `knt:dlq:reasons`, `knt:dlq:by_agent`
- **Watchdog & Health:** `knt:health:{process_name}:{pid}`
- **API Keys / Auth:** `knt:api:keys:{digest}`
- **Memória & Continuidades:** `knt:session:{id}`, `knt:profile:{tenant_id}:{user_id}`, `knt:continuity:{tenant_id}:{user_id}`

Em arquiteturas corporativas multi-serviços (como no ecossistema `soldi/backoffice`), múltiplas aplicações compartilham a mesma instância ou cluster de Redis. Para alinhar o armazenamento com a convenção de namespace corporativa estabelecida no **ADR-014 do Backend** (`soldi:<servico>:<dominio>:<recurso>`), os serviços precisam isolar suas chaves sob um escopo bem definido (ex: `soldi:backoffice:knt:`).

Sem o suporte a um prefixo configurável:
1. **Risco de Colisão:** Múltiplas instâncias ou serviços distintos reutilizando o mesmo Redis sob `knt:*` podem sobrescrever ou interferir no estado uns dos outros.
2. **Falta de Segregação por ACL:** Dificuldade de aplicar políticas de permissão (Redis ACL) específicas por serviço baseadas em padrões de chave (ex: `soldi:backoffice:*`).
3. **Rigidez de Infraestrutura:** Impossibilidade de customizar o prefixo sem alterar constantes estáticas dentro do pacote `kntgraph`.

---

## 2. Decisão Arquitetural

Adotaremos a seguinte solução para habilitar o isolamento de namespace nas chaves Redis:

### 2.1 Preservação do Namespace Nativo com Prepend (`Prepended Prefixing`)

O prefixo configurável será **adicionado antes** do namespace nativo `knt:`. 
Dessa forma, o namespace interno do framework permanece intacto e identificável, evitando ambiguidades com outras chaves próprias do serviço no mesmo banco Redis.

**Exemplo:**
- **Prefixo configurado:** `KNT_REDIS_KEY_PREFIX="soldi:backoffice:"`
- **Chave nativa no Kinetgraph:** `knt:agents:agent_123:events`
- **Chave gravada no Redis:** `soldi:backoffice:knt:agents:agent_123:events`

### 2.2 Padrão Proxy Wrapper (`PrefixedRedisClient`)

Em conformidade com a **AGENTS.md §1** e o **ADR-019** (que estabelecem a fronteira tipada via protocolo `RedisLike`), não alteraremos as constantes estáticas de chaves (`_keys.py`) dentro dos adaptadores do `kntgraph`.

Em vez disso, implementaremos o padrão **Decorator / Key-Prefixing Proxy Wrapper** sobre o protocolo `RedisLike`:

```
+------------------------------+
|  kntgraph Adapters           |  (EventLogAdapter, CheckpointAdapter, etc.)
|  Chaves: knt:agents:...      |
+------------------------------+
               |
               v (Consome RedisLike)
+------------------------------+
|  PrefixedRedisClient Proxy   |  (Intercepta comandos e adiciona prefixo)
|  knt:... -> prefix + knt:... |
+------------------------------+
               |
               v (Consome Redis real)
+------------------------------+
|  Redis Server (Asyncio)      |  (Chave final: soldi:backoffice:knt:agents:...)
+------------------------------+
```

O `PrefixedRedisClient` intercepta os métodos de comandos Redis (`get`, `set`, `delete`, `hget`, `hset`, `xadd`, `xreadgroup`, `xack`, `xautoclaim`, `scan_iter`, etc.) e aplica a transformação:

1. **Escrita / Leitura Direta:** Qualquer argumento de chave que inicie com `knt:` é transformado para `{prefix}{key}`.
2. **Busca / Scan (`scan_iter`, `keys`):** O padrão de busca de entrada (ex: `scan_iter("knt:*")`) é traduzido para `scan_iter("soldi:backoffice:knt:*")`. Na devolução dos resultados, o wrapper desfaz o prefixo para que o chamador continue recebendo a string esperada no padrão `knt:...`.
3. **Chaves já prefixadas ou externas:** Se uma chave passada pelo serviço consumidor já contiver o prefixo configurado ou for um barramento externo (ex: `soldi:backend:events:inbound`), o wrapper a preserva sem alterar.

### 2.3 Configuração por Variável de Ambiente

A configuração será alimentada por variável de ambiente:

- **Variável:** `KNT_REDIS_KEY_PREFIX` (ou no contexto do serviço consumidor: `BO_REDIS_KEY_PREFIX`).
- **Valor Padrão:** `""` (string vazia).

Quando o valor for `""`, o `PrefixedRedisClient` opera como um *pass-through* sem custo de mutação de strings, mantendo o comportamento 100% idêntico ao legado (`knt:...`).

---

## 3. Esboço da Implementação (`PrefixedRedisClient`)

```python
from typing import Any, AsyncIterator
from kntgraph.infra.redis._client import RedisLike


class PrefixedRedisClient:
    """Wrapper proxy sobre RedisLike para prefixagem transparente de chaves."""

    def __init__(self, inner: RedisLike, prefix: str = "") -> None:
        self._inner = inner
        self._prefix = prefix

    def _prefix_key(self, key: str) -> str:
        if not self._prefix or key.startswith(self._prefix):
            return key
        return f"{self._prefix}{key}"

    def _unprefix_key(self, key: str) -> str:
        if self._prefix and key.startswith(self._prefix):
            return key[len(self._prefix) :]
        return key

    async def get(self, name: str) -> Any:
        return await self._inner.get(self._prefix_key(name))

    async def set(self, name: str, value: Any, **kwargs: Any) -> Any:
        return await self._inner.set(self._prefix_key(name), value, **kwargs)

    async def xadd(self, name: str, fields: dict[str, Any], **kwargs: Any) -> Any:
        return await self._inner.xadd(self._prefix_key(name), fields, **kwargs)

    async def scan_iter(
        self, match: str | None = None, **kwargs: Any
    ) -> AsyncIterator[str]:
        pattern = self._prefix_key(match) if match else None
        async for key in self._inner.scan_iter(match=pattern, **kwargs):
            yield self._unprefix_key(key)
```

---

## 4. Injeção no Bootstrapping do Serviço

No ponto de inicialização e montagem do serviço (ex: `build_monolith()` no `main.py` do consumidor):

```python
# 1. Instanciação do pool Redis base
redis_pool = create_redis_pool(settings.REDIS_URL)

# 2. Encapsulamento com o Proxy PrefixedRedisClient
prefixed_client = PrefixedRedisClient(
    inner=redis_pool.client,
    prefix=settings.BO_REDIS_KEY_PREFIX,  # ex: "soldi:backoffice:"
)

# 3. Injeção transparente nos adaptadores do Kinetgraph
event_log_storage = RedisEventLogAdapter(redis_client=prefixed_client)
checkpoint_storage = RedisCheckpointAdapter(redis_client=prefixed_client)
worker_manager = WorkerManager(redis_client=prefixed_client)
```

---

## 5. Suíte de Testes Automatizados (`pytest`)

Para preservar total retrocompatibilidade e evitar quebras na suíte de testes automatizados existente:

1. **Configuração Default:** Em ambiente de teste (`pytest`), `KNT_REDIS_KEY_PREFIX` permanecerá como `""` (vazio) por padrão.
2. **Mocks e Fakeredis:** Todos os testes unitários e de integração existentes continuarão funcionando sem alterações nos mocks que esperam o padrão `knt:*`.
3. **Novos Testes de Regressão:** Será criada uma suíte dedicada em `tests/infra/test_prefixed_redis_client.py` usando `fakeredis` para garantir que a tradução bidirecional de chaves e o `scan_iter` funcionem corretamente em cenários com e sem prefixo.

---

## 6. Estratégia de Migração (Zero-Downtime)

Ao ativar o prefixo em um ambiente que já possui estado mantido no formato legado `knt:*`:

1. **Incompatibilidade de Leitura:** Sem migração, o sistema iniciará assumindo um estado limpo (novos checkpoints e novos event logs sob a chave prefixada).
2. **Script de Migração:** Para ambientes com persistência viva, um script de migração via Redis CLI (`RENAME` ou `SCAN` + `RENAME`) deve ser executado antes da inicialização do serviço com a nova variável de ambiente:

```bash
# Exemplo de renomeação manual de chaves via redis-cli
redis-cli --eval - <<< '
for _, key in ipairs(redis.call("keys", "knt:*")) do
    local new_key = "soldi:backoffice:" .. key
    redis.call("rename", key, new_key)
end'
```

---

## 7. Consequências

### Prós

- **Isolamento de Namespaces:** Permite o compartilhamento seguro da mesma instância de Redis entre múltiplos serviços ou ambientes sem risco de colisão.
- **Conformidade Enterprise:** Atende às convenções corporativas de nomenclatura (`soldi:<servico>:<dominio>:<recurso>`).
- **Transparência e Decoupling:** Nenhuma alteração é necessária dentro do código dos adaptadores do `kntgraph`; a complexidade fica restrita à camada de proxy `RedisLike`.
- **Zero Breaking Change:** Mantém a suíte de testes `pytest` e instalações padrão funcionando sem alterações quando a env estiver vazia.

### Contras / Considerações

- **Scripts Lua Customizados:** Se forem introduzidos scripts Lua (`EVAL`) executados diretamente, as chaves passadas via `KEYS[...]` devem ser prefixadas antes da invocação do script.
- **Overhead de String:** Adição marginal de conversão de strings de chave em chamadas I/O do Redis (overhead negligenciável em microssegundos).

---

## 8. Status

**Aceito.** Aprovado para inclusão na arquitetura do Kinetgraph 0.15.3+ e integração no ecossistema `backoffice`.
