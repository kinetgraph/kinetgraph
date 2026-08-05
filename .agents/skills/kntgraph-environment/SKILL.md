<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

---
name: kntgraph-environment
description: Use when setting up the local dev environment for kntgraph — running tests, choosing the LLM model, or starting the Redis/FalkorDB/Ollama containers for integration tests. Covers the KNT_REDIS_FAKE env var (fakeredis for unit tests), the KNT_LLM_DEFAULT_MODEL env var (default gpt-4o-mini, local dev ollama/qwen3.5:4b), the docker run commands for Redis/FalkorDB/Ollama, and the build-artifact gitignore set. Trigger keywords: KNT_REDIS_FAKE, KNT_LLM_DEFAULT_MODEL, docker run, redis, falkordb, ollama, qwen3.5, .gitignore, build artifacts, scratch script.
---

# Environment

## Required env vars

```bash
# Switch the EventLog / Redis adapters to in-process fakeredis
# (no Redis container required for unit tests).
export KNT_REDIS_FAKE=1

# Default model for the LLM worker (LiteLLMToolWorker).
# Default: gpt-4o-mini. Local dev typically uses Ollama:
export KNT_LLM_DEFAULT_MODEL="ollama/qwen3.5:4b"
```

## Local services (integration tests)

```bash
# Redis (port 6379, password "redispassword")
docker run -d -p 6379:6379 --name kntgraph-redis \
    -e REDIS_PASSWORD=redispassword redis:7 \
    --requirepass redispassword

# FalkorDB (port 16379)
docker run -d -p 16379:16379 --name kntgraph-falkordb \
    falkordb/falkordb:latest

# Ollama (port 11434) with the qwen3.5:4b model
docker run -d -p 11434:11434 --name kntgraph-ollama \
    ollama/ollama:latest
ollama pull qwen3.5:4b
```

## Build artifacts (do NOT commit)

The `.gitignore` already excludes the build artifacts (`build/`, `dist/`, `.venv/`, `.pytest_cache/`, `.ruff_cache/`, `.coverage`, `.egg-info/`, etc.) — setuptools defaults plus a few project-specific entries.

Scratch / debug scripts at the repo root (`scratch_*.py`) are NOT part of the build artifact set; they were removed from tracking on 2026-07-14 because they were one-off debug helpers, not production code. Add new scratch scripts inside `scripts/` (or `/tmp/opencode/`) so the project's `__init__.py` layout and the gate's test discovery stay clean.
