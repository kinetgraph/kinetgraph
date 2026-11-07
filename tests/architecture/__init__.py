# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""Architecture checks — executable invariants for the repository.

Each module in this package asserts **one** architectural invariant.
The mechanics (parsing, collecting) live in ``_collectors.py``; the
tests contain only the rule.

Regimes:

- **Fail-fast** — a structural violation that propagates to every
  consumer. It breaks CI on sight.
- **Conditional** — local debt. It is inventoried and only corrected
  when the file is touched (``fix-on-touch``).

See ``.agents/skills/kntgraph-harness/SKILL.md`` for the rules and
``docs/tasks/README.md`` for the Definition-of-Done contract.
"""
