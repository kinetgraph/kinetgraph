<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

---
name: migrate-on-touch
description: Strategy for aligning documentation and legacy code with the current implementation — incrementally, at the moment the area is touched. Use when a document describes code that no longer exists, or when you edit code whose documentation has drifted. Trigger keywords: migrate-on-touch, doc drift, outdated documentation, stale doc, feature doc, documentation drift.
---

# Migrate-on-Touch — align the documentation when the area is touched

There is debt in two places: in the **documentation** and in the
**code**. The strategy for both is the same: **do not migrate in
bulk; migrate when it is necessary**.

## Why not migrate in bulk

A migration campaign ("I will convert all 30 documents") has a high
up-front cost, risks a giant diff, and competes with real work.
Worse: the code changes. A document migrated in October is stale by
November.

Migrating at the moment of the touch has **zero marginal cost** — the
context is already loaded, the code is already open.

## Documentation

### What "migrating" a document means

It is not rewriting it. It is **aligning it with the code**:

| Item | Action |
|---|---|
| A cited symbol that does not exist in the code | Correct the name to the one that exists (or remove it, if the feature was reverted) |
| A checklist item that has become a verifiable check | Give it a `check:` command, or open a plan in `docs/tasks/` |
| A stale checklist | Update the criteria to reflect the current behaviour |
| A feature that is **not implemented** (every cited symbol absent) | **Leave it alone** — a plan cites the future; that is not drift |

### When to migrate

When you edit code that a document describes:

```bash
# 1. Identify the document that describes the code you touched.
# 2. Ask what debt the touched file carries:
uv run python scripts/debt.py src/kntgraph/<touched>.py
# 3. If the document cites symbols that no longer match the code:
#    - the document is wrong  -> fix the document (the code is the truth)
#    - the code is wrong      -> the work is incomplete; that is a bug,
#                                not a documentation migration
# 4. Do not touch documents for code you did NOT touch.
```

### The limit

**One document at a time, and only the one you touched.** Not "I will
fix the documentation of features 008, 009 and 010 while I am here" —
those are planned, not implemented.

## Legacy code

The same policy as [`fix_on_touch.md`](fix_on_touch.md): when you
touch the file, correct its debt.

```bash
# 1. uv run python scripts/debt.py <file>
# 2. Correct the debt you found and adjust the test.
# 3. Do not propagate into another file without need.
```

## What is blocking (does not wait for the touch)

These **fail the CI** — they do not wait for the moment of the touch:

| Blocking | Why |
|---|---|
| A framework module importing a vertical | Contaminates every layer above (ADR-042) |
| A third-party SDK leaking outside an adapter | Breaks the adapter contract (ADR-047) |
| A broad `except Exception` with no justification | Hides the failure the typed error was meant to expose (ADR-077) |
| A broken link in a skill or a normative document | The agent receives a 404 mid-reasoning |
| A plan whose Definition of Done is not verifiable | "Done" becomes an opinion |
| A check that the harness skill does not register | A rule nobody knows about |

> **The distinction:** structural debt (**blocking**, always fails)
> versus local debt (corrected when touched). The first breaks the
> system; the second only inconveniences whoever touches the file.

## Cross-references

- The code policy: [`fix_on_touch.md`](fix_on_touch.md)
- The debt query tool: `uv run python scripts/debt.py <file>`
- The blocking checks: [`tests/architecture/`](../../tests/architecture/)
- The verifiable Definition of Done: [`docs/tasks/README.md`](../../docs/tasks/README.md)
