# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 09c: Zero Token Architecture (ZTA) — Redis-backed Solution store.

Same shape as ``09b_solution_lookup_zta.py`` but the
``SolutionLookupSystem`` reads from a
:class:`RedisSolutionStore` (production adapter) instead
of an :class:`InMemorySolutionStore`. Demonstrates:

  - Wiring the canonical production read-side cache
    (`ADR-010` + `ADR-049`) with a real Redis client.
  - The wire format: one Redis Hash per tool
    (``knt:solution:<tool_name>``); field =
    ``params_fingerprint``; value = JSON ``CachedSolution``.
  - Operator-side inspection: ``iter_keys`` /
    ``read_all`` for auditing the cache contents.
  - Fail-open semantics: a Redis outage degrades to a
    miss so the LLM fallback takes over (ADR-049 §2.1.3).

## What this example demonstrates

  1. **Production cache adapter** — same dispatcher
     stack as ``09b`` (rule-based system + lookup
     system); the only difference is the store backing
     the lookup.
  2. **TTL knob** — Solutions carry an optional
     ``ttl_seconds`` so a stale Solution cannot haunt
     the read side forever (the operator decides).
  3. **Audit** — the example prints the cached entries
     per tool after the run, demonstrating how an
     operator would inspect what was loaded.

## Architecture

```
[user.intent] ─► EventLog (Redis Stream)
       │
       ▼
  ReactiveDispatcher
       │
       │  ┌────────────────────────────────────┐
       │  │ T1: RuleBasedChatSystem            │
       │  │ T2: SolutionLookupSystem           │
       │  │       reads Redis Hash:            │
       │  │       knt:solution:knowledge_lookup│
       │  └────────────────────────────────────┘
       │
       ▼
  [chat.reply.generated] / [tool.<name>.completed]
```

## Run with

    uv run python examples/09c_solution_lookup_zta_redis.py

(default Redis on ``localhost:6379`` with password
``redispassword``). For CI / unit tests, prefer the
InMemory variant (``09b``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import redis.asyncio as aioredis

from kntgraph.agents.memory.solution_lookup import (
    CachedSolution,
    SolutionLookupSystem,
)
from kntgraph.agents.role_systems import (
    ChatRule,
    RuleBasedChatSystem,
)
from kntgraph.core.event import CorrelationContext, Event
from kntgraph.infra.redis import RedisSolutionStore
from kntgraph.infra.redis import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog

from _lib.redis_or_fake import make_redis_client


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


SOLUTION_TTL_SECONDS = 60 * 60  # 1h sliding; operators tune per tool.


# ---------------------------------------------------------------------------
# 1. Pre-populate the Redis cache (the write side is the
#    Solution promoter's job; here we seed by hand for the
#    example to be self-contained).
# ---------------------------------------------------------------------------


async def _seed_redis_cache(store: RedisSolutionStore) -> None:
    """
    Load three Solutions into the Redis-backed store.

    Each ``CachedSolution`` is keyed by
    ``(tool_name, params_fingerprint)``; the adapter
    writes one Redis Hash per tool. The write is
    transactional (``DELETE`` is replaced by ``HSET``
    here because the field is unique per fingerprint)
    with the TTL applied atomically.
    """
    seeds = [
        CachedSolution(
            tool_name="knowledge_lookup",
            params_fingerprint=_fingerprint_for(
                {
                    "tool": "knowledge_lookup",
                    "params": {"question_id": "export-data-v1"},
                }
            ),
            confidence=5,
            result={
                "answer": "Click Settings → Export. The file is sent by email.",
                "tags": ("export", "data"),
            },
            source_completion_event_id="00000000-0000-0000-0000-000000000001",
        ),
        CachedSolution(
            tool_name="knowledge_lookup",
            params_fingerprint=_fingerprint_for(
                {
                    "tool": "knowledge_lookup",
                    "params": {"question_id": "contact-support-v1"},
                }
            ),
            confidence=5,
            result={
                "answer": "Email support@example.com or open a ticket in the dashboard.",
                "tags": ("support", "contact"),
            },
            source_completion_event_id="00000000-0000-0000-0000-000000000002",
        ),
    ]
    for sol in seeds:
        r = await store.put(sol)
        if r.is_err():
            logger.error(
                "seed put failed: %s",
                r.err_value_or_raise(),
            )
            raise RuntimeError("seed failed")
    logger.info("seeded %d solutions into redis", len(seeds))


def _fingerprint_for(params: dict[str, Any]) -> str:
    """Compute the canonical ``params_fingerprint`` for a
    tool request, matching the ``overlay_tool_calls``
    projection's algorithm (``json.dumps`` + ``short_hash``).
    """
    import json

    from kntgraph.infra.hashing import short_hash

    payload = json.dumps(params, sort_keys=True, default=str)
    return short_hash(payload)


# ---------------------------------------------------------------------------
# 2. Drive a few events through the dispatcher
# ---------------------------------------------------------------------------


async def _emit_tool_request(
    log: EventLog, *, agent_id: str, tool_name: str, params: dict[str, Any]
) -> None:
    """Append a ``tool.<name>.requested`` event.

    The wire format mirrors what ``ToolAwareSystem.request_tool``
    emits: ``data["tool"]`` is the tool name and
    ``data["params"]`` is the parameter payload. The
    fingerprint includes ``data`` verbatim (the
    ``overlay_tool_calls`` projection's algorithm).
    """
    result = await log.append(
        Event.create(
            event_type=f"tool.{tool_name}.requested",
            agent_id=agent_id,
            event_class="domain",
            correlation=CorrelationContext.new(),
            data={"tool": tool_name, "params": params},
        )
    )
    if result.is_err():
        logger.error("append failed: %s", result.err_value_or_raise())


async def _run_scenarios(
    log: EventLog,
    dispatcher: ReactiveDispatcher,
    lookup_system: SolutionLookupSystem,
) -> None:
    # --- Scenario A: known question → cache hit ---
    await _emit_tool_request(
        log,
        agent_id="tenant-A-user-1",
        tool_name="knowledge_lookup",
        params={"question_id": "export-data-v1"},
    )
    # --- Scenario B: another known question → cache hit ---
    await _emit_tool_request(
        log,
        agent_id="tenant-B-user-1",
        tool_name="knowledge_lookup",
        params={"question_id": "contact-support-v1"},
    )
    # --- Scenario C: unknown question → cache miss ---
    await _emit_tool_request(
        log,
        agent_id="tenant-C-user-1",
        tool_name="knowledge_lookup",
        params={"question_id": "quantum-physics-v1"},
    )

    # Track every agent we created so the dispatcher
    # polls their streams. We must call ``track_agent``
    # BEFORE the first ``dispatch_once`` so the
    # dispatcher knows which streams to watch.
    for agent_id in await log.list_agents():
        dispatcher.track_agent(agent_id)

    # First tick: fold the batch into the World and
    # surface the ToolCallRequest to the lookup system.
    # Second tick: drain the lookups (the completion is
    # queued).
    # Third tick: surface the synthetic completion.
    # The lookup system's ``__call__`` returns the
    # pending completions from the previous tick's
    # ``run_pending_lookups``; we run enough ticks so
    # the completions actually land in the EventLog.
    for tick_idx in range(8):
        await dispatcher.dispatch_once()
        await lookup_system.run_pending_lookups()
        logger.debug("dispatcher tick %d done", tick_idx)


async def _print_cache_state(store: RedisSolutionStore) -> None:
    """Operator-side audit: show every cached Solution per tool."""
    print("\n=== Redis-backed Solution cache ===")
    for tool_name in ("knowledge_lookup",):
        keys = [k async for k in store.iter_keys(tool_name)]
        print(f"  tool={tool_name} fingerprints={keys}")
        for fp, sol in (await store.read_all(tool_name)).items():
            print(
                f"    fp={fp} confidence={sol.confidence} "
                f"answer={(sol.result.get('answer') if isinstance(sol.result, dict) else '')!r}"
            )


async def _print_redis_keys(client: aioredis.Redis, prefix: str) -> None:
    """Show what landed in Redis (operator curiosity)."""
    print(f"\n=== Redis keys matching {prefix}* ===")
    async for key in client.scan_iter(match=f"{prefix}*", count=100):
        decoded = key.decode("utf-8") if isinstance(key, bytes) else key
        ttl = await client.ttl(decoded)
        hlen = await client.hlen(decoded)
        print(f"  {decoded}  ttl={ttl}s  fields={hlen}")


# ---------------------------------------------------------------------------
# 3. Entry point
# ---------------------------------------------------------------------------


async def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    client = make_redis_client()
    store = RedisSolutionStore(client=client, ttl_seconds=SOLUTION_TTL_SECONDS)
    await _seed_redis_cache(store)

    adapter = RedisEventLogAdapter(client)
    log = EventLog(adapter)

    rule_system = RuleBasedChatSystem(
        rules=[
            ChatRule(
                tenant_id="*",
                persona_pattern="*",
                message_pattern="hello",
                response="Hello! How can I help?",
                priority=0,
            )
        ]
    )
    lookup_system = SolutionLookupSystem(
        solution_store=store,
        allowlist=frozenset({"knowledge_lookup"}),
        min_confidence=3,
    )

    dispatcher = ReactiveDispatcher(
        log=log,
        systems=[rule_system, lookup_system],
        redis=client,
    )

    await _run_scenarios(log, dispatcher, lookup_system)

    completions: list[Event] = []
    for agent_id in await log.list_agents():
        for e in await log.read(agent_id):
            if e.event_type.startswith("tool.") and e.event_type.endswith(".completed"):
                completions.append(e)

    print("\n=== tool.<name>.completed events (solution lookup path) ===")
    for e in completions:
        data = e.data
        result = data.get("result", {})
        answer = result.get("answer") if isinstance(result, dict) else None
        print(
            f"  agent={e.agent_id}  type={e.event_type}  "
            f"source={data.get('source')}  answer={answer!r}"
        )

    print("\n=== lookup stats ===")
    stats = lookup_system.stats
    print(
        f"  hits={stats.cache_hit}  misses={stats.cache_miss}  "
        f"bypass_low={stats.bypass_low_confidence}"
    )

    await _print_cache_state(store)
    await _print_redis_keys(client, "knt:solution:")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
