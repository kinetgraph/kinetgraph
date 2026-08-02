<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-053: CLI Boilerplate Generation v2 (in-template rendering, `knt upgrade`)

**Status:** Accepted
**Date:** 2026-08-02
**Version:** 0.11.0
**Authors:** kntgraph CLI team
**Related to:** [ADR-038](./ADR-038-CLI-Boilerplate-Generator.md) (initial CLI), [ADR-039](./ADR-039-Role-rethinking-and-intentions-routing.md) (removal of `IntentResolutionSystem`), [ADR-047](./ADR-047-Tool-Adapter-Pattern.md) (`Result[dict, ToolError]` return type), [ADR-052](./ADR-052-PyPI-Publishing.md) (the runbook reference links from generated `consumer.py`)

> **Reactive maintenance, not single-shot.** The CLI's
> `knt new <artifact>` commands render Jinja templates into
> the user's project. The templates inevitably drift (the
> framework evolves; the citations of removed symbols
> `CapabilityPolicy` / `IntentResolutionSystem` /
> `Result[dict, Exception]` lingered across multiple
> framework releases). The pre-v0.11.0 CLI had no
> `upgrade` workflow: a user who upgraded the framework had
> to either re-run `knt init` (losing local edits) or
> hand-merge the diff. This ADR introduces the first-class
> regeneration path.

## 1. Context

### 1.1 The drift problem

The CLI's J2 templates (`src/kntgraph/cli/templates/*.jinja`)
were authored during the v0.7.0 / v0.8.0 era. The framework
subsequently removed several symbols the templates cited:

- **`CapabilityPolicy`** was never implemented in the
  framework. The `agent.py.jinja` template imported
  `from kntgraph.security.authorization import CapabilityPolicy`,
  which broke on first import. Operators worked around the
  gap by defining a local stub (the convention in the
  `soldi/backoffice` audit).
- **`IntentResolutionSystem`** was removed in v0.9.0
  (ADR-039). The `main.py.jinja` template still imported
  it on the routing-only path.
- **`Result[dict, Exception]`** was tightened to
  `Result[dict, ToolError]` in v0.9.0 (ADR-047). The
  `tool.py.jinja` template still emitted the pre-ADR-047
  return type.
- **`correlation_middleware`** was moved from
  `kntgraph.core.correlation` to
  `kntgraph.core.event.correlation`. The `event.py.jinja`
  template still imported the historical path.

### 1.2 The over-coupling problem

The `main.py.jinja` template initialised the
`ReactiveDispatcher` without threading the `redis` and
`tool_router` arguments through it. The dispatcher
therefore re-instantiated the Redis pool internally -- a
**silent waste** (per-pool connection cost) and a
**deviation from the framework's canonical contract**
(matching the `dispatcher.py.jinja` template was missing
the same two arguments). The drift was item-level
(per-file) but the impact was system-wide: every operator
who pasted the dispatcher factory had to thread the two
arguments manually.

### 1.3 The regeneration problem

A user who upgraded `kntgraph` from v0.10.0 to v0.11.0
had to choose between:

1. **Re-run `knt init project`**: lost every local edit
   (the `nuance` in the generated `consumer.py`, the
   `Settings` fields added by the operator, the custom
   adapter in `routing/adapters/`).
2. **Hand-merge the diff**: error-prone, no audit trail,
   drift accumulates across releases.

Neither was acceptable for a production deploy.

## 2. Decision

### 2.1 Fix the templates in place

The first sub-decision is **regenerate the templates
against the current framework**. The changes are
**mechanical** (path / type / import fixes), not
**architectural** (the CLI's structure is unchanged).

Specifically:

- **`agent.py.jinja`**: drop the `CapabilityPolicy` import
  + class entirely. Replace the `build_<agent>_policy()`
  function with `get_<agent>_allowed_events()` returning
  a **documentation-only** event allow-list. The
  historical reference is preserved in a comment that
  documents the ADR-039 removal.
- **`event.py.jinja`**: import
  `correlation_middleware` from
  `kntgraph.core.event.correlation` (the canonical path
  post-rename). The `causation_id` parameter is
  `UUID | None` (the `Event.domain_from` constructor
  expects a UUID, not a string).
- **`tool.py.jinja`**: return type is
  `Result[dict[str, Any], ToolError]` (the post-ADR-047
  contract). The docstring documents the canonical
  error-wrapping pattern (`Err(ToolError(...))`).
- **`main.py.jinja`** (HTTP path): thread
  `redis=redis, tool_router=tool_router` through the
  `build_<context>_dispatcher()` call (the placeholder is
  commented-out as before; the **commented** call MUST
  pass the two arguments so the operator's uncomment
  yields a working call). Record the `routing_mode` choice
  in a code comment.
- **`dispatcher.py.jinja`**: thread
  `redis=redis, tool_router=tool_router` through the
  `ReactiveDispatcher` constructor. Add an optional
  `with_supervisor` flag that emits a
  `build_<context>_supervisor_runner()` and a
  `build_<context>_dispatcher_with_supervisor()` helper
  (the canonical two-layer wiring from the
  `soldi/backoffice` audit).
- **`event.py.jinja`** + **`tool.py.jinja`** + others:
  replace the Jinja `{# #}` SPDX comment with Python
  `#` comments. The Jinja comment was **stripped** at
  render time, so the rendered file had no SPDX header
  and the REUSE 3.3 lint gate failed. The new format
  preserves the header through the render.

### 2.2 Add `consumer.py.jinja` and `config.py.jinja`

The pre-v0.11.0 CLI had no template for **either** the
Redis Streams intent consumer or the `Settings` module.
Operators wrote both from scratch in every project. The
new templates encode the canonical
**3-responsibility consumer** pattern (Long-poll loop /
Pydantic-validated payload / Context assembler) and the
**framework's `Settings` subclass** (the `KNT_` env
prefix is inherited from the framework's base, so the
project's `Settings` only declares the project-specific
fields).

### 2.3 Add `knt upgrade`

The **second** sub-decision is the **regeneration
workflow**. The new `knt upgrade` sub-command has three
operating modes:

| Mode | Behaviour | Exit code |
|---|---|---|
| `knt upgrade --check` | Report drift. No writes. | 0 = no drift, 1 = drift found |
| `knt upgrade --apply <path>` | Regenerate one file. Refuse if drift AND local edits. | 0 / 1 |
| `knt upgrade --apply-all [--force]` | Regenerate every boilerplate file. Skip files with local edits unless `--force`. | 0 / 1 |

The drift detection is **content-based** (the renderer
re-runs the template against the discovered context and
`difflib.unified_diff`s the result against the on-disk
file). The **context recovery** is **heuristic**: the
`main.py.jinja` template's `use_intent_http` flag is
inferred from the presence of `from kntgraph.api import
create_app` in the existing `main.py`; the `routing_mode`
is parsed from the `routing.adapters.<mode> import` line.

The mapping of (template name, rendered path) is a
single source of truth in
`src/kntgraph/cli/commands/upgrade.py::_MAPPING`. Adding
a new template is a one-line entry.

### 2.4 Out of scope (explicit)

- **No merge mode for diffs.** The `upgrade --apply` mode
  is **all-or-nothing**: the operator either accepts the
  full template (overwriting local edits) or skips the
  file. A 3-way merge tool (auto-detect + safe merge of
  **comment** vs. **code** edits) is a follow-up ADR.
- **No template upgrade notifications.** The CLI does not
  warn when a new template is added that the local
  project does not have (e.g. a project init'd in v0.10.0
  does not have a `consumer.py`). The package release
  notes call out the new templates; the operator runs
  `knt upgrade check` to discover them.
- **No version pinning.** The templates are versioned
  alongside the framework; there is no separate template
  package. The release cadence is the same as the
  framework.

## 3. Consequences

### 3.1 Positive

- **The CLI no longer ships broken boilerplate.** The
  23-test `tests/unit/cli/test_templates.py` suite
  catches every historical regression at the
  template-render level. The forbidden-symbols
  parametrize blocks are the contract: a future template
  that imports `CapabilityPolicy` (or any other
  removed symbol) fails the test.
- **Operators can regenerate against the current
  framework.** The `knt upgrade --check` mode is the
  CI gate (it can be added to `scripts/ci.py` as a
  separate check; out of scope for this ADR).
- **The Settings / consumer / dispatcher patterns are
  canonical.** The pre-ADR-053 templates taught operators
  the wrong patterns (constructors without `redis`/
  `tool_router`, hard-coded `"default-tenant"` fallback,
  mixed-lifecycle consumer). The new templates teach the
  canonical patterns.
- **The CLI render is now lint-gated.** The
  `# SPDX-FileCopyrightText` + `# SPDX-License-Identifier`
  comments survive the render; the REUSE 3.3 gate
  passes on every generated file.

### 3.2 Negative

- **`knt upgrade` overwrites without three-way merge.**
  Operators with local edits to a template-generated file
  must either (a) commit those edits elsewhere and
  re-apply them after the upgrade, or (b) update the
  generated file by hand. The CI cannot do this for them.
  The mitigation is: the CLI's `apply-all` mode **skips**
  files with drift + local edits, so the operator can
  upgrade the non-modified files and treat the modified
  ones manually.
- **The context recovery is heuristic.** The
  `use_intent_http` flag is inferred from a single import
  line; the `routing_mode` is parsed from a snippet. If
  the operator manually rewrites the imports (e.g. adds
  the `create_app` import in the routing-only path), the
  inference breaks. The mitigation is: the
  `knt upgrade list-templates` command prints the
  resolved context for debugging.
- **No template version pinning.** The templates are
  bundled with the framework; a project that wants to
  stay on v0.11.0 templates while the framework moves to
  v0.12.0 has to pin the framework version (the standard
  Python package pin). The templates are not separately
  versioned.

### 3.3 Neutral

- **The CLI grows by one sub-command.** The Typer
  surface grows by `knt upgrade [list-templates|check|apply|apply-all]`.
  Each sub-command has a focused contract (the
  `check`/`apply`/`apply-all` split mirrors the
  `git fetch`/`git checkout`/`git pull` line of reasoning).
- **The tests grow by 23 entries.** The new
  `test_templates.py` suite covers every template render
  path. The forbidden-symbols parametrize blocks are the
  **executable specification** of "what the framework
  exports"; if a future removal is missed, the test
  fails with a clear message ("Template X renders a
  forbidden symbol Y").
- **The `main.py.jinja` template now has a `use_intent_http`
  branch and a `routing_mode` branch.** The branch logic
  is the same as the pre-v0.11.0 templates; the diff is
  the threading of `redis` and `tool_router`.

## 4. Migration plan

The migration is **1 PR**. Each step is independently
mergeable.

### PR 1 — CLI Boilerplate v2 + `knt upgrade` (~1 day)

1. **Replace the templates** with the new versions.
   Specifically:
   - `agent.py.jinja`: drop `CapabilityPolicy`, add
     `get_<agent>_allowed_events`.
   - `event.py.jinja`: fix `correlation_middleware`
     import path; `causation_id: UUID | None`.
   - `tool.py.jinja`: `Result[dict, ToolError]`.
   - `main.py.jinja`: thread `redis`/`tool_router`
     through the dispatcher factory placeholder.
   - `dispatcher.py.jinja`: thread `redis`/`tool_router`
     through the `ReactiveDispatcher` constructor; add
     `with_supervisor` flag.
   - All templates: replace `{# #}` SPDX comment with
     `#`-style Python comments (preserves the header
     through the render).
2. **Add the new templates**:
   - `consumer.py.jinja` (3-responsibility pattern).
   - `config.py.jinja` (Pydantic Settings subclass).
3. **Update `init.py`**: when `--use-intent-http` is set,
   the `init` command also renders `consumer.py` and
   `core/config.py` (the settings needed by the consumer).
4. **Add `upgrade.py`** with the `check`/`apply`/
   `apply-all` modes and the `_MAPPING` registry.
5. **Register `upgrade` in `main.py`** (a one-line
   `app.add_typer(upgrade.app, name="upgrade")`).
6. **Add `tests/unit/cli/test_templates.py`** with the
   23-test render-with-real-context suite.
7. **Update the existing tests** (`test_init.py`,
   `test_new_agent.py`) to assert the new contracts
   (the `agent` template no longer emits
   `CapabilityPolicy`; the `agent` file has
   `get_<name>_allowed_events` instead of
   `build_<name>_policy`).

### Acceptance

- `uv run scripts/ci.py` is green (all 9 gates).
- `uv run pytest tests/unit/cli/ -v` is green (41 tests).
- The forbidden-symbols parametrize blocks fail loudly
  if a future template regresses.
- `knt upgrade --check` reports no drift on a fresh
  `knt init project my_app --use-intent-http` scaffold.
- `knt upgrade --apply-all --force` regenerates the
  scaffold without manual edits.

## 5. References

- [ADR-038 — CLI Boilerplate Generator](./ADR-038-CLI-Boilerplate-Generator.md)
  (the original `knt` CLI, ADR-050 refs).
- [ADR-039 — Role Rethinking and Intentions Routing](./ADR-039-Role-rethinking-and-intentions-routing.md)
  (the removal of `IntentResolutionSystem` and
  the per-role `WorldSystem` model).
- [ADR-047 — Tool-Adapter Pattern](./ADR-047-Tool-Adapter-Pattern.md)
  (the `Result[dict, ToolError]` return type).
- [ADR-050 — CLI Command Consistency](./ADR-050-CLI-Command-Consistency.md)
  (the sub-Typer convention `knt init project <name>`).
- [ADR-052 — PyPI Publishing via Trusted Publishing](./ADR-052-PyPI-Publishing.md)
  (the runbook that links from the generated
  `consumer.py` docstring).
- [Jinja2 docs — `{# #}` (comment) vs `#` (output)](https://jinja.palletsprojects.com/en/stable/templates/#comments)
  (the rendering contract that motivates the SPDX
  header fix).
