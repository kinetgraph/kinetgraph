---
name: kntgraph-typed-errors
description: Use when writing error handling in kntgraph — choosing between Result[T, E] and a typed *Error exception, designing the return type of a mutating storage adapter, or reviewing code that "raise Exception". Covers the never-raise-Exception rule, the Result[T, E] expected-failure path, the typed *Error crash-signal pattern, and the mutating-operation-returns-Result contract for Redis adapters. Trigger keywords: Result, ToolError, GraphError, CheckpointError, raise Exception, error handling, mutating operation, storage adapter, Redis adapter, DLQ, expected failure.
---

<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->


# Errors are typed (`Result[T, E]`)

## 6.1 Never `raise Exception`

Domain errors are typed. The framework's `Result[T, E]` (in `kntgraph.core.result`) encodes the expected-failure path; typed `*Error` classes (`ToolError`, `GraphError`, `CheckpointError`, etc.) are the per-domain "raised to crash the process" signal for **unexpected** failures.

## 6.2 Mutating operations return `Result`

All mutating operations on framework and vertical storage adapters return `Result[T, ToolError]`, `Result[T, GraphError]`, etc. The adapters this covers include:

- `RedisEventLogAdapter`
- `RedisCheckpointStorage`
- `RedisSessionStorage`
- `RedisProfileStorage`
- `RedisContinuityStorage`
- The `DLQ` adapters
- The graph adapters

Tests document the contract — see for example `tests/unit/infra/redis/_memory/test_session.py`.
