---
name: kntgraph-deprecation-policy
description: Use when removing a public API, deprecating a class or function, or updating call sites after an API change in the kntgraph codebase. Covers the no-compat-shims rule, the "Deprecation removal" lifecycle (one minor cycle of DeprecationWarning then git rm), removal-target commitments, and the kntgraph.agents.roles precedent. Trigger keywords: deprecate, deprecation, removal target, breaking change, compat shim, kwargs-optional, delete deprecated, git rm.
---

<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->


# Deprecation policy

## 2.1 When an API changes, update all call sites in the same commit

Do **not** add kwargs-optional branches that detect the old API at runtime. Do **not** keep deprecated classes / functions / modules alive past their documented removal target. The "Deprecation removal" pattern (issue a `DeprecationWarning` for one minor cycle, then `git rm` the deprecated code in the next) is the framework's standard lifecycle.

## 2.2 Removal targets are not optional

A `Removal target: v0.9.0` line in a deprecation warning is a **commitment**. The deprecated code MUST be removed in the major version that follows the deprecation. Exceptions require an ADR.

## 2.3 `kntgraph.agents.roles` is the precedent

The package was deprecated in v0.8.0 (ADR-041) with a removal target of v1.0.0; the cleanup commit moved all internal usages to the ECS-shaped `kntgraph.agents.role_systems` module and then removed the package. The pattern is the reference for any future deprecation — this is exactly what was applied in the v0.9.0 breaking change that removed `LiteLLMTool`, `ToolInvoker`, and `agents/roles/`.
