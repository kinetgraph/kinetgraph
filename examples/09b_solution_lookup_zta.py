# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 09b: Zero Token Architecture (ZTA) — solution lookup + rule-based chat.

This example shows how to build **token-free** chat paths
on top of kntgraph using two systems that ship in
v0.10.0 (ADR-049):

  - :class:`RuleBasedChatSystem` — short-circuits
    ``user.intent`` events with deterministic replies
    loaded from a per-tenant rule table. The wire
    format is identical to the LLM-backed
    :class:`ChatRoleSystem`, so downstream consumers
    cannot tell the difference.

  - :class:`SolutionLookupSystem` — reads a previously
    extracted answer from the in-memory store and
    synthesises a ``tool.<name>.completed`` event when
    a known ``(tool_name, params_fingerprint)`` pair
    surfaces again. Replaces re-running
    :class:`SolutionExtractor` (ADR-010).

Both systems are pure ``WorldSystem``s: no LLM is
called in this example, and the lookup system does
not even touch a graph DB — it reads from a
pre-populated in-process store.

## What this example demonstrates

  1. **Rule-based chat** (ZTA principle 2) — the
     system emits ``chat.reply.generated`` directly
     when a rule matches, bypassing the LLM.
  2. **Read-side cache** (ZTA principle 3) — the
     lookup system inspects ``tool.<name>.requested``
     events on the world view; when a matching
     cached Solution exists, it synthesises a
     ``tool.<name>.completed`` event with the cached
     payload.
  3. **Composability** — the systems are independent
     and stackable. In production a
     :class:`ChatRoleSystem` registered AFTER both
     handles the ``user.intent`` events that fall
     through both — that ordering is the canonical
     expression of ZTA principle 4 ("stable →
     software, uncertain → AI").

## Architecture (this example)

```
[user.intent] ─► EventLog (Redis Stream)
       │
       ▼
  ReactiveDispatcher
       │
       │  ┌────────────────────────────────────┐
       │  │ T1: RuleBasedChatSystem            │
       │  │       matches rule?                │
       │  │         YES → emit chat.reply      │
       │  │         NO  → [] (pass-through)    │
       │  │ T2: SolutionLookupSystem           │
       │  │       matches a stored solution?   │
       │  │         YES → emit                 │
       │  │            tool.<name>.completed   │
       │  │         NO  → []                   │
       │  └────────────────────────────────────┘
       │
       ▼
  [chat.reply.generated] / [tool.<name>.completed]
```

The example prints the resulting events so the reader
can see the wire format.

## Run with

    KNT_REDIS_FAKE=1 uv run python examples/09b_solution_lookup_zta.py

or against a real Redis on ``localhost:6379`` (see the
README for credentials).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kntgraph.agents.memory.solution_lookup import (
    CachedSolution,
    InMemorySolutionStore,
    SolutionLookupSystem,
)
from kntgraph.agents.role_systems import (
    ChatRule,
    RuleBasedChatSystem,
)
from kntgraph.core.event import CorrelationContext, Event
from kntgraph.infra.redis import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog

from _lib.redis_or_fake import make_redis_client


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Build the deterministic rule table (ZTA principle 2)
# ---------------------------------------------------------------------------


def _build_rules() -> list[ChatRule]:
    """
    A small per-tenant rule table. Each rule is a
    5-tuple ``(tenant_id, persona_pattern,
    message_pattern, response, priority)``.

    The ``persona_pattern`` is a ``fnmatch`` glob;
    ``message_pattern`` is a substring match (case
    insensitive by default). ``priority`` breaks ties
    when multiple rules match a single request
    (higher wins).

    An equivalent YAML file is shipped at
    ``examples/_data/zta_rules.yaml``; load it with
    :meth:`RuleBasedChatSystem.register_from_yaml`.
    """
    return [
        ChatRule(
            tenant_id="*",
            persona_pattern="*",
            message_pattern="hours",
            response="Mon-Fri, 9-18 UTC.",
            priority=10,
        ),
        ChatRule(
            tenant_id="tenant-A",
            persona_pattern="support-*",
            message_pattern="refund",
            response="Please contact billing@tenant-A.example.",
            priority=20,
        ),
        ChatRule(
            tenant_id="tenant-A",
            persona_pattern="*",
            message_pattern="refund",
            response="See our refund policy at tenant-A.example/refunds.",
            priority=0,
        ),
        ChatRule(
            tenant_id="*",
            persona_pattern="*",
            message_pattern="hello",
            response="Hello! How can I help?",
            priority=0,
        ),
    ]


# ---------------------------------------------------------------------------
# 2. Build the solution store (ZTA principle 3)
# ---------------------------------------------------------------------------


def _build_solution_store() -> InMemorySolutionStore:
    """
    A tiny pre-populated store. In production this is
    fed by :class:`SolutionExtractor` (ADR-010) on the
    write side; the lookup system here only reads.

    Each :class:`CachedSolution` is keyed by
    ``(tool_name, params_fingerprint)``. The fingerprint
    is a short hash of the canonical-JSON of the
    request params (the same algorithm
    :class:`SolutionExtractor` uses; see
    ``agents/memory/solutions/_fingerprints.py``). To
    compute the fingerprint for a given params dict,
    instantiate a ``ToolCallRequest`` and pass it to
    ``_params_fingerprint_from_request``.
    """
    store = InMemorySolutionStore()
    store.add(
        CachedSolution(
            tool_name="knowledge_lookup",
            params_fingerprint="f78d5746c6ffc185",  # {"tool":"knowledge_lookup","params":{"question_id":"export-data-v1"}}
            confidence=5,
            result={
                "answer": "Click Settings → Export. The file is sent by email.",
                "tags": ("export", "data"),
            },
            source_completion_event_id="00000000-0000-0000-0000-000000000001",
        )
    )
    store.add(
        CachedSolution(
            tool_name="knowledge_lookup",
            params_fingerprint="db3ca9f19bd77d59",  # {"tool":"knowledge_lookup","params":{"question_id":"contact-support-v1"}}
            confidence=5,
            result={
                "answer": "Email support@example.com or open a ticket in the dashboard.",
                "tags": ("support", "contact"),
            },
            source_completion_event_id="00000000-0000-0000-0000-000000000002",
        )
    )
    return store


# ---------------------------------------------------------------------------
# 3. Drive a few events through the dispatcher
# ---------------------------------------------------------------------------


async def _emit_user_intent(log: EventLog, *, agent_id: str, message: str) -> None:
    """Append a ``user.intent`` event. The rule system
    reacts to this."""
    result = await log.append(
        Event.create(
            event_type="user.intent",
            agent_id=agent_id,
            event_class="domain",
            correlation=CorrelationContext.new(),
            data={"message": message, "tenant_id": agent_id.split("-")[0]},
        )
    )
    if result.is_err():
        logger.error("append failed: %s", result.err_value_or_raise())


async def _emit_tool_request(
    log: EventLog, *, agent_id: str, tool_name: str, params: dict[str, Any]
) -> str:
    """Append a ``tool.<name>.requested`` event. The
    lookup system reacts to ``tool_requests`` on the
    view (these come from any other system; here we
    emit them by hand to keep the example self-
    contained)."""
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
        return ""
    return result.unwrap()


async def _run_scenarios() -> None:
    client = make_redis_client()
    adapter = RedisEventLogAdapter(client)
    log = EventLog(adapter)

    store = _build_solution_store()
    rule_system = RuleBasedChatSystem(rules=_build_rules())
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

    # --- Scenario A: rule-based chat (ZTA principle 2) ---
    # The agent_id doubles as the tenant scope in this
    # minimal example (no ``SessionComponent`` loaded;
    # ``RuleBasedChatSystem`` falls back to ``agent_id``
    # when no session is present). We dispatch ONE
    # user.intent per agent so the rule matches cleanly
    # (the base system only reacts to the latest
    # ``user.intent`` event folded onto the view).
    await _emit_user_intent(
        log, agent_id="tenant-A", message="What are your support hours?"
    )

    # --- Scenario B: solution lookup (ZTA principle 3) ---
    await _emit_tool_request(
        log,
        agent_id="tenant-A-user-3",
        tool_name="knowledge_lookup",
        params={"question_id": "export-data-v1"},
    )
    await _emit_tool_request(
        log,
        agent_id="tenant-B-user-1",
        tool_name="knowledge_lookup",
        params={"question_id": "contact-support-v1"},
    )
    # Miss scenario: an unknown question id.
    await _emit_tool_request(
        log,
        agent_id="tenant-C-user-1",
        tool_name="knowledge_lookup",
        params={"question_id": "quantum-physics-v1"},
    )

    # Track every agent we created so the dispatcher
    # polls their streams.
    for agent_id in await log.list_agents():
        dispatcher.track_agent(agent_id)

    # Drain the dispatcher; the lookup system is async,
    # so we drain its pending lookups once per tick.
    for tick_idx in range(8):
        n = await dispatcher.dispatch_once()
        await lookup_system.run_pending_lookups()
        logger.debug("dispatcher tick %d done (n=%d)", tick_idx, n)

    chat_replies: list[Event] = []
    completions: list[Event] = []
    for aid in await log.list_agents():
        for e in await log.read(aid):
            if e.event_type == "chat.reply.generated":
                chat_replies.append(e)
            elif e.event_type.startswith("tool.") and e.event_type.endswith(
                ".completed"
            ):
                completions.append(e)

    print("\n=== chat.reply.generated events (rule-based path) ===")
    for e in chat_replies:
        data: dict[str, Any] = e.data  # type: ignore[assignment]
        output = data.get("output", {})
        print(f"  agent={e.agent_id}  reply={output.get('reply')!r}")

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
        f"bypass_low={stats.bypass_low_confidence}  "
        f"bypass_not_in_allowlist={stats.bypass_not_in_allowlist}"
    )


# ---------------------------------------------------------------------------
# 4. Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_run_scenarios())


if __name__ == "__main__":
    main()
