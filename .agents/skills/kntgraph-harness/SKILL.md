---
name: kntgraph-harness
description: Use when adding or changing an architectural rule, writing an execution plan, or deciding whether a violation should break the CI. Covers the four levels of the architectural harness, the check inventory, the verifiable Definition of Done, and the fix-on-touch / migrate-on-touch policies. Trigger keywords: harness, architectural check, architecture check, rule, invariant, enforcement, fail-fast, conditional, definition of done, task.md, task plan, execution plan, fix-on-touch, migrate-on-touch, debt, debt inventory, baseline, do I need a check, the CI is red because of a rule.
---

<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# The architectural harness

> **The thesis.** A rule written in prose ages. A rule written as a
> check survives. Both together survive the rotation of context.

This skill is the normative source of the harness rules. The checks
in [`tests/architecture/`](../../../tests/architecture/) and
[`scripts/design_gate.py`](../../../scripts/design_gate.py) are the
enforcement; this file is the intent. A rule that lives only here is
prose, and prose ages.

---

## 1. The four levels

The harness is a pyramid. Each level is useful on its own.

| Level | Delivers | Where it lives here |
|---|---|---|
| **1 — Convention** | A shared vocabulary, ADRs, skills | [`ADRs/`](../../../ADRs/), [`.agents/skills/`](../../../.agents/skills/) |
| **2 — Coherence** | A pattern library, referential integrity | [`tests/architecture/test_docs_consistency.py`](../../../tests/architecture/test_docs_consistency.py) |
| **3 — Enforcement** | Executable checks in the CI | [`tests/architecture/`](../../../tests/architecture/), [`scripts/design_gate.py`](../../../scripts/design_gate.py) |
| **4 — Evolution** | Plans, a verifiable Definition of Done | [`docs/tasks/`](../../../docs/tasks/), [`.agents/rules/`](../../../.agents/rules/) |

**The order is mandatory.** Do not start at level 3. A check written
before the convention (1) and the coherence (2) exist validates
nothing — the rule has to exist before it can be executable.

---

## 2. The two regimes

Every rule belongs to exactly one regime. Choosing wrongly is the
classic error.

| Regime | Behaviour | Use it for |
|---|---|---|
| **fail-fast** | A violation **always** breaks the CI | A structural rule that breaks the system |
| **conditional** | A violation breaks the CI only when the file is touched | Local debt, migrated incrementally |

> **The decision rule.** Structural debt — debt that *contaminates
> other code* — is fail-fast. Local debt — debt that only bothers
> whoever touches the file — is conditional.

A check that fails on the mere **existence** of debt trains the team
to ignore the CI. And a CI that is ignored is worse than no CI,
because it gives a false sense of compliance.

---

## 3. The golden rule

> **A new rule is always born in two places: a check and the rule in
> this skill.** A rule without a check is prose.

The parity check
([`test_harness_parity.py`](../../../tests/architecture/test_harness_parity.py))
enforces this in both directions: every check on disk must be
registered in the *Check inventory* below, and every entry in the
inventory must exist on disk.

### Anatomy of a check

Three elements are mandatory in every failure message:

1. **Which rule was violated** — the developer who fails the CI must
   know *where* the norm is, or they will invent an exception.
2. **Where** — path and line.
3. **What to do** — the remedy.

### Checks must not be vacuous

> **A check that has never failed has not been tested.** Test it
> against a deliberately introduced violation.

```bash
# Introduce the violation, confirm the check fails, revert.
cp <file> /tmp/backup.py
# ... introduce the violation ...
uv run pytest tests/architecture/ -q   # MUST fail
cp /tmp/backup.py <file>
```

### Calibrate before you enforce

Measure the current debt **first**, and classify each hit as a real
violation or a false positive. Adjust the detector until the false
positives reach zero. If what remains is only a categorisation
problem, **rewrite the rule** — do not tighten the detector.

Worked example from this repository: a naive detector for
`except Exception` reports 25 sites in framework code. Classifying
each one showed that 13 carry an explicit justification, 7 are the
ADR-077 typed-translation boundary, and 8 are deliberate last-resort
handlers in worker loops. The naive rule was therefore
**unreachable**, and the rule was rewritten to measure what is
reachable.

---

## 4. Check inventory

Every enforcement file of the harness, the rule it asserts, and its
regime.

| Check | Rule | Regime |
|---|---|---|
| `scripts/design_gate.py` | The framework never imports a vertical (ADR-042); a third-party SDK never leaks outside an adapter (ADR-047); `TypedDict` is banned in framework code (ADR-079); a wire payload is not `dict[str, Any]` (ADR-067). | fail-fast |
| `tests/architecture/test_task_docs.py` | Every Definition-of-Done item carries an executable `check:`; every check names a target that exists; the `Feature doc` link resolves; `Status: DONE` requires every item to be ticked. | fail-fast |
| `tests/architecture/test_docs_consistency.py` | Every relative link on the normative surface resolves; every ADR file name is well-formed; `AGENTS.md` and the skills directory agree. | fail-fast |
| `tests/architecture/test_debt_inventory.py` | The inventoried debt does not grow: no file introduces debt the baseline does not record. | conditional |
| `tests/architecture/test_harness_parity.py` | Every check is registered in this skill, and every registered check exists. | fail-fast |

The rules the *debt inventory* covers are listed in
[`.agents/rules/fix_on_touch.md`](../../../.agents/rules/fix_on_touch.md).
The known debt is recorded in `.debt-baseline.json`; regenerate it
after an intentional cleanup with:

```bash
uv run python scripts/debt.py --update-baseline
```

---

## 5. What is check-worthy

| Candidate | A check? | Why |
|---|---|---|
| "The framework never imports a vertical" | Yes | Mechanical, verifiable |
| "A tool has an `idempotency_key`" | Yes | A framework contract |
| "The file name is `ADR-NNN-Title.md`" | Yes | Mechanical |
| "The verb is `Submit` / `Approve`" | No | Semantic — false positives guaranteed |
| "The aggregate has the right methods" | No | Requires human judgement |

> **Prefer mechanical and structural checks** (AST, structured
> searches, file presence). When a rule needs judgement, leave it to
> code review — and say so in the review checklist.

Use **AST**, not text search, for anything semantic. A `grep` for
`object` accuses the docstring of a module that says "we do not use
`object`".

---

## 6. The Definition of Done

A plan in [`docs/tasks/`](../../../docs/tasks/) is an execution plan
whose every item carries an executable command. The format, the
lifecycle and the validator are documented in
[`docs/tasks/README.md`](../../../docs/tasks/README.md).

The short version:

```markdown
- [ ] **D1** — The Tool receives `idempotency_key` as keyword-only
  - check: `uv run pytest tests/unit/tools/test_worker.py::test_invoke_requires_idempotency_key`
```

A ticked box is not evidence. The command is the evidence.

---

## 7. The policies

| Policy | What it says |
|---|---|
| [`fix_on_touch.md`](../../../.agents/rules/fix_on_touch.md) | When you touch a file, correct the debt *in that file* — within four limits. |
| [`migrate_on_touch.md`](../../../.agents/rules/migrate_on_touch.md) | Align the documentation when the area is touched; never migrate in bulk. |

Both replace the migration campaign, which never ends.

---

## 8. How to add a rule

1. **Write the rule here** — in this skill, with the ADR it comes
   from.
2. **Write the check** — one file per rule under
   [`tests/architecture/`](../../../tests/architecture/), containing
   only the rule; the mechanics belong in `_collectors.py`.
3. **Calibrate it** — measure the current debt; adjust the detector
   until the false positives reach zero.
4. **Classify the regime** — fail-fast (structural) or conditional
   (local).
5. **Register it** — add a row to the *Check inventory* above.
6. **Test it against a deliberate violation** — confirm it fails.

---

## 9. The gate

The architecture checks run as their own step of the single CI gate,
right after the design gate and before the unit tests:

```bash
uv run scripts/ci.py --only architecture   # the architecture checks alone
uv run scripts/ci.py                       # the whole gate
uv run python scripts/debt.py <file>       # what debt does this file carry?
```

See [`kntgraph-ci-gate`](../kntgraph-ci-gate/SKILL.md) for the full gate.
