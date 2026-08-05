<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

---
name: kntgraph-testing
description: Use when writing or reviewing tests in the kntgraph codebase — choosing between behaviour tests and mock-heavy unit tests, marking async def tests, using KNT_REDIS_FAKE for fakeredis, and meeting the happy-path-plus-one-failure-mode bar for every public function. Covers the real-EventLog / real-World / real-WorkerManager rule, the fakeredis env var, the pytest.mark.asyncio strict-mode requirement, and the per-public-function coverage gate. Trigger keywords: pytest, unit test, integration test, fakeredis, KNT_REDIS_FAKE, asyncio_mode, mock, behaviour test, happy path, failure mode.
---

# Testing

## 7.1 Behaviour tests, not mock-heavy unit tests

Use the real `EventLog`, the real `World`, the real `WorkerManager`. Mock **only** when the external system is unavailable in CI (e.g. GLiNER2 with a GPU, real Ollama with a local LLM, real FalkorDB with a graph).

The `KNT_REDIS_FAKE=1` env var switches the `EventLog` to an in-process `fakeredis` client so unit tests do not need a Redis container.

## 7.2 Cover the happy path + at least one failure mode per public function

This is the standard gate for new code (per `CONTRIBUTING.md`); the test files document the expected shape.

## 7.3 `pytest.mark.asyncio` only on `async def` tests

The project's `pyproject.toml` sets `asyncio_mode = "strict"`, which:

- requires an explicit mark on every `async def test_*`
- rejects stray marks on sync `def test_*`

The gate is the `pytest -W error::pytest.PytestWarning` step in `scripts/ci.py`.
