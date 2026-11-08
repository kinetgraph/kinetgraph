<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Execution plans — the verifiable Definition of Done

> **Why this directory exists.** A checklist that nothing verifies is
> a wish list. A ticked box is not evidence. This directory holds
> **execution plans whose every item carries an executable command**,
> so that "done" means the command passes — not that somebody
> believed it was finished.

Plans live here. The rules live in
[`.agents/skills/kntgraph-harness/SKILL.md`](../../.agents/skills/kntgraph-harness/SKILL.md);
the format is enforced by
[`tests/architecture/test_task_docs.py`](../../tests/architecture/test_task_docs.py).

---

## 1. The format

A plan is a Markdown file named after the work it describes:

```
docs/tasks/
├── README.md          # this guide
├── TEMPLATE.md        # the starting point for every plan
└── <slug>.md          # one file per plan
```

Copy [`TEMPLATE.md`](TEMPLATE.md) to `docs/tasks/<slug>.md` and fill
it in. The header is mandatory:

```markdown
- **BC:** `<bounded context>`
- **Feature doc:** [`docs/<file>.md`](../<file>.md)
- **Status:** `PLANNING`
- **Criado:** YYYY-MM-DD
- **Concluído:** —
```

| Field | Meaning |
|---|---|
| `BC` | The bounded context the work belongs to |
| `Feature doc` | The design document that describes the work |
| `Status` | `PLANNING`, `IN_PROGRESS`, `BLOCKED` or `DONE` |
| `Criado` | The date the plan was opened |
| `Concluído` | The date the plan was closed (`—` while open) |

`Status: DONE` is only legal when **every** Definition-of-Done item
is ticked. A plan cannot declare itself finished with a pending
item.

---

## 2. The Definition of Done

Each item is one **verifiable behaviour**, not one step of work. The
item states what is true when it is done; the command states how a
machine confirms it.

```markdown
## Definition of Done

- [ ] **D1** — The Tool receives `idempotency_key` as keyword-only
  - check: `uv run pytest tests/unit/tools/test_worker.py::test_invoke_requires_idempotency_key`
- [ ] **D2** — The framework gate passes with zero violations
  - check: `uv run python scripts/design_gate.py`
```

Two rules govern an item:

1. **Every item has at least one `check:`.** An item without a check
   is not verifiable, and therefore is not part of a Definition of
   Done. The validator rejects it.
2. **The check points at something that exists.** The file it names
   must exist; when the command names a test node
   (`path::test_name`), that node must exist too. A check that points
   at a renamed test is worse than no check — it looks like evidence.

### Choosing the command

| The item asserts | The command |
|---|---|
| A behaviour of the code | `uv run pytest tests/unit/<path>::<test>` |
| An architectural invariant | `uv run pytest tests/architecture/<check>` |
| A boundary violation | `uv run python scripts/design_gate.py` |
| A type or lint property | `uv run ruff check <path>` / `uv run pyright <path>` |
| The whole gate | `uv run scripts/ci.py` |

Prefer the **narrowest** command that proves the item. A plan whose
items all run the full gate proves nothing specific, and takes
minutes to verify.

---

## 3. The lifecycle

```
PLANNING ──► IN_PROGRESS ──► DONE
                 │
                 └──► BLOCKED ──► IN_PROGRESS
```

`BLOCKED` is for a plan that cannot proceed — it records *why*, in
the **Notas de implementação** section, so that the next reader knows
what was tried.

---

## 4. What the validator does — and does not do

[`tests/architecture/test_task_docs.py`](../../tests/architecture/test_task_docs.py)
asserts that the Definition of Done is **verifiable**:

- the mandatory header fields are present;
- `Status` is one of the known values;
- every item carries a `check:`;
- every `check:` names an existing target;
- no `check:` sits outside an item;
- `Status: DONE` requires every item to be ticked.

It does **not** run the commands. Execution belongs to the CI gate.
The separation is deliberate: structural validation is fast and
deterministic; execution is expensive.

---

## 5. Relationship to the rest of the harness

| Concern | Where it lives |
|---|---|
| The rules | [`.agents/skills/kntgraph-harness/SKILL.md`](../../.agents/skills/kntgraph-harness/SKILL.md) |
| The policies | [`.agents/rules/`](../../.agents/rules/) |
| The checks | [`tests/architecture/`](../../tests/architecture/) |
| The debt query | `uv run python scripts/debt.py <file>` |
| The gate | `uv run scripts/ci.py` |
| The decisions | [`ADRs/`](../../ADRs/) |

> **The golden rule.** A new rule is always born in two places: a
> check **and** the rule in the skill. A rule without a check is prose
> — and prose ages.
