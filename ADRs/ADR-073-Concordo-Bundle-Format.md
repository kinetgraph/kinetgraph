<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-073: Concordo Bundle Format

- **Status:** Accepted
- **Date:** 2026-09-12
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-069](./ADR-069-Agent-Concordo-Foundation.md) — Concordo Foundation
  - [ADR-071](./ADR-071-BusinessFSM-Concordo.md) — BusinessFSM Concordo
  - [ADR-072](./ADR-072-WorkflowSaga-Concordo.md) — WorkflowSaga Concordo
- **Implementation reference:** [docs/concordos-bundle-spec.md](../docs/concordos-bundle-spec.md)

---

## 1. Context

The Concordo Foundation (ADR-069), BusinessFSM
(ADR-071), and WorkflowSaga (ADR-072) define the
**Python API** for declaring behaviour. A vertical that
prefers declarative configuration — for operational
review, multi-tenant overrides, or CI gating — can
declare the same behaviour via a YAML or JSON
**bundle** loaded by `ConcordoCatalog.from_yaml(...)`.

This ADR captures the *design intent*: why the format
exists, what shapes it covers, and what it deliberately
does **not** cover. The full grammar, the validator
pipeline, and the edge cases are in
[docs/concordos-bundle-spec.md](../docs/concordos-bundle-spec.md)
— that doc is the implementation reference and is
maintained alongside the code, not through the ADR
review process.

---

## 2. Why externalise

- **Operational review.** A reviewer can read the
  YAML and reason about the saga's lifecycle
  without learning the Python API.
- **Multi-tenant overrides.** A tenant can ship a
  YAML override that adjusts `saga_timeout_ms` or
  swaps a guard, without the framework shipping a
  new release.
- **CI gate.** The `knt concordo validate` CLI
  parses and validates the YAML, so a broken config
  fails the pipeline before deploy.
- **Static cross-validation.** Declaring events
  up-front lets the loader catch typos at parse
  time (`transition.on_event = "docment.ingested"`
  → "event not declared in bundle").

The Python API remains the canonical source. The
YAML is a serialised view of the same objects;
loading and dumping round-trips through Pydantic
schemas without loss.

---

## 3. The bundle format

The top-level unit is a **bundle**: a named,
versioned, atomic package of behaviour. A bundle
declares its event vocabulary, its named
predicates, and its FSMs / sagas.

```yaml
# fmh_office/concordos/knowledge_pipeline.yaml
bundle_id: "com.acme.knowledge_pipeline"
version: "1.0.0"

# Event vocabulary. Schemas are Pydantic model
# references (resolved via importlib). Unknown event
# types in transitions / steps are caught at load
# time.
events:
  - name: "document.ingested"
    schema: "fmh_office.knowledge.events.DocumentIngested"
  - name: "knowledge.entities_extracted"
    schema: "fmh_office.knowledge.events.EntitiesExtracted"

# Named predicates. Reused across transitions and
# steps; can reference other names and the
# mini-language builtins.
specifications:
  - id: "IsHighPriority"
    expression: "event.data.content_length > 50000"

  - id: "ExtractionConfidencePassed"
    expression: "event.data.confidence_score >= 0.80"

# FSM. `id` becomes the `Concordo.name`
# (prefix ``fsm:``). `component` is a dotted path
# resolved at load time. `states` is explicit so the
# loader can validate transition targets, terminal
# membership, and reachability.
business_fsm:
  id: "fsm:KnowledgeLifecycle"
  component: "fmh_office.knowledge.components.DocumentComponent"
  state_field: "lifecycle_state"
  initial_state: "INGESTED"
  states:
    - "INGESTED"
    - "EXTRACTING"
    - "WAITING_HUMAN_REVIEW"
    - "CONSOLIDATED"
    - "REJECTED"
  terminal: ["CONSOLIDATED", "REJECTED"]

  transitions:
    - {from: "INGESTED",  to: "EXTRACTING", on_event: "document.ingested"}
    - {from: "EXTRACTING", to: "CONSOLIDATED", on_event: "knowledge.entities_extracted",
       guard: "ExtractionConfidencePassed"}
    - {from: "EXTRACTING", to: "WAITING_HUMAN_REVIEW", on_event: "knowledge.entities_extracted",
       guard: "not(ExtractionConfidencePassed)"}

  on_entry:
    CONSOLIDATED:         "knowledge.consolidated"
    WAITING_HUMAN_REVIEW:  "knowledge.awaiting_review"

# Sagas. `id` becomes the `Concordo.name`
# (prefix ``saga:``). `trigger_event` is the external
# event that starts the saga; the projection
# materialises `SagaProgressComponent` (ADR-072 §3.3).
workflow_sagas:
  - id: "saga:EntityExtractionSaga"
    trigger_event: "document.ingested"
    saga_timeout_ms: 300000

    # Failure policy. Default if omitted: fail on the
    # first failure of any step (matches the Python
    # ``fail_when=None`` semantics).
    fail_when: "step_failed('EntityExtraction') or step_timed_out('EntityExtraction')"

    steps:
      - name: "TextChunking"
        tool: "text_chunker_tool"
        timeout_ms: 10000
        input_mapping:
          text: "event.data.content"

      - name: "EntityExtraction"
        tool: "gliner2_entity_extraction_tool"
        pre_condition: "step_completed('TextChunking')"
        timeout_ms: 60000
        compensate_tool: "gliner2_rollback_tool"
        compensate_when: "not(step_timed_out('EntityExtraction'))"
        input_mapping:
          chunks: "steps.TextChunking.output.chunks"
```

The same shape is accepted as JSON. The loader
detects the file extension and routes to
`yaml.safe_load` (via `pyyaml`) or `json.loads`.

**Unknown keys are rejected** at every level — both
top-level (a typo in `bundleid` instead of `bundle_id`
is fatal) and nested (a typo in `guardd` fails the
load). The error path is dotted
(`workflow_sagas[0].steps[1].compensate_tool`) so
the operator can find the offender in a file without
reading line-by-line.

---

## 4. Design decisions

### 4.1 Bundle as the unit of versioning

The `bundle_id` follows reverse-DNS
(`com.acme.knowledge_pipeline`) — the convention used
by Java/OSGi/Kubernetes. The `version` follows semver.
Two bundles with the same `bundle_id` but different
versions are different artefacts; the loader
collapses them by name (last-loaded wins).

The Foundation's individual Concordos
(`fsm:KnowledgeLifecycle`, `saga:EntityExtractionSaga`)
live **inside** a bundle. Their `Concordo.name` is a
namespace-qualified identifier; the bundle `id` is a
release identifier. The two concepts are
intentionally distinct — a bundle ships Concordos,
Concordos do not carry versions.

### 4.2 Pydantic for event schemas, not JSON Schema

The `events[].schema` field is a **string reference
to a Pydantic model subclass** declared in Python
code. The model validates event payloads at **emit
time** (when the producer calls `Event.domain_from`),
not at bundle-load time.

Rationale:

- The framework already uses Pydantic
  (`pyproject.toml: pydantic>=2.13.4`). Two schema
  languages would require the team to maintain
  both.
- Event validation at emit time catches producer
  bugs before they reach the EventLog. JSON Schema
  validation at load time would only catch the
  absence of a schema reference, not malformed
  payloads.
- A bundle's `events` list is a vocabulary
  declaration, not a schema definition. The actual
  schema lives in code where it can be
  type-checked, IDE-autocompleted, and unit-tested.

The full schema definitions are in
[docs/concordos-bundle-spec.md §3](../docs/concordos-bundle-spec.md#3-pydantic-schemas).

### 4.3 Custom mini-language for predicates

The mini-language is a small recursive-descent
parser that handles:

- Path access (`event.data.x`, `steps.<name>.output.y`).
- Comparison operators (`==`, `!=`, `<`, `<=`, `>`, `>=`).
- Boolean operators (`and`, `or`, `not`).
- Function calls on builtins (`step_completed(name)`).
- Numeric, string, boolean, and null literals.

The grammar, parser, evaluator, and built-in spec
catalog live in
[docs/concordos-bundle-spec.md §2-4](../docs/concordos-bundle-spec.md).
The mini-language is **declarative** — it cannot
express lambdas or arbitrary code. Specs that need
runtime logic are registered in Python via
`SpecRegistry` (ADR-069 §2.4).

### 4.4 FSM `states` explicit

The FSM declares its `states` list explicitly. This
enables:

- Validation that every transition references a
  declared state.
- Validation that `terminal` states are a subset of
  `states`.
- Reachability analysis (BFS from `initial_state`).
- Documentation generation (diagrams).

Transitions are a list of `{from, to, on_event, guard}`
records — more readable than a nested dict and easier
to scan top-down.

### 4.5 Saga `trigger_event` explicit

The saga declares `trigger_event` as the external
event that starts it. This is separate from
`saga.<name>.started`, which is the internal event
emitted by the projection. The explicit trigger
makes the saga's entry point declarative.

### 4.6 `input_mapping` with explicit paths

Each step declares an `input_mapping:
{param_name: path}` that maps the tool's params to
expressions over the trigger event and previous step
results. The paths are evaluated at dispatch time.

This is more explicit than the Python API's
`enrich_from: tuple[str, ...]`, which lists field
names to inject without expressing the source. The
two are equivalent — the bundle loader converts
`input_mapping` to the Python API.

### 4.7 Cross-bundle references

A bundle cannot reference predicates declared in
another bundle. Cross-bundle references are out of
scope for v1 — they would require a global registry
(beyond `SpecRegistry`) and an explicit resolution
policy. The mini-language resolves names against
(a) the bundle's own `specifications:` list, then
(b) the global `SpecRegistry`, then (c) the built-in
catalog.

### 4.8 Sandbox and security

A bundle's `component:` and `schema:` fields are
dotted paths resolved via `importlib`. A bundle can
therefore import any module accessible to the
Python process. **Bundles are trusted code**. Loading
untrusted YAML is not supported in v1. A future
revision may add an allowlist sandbox if needed.

---

## 5. Loader

`ConcordoCatalog.from_yaml(path)` runs five checks in
order. The first one that fails raises and the rest
are skipped:

1. **Parse** — `yaml.safe_load` or `json.loads`.
2. **Schema validation** — Pydantic against
   `BundleSchema`.
3. **Cross-reference validation** — `on_event` /
   `trigger_event` in `events[]`; `from` / `to` in
   `states`; `tool` registered; `input_mapping` paths
   reference declared scopes.
4. **FSM graph validation** — reachability,
   `initial_state` in `states`, `terminal` subset of
   `states`.
5. **Predicate syntax validation** — every
   `expression` parses. Semantic evaluation is **not**
   run at load time (it requires a `StepContext`).

The detailed pipeline is in
[docs/concordos-bundle-spec.md §5](../docs/concordos-bundle-spec.md#5-validator-pipeline).

```python
from pathlib import Path
from kntgraph.concordos import ConcordoCatalog

catalog = ConcordoCatalog.from_yaml(Path("app.yaml"))
# ``catalog`` now contains one Concordo per
# ``business_fsm`` and one per ``workflow_sagas``
# entry.

dispatcher = ReactiveDispatcher(log=log, redis=redis)
catalog.install_all(dispatcher)
```

### 5.1 Round-trip

```python
catalog.to_yaml("app.normalized.yaml")  # canonical formatting
catalog.to_dict()  # round-trip via Pydantic
```

`to_yaml` / `to_json` emit the canonical form so
version-controlled configs do not drift in formatting
(whitespace, key ordering, comment preservation is
**not** a goal). Round-tripping a bundle that uses
Python-registered specs loses those specs (the
loader doesn't know about them); they must be
re-registered on the new process.

---

## 6. Composition and installation

### 6.1 Three entry points

```python
# Programmatic
from fmh_office.concordos.invoice_fsm import invoice_fsm
from fmh_office.concordos.nfe_emission_saga import nfe_emission_saga

dispatcher = ReactiveDispatcher(log=log, redis=redis)
ConcordoCatalog(invoice_fsm, nfe_emission_saga).install_all(
    dispatcher
)


# YAML-loaded
from kntgraph.concordos import ConcordoCatalog

catalog = ConcordoCatalog.from_yaml(Path("app.yaml"))
dispatcher = ReactiveDispatcher(log=log, redis=redis)
catalog.install_all(dispatcher)


# Hybrid
catalog = ConcordoCatalog.from_yaml(Path("app.yaml"))
catalog.add(invoice_fsm)  # override the YAML one
```

### 6.2 Multi-bundle loading

A vertical that needs multiple bundles (e.g. a shared
`knowledge_pipeline` bundle plus a tenant-specific
override) loads each one and composes them:

```python
shared = ConcordoCatalog.from_yaml(Path("bundles/knowledge_pipeline.yaml"))
tenant = ConcordoCatalog.from_yaml(Path(f"tenants/{tenant_id}.yaml"))
shared.install_all(dispatcher)
tenant.install_all(dispatcher)  # shadows by Concordo.name
```

### 6.3 DLQ ingestion

The catalog does not carry a DLQ handle. DLQ
ingestion is wired by the application via
`dispatcher.subscribe` (ADR-069 §5.2). See
ADR-072 §3.6 for the full pattern.

---

## 7. CLI

```bash
# Validate a bundle (no side effects; exits non-zero on
# any of the five checks in §5).
uv run knt concordo validate --bundle app.yaml

# Round-trip a bundle to canonical form.
uv run knt concordo format --bundle app.yaml > app.normalized.yaml

# Scaffold a starter bundle for an existing DomainComponent.
uv run knt concordo add fsm InvoiceFSM \
  --component InvoiceDomainComponent \
  --state-field status

# Scaffold a starter bundle for a WorkflowSaga.
uv run knt concordo new saga NfeEmission \
  --trigger document.ingested \
  --steps validate_fiscal:sefaz_validator,\
          emit_nfe:nfe_emitter,\
          register_receivable:erp_tool \
  --timeout-ms 300000

# List registered Specifications (built-in + app-registered).
uv run knt concordo specs list
```

The `add` / `new` commands produce:
1. A `concordos/<bundle_id>.yaml` bundle with typed
   configuration and starter states / steps.
2. A stub test file in `tests/unit/concordos/`.

The CLI deliberately does **not** ship `lint` or
`codegen` in v1 — those are deferred to a follow-up
ADR (they are non-trivial features that need their
own design).

---

## 8. Source code layout

```
src/kntgraph/concordos/
+-- _loader.py             # Bundle loaders (YAML / JSON / dict);
│                          #   cross-validates events ⇄ transitions
│                          #   ⇄ steps; surfaces typed
│                          #   ConcordoValidationError with dotted path
+-- schemas.py             # Pydantic v2 models for the bundle
│                          #   surface (BundleSchema, EventSchema,
│                          #   SpecificationSchema, FSMConfigSchema,
│                          #   SagaConfigSchema, FSMTransitionSchema,
│                          #   SagaStepSchema)
+-- _mini_lang.py          # Predicate parser + evaluator
│                          #   (~1100 lines total; recursive descent,
│                          #   no eval; builtins bound to the spec
│                          #   registry)
```

---

## 9. Open questions

1. **Multi-tenant overrides** — see ADR-069 §7.
2. **Cross-bundle predicate references** — see §4.7.
3. **Sandbox for untrusted bundles** — see §4.8.
4. **`lint` and `codegen` CLI** — see §7.

---

## 10. References

- [ADR-069 — Concordo Foundation](./ADR-069-Agent-Concordo-Foundation.md)
- [ADR-071 — BusinessFSM Concordo](./ADR-071-BusinessFSM-Concordo.md)
- [ADR-072 — WorkflowSaga Concordo](./ADR-072-WorkflowSaga-Concordo.md)
- [docs/concordos-bundle-spec.md](../docs/concordos-bundle-spec.md) — implementation reference
