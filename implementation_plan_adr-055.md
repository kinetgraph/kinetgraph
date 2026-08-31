<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-055 — Fase 1: GlinerModelRegistry

Escopo desta fase: introduzir o process-level model registry com suporte
a `cache_dir` configurável, e redirecionar os dois adapters existentes
que chamam `GLiNER2.from_pretrained` diretamente.

A extração estruturada (Protocol + Adapter + Tool) e a revisão das
arquiteturas de intent/entity/argument ficam para a Fase 2, com novos
ADRs por frente.

---

## O que muda

| Artefato | Tipo | Localização |
|---|---|---|
| `_knowledge.py` | +1 campo | `src/kntgraph/infra/config/` |
| `_gliner_model_registry.py` | **NOVO** | `src/kntgraph/knowledge/extraction/` |
| `gliner_intent.py` | 1 linha | linha 166 |
| `argument/_gliner_finder.py` | 1 linha | linha 345 |
| `gliner.py` | docstring only | linhas 84–92 (exemplo de subclasse) |
| `extraction/__init__.py` | re-export | bloco lazy imports |
| `test_gliner_model_registry.py` | integração | `tests/integration/knowledge/extraction/` |
| `test_adapter_registry_wiring.py` | unit | `tests/unit/knowledge/extraction/` |

---

## Proposed Changes

### 0 · `_knowledge.py` — +1 campo [MODIFY]

`KnowledgeSettingsMixin` recebe um novo campo opcional:

```python
# Absolute or relative path where GLiNER2 model weights are cached.
# When None (default), the HuggingFace default applies (~/.cache/huggingface).
# Map to env var KNT_MODEL_CACHE_DIR.
model_cache_dir: str | None = Field(default=None)
```

**Env var**: `KNT_MODEL_CACHE_DIR`  
**Default**: `None` → HuggingFace usa `~/.cache/huggingface` (ou `HF_HOME` se setado no ambiente)

> [!NOTE]
> Dois mecanismos coexistem sem conflito:
> - `KNT_MODEL_CACHE_DIR` → passa `cache_dir=` ao `from_pretrained` (controle por Settings)
> - `HF_HOME` (env do sistema) → lido pelo HuggingFace antes de qualquer `from_pretrained` (fallback global)
>
> Há um bug conhecido em algumas versões do `gliner2` onde `cache_dir` não propaga
> corretamente ao tokenizer interno. Nesse caso, `HF_HOME` é o mecanismo mais confiável.
> O campo `model_cache_dir` está documentado com esse aviso.

---

### 1 · `_gliner_model_registry.py` [NEW]

A chave de cache passa a ser `(model_name, device, cache_dir)` para
que o mesmo modelo em caminhos de cache distintos coexista sem colisão:

```python
# src/kntgraph/knowledge/extraction/_gliner_model_registry.py

class GlinerModelRegistry:
    """
    Process-level cache of loaded GLiNER2 instances.

    Keyed by ``(model_name, device, cache_dir)`` so a deployment that
    loads the same checkpoint from two different local paths keeps
    separate instances. A typical deployment — same model, same device,
    same cache dir — pays ``from_pretrained`` once regardless of how
    many adapters consume the result.

    No eviction in V1. The instance lives until process exit.
    Multi-tenant LRU eviction is a pending concern (ADR-055 §4).
    """

    # The class-level dict is intentionally typed with the resolved
    # key so two calls with equivalent kwargs land on the same slot.
    _cache: dict[tuple[str, str | None, str | None], "GLiNER2"] = {}

    @classmethod
    def get(
        cls,
        model_name: str,
        device: str | None = None,
        cache_dir: str | None = None,
    ) -> "GLiNER2":
        """
        Return the cached GLiNER2 instance for
        ``(model_name, device, cache_dir)``, loading it on the first call.

        ``cache_dir`` is passed verbatim to ``GLiNER2.from_pretrained``.
        When ``None``, the HuggingFace default applies. Callers should
        read this from ``Settings.model_cache_dir`` — the registry
        does not read Settings directly so it stays dependency-free.

        Thread-safety: the GIL protects the dict read. Two threads
        racing on the first call both call ``from_pretrained``; PyTorch
        serialises them via a file lock on the HuggingFace cache. The
        second caller stores the second instance — correctness-neutral.
        A future iter can add a per-key ``threading.Lock`` if profiling
        shows a measurable cost. V1 accepts the duplicate-load window.
        """
        key = (model_name, device, cache_dir)
        if key not in cls._cache:
            from kntgraph._optional import require_optional
            GLiNER2 = require_optional(
                "gliner2",
                "kntgraph[gliner]",
                purpose="GlinerModelRegistry",
            ).GLiNER2
            cls._cache[key] = GLiNER2.from_pretrained(
                model_name,
                device=device,
                cache_dir=cache_dir,
            )
        return cls._cache[key]

    @classmethod
    def _clear(cls) -> None:
        """
        Flush the cache. Test-only — not part of the public API.
        Production code never calls this.
        """
        cls._cache.clear()
```

---

### 2 · `gliner_intent.py` — substituição [MODIFY]

Bloco atual (linhas 157–168):

```diff
- from ..._optional import require_optional
-
- GLiNER2 = require_optional(
-     "gliner2",
-     "kntgraph[gliner]",
-     purpose="GlinerIntentAdapter",
- ).GLiNER2
-
- self._model = GLiNER2.from_pretrained(model_name, device=device)
+ from kntgraph.knowledge.extraction._gliner_model_registry import (
+     GlinerModelRegistry,
+ )
+ from kntgraph.infra.config import fresh_settings
+ self._model = GlinerModelRegistry.get(
+     model_name,
+     device=device,
+     cache_dir=fresh_settings().model_cache_dir,
+ )
```

---

### 3 · `argument/_gliner_finder.py` — substituição [MODIFY]

Bloco atual (linhas 337–345):

```diff
- from kntgraph._optional import require_optional
-
- GLiNER2 = require_optional(
-     "gliner2",
-     "kntgraph[gliner]",
-     purpose="GlinerFieldFinder and GlinerArgumentAdapter",
- ).GLiNER2
-
- self._model = GLiNER2.from_pretrained(model_name, device=device)
+ from kntgraph.knowledge.extraction._gliner_model_registry import (
+     GlinerModelRegistry,
+ )
+ from kntgraph.infra.config import fresh_settings
+ self._model = GlinerModelRegistry.get(
+     model_name,
+     device=device,
+     cache_dir=fresh_settings().model_cache_dir,
+ )
```

---

### 4 · `gliner.py` — docstring [MODIFY]

Exemplo de subclasse (linhas 84–92) atualizado:

```diff
  To wire a real model, subclass:

      class MyGliner(GlinerEntityAdapter):
          def __init__(self, model_path):
              super().__init__()
-             self._model = load_my_model(model_path)
+             # Use GlinerModelRegistry.get so the loaded checkpoint is
+             # shared across adapters in the same process (one cold start,
+             # one copy in RAM). Pass Settings.model_cache_dir as cache_dir
+             # to honour the operator's local cache configuration.
+             from kntgraph.knowledge.extraction import GlinerModelRegistry
+             from kntgraph.infra.config import fresh_settings
+             self._model = GlinerModelRegistry.get(
+                 model_path,
+                 cache_dir=fresh_settings().model_cache_dir,
+             )
          async def _run_model(self, text):
              spans = self._model.predict(text, self._labels)
              return [(self._span_to_entity(s), s.start) for s in spans]
```

---

### 5 · `extraction/__init__.py` — re-export [MODIFY]

```python
# Após o bloco dos Gliner* existentes:
try:
    from ._gliner_model_registry import GlinerModelRegistry
    _HAS_REGISTRY = True
except ImportError:  # pragma: no cover
    GlinerModelRegistry = None
    _HAS_REGISTRY = False
```

Adicionar `"GlinerModelRegistry"` ao `__all__`.

---

## Tests

### `test_gliner_model_registry.py` — integração [NEW]

```
tests/integration/knowledge/extraction/test_gliner_model_registry.py
```

**Sem mocks. Usa um modelo small real** (ex: `urchade/gliner_small-v2.1`
ou o `gliner2-base` se disponível no cache do CI). Guarded por
`pytest.mark.integration` e pela disponibilidade do `[gliner]` extra.

| Teste | O que verifica |
|---|---|
| `test_same_key_returns_same_instance` | Duas chamadas `get(model, None, None)` → mesmo objeto Python (`is`) |
| `test_different_model_keys_return_different_instances` | Dois modelos distintos → instâncias distintas |
| `test_cache_dir_creates_separate_entry` | Mesmo modelo, `cache_dir` diferente → chaves distintas no cache |
| `test_model_is_functional_after_get` | Objeto retornado responde à inferência (smoke test end-to-end) |

Fixture de escopo de módulo: `GlinerModelRegistry._clear()` no `setup`
para isolar do estado de outros testes que possam ter carregado o mesmo modelo.

> [!IMPORTANT]
> Estes testes **não rodam no CI rápido** (sem GPU, sem modelo baixado).
> O marker `integration` e a verificação do extra `[gliner]` garantem
> que só executam quando o ambiente tem o modelo disponível.

---

### `test_adapter_registry_wiring.py` — unit [NEW]

```
tests/unit/knowledge/extraction/test_adapter_registry_wiring.py
```

**Sem modelo real.** Verifica apenas a delegação ao registry via
inspecção de source code e mocking do `GlinerModelRegistry.get`.

| Teste | O que verifica |
|---|---|
| `test_intent_adapter_calls_registry_not_from_pretrained` | `gliner_intent.py` source não contém `from_pretrained` |
| `test_field_finder_calls_registry_not_from_pretrained` | `_gliner_finder.py` source idem |
| `test_intent_adapter_and_field_finder_share_model` | Mock do registry retorna sentinel; ambos os adapters recebem o mesmo objeto |
| `test_gliner_entity_adapter_no_from_pretrained_in_framework` | `gliner.py` source não contém `from_pretrained` |
| `test_cache_dir_forwarded_from_settings` | `monkeypatch` seta `KNT_MODEL_CACHE_DIR`; `GlinerModelRegistry.get` é chamado com o valor correto |

---

## O que NÃO está nesta fase

| Item | Status |
|---|---|
| `StructuredExtractor` Protocol | Fase 2 → novo ADR |
| `GlinerStructuredAdapter` | Fase 2 → novo ADR |
| `SLMStructuredExtractor` | Fase 2 → novo ADR |
| `StructuredExtractionTool` | Fase 2 → novo ADR |
| Revisão arquitetura semantic router | Revisão → novo ADR |
| Revisão arquitetura entity | Revisão → novo ADR |
| Revisão arquitetura argument | Revisão → novo ADR |
| ADR-055 Status → `Accepted` | Após Fase 2 |

---

## Verification Plan

```bash
# Suite unit existente — deve continuar verde sem mudança
KNT_REDIS_FAKE=1 uv run pytest tests/unit/knowledge/extraction/ -v

# Novos unit tests de wiring
KNT_REDIS_FAKE=1 uv run pytest \
  tests/unit/knowledge/extraction/test_adapter_registry_wiring.py \
  -v

# Integração (requer [gliner] extra + modelo no cache)
uv run pytest tests/integration/knowledge/extraction/test_gliner_model_registry.py \
  -v -m integration

# CI gate completo
uv run python scripts/ci.py
```

### Checklist Fase 1

- [ ] `KnowledgeSettingsMixin.model_cache_dir` adicionado (`KNT_MODEL_CACHE_DIR`, default `None`)
- [ ] `GlinerModelRegistry` criado com chave `(model_name, device, cache_dir)`
- [ ] `GlinerIntentAdapter` usa `GlinerModelRegistry.get` com `cache_dir` de Settings
- [ ] `GlinerFieldFinder` usa `GlinerModelRegistry.get` com `cache_dir` de Settings
- [ ] `GlinerEntityAdapter` docstring atualizado com exemplo de registry + `cache_dir`
- [ ] `extraction/__init__.py` re-exporta `GlinerModelRegistry`
- [ ] `test_adapter_registry_wiring.py` — todos os 5 casos unit passando
- [ ] `test_gliner_model_registry.py` — 4 casos integration documentados
- [ ] `uv run python scripts/ci.py` verde
