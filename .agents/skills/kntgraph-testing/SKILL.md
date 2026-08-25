<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

---
name: kntgraph-testing
description: Use when writing or reviewing tests in the kntgraph codebase — choosing between behaviour tests and mock-heavy unit tests, marking async def tests, using KNT_REDIS_FAKE for fakeredis, and meeting the happy-path-plus-one-failure-mode bar for every public function. Covers the real-EventLog / real-World / real-WorkerManager rule, the fakeredis env var, the pytest.mark.asyncio strict-mode requirement, and the per-public-function coverage gate. Trigger keywords: pytest, unit test, integration test, fakeredis, KNT_REDIS_FAKE, asyncio_mode, mock, behaviour test, happy path, failure mode.
---

# Testing

## 7.1 Behaviour tests, not mock-heavy unit tests

Use the real `EventLog`, the real `World`, the real `WorkerManager`. Mock **only** when the external system is unavailable in CI (e.g. GLiNER2 with a GPU, real Ollama with a local LLM, real FalkorDB with a graph).

The `KNT_REDIS_FAKE=1` env var switches the `EventLog` to an in-process `fakeredis` client so unit tests do not need a Redis container.

## 7.2 Cover the happy path + at least one failure mode per public function

This is the standard gate for new code (per `CONTRIBUTING.md`); the test files document the expected shape.

## 7.3 `pytest.mark.asyncio` only on `async def` tests

The project's `pyproject.toml` sets `asyncio_mode = "strict"`, which:

- requires an explicit mark on every `async def test_*`
- rejects stray marks on sync `def test_*`

The gate is the `pytest -W error::pytest.PytestWarning` step in `scripts/ci.py`.

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

**Anti-pattern (the bug that motivated this rule):**
in `tests/agents/unit/roles/test_role_systems.py`, an
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

**Resolution (2026-08-26).** Delete the tests AND
fix the production code. The 15 tests were deleted;
`src/kntgraph/runner/_folding.py::fold_with_filter`
now composes ``project_memory`` between the default fold
and the tool overlay. ``examples/05b`` and ``05c`` keep
their internal shims for now (redundant but harmless);
a follow-up should remove them now that production
behaves correctly. Reintroducing role-system tests is
now safe (they will exercise real production behaviour).
fixture for a system that does not work; once the
production bug is fixed (the dispatcher must compose
`project_memory` per ADR-042 §6.1), the tests come
back with a real (non-shimmed) dispatcher.

**Rule for new monkey-patches on core classes:** if
you find yourself patching production code to make
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

**Resolution (2026-08-26).** The shim that motivated this
rule was removed in
``tests/agents/unit/roles/test_role_systems.py`` (15 tests
deleted) and replaced with the **real production code**.
``src/kntgraph/runner/_folding.py::fold_with_filter``
now composes ``project_memory`` between the default fold
and the tool overlay (ADR-042 §6.1, ADR-059 §2.2).
Three new integration tests in
``tests/unit/runner/test_runner_split_modules.py``
cover the composition. Reintroducing role-system tests
that previously relied on the shim is now safe: they
exercise real production behaviour.
