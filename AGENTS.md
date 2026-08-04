<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# AGENTS.md — stub

The project conventions previously documented in this
file now live as opencode skills under
`.agents/skills/`. Each skill is a per-topic reference
loaded on demand by opencode when the conversation
matches the skill's `description` keywords.

| Section (old) | Skill                                |
| ------------- | ------------------------------------ |
| §1  Type discipline        | `kntgraph-type-discipline`   |
| §2  No compat shims        | `kntgraph-deprecation-policy` |
| §3  File size and layout   | `kntgraph-file-layout`        |
| §4  Style                  | `kntgraph-style`              |
| §6  Errors are typed       | `kntgraph-typed-errors`       |
| §7  Testing                | `kntgraph-testing`            |
| §9  The single CI gate     | `kntgraph-ci-gate`            |
| §10 Prose language         | `kntgraph-prose-language`     |
| §11 Branch policy          | `kntgraph-branch-policy`      |
| §13 Environment            | `kntgraph-environment`        |

> **Behavioural change.** Skills are loaded on demand,
> not on every turn. An AI agent that is about to
> write a `Mapping[str, Any]` in framework code, for
> example, will only see the `JsonValue` rule if the
> prompt's keywords match `kntgraph-type-discipline`.
> The enforcement model is now trigger-based; the gate
> in `scripts/ci.py` is unchanged.

The companion documents are unchanged:

- `CONTRIBUTING.md` — the PR workflow.
- `DEBT.md` — the project's tech-debt log.
- `CHANGELOG.md` — the version history.
- `ADRs/` — Architecture Decision Records.
