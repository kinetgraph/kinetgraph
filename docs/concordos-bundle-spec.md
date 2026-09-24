<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Concordo Bundle Format — Implementation Reference

This document is the **implementation reference** for the
Concordo bundle format defined in
[ADR-073](./ADR-073-Concordo-Bundle-Format.md). The ADR
captures *what* the format is and *why*; this doc captures
*how* — the grammar, the schemas, the validator pipeline,
and the edge cases a builder implementation must handle.

It is **not** an ADR. Changes to this doc follow the regular
PR review process; they do **not** require an ADR amendment.
The ADR is the source of truth for design intent; this doc
is the source of truth for implementation details.

---

## 1. Overview

A bundle is a YAML or JSON document that declares:

- `bundle_id` and `version` — the unit of release.
- `events` — the event vocabulary the bundle references,
  with per-event schemas (Pydantic models in code).
- `specifications` — named predicates reused across the
  bundle, expressed in the mini-language.
- `business_fsm` — at most one FSM definition.
- `workflow_sagas` — zero or more saga definitions.

The format is loaded by
`ConcordoCatalog.from_yaml(path)` /
`ConcordoCatalog.from_json(path)` /
`ConcordoCatalog.from_dict(d)` and produces one
[`Concordo`](./ADR-069-Agent-Concordo-Foundation.md#concordo-protocol)
per `business_fsm` and per `workflow_sagas` entry.

Cross-references in this doc assume the reader knows the
foundation (Specification Pattern, Concordo Protocol, Catalog)
from [ADR-069](./ADR-069-Agent-Concordo-Foundation.md), the
FSM Concordo from
[ADR-071](./ADR-071-BusinessFSM-Concordo.md), and the Saga
Concordo from
[ADR-072](./ADR-072-WorkflowSaga-Concordo.md).

---

## 2. The predicate mini-language

Predicates appear in seven places: `guard`, `fail_when`,
`skip_when`, `compensate_when`, `pre_condition`,
`proceed_when`, and the `expression` field of a
`specifications` entry. The grammar is the same in every
location.

### 2.1 Two forms: name lookup or expression

A predicate string is classified syntactically:

- **No parens, no operator, no path, no number** → it is
  a **name**. The loader resolves it against (a) the
  bundle's `specifications:` list, then (b) the global
  `SpecRegistry` (Python-side), then (c) the built-in
  spec catalog (§4 below).
- **Anything else** → it is an **expression**. The
  loader parses it (§2.2) and produces an AST evaluator.

```yaml
guard: "ExtractionConfidencePassed"      # name lookup
guard: "event.data.score >= 0.80"          # expression
guard: "not(ExtractionConfidencePassed)"  # expression with composition
guard: "step_completed('TextChunking')"   # builtin call
```

### 2.2 Expression grammar (EBNF)

```ebnf
(* Top-level *)
expr        = or_expr ;

(* Boolean operators, in precedence order: or < and < not *)
or_expr     = and_expr , { "or"  , and_expr } ;
and_expr    = not_expr , { "and" , not_expr } ;
not_expr    = "not" , not_expr
            | atom ;

(* Atomic forms *)
atom        = comparison
            | call
            | path
            | "(" , expr , ")"
            | literal ;

(* Comparison: path op value *)
comparison  = path , comp_op , value ;
comp_op     = "==" | "!=" | "<=" | ">=" | "<" | ">" ;

(* Built-in call: name "(" arglist? ")" *)
call        = identifier , "(" , [ expr_list ] , ")" ;
expr_list   = expr , { "," , expr } ;

(* Path: scope.tail *)
path        = scope , { "." , identifier } ;
scope       = "event.data" | "steps" | "agent" | "now" ;

(* Literals *)
literal     = number | string | "true" | "false" | "null" ;
number      = digit , { digit } , [ "." , { digit } ] ;
string      = "'" , { character - "'" | "''" } , "'"
            | '"' , { character - '"' | '""' } , '"' ;
```

### 2.3 Operator precedence

```
lowest   or
         and
         not (unary)
         == != < <= > >=
highest  primary (literal, path, call, parens)
```

Examples:

| Expression | Parses as |
|---|---|
| `a or b and c` | `a or (b and c)` |
| `not a == b` | `not (a == b)` |
| `a < b and c > d` | `(a < b) and (c > d)` |
| `f(x, y) > 5` | `(f(x, y)) > 5` |

### 2.4 Type coercion rules

The evaluator applies these coercions when comparing:

| LHS type | RHS type | Coercion |
|---|---|---|
| number | number | numeric comparison; mixed `int` / `float` is fine |
| string | string | lexicographic comparison |
| bool | bool | strict equality only (no coercion to int) |
| anything | `null` | comparison is `False` unless LHS is also `null` |
| path | missing | the path evaluates to `null`; comparison is `False` |
| datetime | datetime | compared via `datetime.timestamp()` (wall-clock) |

`null` propagation: a path that resolves to a missing key
returns `null`. Comparisons involving `null` (other than
`null == null`) evaluate to `False`. This is the principle of
**graceful failure**: a typo in `event.data.content_lenght`
makes the comparison fail without raising.

### 2.5 Path scopes

| Prefix | Resolves to |
|---|---|
| `event.data.*` | The data payload of the trigger event |
| `steps.<name>.output.*` | The tool worker's result (the `result` of `ToolCallCompletion`, ADR-034) |
| `steps.<name>.result.*` | Alias for `output.*` — kept for clarity in user-facing configs |
| `agent.<field>` | The agent's `DomainComponent` fields (e.g. `agent.lifecycle_state`) |
| `now` | The dispatcher-injected `datetime` (§3.4 / §4.5 of the FSM/Saga ADRs) |

Path resolution depth:

- `event.data.<a>.<b>.<c>` walks three dict levels. Missing
  intermediate keys return `null`.
- `steps.<name>.<output|result>` requires the named step
  to have been dispatched at least once; if not, the path
  resolves to `null`.
- `agent.<field>` is `getattr`-style access on the agent's
  `DomainComponent`; a missing field returns `null`.
- `now` is a `datetime`; attributes (`now.year`,
  `now.hour`, `now.weekday()`) are accessible via the
  same dot syntax the parser supports. Method calls on
  `now` use the same `call` production as builtin specs.

### 2.6 Comments

The grammar permits `#` line comments in expressions:

```
# The chunking step must succeed before extraction.
pre_condition: "step_completed('TextChunking')"
```

The lexer strips `#` to end-of-line. Multi-line comments
(`# ... \n # ...`) are not supported — write them outside
the expression string.

### 2.7 Parser error reporting

The parser tracks line and column through the source
string (treating `\n` as line break). On parse failure
it raises `ConcordoSyntaxError` carrying:

```python
@dataclass(frozen=True, slots=True)
class ConcordoSyntaxError(ValueError):
    expression: str       # the full expression that failed
    line: int            # 1-indexed
    column: int          # 1-indexed
    expected: str        # what the parser expected (e.g. "comparison operator")
    found: str           # what it actually found (e.g. "identifier 'foo'")
    hint: str | None     # optional remediation
```

The CLI's `validate` command (§5.5) prints the error
with caret-pointer context.

### 2.8 Implementation size

The parser + evaluator + bindings + tests is **~1100
lines of code**, not ~150 as the original ADR draft
estimated. Breakdown:

| Component | Lines |
|---|---|
| Tokenizer | 80 |
| Recursive-descent parser (produces AST) | 200 |
| AST evaluator (walks AST against `StepContext`) | 200 |
| Built-in bindings (function names → spec constructors) | 100 |
| Error reporting (line/column tracking) | 80 |
| Parser tests (`pytest` table-driven) | 300 |
| Evaluator tests (with fixtures) | 200 |
| **Total** | **~1160** |

PR 1.5 ships this in `concordos/_mini_lang.py` and
`concordos/_mini_lang_tests.py`.

---

## 3. Pydantic schemas

The bundle is validated against Pydantic v2 models.
Pydantic is already a framework dependency
(`pyproject.toml: pydantic>=2.13.4`); this spec does **not**
add a `jsonschema` dependency.

### 3.1 Pattern vocabulary

```python
# Pattern matches ``domain.subdomain.name`` /
# ``tool.<name>.requested`` / ``saga.<name>.started``.
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

# Bundle IDs follow Java/OSGi/Kubernetes convention.
_BUNDLE_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

# Concordo IDs are namespace-prefixed: ``fsm:KnowledgeLifecycle``,
# ``saga:EntityExtractionSaga``.
_FSM_ID = re.compile(r"^fsm:[A-Za-z_][A-Za-z0-9_]*$")
_SAGA_ID = re.compile(r"^saga:[A-Za-z_][A-Za-z0-9_]*$")

# Step / spec names are valid Python identifiers.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Semantic version.
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
```

### 3.2 Schemas

```python
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class _StrictModel(BaseModel):
    """Base for all bundle models.

    ``extra='forbid'`` rejects unknown keys at every level
    — a typo in ``compensate_tool`` (e.g. ``compesate_tool``)
    fails loudly with the dotted path, not silently.
    """
    model_config = ConfigDict(extra="forbid")


class EventSchema(_StrictModel):
    """One event in the bundle's vocabulary."""
    name: str = Field(pattern=_EVENT_NAME.pattern)
    schema_ref: str = Field(
        alias="schema",
        description=(
            "Dotted path to a Pydantic model subclass. "
            "Resolved via importlib at load time. "
            "Models validate event payloads at emit time."
        ),
    )


class SpecificationSchema(_StrictModel):
    """A named predicate (§2 of the bundle-format ADR)."""
    id: str = Field(pattern=_IDENT.pattern)
    expression: str = Field(min_length=1, max_length=2048)


class FSMTransitionSchema(_StrictModel):
    from_: str = Field(alias="from", pattern=_IDENT.pattern)
    to: str = Field(pattern=_IDENT.pattern)
    on_event: str = Field(pattern=_EVENT_NAME.pattern)
    guard: str | None = Field(default=None, max_length=2048)


class FSMConfigSchema(_StrictModel):
    id: str = Field(pattern=_FSM_ID.pattern)
    component: str = Field(
        description=(
            "Dotted path to a DomainComponent subclass. "
            "Resolved via importlib at load time."
        )
    )
    state_field: str = Field(pattern=_IDENT.pattern)
    initial_state: str = Field(pattern=_IDENT.pattern)
    states: list[str] = Field(min_length=1)
    terminal: list[str] = Field(default_factory=list)
    transitions: list[FSMTransitionSchema] = Field(min_length=1)
    on_entry: dict[str, str] = Field(default_factory=dict)

    @field_validator("terminal")
    @classmethod
    def _terminal_subset(cls, v: list[str], info) -> list[str]:
        states: list[str] = info.data.get("states", [])
        unknown = set(v) - set(states)
        if unknown:
            raise ValueError(
                f"terminal states not declared in 'states': {sorted(unknown)}"
            )
        return v

    @field_validator("on_entry")
    @classmethod
    def _on_entry_targets(cls, v: dict[str, str]) -> dict[str, str]:
        for target in v:
            if not _EVENT_NAME.match(target):
                raise ValueError(
                    f"on_entry target {target!r} must be a dotted event name"
                )
        return v


class SagaStepSchema(_StrictModel):
    name: str = Field(pattern=_IDENT.pattern)
    tool: str | None = Field(
        default=None,
        description=(
            "None declares a human step (ADR-072 §4.3). "
            "String declares the @tool_worker name."
        ),
    )
    timeout_ms: int = Field(default=30_000, ge=1)
    pre_condition: str | None = Field(default=None, max_length=2048)
    skip_when: str | None = Field(default=None, max_length=2048)
    compensate_tool: str | None = Field(default=None)
    compensate_when: str | None = Field(default=None, max_length=2048)
    approval_timeout_ms: int | None = Field(default=None, ge=1)
    input_mapping: dict[str, str] = Field(default_factory=dict)

    @field_validator("input_mapping")
    @classmethod
    def _input_paths(cls, v: dict[str, str]) -> dict[str, str]:
        for param_name, path in v.items():
            if not path.startswith(("event.data.", "steps.", "agent.")):
                raise ValueError(
                    f"input_mapping[{param_name!r}]={path!r} "
                    f"must start with 'event.data.', 'steps.', or 'agent.'"
                )
        return v


class SagaConfigSchema(_StrictModel):
    id: str = Field(pattern=_SAGA_ID.pattern)
    trigger_event: str = Field(pattern=_EVENT_NAME.pattern)
    saga_timeout_ms: int = Field(default=300_000, ge=1)
    fail_when: str | None = Field(default=None, max_length=2048)
    steps: tuple[SagaStepSchema, ...] = Field(min_length=1)


class BundleSchema(_StrictModel):
    bundle_id: str = Field(pattern=_BUNDLE_ID.pattern)
    version: str = Field(pattern=_SEMVER.pattern)
    events: list[EventSchema] = Field(default_factory=list)
    specifications: list[SpecificationSchema] = Field(default_factory=list)
    business_fsm: FSMConfigSchema | None = None
    workflow_sagas: list[SagaConfigSchema] = Field(default_factory=list)

    @field_validator("workflow_sagas")
    @classmethod
    def _unique_step_names(
        cls, v: list[SagaConfigSchema]
    ) -> list[SagaConfigSchema]:
        for saga in v:
            names = [s.name for s in saga.steps]
            if len(names) != len(set(names)):
                dupes = {n for n in names if names.count(n) > 1}
                raise ValueError(
                    f"saga {saga.id!r} has duplicate step names: "
                    f"{sorted(dupes)}"
                )
        return v
```

### 3.3 Why Pydantic, not JSON Schema

The bundle's `events[].schema` is **not** a JSON Schema
object. It is a string reference to a Pydantic model
subclass declared in Python code. The model validates
event payloads at **emit time** (when the producer
calls `Event.create(...)`), not at bundle-load time.

Rationale:

- The framework already uses Pydantic (ADR-069 Foundation
  cites this). Two schema languages would require the
  team to maintain both.
- Event validation at emit time catches producer bugs
  before they reach the EventLog. JSON Schema validation
  at load time would only catch the absence of a schema
  reference, not malformed payloads.
- A bundle's `events` list is a vocabulary declaration, not
  a schema definition. The actual schema lives in code
  where it can be type-checked, IDE-autocompleted, and
  unit-tested.

If a vertical wants JSON Schema-style runtime validation
of arbitrary dicts, it can register a Pydantic model
that uses `RootModel` with a hand-written validator. The
Pydantic layer is the only one in the bundle format.

---

## 4. Built-in specs catalog

The mini-language resolves the following function names
to Python `Specification` subclasses declared in
`concordos/specs.py`:

| Function name | Maps to | Reads from |
|---|---|---|
| `step_completed(name)` | `StepCompleted(step_name=name)` | `ctx.step_states.get(name) == "completed"` |
| `step_failed(name)` | `StepFailed(step_name=name)` | `ctx.step_states.get(name) in ("failed", "timed_out")` |
| `step_timed_out(name)` | `StepTimedOut(step_name=name)` | `ctx.step_states.get(name) == "timed_out"` |
| `domain_state_is(field, value)` | `DomainStateIs(field=field, value=value)` | `getattr(ctx.domain, field, None) == value` |
| `profile_tier_is(tier)` | `ProfileTierIs(tier=tier)` | `ctx.profile.tier == tier` |
| `continuity_tool_used(name)` | `ContinuityToolUsed(tool_name=name)` | `name in ctx.continuity.last_tools` |

### 4.1 Sync between mini-language and Python

The `concordos/_mini_lang.py` module exposes a
`BUILTIN_SPECS` dict:

```python
BUILTIN_SPECS: dict[str, Callable[..., Specification]] = {
    "step_completed":      lambda name: StepCompleted(step_name=name),
    "step_failed":         lambda name: StepFailed(step_name=name),
    "step_timed_out":      lambda name: StepTimedOut(step_name=name),
    "domain_state_is":     lambda field, value: DomainStateIs(field=field, value=value),
    "profile_tier_is":     lambda tier: ProfileTierIs(tier=tier),
    "continuity_tool_used": lambda name: ContinuityToolUsed(tool_name=name),
}
```

A CI test (`tests/unit/concordos/test_builtin_sync.py`)
walks both the `BUILTIN_SPECS` dict and the
`concordos.specs` module's public class list and asserts
that the two stay in sync. A drift between the parser
and the Python classes fails the build.

### 4.2 Adding a new builtin

Adding a builtin is a two-step change:

1. Implement the `Specification` subclass in
   `concordos/specs.py`.
2. Register the parser token in
   `concordos/_mini_lang.py:BUILTIN_SPECS`.

The CI sync test (above) catches the case where only
one of the two is done.

---

## 5. Validator pipeline

`ConcordoCatalog.from_yaml(path)` runs five checks in
order. The first one that fails raises and the rest are
skipped.

### 5.1 Step 1 — Parse

Detect file extension, route to `yaml.safe_load` or
`json.loads`. For JSON, the parse errors carry the byte
offset; for YAML, the line/column.

### 5.2 Step 2 — Schema validation

Pydantic validates the parsed dict against `BundleSchema`
(§3.2). Errors carry the dotted path
(`workflow_sagas[0].steps[1].compensate_tool`).

The error class is `ConcordoValidationError`, a subclass
of `pydantic.ValidationError` that adds:

```python
@dataclass(frozen=True, slots=True)
class ConcordoValidationError(pydantic.ValidationError):
    bundle_path: Path | None  # source file, if any
    bundle_id: str | None     # bundle_id from the parsed doc
```

### 5.3 Step 3 — Cross-reference validation

After schema, the loader runs semantic checks:

- `business_fsm.transitions[].on_event` is in
  `events[].name`.
- `business_fsm.transitions[].from` / `.to` are in
  `business_fsm.states`.
- `business_fsm.transitions[].from` is not in
  `business_fsm.terminal` (terminal states have no
  outgoing transitions).
- `workflow_sagas[].trigger_event` is in `events[].name`.
- `workflow_sagas[].steps[].tool` is registered as a
  `@tool_worker` (the dispatcher exposes the lookup;
  the loader calls it via `dispatcher.tool_router.is_known`).
- `workflow_sagas[].steps[].name` is unique within the
  saga (already enforced by Pydantic validator).
- `input_mapping` paths reference declared scopes
  (§2.5). Already enforced by the
  `_input_paths` validator on `SagaStepSchema`.

### 5.4 Step 4 — FSM graph validation

- `business_fsm.initial_state` is in `business_fsm.states`.
- Every state in `states` is reachable from
  `initial_state` via `transitions`.
- Terminal states have no outgoing transitions
  (also enforced by schema validator).
- `on_entry` keys are states that are actually reachable.

Reachability uses BFS over the transition graph. The
algorithm is in `concordos/_loader.py:_check_reachability`
(~50 lines).

### 5.5 Step 5 — Predicate syntax validation

Every predicate string (`guard`, `fail_when`,
`skip_when`, `compensate_when`, `pre_condition`,
`proceed_when`, `specifications[].expression`) is
parsed by the mini-language. Parse errors raise
`ConcordoSyntaxError` (§2.7).

**Semantic evaluation is NOT run at validate time.** It
happens lazily at guard execution when a `StepContext`
is available. This keeps `validate` pure and free of
runtime dependencies (Redis, DomainComponent
instances, etc.).

### 5.6 Step 6 — Resolved-component check

After all of the above, the loader resolves the
`component:` and `schema:` dotted paths via `importlib`.
A path that does not resolve raises
`ConcordoResolutionError` with the unresolved path.

This step is the only one that touches the filesystem
(via importlib's path hooks) and the network (none
unless the imported module does its own). Tests stub
importlib to keep this offline.

---

## 6. Edge cases the loader handles

These are decisions made during implementation that
operators should know about.

### 6.1 Duplicate `specifications[].id`

A bundle with two entries sharing `id` is rejected by
the schema validator (`field_validator` checks
uniqueness). The error message lists the offending ids
and the bundles where they appear.

### 6.2 Empty `events` list

Allowed. A bundle may not need to declare events if it
only references events defined elsewhere. The validator
flags `transitions[].on_event` and
`sagas[].trigger_event` references that are not in
`events` as errors.

### 6.3 `events[].name` colliding with framework events

Events named `agent.*` (operational namespace) are
rejected by `_EVENT_NAME` pattern? **No** — the pattern
allows `agent.something`. The conflict is caught at
emit time by `Event.domain_from` (the validator
rejects `agent.*` in domain events). The bundle loader
does **not** duplicate this check; the bundle's events
list is for cross-reference, not for namespace
validation.

### 6.4 `on_entry` event declared but never emitted

Allowed. The FSM emits `on_entry` events when
transitioning into the named state. If no transition
ever reaches the state, the `on_entry` event is never
emitted. The validator does not require every declared
`on_entry` to be reachable — that would force a
double-check on transitions.

### 6.5 Step name shadowing

Steps within the same saga must have unique names
(Pydantic validator). Steps across different sagas can
share names — they are independent state machines.

### 6.6 `input_mapping` paths referencing missing steps

If `input_mapping` says `steps.MissingStep.output.foo`
and no step named `MissingStep` exists in the saga,
the path resolves to `null` at evaluation time. This
is silent (no parse error). A future improvement is to
validate `input_mapping` paths against the saga's
declared steps at load time.

### 6.7 Predicate length limit

`expression` and predicate strings are capped at
**2048 characters**. The limit exists because the
parser uses a recursive-descent strategy with linear
recursion depth; very long strings could blow the
stack. The limit is generous — no real predicate
should approach it. Operators who hit it can split a
complex predicate into named pieces.

---

## 7. Examples

### 7.1 Minimal FSM

```yaml
bundle_id: "com.example.invoice"
version: "1.0.0"

events:
  - name: "invoice.submitted"
    schema: "example.invoice.events.InvoiceSubmitted"
  - name: "invoice.paid"
    schema: "example.invoice.events.InvoicePaid"

business_fsm:
  id: "fsm:InvoiceLifecycle"
  component: "example.invoice.components.InvoiceDomainComponent"
  state_field: "status"
  initial_state: "draft"
  states: ["draft", "issued", "paid", "cancelled"]
  terminal: ["paid", "cancelled"]

  transitions:
    - {from: "draft", to: "issued", on_event: "invoice.submitted"}
    - {from: "issued", to: "paid", on_event: "invoice.paid"}
    - {from: "issued", to: "cancelled", on_event: "invoice.cancelled"}
```

### 7.2 Saga with compensation and human step

```yaml
bundle_id: "com.acme.entity_extraction"
version: "1.0.0"

events:
  - name: "document.ingested"
    schema: "acme.events.DocumentIngested"
  - name: "knowledge.entities_extracted"
    schema: "acme.events.EntitiesExtracted"

specifications:
  - id: "ConfidencePassed"
    expression: "event.data.confidence_score >= 0.80"

business_fsm:
  id: "fsm:KnowledgeLifecycle"
  component: "acme.components.KnowledgeComponent"
  state_field: "lifecycle_state"
  initial_state: "INGESTED"
  states: ["INGESTED", "EXTRACTING", "WAITING_HUMAN_REVIEW", "CONSOLIDATED"]
  terminal: ["CONSOLIDATED"]
  transitions:
    - {from: "INGESTED", to: "EXTRACTING", on_event: "document.ingested"}
    - {from: "EXTRACTING", to: "CONSOLIDATED", on_event: "knowledge.entities_extracted",
     guard: "ConfidencePassed"}
    - {from: "EXTRACTING", to: "WAITING_HUMAN_REVIEW", on_event: "knowledge.entities_extracted",
     guard: "not(ConfidencePassed)"}

workflow_sagas:
  - id: "saga:EntityExtraction"
    trigger_event: "document.ingested"
    saga_timeout_ms: 300000
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
      - name: "HumanReview"
        # tool: null declares a human step (ADR-072 §4.3).
        approval_timeout_ms: 86400000
        pre_condition: "step_failed('EntityExtraction') or step_timed_out('EntityExtraction')"
```

---

## 8. Versioning policy

A bundle's `version` follows semver:

| Change | Bump | Example |
|---|---|---|
| Add optional field to `events[]` schema (in code) | MINOR | `1.0.0 → 1.1.0` |
| Add new `transitions` entry | MINOR | `1.0.0 → 1.1.0` |
| Add new `specifications[]` entry | MINOR | `1.0.0 → 1.1.0` |
| Rename an event in `events[]` | MAJOR | `1.x.y → 2.0.0` |
| Change the meaning of a transition | MAJOR | `1.x.y → 2.0.0` |
| Remove a `specifications[]` entry | MAJOR | `1.x.y → 2.0.0` |
| Fix typo in a predicate string (no semantic change) | PATCH | `1.0.0 → 1.0.1` |

The loader does **not** enforce semver — it accepts any
`MAJOR.MINOR.PATCH` string. The policy is a deployment
convention enforced by CI / release tooling.

A vertical that loads two bundles with the same
`bundle_id` gets the last-loaded one wins. This is
how tenant-specific overrides work (§3.3 of the
bundle-format ADR).

---

## 9. Open questions

These are tracked but not yet resolved:

1. **`agent.*` access.** Should `agent.<field>` be
   allowed to read non-`DomainComponent` fields? Today
   the path resolves to `None` for missing fields,
   which is silent. A future revision may restrict
   access to a declared allowlist per bundle.
2. **`steps.<name>.error` path.** When a step fails,
   `ToolCallCompletion.error` is a string. The mini-
   language has no path component for it (the
   convention is to use `step_failed(name)` builtin).
   Document this so users don't try
   `steps.<name>.error == "..."`.
3. **Cross-bundle predicate references.** A predicate
   defined in bundle A cannot currently be referenced
   from bundle B's transitions. The cross-reference
   would require a global registry (like
   `SpecRegistry` but bundle-scoped). Out of scope
   for v1.
4. **Function calls on `now`.** Method calls
   (`now.weekday()`, `now.hour`) work but use the
   same `call` production as builtins. If `weekday`
   is ever added as a builtin spec, the parser would
   not be able to distinguish. Out of scope for v1;
   operators can split a datetime check into
   `now.year > 2025 and now.month > 6`.

These are tracked in the foundation ADR's open
questions section.
