---
name: kntgraph-typed-errors
description: Use when writing error handling in kntgraph — choosing between Result[T, E] and a typed *Error exception, designing the return type of a mutating storage adapter, enforcing adapter exception containment boundaries (ADR-047, ADR-077), or reviewing code that "raise Exception". Covers the never-raise-Exception rule, the Result[T, E] expected-failure path, the typed *Error crash-signal pattern, mutating-operation-returns-Result contract, third-party exception containment, and asyncio.CancelledError propagation. Trigger keywords: Result, ToolError, GraphError, CheckpointError, raise Exception, error handling, mutating operation, storage adapter, Redis adapter, DLQ, expected failure, adapter exception containment, ADR-047, ADR-077, CancelledError, AdapterContractVerifier, exception translation, third-party exceptions.
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

## 6.3 Adapter exception containment & translation boundary (ADR-047 / ADR-077)

Adapters encapsulate all external implementation details and third-party SDKs (Redis, FalkorDB, HTTP, Boto3, etc.).
Raw third-party exceptions (`redis.exceptions.RedisError`, `httpx.HTTPError`, `OSError`, etc.) **must never escape the adapter boundary**.

1. **Containment & Translation**: Every external call inside an adapter must intercept provider exceptions and translate them into a domain-typed `Result.err(ExpectedLayerError)`. Callers must never encounter raw third-party driver errors.
2. **Cancellation Signal Preservation**: `asyncio.CancelledError` is a system runtime control signal (ADR-077 §2.5), **not** an operational error. Adapters must never catch or swallow `CancelledError` or wrap it into `Result.err`. It must propagate cleanly to allow clean task teardown and supervision.
3. **Automated Verification**: Use `AdapterContractVerifier.assert_exception_contained` and `AdapterContractVerifier.assert_cancellation_propagates` from `kntgraph.testing.concordo_harness` (ADR-089) in every adapter unit test to enforce this boundary.
