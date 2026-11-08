<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

---
name: fix-on-touch
description: Policy for correcting technical debt in the files you touch — and the limits that keep it from becoming scope creep. Use when editing a file that contains an architectural violation, an outdated test, or a divergent naming. Trigger keywords: fix-on-touch, debt in this file, correct the debt, scope creep, while I am here.
---

# Fix-on-Touch — correct the debt in the file you are touching

You are reading this rule because you are about to edit a file that
contains **technical debt** — an architectural violation, an outdated
test, a divergent naming.

## The policy

> **When you touch a file, correct the technical debt *in that
> file*.**

This is not a campaign. It is not "fix everything". It is: **while
you are in that file, leave it in compliance.**

### Why this policy

- **Zero marginal cost.** The file is already open, the context is
  already loaded, the test already runs. Fixing it now is cheaper
  than fixing it later.
- **Sustainable.** A campaign of 231 checkboxes never ends. "While
  you are in the file" **always ends**.
- **Low risk.** The change is small, local and reviewable.

### What gets fixed

| Fix | Do not fix |
|---|---|
| An architectural rule violation **in the touched file** | Debt in **another** file |
| A test that mocks what it should exercise **in the touched file** | Pre-existing debt in an unrelated module |
| Divergent naming **in the touched file** | Opportunistic refactoring |
| A dataclass without `slots=True` **in the touched file** | A change to a public contract |
| A docstring that describes removed code | Documentation of **another** area |
| An import that points at dead code **in the touched file** | Deleting a whole feature |

### The limits

**1. The scope is the file, not the module.**
You touch `foo/bar.py`. The debt in `foo/baz.py` is **out of
scope** — unless `bar.py` depends on it in a way the correction
requires.

**2. No contract change.**
Correcting debt **may** change an internal signature, but **not**:

- a public API (`kntgraph.__all__`);
- an event type or its payload shape;
- a tool name;
- a persisted idempotency key.

If the correction requires one of those, it is **not**
fix-on-touch. Open a plan in `docs/tasks/` and treat it as a change
with its own Definition of Done.

**3. No opportunistic refactoring.**
Rewriting an `if` that "could be more elegant" is not debt
correction. The rule is: **the file starts obeying the declared
rules**. Not "the file looks nicer".

**4. No complexity increase.**
If the correction needs more than roughly 30 changed lines **in the
same file**, it is probably a task, not a fix-on-touch. Stop and
re-assess.

**5. The test comes along.**
Debt corrected without a test that proves the correction **is not
corrected**. If the debt was "the test does not cover X", the
correction **includes** the test.

## How to execute

```bash
# 1. You touched the file (it is already in your context).
# 2. Ask what debt it carries:
uv run python scripts/debt.py src/kntgraph/<touched>.py
# 3. Correct the debt and adjust or add the test.
# 4. Re-run the gate:
uv run scripts/ci.py
# 5. If the correction requires a contract change -> stop, open a plan
#    in docs/tasks/.
```

## Recording

If you corrected debt **outside** the strict scope (you had to touch a
second file), record it in the plan's *Implementation notes* — so the
reviewer knows what went beyond the file.

> **Version-control control.** Do not run `git commit` or `git push`
> without the human operator asking for it explicitly. See
> [`kntgraph-branch-policy`](../../.agents/skills/kntgraph-branch-policy/SKILL.md).

## Cross-references

- The rules these debts violate: [`.agents/skills/`](../../.agents/skills/)
- The checks that detect them: [`tests/architecture/`](../../tests/architecture/)
- Plans with a verifiable Definition of Done: [`docs/tasks/README.md`](../../docs/tasks/README.md)
- The debt query tool: `uv run python scripts/debt.py <file>`
