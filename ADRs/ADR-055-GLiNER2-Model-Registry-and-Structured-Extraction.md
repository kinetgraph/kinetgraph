<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-055: GLiNER2 model registry and structured extraction adapter

**Status:** Proposed
**Date:** 2026-08-05
**Related:** [ADR-013](./ADR-013-Semantic-Routing-GLiNER2.md) (the original GLiNER2 adoption for semantic routing), [ADR-019](./ADR-019-Redis-Adapter-Typing.md) (the Adapter convention this ADR inherits), [ADR-026](./ADR-026-Close-GLiNER2-Binding-Leak.md) (the `kntgraph → kntgraph.agents` leak this ADR does **not** reopen), [ADR-047](./ADR-047-Tool-Adapter-Pattern.md) (the Tool adapter pattern the new Tool follows)

> **Scope.** This ADR covers exactly two things, both in
> the framework's GLiNER2 surface:
>
>   1. A process-level model registry that lets multiple
>      adapters share a single loaded `GLiNER2` instance
>      (one cold start, one copy in RAM, N consumers).
>   2. A new `StructuredExtractor` Protocol with a
>      GLiNER2-backed adapter and a `StructuredExtractionTool`
>      that takes an inline schema and returns a list of
>      structured records. This is the path the
>      PDF-OCR use case (RG, CNH, NF-e, etc.) needs.
>
> Out of scope: the `Entity`/`GlinerEntityAdapter` redesign
> raised in the discussion (entity extraction for the
> FalkorDB graph stays untouched), multi-tenant model
> eviction, model warmup APIs. These are flagged in
> §4 as pending work and will be addressed in follow-up
> ADRs.

## 1. Contexto

The kntgraph framework adopts GLiNER2 as its local NLU
backend for three different concerns:

| Concern | Adapter | Public facade | Default model |
|---|---|---|---|
| Entity extraction (FalkorDB graph) | `GlinerEntityAdapter` (`knowledge/extraction/gliner.py`) | `SLMEntityExtractor` | `arg_extractor_model_id` (currently `gliner2-base`) |
| Intent classification (semantic routing, ADR-013 M1) | `GlinerIntentAdapter` (`knowledge/extraction/gliner_intent.py`) | `SLMIntentClassifier` | same |
| Argument extraction (Tool slots, ADR-013 M2) | `GlinerFieldFinder` (`knowledge/extraction/argument/_gliner_finder.py`) | `SLMArgumentExtractor` | same |

All three adapters are **template methods** that call
`GLiNER2.from_pretrained(...)` in their `__init__`
(eager model load). Three things follow from this:

1. **RAM cost is N× the model size per process.** Each
   adapter instance holds its own copy of the model. A
   deployment that uses entity + argument + intent
   adapters in the same process holds **three** copies
   of the same weights (~500 MB each on the 205M base
   model, ~700 MB on the 340M large variant), totalling
   roughly 1.5–2.1 GB of GPU/CPU RAM for the model
   objects alone, with no functional benefit.

2. **Cold start is paid per adapter.** Each facade
   construction pays the `from_pretrained` cost
   (1–3 s on CPU, longer on cold GPU). When facades are
   created eagerly at process boot (the common case),
   the three cold starts serialise on the same disk
   read, multiplying the boot latency.

3. **There is no path for a "PDF → OCR → structured
   JSON" use case.** The three existing adapters are
   tied to specific concerns (graph, intent, Tool
   slots). The consumer the user is building
   (a `StructuredExtractionTool` that receives
   `{text, schema}` inline and returns
   `list[dict]`) does not fit any of them: the
   schema is not a `Tool.input_schema`, the output is
   not a list of `Entity` nodes, and the routing
   layer (M1) is a different concern. Forcing this
   use case into `SLMArgumentExtractor` loses the
   features the GLiNER2 `extract_json` primitive
   supports natively (lists, choice fields,
   description anchors, multiple instances per
   document).

The natural fix is the same on both fronts: a
**model-level registry** (one `GLiNER2` per
`(model_name, device)`) plus a **new adapter and
Protocol** for the structured-extraction use case.
Both are small, additive, and zero-migration.

## 2. Decisão

### 2.1 `GlinerModelRegistry` — process-level singleton by `(model_name, device)`

A new module
`src/kntgraph/knowledge/extraction/_gliner_model_registry.py`
exposes a single class `GlinerModelRegistry` with one
class method:

```python
@classmethod
def get(cls, model_name: str, device: str | None = None) -> GLiNER2: ...
```

Semantics:

- First call for a given `(model_name, device)` calls
  `GLiNER2.from_pretrained(model_name, device=device)`
  and caches the result in a class-level dict.
- Subsequent calls for the same key return the cached
  instance (O(1) dict lookup).
- Different keys are independent: a deployment that
  loads both `gliner2-base` on CPU and
  `urchen/gliner-multi-pii-base` on GPU keeps two
  separate instances.
- The `gliner2` package import goes through
  `require_optional("gliner2", "kntgraph[gliner]", ...)`,
  the same pattern the three existing adapters use.
- No unload. The instance lives until process exit.
  Multi-tenant eviction is a pending concern (§4).

#### 2.1.1 Concurrency

The registry uses the GIL for the dict read; the
`from_pretrained` call inside the first call is **not**
protected by an explicit lock in V1. PyTorch's
`from_pretrained` uses a file lock on the cached
weights, so two threads racing on the first call will
serialise on the file lock and end up with the same
process-local model object once the second call returns
the cached value. The window is small (one disk read)
and a duplicate load is a correctness-neutral cost.

A future iter can add an `asyncio.Lock` per key if
profiles show a measurable race in practice.

### 2.2 Adapters use the registry (3 × 1-line change)

The three existing adapters (`GlinerEntityAdapter`,
`GlinerIntentAdapter`, `GlinerFieldFinder`) replace the
direct call to `GLiNER2.from_pretrained(...)` with
`GlinerModelRegistry.get(model_name, device=device)`.
The change is local to each adapter's `__init__`; the
public surface (Protocols, facades, return types) does
not change. Existing tests that mock
`GLiNER2.from_pretrained` are updated to mock
`GlinerModelRegistry.get` at the call site.

After this change:

- Creating all three facades in one process loads the
  model **once** (the first call hits the registry; the
  other two are dict lookups).
- The cold start is paid **once** per
  `(model_name, device)`.
- The total model RAM is **one copy** per
  `(model_name, device)`.

### 2.3 `StructuredExtractor` Protocol

A new Protocol in
`src/kntgraph/knowledge/extraction/base.py`:

```python
@runtime_checkable
class StructuredExtractor(Protocol):
    """
    Extracts structured records from text against a
    free-form schema. The schema is opaque to the
    framework: the adapter passes it to the
    underlying model (e.g. ``gliner2``) verbatim.

    The output is a list of records (possibly empty,
    possibly more than one — e.g. multiple invoices
    in the same text). Each record is a flat dict
    keyed by the schema field names.
    """

    async def extract(
        self, text: str, schema: dict,
    ) -> list[dict]: ...
```

The Protocol is **untyped on the schema** on purpose:
the schema is whatever the underlying model accepts.
For GLiNER2 it is the `{"<structure>": ["field::str",
...]}` format from the upstream tutorial; a future
backend (a different SLM, a fine-tuned model) may have
its own dialect. The framework does not inspect.

The return type is `list[dict]`, **not**
`list[Entity]`. This is the explicit break from the
`Entity` canon: the structured path does not produce
graph nodes. The FalkorDB graph is fed by
`SLMEntityExtractor`; the structured path feeds a JSON
shape to a Tool consumer. Two different consumers,
two different return types.

### 2.4 `GlinerStructuredAdapter` — the low-level GLiNER2 backend

A new module
`src/kntgraph/knowledge/extraction/gliner_structured.py`:

```python
class GlinerStructuredAdapter(StructuredExtractor):
    def __init__(
        self,
        model_name: str | None = None,
        *,
        device: str | None = None,
        field_threshold: float = 0.5,
    ) -> None: ...

    async def extract(
        self, text: str, schema: dict,
    ) -> list[dict]: ...
```

Behaviour:

- `__init__` resolves `model_name` (explicit arg or
  `Settings.arg_extractor_model_id`, the existing
  setting) and acquires the model through
  `GlinerModelRegistry.get(model_name, device=device)`.
  The `device=None` default lets GLiNER2 pick
  (CPU when no accelerator is available).
- `extract` calls
  `model.extract_json(text, schema, include_confidence=True)`
  inside `asyncio.to_thread` (PyTorch is a blocking
  call) and normalises the result into `list[dict]`.
  The confidence of each field is preserved as a
  sibling key (`__confidence_<field>`) when the
  caller wants to inspect it; this is an opt-in
  flag, off by default to keep the output clean.
- `field_threshold` is the minimum confidence to keep
  a field in the output. Mirrors the
  `SchemaArgumentExtractor.field_threshold` semantic.
  Applied post-`extract_json`, in pure Python (the
  upstream API does not accept per-field thresholds
  through `extract_json`).

### 2.5 `SLMStructuredExtractor` — the public facade

A new facade in
`src/kntgraph/knowledge/extraction/_slm_facades.py`,
following the pattern of `SLMEntityExtractor` /
`SLMIntentClassifier` / `SLMArgumentExtractor`:

```python
class SLMStructuredExtractor(StructuredExtractor):
    def __init__(
        self,
        *,
        adapter: StructuredExtractor | None = None,
        model_name: str | None = None,
        device: str | None = None,
        field_threshold: float = 0.5,
    ) -> None: ...
```

The `SLM` prefix is preserved: GLiNER2 is the default
backend, but a future `TinyLLMStructuredAdapter` (or
similar) can be injected via the `adapter=` kwarg
without changing the facade's public API. This mirrors
the Iter-21 decision that gave us the `SLM` family.

### 2.6 `StructuredExtractionTool` — the Tool

A new module
`src/kntgraph/agents/tools/structured_extraction.py`:

```python
class StructuredExtractionTool:
    name = "extract_structured"
    description = "Extracts structured records from text using an inline schema."
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1},
            "schema": {"type": "object"},
        },
        "required": ["text", "schema"],
        "additionalProperties": False,
    }

    def __init__(
        self, *, extractor: SLMStructuredExtractor,
    ) -> None: ...

    async def invoke(
        self, *, idempotency_key: str,
        text: str, schema: dict,
    ) -> Result[list[dict], ToolError]: ...
```

The Tool is a thin adapter:

1. Validates `schema` is a non-empty `dict` with a
   single top-level key whose value is a list of
   field specifications (regex spot-check, no
   full JSON-Schema validation — the schema
   dialect is GLiNER2's, not the Tool's).
2. Delegates to
   `await extractor.extract(text, schema)`.
3. Returns `Ok(list[dict])` on success or
   `Err(ToolError("invalid_schema: ..."))` /
   `Err(ToolError("extraction_failed: ..."))` on
   failure.

The Tool does **not** read from a catalog: the
`schema` argument is **inline** in the request.
This is the explicit decision recorded in the
discussion: a central catalog of schemas adds
configuration weight and a registry for very
little gain at V1. The application knows what
schema it wants; the Tool takes what it gets.

### 2.7 Re-exports

`knowledge/extraction/__init__.py` adds to
`__all__` and to the re-export list:

- `StructuredExtractor` (Protocol)
- `GlinerStructuredAdapter`
- `SLMStructuredExtractor`

`agents/tools/structured_extraction.py` is registered
the same way the other Tools are: the application
imports it and calls `registry.register(tool)`. There
is no auto-discovery.

## 3. Consequências

### 3.1 RAM and cold start (the user-visible win)

| Scenario | Before | After |
|---|---|---|
| Process boots `SLMEntityExtractor` + `SLMArgumentExtractor` + `SLMIntentClassifier` (same `model_name`, same `device`) | 3 × ~500 MB = ~1.5 GB | ~500 MB (one model, three references) |
| Process boots the same plus `SLMStructuredExtractor` (same `model_name`) | 4 × ~500 MB = ~2.0 GB | ~500 MB |
| Process boots the same plus a PII tool that uses a **different** model (`urchen/gliner-multi-pii-base`) | 5 × ~500 MB = ~2.5 GB | ~1.0 GB (two models) |
| Cold start of the boot above | 4 × 1-3 s = 4-12 s serialised on disk read | 2 × 1-3 s = 2-6 s (one per `model_name`) |

### 3.2 The structured path is finally expressible

The PDF-OCR use case (RG, CNH, NF-e, sindicato
registration, etc.) gets a first-class path:

- The schema is the **GLiNER2 native dialect**
  (`{"documento": ["nome::str::...", "cpf::str", ...]}`),
  not a JSON-Schema consumed by `walk_schema`.
  This unlocks the features the upstream supports
  (lists with `::list`, choice fields with
  `::[a|b|c]::str`, description anchors) that the
  M2 path does not.
- The return is `list[dict]`, with one entry per
  instance found in the text. The caller picks
  `[0]` for single-instance documents (RG, CNH)
  and iterates for multi-instance ones
  (multiple NF-es in a bundle).
- The Tool accepts the schema **inline** in the
  request. No catalog, no shadow Tool, no
  per-schema registration.

### 3.3 The `Entity` and `GlinerEntityAdapter` are untouched

The FalkorDB graph still gets its `Entity` nodes from
`SLMEntityExtractor`. The `Entity` value object is
unchanged. The `extract` / `extract_with_mentions`
Protocols are unchanged. The graph projection is
unchanged. The user's call to redesign the entity
extraction is acknowledged in §4 (pending), not
sneaked through this ADR.

### 3.4 The `SLMArgumentExtractor` rename is **not** done

The discussion considered renaming
`SLMArgumentExtractor` to something more
user-friendly. This ADR **does not** do that. The
facade keeps its name, the `SLM` prefix is preserved
across the new facade, and the rename is left for a
follow-up if the maintainers agree it is worth the
deprecation window.

### 3.5 No migration, no deprecation

- `SLMEntityExtractor`, `SLMIntentClassifier`,
  `SLMArgumentExtractor` keep their constructors
  and their return types.
- The `Entity` Protocol keeps its
  `extract` / `extract_with_mentions` shapes.
- The `ArgExtraction` value object is unchanged.
- `Tool` consumers see one new optional Tool
  (`StructuredExtractionTool`); existing Tools
  are unaffected.

### 3.6 Test impact

- The three existing adapters' `__init__` tests
  change from mocking `from_pretrained` to
  mocking `GlinerModelRegistry.get`. Mechanical.
- New unit tests for `GlinerStructuredAdapter`
  use a fake `GlinerModelRegistry.get` that
  returns a mock `GLiNER2`. The adapter's
  `extract_json` invocation is asserted on the
  mock.
- New unit tests for `SLMStructuredExtractor`
  follow the same pattern as the other
  `SLM*` facades (verify default adapter is
  `GlinerStructuredAdapter`, verify injected
  adapter is used as-is).
- New unit tests for `StructuredExtractionTool`
  use a fake `SLMStructuredExtractor` (the
  pattern is identical to
  `tests/unit/knowledge/extraction/test_argument_framework.py`).
- A new registry test asserts that two adapters
  with the same `(model_name, device)` receive
  the same instance, and two adapters with
  different keys receive different instances.

### 3.7 Configuration

No new `Settings` field is required. The existing
`arg_extractor_model_id` (env `KNT_ARG_EXTRACTOR_MODEL_ID`,
default `gliner2-base`) is the model all three
existing adapters and the new structured adapter
read. The dedicated `pii_model_id`,
`entity_model_id`, `intent_model_id` split
proposed in the discussion is **not** done in this
ADR — it is recorded as a pending concern (§4) that
deserves its own decision.

## 4. Pending (out of scope for this ADR)

- **Lazy load + `warmup()`.** Today the adapters are
  eager. The cold start is paid at facade
  construction, which is not always the right time.
  A future ADR should add a `warmup()` method to
  each facade and shift the model load from
  `__init__` to the first `extract` call.
- **Per-concern model settings.** PII
  (`urchen/gliner-multi-pii-base`) and
  entity/argument (`gliner2-large-v1`) have
  different optimal checkpoints today. The current
  shared setting (`arg_extractor_model_id`) is
  workable but coarse. A future ADR should split
  it into `pii_model_id`, `entity_model_id`,
  `intent_model_id`, `arg_model_id`,
  `structured_model_id`.
- **Multi-tenant LRU eviction.** The registry holds
  models for the life of the process. A multi-tenant
  server that serves many tenants with different
  model needs will eventually need an LRU
  (`GLiNER2ModelPool`).
- **`Entity` decoupling from the graph.** The
  `Entity` value object is the right shape for the
  `FalkorDBProjector` consumer; the coupling between
  `GlinerEntityAdapter` and the graph's `Entity`
  schema is real but bounded. A future ADR can
  introduce an `ExtractedSpan` Protocol as the
  graph-agnostic primitive, with `Entity` becoming
  a graph-specific projection on top.
- **`SLMArgumentExtractor` rename.** Left for a
  follow-up. The current name is internally
  consistent with the other `SLM*` facades; the
  discussion's preference for something like
  `FieldSchemaExtractor` is a separate decision.

## 5. Decisões relacionadas

- **ADR-013 (Semantic Routing via GLiNER2).** The
  reason GLiNER2 is in the framework at all. This
  ADR does not change the M1 or M2 paths; it
  adds a fourth consumer (the structured path)
  and a registry that benefits M1, M2 and the
  graph entity path at once.
- **ADR-019 (Epílogo Typed Adapters / Redis
  Adapter Typing).** The Adapter convention this
  ADR follows: lowercase `Adapter` suffix for
  low-level, `SLM` prefix for the public facade,
  Protocol-driven injection. `GlinerStructuredAdapter`
  and `SLMStructuredExtractor` follow the same
  pattern as `GlinerEntityAdapter` /
  `SLMEntityExtractor`.
- **ADR-026 (Close GLiNER2 binding leak).** The
  Iter-28 work that promoted the argument
  extraction pieces to the framework and removed
  the `kntgraph → kntgraph.agents` lazy import.
  This ADR continues that direction: the new
  `GlinerStructuredAdapter` and the registry live
  in the framework, with no vertical import.
- **ADR-047 (Tool Adapter Pattern).** The
  `StructuredExtractionTool` follows the same
  shape as the Tools ADR-047 documents.

## 6. References

- `src/kntgraph/knowledge/extraction/gliner.py` —
  the `GlinerEntityAdapter` this ADR's registry
  refactor touches.
- `src/kntgraph/knowledge/extraction/gliner_intent.py` —
  the `GlinerIntentAdapter` (M1 of ADR-013).
- `src/kntgraph/knowledge/extraction/argument/_gliner_finder.py` —
  the `GlinerFieldFinder` (M2 of ADR-013).
- `src/kntgraph/knowledge/extraction/_slm_facades.py` —
  the `SLM*` facade family this ADR extends.
- `src/kntgraph/tools/protocol.py` — the `Tool`
  Protocol the new `StructuredExtractionTool`
  implements.
- `src/kntgraph/tools/registry.py` — the
  `ToolRegistry` that holds the new Tool.
- GLiNER2 tutorial
  `tutorial/3-json_extraction.md` — the schema
  dialect (`{"<structure>": ["field::str", ...]}`)
  the new `GlinerStructuredAdapter` passes
  through to `model.extract_json`.
- `gliner2` Python API — `GLiNER2.from_pretrained`,
  `GLiNER2.extract_json`, thread-safety of
  inference (`asyncio.to_thread`).
