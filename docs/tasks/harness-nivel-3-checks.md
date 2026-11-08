<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# TASK-harness-nivel-3-checks — Close the executable check layer

- **BC:** `platform`
- **Feature doc:** [`ADRs/ADR-089-Architecture-Fitness-Harness-for-Model-Driven-Evolution.md`](../../ADRs/ADR-089-Architecture-Fitness-Harness-for-Model-Driven-Evolution.md)
- **Status:** `PLANNING`
- **Criado:** 2026-11-07
- **Concluído:** —

## Context

The harness now has the Nível 4 layer (plans, a verifiable
Definition of Done, fix-on-touch, a debt query tool) and the
Nível 3 layer in its new shape (one check per rule under
`tests/architecture/`). Two gaps remain.

First, the measured debt that the new detectors found is still
**open**: 73 broken relative links in `docs/`, 5 duplicated ADR
numbers, 8 unjustified broad `except Exception` handlers and 1
ECS bag key that still uses `type[Any]`. Under the conditional
regime this debt is inventoried rather than blocking — which is
correct — but it never gets corrected unless somebody picks it up.

Second, `scripts/design_gate.py` documents two rules it never
implements (`KNT-EXC-001` and `KNT-TYPE-003`). A rule documented
but not enforced is exactly the "prose that ages" the harness
exists to prevent: the docstring promises a guard the gate does
not provide.

## Scope

**Included:**

- The residual debt of the four kinds listed above.
- Reconciling the `design_gate.py` docstring with the rules it
  actually implements — either implementing the two missing rules
  or removing the claim.

**Out of scope:**

- The 80 broken links in `ADRs/` and `CHANGELOG.md`. Those are
  historical records; rewriting them would falsify the record.
  They stay inventoried.
- A pattern library (Nível 2). It is a separate plan.

## Decisions

| # | Decision | Rationale | ADR / Skill |
|---|---|---|---|
| D1 | The ADR duplicates are corrected by **renumbering the younger ADR**, not by deleting it | A deletion loses the decision record; renumbering keeps it and makes the reference unique | [`ADR-089`](../../ADRs/ADR-089-Architecture-Fitness-Harness-for-Model-Driven-Evolution.md) |
| D2 | The `docs/` links are corrected file by file, on the file that owns them | A link is the responsibility of the document that writes it | [`migrate_on_touch`](../../.agents/rules/migrate_on_touch.md) |
| D3 | `KNT-EXC-001` is rewritten to the **reachable** rule, not the naive one | The naive rule reports 25 framework sites, 24 of which are legitimate. A rule that cannot be satisfied is not a rule | [`kntgraph-harness`](../../.agents/skills/kntgraph-harness/SKILL.md) §3 |

## Phases

| # | Phase | Deliverable | Verified by |
|---|---|---|---|
| F1 | The ADR numbering is unique | The five duplicated numbers are renumbered, and every reference updated | `tests/architecture/test_debt_inventory.py` |
| F2 | The `docs/` links resolve | Every relative link in `docs/` resolves | `tests/architecture/test_debt_inventory.py` |
| F3 | The broad `except` debt is closed | Every framework `except Exception` either re-raises a typed error or carries a justification | `tests/architecture/test_debt_inventory.py` |
| F4 | The ECS bag key is narrowed | No mapping key uses `type[Any]` | `tests/architecture/test_debt_inventory.py` |
| F5 | The design gate docstring tells the truth | Every rule the docstring names is implemented, or removed from the docstring | `tests/scripts/test_design_gate.py` |

## Definition of Done

- [ ] **D1** — Every ADR number identifies exactly one document
  - check: `uv run python scripts/debt.py ADRs/ --kind duplicate-adr-number`
- [ ] **D2** — No relative Markdown link in `docs/` is broken
  - check: `uv run python scripts/debt.py docs/ --kind broken-doc-link`
- [ ] **D3** — No broad `except Exception` remains unjustified in `src/kntgraph`
  - check: `uv run python scripts/debt.py src/kntgraph --kind broad-except`
- [ ] **D4** — No ECS bag key uses `type[Any]`
  - check: `uv run python scripts/debt.py src/kntgraph --kind type-any-key`
- [ ] **D5** — The design gate's docstring names only rules the gate implements
  - check: `uv run pytest tests/scripts/test_design_gate.py`
- [ ] **D6** — The architecture checks run as a step of the gate
  - check: `uv run scripts/ci.py --only architecture`

## Implementation notes

The debt kinds and their detectors live in
`tests/architecture/_debt.py`. The known debt is recorded in
`.debt-baseline.json`; after each phase, regenerate it so that the
baseline reflects the progress:

```bash
uv run python scripts/debt.py --update-baseline
```

Regenerating the baseline is only legitimate **after** the
corresponding debt is actually corrected. A baseline that absorbs a
new violation turns the regression check into a rubber stamp.
