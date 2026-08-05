<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

---
name: kntgraph-branch-policy
description: Use when an AI agent is about to run git push, create a feat/fix/chore branch, open a PR, or commit on the kntgraph repo. Covers the no-direct-push-to-main rule, the human-creates-branch rule, the AI-only-runs-add/diff/status/checkout rule, and the human-reviews-and-commits workflow. Trigger keywords: git push, branch, feat/, fix/, chore/, open PR, create PR, commit, AI agent git, push forbidden.
---

# Branch policy

## 11.3 Don't create new branches without checking with the human

AI agents MUST NOT push to `main` directly and MUST NOT create new long-lived branches (`feat/...`, `fix/...`, `chore/...`) without explicit human direction. The canonical workflow is:

1. **The human creates the branch.**
2. The AI agent works on it, accumulates commits (the AI does **not** commit either; the human reviews and commits).
3. The human opens the PR.

The agent's only git operations during iteration are:

- `git add`
- `git diff`
- `git status`
- `git checkout` (between branches the human already created)

Pushing and PR creation are the human's.
