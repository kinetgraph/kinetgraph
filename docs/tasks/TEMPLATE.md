<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# TASK-\<slug\> — \<Title\>

<!--
  Template for an execution plan with a verifiable Definition of Done.
  Copy this file to `docs/tasks/<slug>.md` and fill it in.

  The format is enforced by `tests/architecture/test_task_docs.py`.
  See `docs/tasks/README.md` for the complete guide.
-->

- **BC:** `<bounded context>`
- **Feature doc:** [`docs/<file>.md`](../<file>.md)
- **Status:** `PLANNING`
- **Criado:** YYYY-MM-DD
- **Concluído:** —

## Context

<2-4 sentences: what is wrong or missing today, and why it matters.>

## Scope

**Included:**

- <item>

**Out of scope:**

- <item — and where it belongs, if elsewhere>

## Decisions

| # | Decision | Rationale | ADR / Skill |
|---|---|---|---|
| D1 | <what was decided> | <why> | [`adr-0XX`](../../ADRs/ADR-0XX-<Title>.md) |

> A **new** architectural decision needs an ADR. A decision that is
> already recorded only needs a reference.

## Phases

| # | Phase | Deliverable | Verified by |
|---|---|---|---|
| F1 | <description> | <file or behaviour> | `tests/...::test_x` |

## Definition of Done

- [ ] **D1** — <verifiable behaviour>
  - check: `uv run pytest tests/unit/<path>/test_<file>.py::test_<name>`
- [ ] **D2** — <verifiable behaviour>
  - check: `uv run pytest tests/architecture/test_<check>.py`
- [ ] **D3** — <verifiable behaviour>
  - check: `uv run python scripts/ci.py`

## Implementation notes

<Details that are not part of the Definition of Done — decisions taken
during execution, dead ends, follow-ups.>
