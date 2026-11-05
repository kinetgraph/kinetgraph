---
name: kntgraph-testing
description: Use when writing or reviewing tests in the kntgraph codebase — choosing between behaviour tests and mock-heavy unit tests, using WorldBuilder and AgentViewBuilder as canonical SUT builders, marking async def tests, using KNT_REDIS_FAKE for fakeredis, verifying Concordos and Adapters with contract verifiers (ADR-089), and meeting the happy-path-plus-one-failure-mode bar for every public function. Covers the real-EventLog / real-World / real-WorkerManager rule, WorldBuilder / AgentViewBuilder, the fakeredis env var, the pytest.mark.asyncio strict-mode requirement, the per-public-function coverage gate, and the FSM/Saga/Adapter contract test harness. Trigger keywords: pytest, unit test, integration test, fakeredis, KNT_REDIS_FAKE, asyncio_mode, mock, behaviour test, happy path, failure mode, world_builder, WorldBuilder, AgentViewBuilder, SUT, concordo_harness, FSMContractVerifier, SagaContractVerifier, AdapterContractVerifier, ADR-089.
---

<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->


# Testing

## 7.1 Behaviour tests, not mock-heavy unit tests

Use the real `EventLog`, the real `World`, the real `WorkerManager`. Mock **only** when the external system is unavailable in CI (e.g. GLiNER2 with a GPU, real Ollama with a local LLM, real FalkorDB with a graph).

The `KNT_REDIS_FAKE=1` env var switches the `EventLog` to an in-process `fakeredis` client so unit tests do not need a Redis container.

### 7.1.1 Fluent SUT builders (`WorldBuilder` and `AgentViewBuilder`)

To test `WorldSystem` instances (pure functions `(World) -> list[Event]`), do **not** construct mock worlds or fake dispatchers. Use the canonical SUT builders in `kntgraph.testing.world_builder`:

- **`AgentViewBuilder(agent_id)`**: Assembles an `AgentView` with components, domain state, and trigger surface (`with_trigger("<event_type>")` sets `domain_phase` and `last_event_id` in sync).
- **`WorldBuilder()`**: Assembles a `World` with one or more agents while keeping internal `ArchetypeStorage` in sync with the views.

Systems run against the real `World` without shims or monkey-patches, honoring §7.4.

## 7.2 Cover the happy path + at least one failure mode per public function

This is the standard gate for new code (per `CONTRIBUTING.md`); the test files document the expected shape.

## 7.3 `pytest.mark.asyncio` only on `async def` tests

The project's `pyproject.toml` sets `asyncio_mode = "strict"`, which:

- requires an explicit mark on every `async def test_*`
- rejects stray marks on sync `def test_*`

The enforcement is the `tests` step in
`scripts/ci.py`, which invokes pytest with
`-W error::pytest.PytestWarning`. A stray mark
turns into a warning; with `-W error`, that
warning fails the run. There is **no separate
step** for this check — the gate is the `tests`
step's warning policy, not a 13th entry in the
ci-gate skill table.

## 7.4 Shims that simulate production behaviour are a smell

A test file that uses **monkey-patches on core classes**
(e.g. overriding `ReactiveDispatcher._fold_with_filter`)
via a `@pytest.fixture(autouse=True)` creates two problems:

1. **Order coupling.** Any other test file that uses
   the patched method fails when collected **before**
   the patching fixture runs. The symptom is intermittent
   CI failure depending on alphabetical order, parallel
   collection, or pytest plugin order.
2. **Fidelity gap.** If the shim simulates behaviour
   that production does not have, the tests pass against
   a simulated dispatcher that does not match production.
   The systems they exercise are then untested in
   production.

**The bar:** `pytest tests/path/to/test_X.py` must
pass when run **alone**, without any other test file
being collected first. Verify locally before merging:

```bash
KNT_REDIS_FAKE=1 uv run pytest tests/path/to/test_X.py
```

**The deeper rule:** if a test needs a shim to make
a system observable, the system is broken in production
— not in the test. Fix the system; the shim goes away
with the fix.

### Anti-pattern (the bug that motivated this rule)

In `tests/agents/unit/roles/test_role_systems.py`, an
`autouse=True` fixture installed a `_fold_with_filter`
shim on `ReactiveDispatcher` that simulated memory
hydration (`project_memory`). The shim made the 15
role-system tests pass. The shim was also what made
`examples/05b` and `examples/05c` work standalone.

Investigation on 2026-08-26 found that the production
dispatcher **did not call `project_memory`** — the
shim simulated behaviour the framework did not have.
`ChatRoleSystem`, `PlannerRoleSystem`, and the rest did
not function in production (no `SessionComponent` ever
reached the `AgentView`). The 15 tests passed against
a simulated dispatcher that masked the production bug.

### Resolution (2026-08-26)

The shim was removed in
`tests/agents/unit/roles/test_role_systems.py` (15 tests
deleted) and replaced with the **real production code**.
`src/kntgraph/runner/_folding.py::fold_with_filter`
now composes `project_memory` between the default fold
and the tool overlay (ADR-042 §6.1, ADR-059 §2.2).
Three new integration tests in
`tests/unit/runner/test_runner_split_modules.py`
cover the composition. `examples/05b` and `05c` keep
their internal shims for now (redundant but harmless);
a follow-up should remove them now that production
behaves correctly. Reintroducing role-system tests
that previously relied on the shim is now safe: they
exercise real production behaviour.

### Rule for new monkey-patches on core classes

If you find yourself patching production code to make
a test pass, **stop**. The patch is hiding a
production bug. Either:

- Fix the production code (preferred — the framework
  already has the right hooks; wire them up).
- Move the patched logic into a non-test module and
  call it from production (acceptable when the patched
  behaviour is genuinely optional).
- Inline the test in a way that does not require
  the patch (e.g. populate the view directly with
  `world.views["agent-1"] = AgentView(agent_id=..., components={SessionComponent: ...})`).

Do not rely on `autouse=True` to make a patch visible
across files. Pytest does not guarantee the order of
fixture setup across files, and the patch is hiding
the very behaviour the test should be exercising.

## 7.5 High-level Concordo and Adapter contract test harness (ADR-089)

When writing or testing FSMs, WorkflowSagas, or Adapters, do **not** invent ad-hoc mocks or handwritten state simulators. Use the contract verifiers provided in `kntgraph.testing`:

- **`FSMContractVerifier` (ADR-071):**
  - `verify_graph(config, initial_state)`: Asserts reachability of all declared states via BFS, detects unhandled dead-ends outside `terminal`, and validates that all guards implement the `Specification` protocol (ADR-069).
  - `verify_purity(system, world)`: Proves deterministic, side-effect-free execution of the real `WorldSystem` SUT against a `World` assembled with `WorldBuilder`.
- **`SagaContractVerifier` (ADR-072):**
  - `verify_step_configs(config)`: Asserts step name uniqueness and validates that `enrich_from` dependencies only reference prior steps or triggers.
  - `simulate_lifo_compensation(config, failed_step_name)`: Proves that for any failing step, rollback compensation runs in exact reverse (LIFO) order.
- **`AdapterContractVerifier` (ADR-047, ADR-077):**
  - `assert_exception_contained(coro_factory, expected_error_type)`: Injects socket/transport failures and verifies that the adapter intercepts all third-party exceptions and returns `Err(LayerError)` (zero raw exceptions leaking).
  - `assert_cancellation_propagates(coro_factory)`: Injects `asyncio.CancelledError` and verifies that runtime control signals are not swallowed.
