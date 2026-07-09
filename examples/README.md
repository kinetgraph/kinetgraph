<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# kntgraph — Examples

Runnable end-to-end examples covering the
`kntgraph` framework and the `kntgraph.agents`
vertical adapters.

Examples 01–07 live in the `agents` layer
(LiteLLM, roles, session, profile, caching).
Examples 08–15 live in the core layer
(FalkorDB projection, solution tier, HTTP
gateway, multi-agent coordination, continuity).

Each file is self-contained and can be run
directly with `python` or via `uv run`.

| #  | File                                  | What it shows                                                                                     | Layer                |
| -- | ------------------------------------- | ------------------------------------------------------------------------------------------------- | -------------------- |
| 01 | `01_llm_basic.py`                     | `LiteLLMTool` — simplest call, 1 request                                                          | `agents`             |
| 02 | `02_llm_with_rate_limit.py`           | `LLMConfig` with rate limit + cost budget                                                         | `agents`             |
| 03 | `03_role_usage.py`                    | `SummarizerRole` / `PlannerRole` in action                                                        | `agents`             |
| 04 | `04_reactive_system_with_llm.py`      | LLM called from a reactive system, via EventLog                                                   | `agents`             |
| 05 | `05_session_chat.py`                  | Multi-turn conversation with `SessionManager` + `ChatRole`                                        | `agents`             |
| 06 | `06_profile_preferences.py`           | Personalised responses via `ProfileManager` + `PersonalizedRole`                                  | `agents`             |
| 07 | `07_caching_transport.py`             | `CachingLLMTransport` for at-most-once LLM calls                                                   | `agents`             |
| 08 | `08_falkordb_projection.py`           | FalkorDB projection of the Document subgraph + `vector_search`                                    | core                 |
| 09 | `09_knowledge_consolidation.py`       | `SolutionExtractor` + PII gate + hybrid retrieval (ADR-010)                                       | core                 |
| 10 | `10_http_intent_router.py`            | External HTTP gateway: `tool.{name}.requested` via REST + API key (ADR-012)                       | core                 |
| 11 | `11_tool_invoker.py`                  | `ToolInvoker` consuming `tool.{name}.requested` from EventLog, dispatching to `LiteLLMTool`     | core + `agents`      |
| 12 | `12_semantic_routing.py`              | `SemanticRoutingRole` (M1) + M2 hook + 4 scenarios (routed/extracted, routed/empty, …)            | core + `agents`      |
| 13 | `13_multi_agent.py`                   | Cooperation between independent `World -> list[Event]` systems on the same agent (producer + approver + consumer) | core                 |
| 14 | `14_sales_logistics.py`               | Per-stream isolation: each `agent_id` is its own stream; changing one order's qty does not leak into another | core                 |
| 15 | `15_audit_supervisor.py`              | Supervisor pattern: an audit system inspects per-agent Worlds and emits `audit.flagged` on inconsistency | core                 |
| 16 | `16_continuity_recency.py`            | `ContinuityManager` (ADR-014): recency-suggest + LGPD `clear`                                     | core                 |

## Setup

1. Copy `.env.example` to `.env` and adjust as
   needed. The defaults use Ollama locally with
   `gemma4`.
2. **For Ollama**: the daemon must be running
   (`ollama serve`) and the model must be pulled
   (`ollama pull gemma4:31b-cloud`).
3. **For cloud providers**: set `OPENAI_API_KEY` /
   `ANTHROPIC_API_KEY` and override
   `KNT_LLM_DEFAULT_MODEL`.
4. **For examples 08 and 09**: FalkorDB on
   `localhost:16379` (password `falkordb`). Run
   `docker run -d -p 16379:6379 --name kntgraph-falkordb falkordb/falkordb`.

## Running

```bash
# General setup
cp .env.example .env  # edit as needed
ollama serve &
ollama pull gemma4:31b-cloud

# Run an LLM example
python examples/01_llm_basic.py

# Run a Knowledge example (08 or 09)
docker run -d -p 16379:6379 --name kntgraph-falkordb falkordb/falkordb
python examples/09_knowledge_consolidation.py
```

Examples 04, 08, 09, and 11 also need Redis on
`localhost:6379`. Example 11 needs Ollama
running locally with the `qwen3.5:4b` model
pulled (`ollama pull qwen3.5:4b`); set
`KNT_LLM_DEFAULT_MODEL` to point at another
model or provider.

## Without Docker (Redis)

`fakeredis>=2.20` is shipped as a `[dev]` extra
of `kntgraph`. It is a drop-in for
`redis.asyncio` in-process — emulates Streams,
`xadd`, `xrange`, `xrevrange`, and `EVALSHA`,
which is enough for `EventLog` and
`ToolInvoker`. Example 12 already wires this:
it uses the helper
`_lib.redis_or_fake.make_redis_client()` which
reads `KNT_REDIS_FAKE=1` and falls back to
`fakeredis.aioredis.FakeRedis(decode_responses=False)`.

## Without Docker (FalkorDB)

`falkordblite>=0.1.0` is shipped as a `[dev]`
extra. It embeds a Redis server + the FalkorDB
module in-process (no container), exposed via
`from redislite.falkordb_client import FalkorDB`.
Supports Cypher (same engine as the official
server), multiple graphs in the same `db`,
optional persistence via a path
(`/tmp/falkordb.db`). Not recommended for
production.

Examples 08 and 09 expect `GraphPool` running
on `localhost:16379` (container). The
integration with `falkordblite` is not yet
wired — it would require a `LiteFalkorDBClient`
adapter (≤ 40 lines) that delegates to
`FalkorDB('/tmp/...')` and exposes
`graph(name).query(...)` with the same
signature that `GraphPool` expects. Once the
path is standardised, it will read an env
(e.g. `KNT_FALKORDB_LITE=1`) and instantiate the
adapter.

## Recognised env vars

See `.env.example`. Summary:

- `KNT_LLM_DEFAULT_MODEL` — primary model
  (with provider prefix, e.g.
  `ollama/gemma4:31b-cloud`,
  `openai/gpt-4o-mini`).
- `KNT_LLM_FALLBACK_MODELS` — CSV of fallbacks.
- `KNT_LLM_RATE_LIMIT_RPM` — requests per
  minute.
- `KNT_LLM_COST_BUDGET_USD` — per-hour budget.
- `KNT_LLM_TIMEOUT_S` — per-call timeout.
- `KNT_KNOWLEDGE_INTERVAL_S` — interval of the
  Solution tier consolidator (default `10.0`).
- `KNT_PII_LEVEL` — PII level (default `1`).
- `KNT_SOLUTIONS_TOOL_ALLOWLIST` — CSV of
  tools allowed in the Solution tier.
- `OLLAMA_API_BASE` — Ollama endpoint (default
  `localhost:11434`).
- `OPENAI_API_KEY` and friends — cloud
  provider keys.

Real env vars take precedence over the `.env`
file.
