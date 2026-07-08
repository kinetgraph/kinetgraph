# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
11 — ToolInvoker: bridging a pure system and a side-effecting Tool.

This example exercises the framework's adapter-side helper,
`fmh_agents.tools.invoker.ToolInvoker`. It is the canonical
path to call a registered `Tool` from a reactive system:

    1. A pure SYSTEM emits `tool.<name>.requested` with the
       arguments in `data` and the original event as
       `causation_id`.
    2. The ToolInvoker reads the request, looks the tool up
       in the `ToolRegistry`, calls `tool.invoke(**kwargs)`,
       and emits either `tool.<name>.completed` (with the
       result) or `tool.<name>.failed` (with the error).
    3. Other reactive systems listen to the `.completed` /
       `.failed` events and react. The system that called
       the tool remains pure (it just produces events).

Idempotency
-----------

The ToolInvoker injects `idempotency_key = str(request.event_id)`
into every `invoke` call. The key is stable across re-runs of
the dispatcher: replaying the log produces the same request
event, the EventLog dedupes it, and the tool is invoked at
most once per (tool, request) tuple. Tools with non-idempotent
side effects (payments, transfers) MUST dedupe on this key.

Pre-requisites:

  - Redis on localhost:6379 (default port).
  - Ollama running locally with the `qwen3.5:4b` model
    pulled (`ollama pull qwen3.5:4b`).

The example reads the model name from `FMH_LLM_DEFAULT_MODEL`
(defaults to `ollama/qwen3.5:4b`) and other settings from
`fmh_agents.config.LLMConfig.from_env()`. Copy `.env.example`
to `.env` to override; real env vars take precedence.

We pass `think=False` to LiteLLM so the qwen3.5 thinking
model returns the answer in `message.content` (not in
`reasoning`, which the framework's `LLMResponse` parser
does not read).

Run:

    cd fmh_agents
    uv run --package kntgraph python \\
        examples/11_tool_invoker.py
"""

from __future__ import annotations

import asyncio

import redis.asyncio as aioredis

from kntgraph.core.event import Event, correlation_middleware
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.stream.event_log import EventLog
from kntgraph.agents.tools.invoker import ToolInvoker
from kntgraph.agents.tools.protocol import (
    ToolEventType,
    ToolRegistry,
)
from kntgraph.agents.config import LLMConfig, load_env
from kntgraph.agents.tools import LiteLLMTool


# Stable agent id. Re-runs of the script reuse the same
# stream; the EventLog dedupes by event_id, so the second
# run is a true no-op for the seed event. The idempotency
# check inside the script (re-running the invoker on the
# SAME log) demonstrates that contract.
AGENT_ID = "agent-007"


# ---------------------------------------------------------------------------
# Reactive systems
# ---------------------------------------------------------------------------
# A reactive system is a pure function
#     (World, Event) -> list[Event]
# The system does NOT call the LLM directly — it emits a
# `tool.llm.complete.requested` event. The ToolInvoker picks
# that up, calls the registered `LiteLLMTool`, and emits the
# `.completed` / `.failed` event back. The system remains
# pure and replayable.
# ---------------------------------------------------------------------------


async def request_summary_on_task_received(world, event: Event) -> list[Event]:
    """
    Reacts to `task.received` by asking the LLM for a
    summary. The system itself does NOT call the LLM —
    it just produces a request event.
    """
    if event.event_type != "task.received":
        return []
    return [
        Event.domain_from(
            agent_id=event.agent_id,
            type=ToolEventType.requested("llm.complete"),
            data={
                "system": (
                    "You are a concise summarizer. "
                    "Reply in one short sentence, no preamble."
                ),
                "user": (f"Summarize: {event.data.get('description', '')}"),
                "temperature": 0.0,
                "max_tokens": 200,
                # qwen3.5 is a thinking model. Without this
                # flag, it fills `max_tokens` with reasoning
                # and the answer ends up in `message.reasoning`
                # (which the LLMResponse parser does not read).
                "think": False,
            },
            causation_id=event.event_id,
            correlation=correlation_middleware.continue_from(event),
        )
    ]


async def react_to_summary(world, event: Event) -> list[Event]:
    """
    Reacts to `tool.llm.complete.completed` by recording
    the summary into the agent's domain state. This is
    the second half of the closed loop.
    """
    if event.event_type != "tool.llm.complete.completed":
        return []
    result = event.data.get("result")
    # The ToolInvoker stores the Tool's Ok(...) payload
    # under "result". For LiteLLMTool that payload is a
    # frozen `LLMResponse` dataclass (`text`, `model`,
    # `usage`, `latency_ms`, `cost_usd`). In-memory
    # access sees the dataclass; reads from Redis see
    # its `repr()` (json.dumps(..., default=str) in
    # _event_to_redis). Handle both shapes so the
    # consumer is robust to round-tripping.
    text = getattr(result, "text", None)
    if not text and isinstance(result, str) and "text=" in result:
        # Best-effort extract from a Redis round-tripped repr.
        import re

        m = re.search(r"text=('([^']*)'|\"([^\"]*)\")", result)
        if m:
            text = m.group(2) or m.group(3) or ""
    latency = event.data.get("latency_ms", 0.0)
    return [
        Event.domain_from(
            agent_id=event.agent_id,
            type="task.summarized",
            data={
                "summary": text or "",
                "tool_latency_ms": latency,
                "request_id": event.data.get("request_id"),
            },
            causation_id=event.causation_id,
            correlation=correlation_middleware.continue_from(event),
        )
    ]


# ---------------------------------------------------------------------------
# Main — drive the flow end-to-end against real Redis + Ollama
# ---------------------------------------------------------------------------


async def main() -> None:
    # 1. Load config. The defaults are: ollama/qwen3.5:4b,
    #    timeout 60s, no rate limit (so the example is
    #    deterministic). Override via env / .env.
    load_env()
    cfg = LLMConfig.from_env()
    if cfg.default_model == "gpt-4o-mini":
        # Default from LLMConfig.from_env is the OpenAI
        # model. The example targets local Ollama; switch
        # to qwen3.5:4b if the user has not set
        # FMH_LLM_DEFAULT_MODEL.
        cfg = LLMConfig(
            default_model="ollama/qwen3.5:4b",
            rate_limit_rpm=cfg.rate_limit_rpm,
            cost_budget_per_hour_usd=cfg.cost_budget_per_hour_usd,
            timeout_s=60.0,
        )

    # 2. Wire the Tool against real Ollama. LiteLLMTool
    #    builds its own LiteLLMTransport when `transport=`
    #    is omitted.
    llm_tool = LiteLLMTool(
        default_model=cfg.default_model,
        rate_limiter=cfg.rate_limiter(),
        cost_budget=cfg.cost_budget(),
        timeout_s=cfg.timeout_s,
    )

    # 3. Register the tool so the invoker can look it up.
    registry = ToolRegistry()
    registry.register(llm_tool)

    # 4. Open Redis-backed EventLog. The framework's
    #    production EventLog lives in
    #    `fmh_backend.stream.event_log.EventLog`.
    redis = aioredis.from_url("redis://:redispassword@localhost:6379")
    log = EventLog(RedisEventLogAdapter(client=redis))
    invoker = ToolInvoker(log=log, registry=registry)

    # 5. Seed a `task.received` event for the agent.
    #    The same payload is used on every run, so the
    #    event_id is stable: re-runs of the script are
    #    a no-op for this step (the EventLog dedupes).
    with correlation_middleware.scope(
        metadata={"example": "11", "agent_id": AGENT_ID}
    ):
        task = Event.domain_from(
            agent_id=AGENT_ID,
            type="task.received",
            data={
                "description": (
                    "Event Sourcing stores state changes as a "
                    "sequence of events instead of persisting the "
                    "current state of an entity; it offers an "
                    "alternative approach to managing and querying "
                    "the state of a system."
                )
            },
            correlation=correlation_middleware.current(),
        )
        append = await log.append(task)
        if append.is_ok():
            print(
                f"[seed] event_id={task.event_id}  "
                f"type={task.event_type}  stream_id={append.unwrap()}"
            )
        else:
            print(
                f"[seed] existing event (idempotent dedup): {task.event_id}"
            )
        print(f"[seed] agent={AGENT_ID}  model={cfg.default_model}")
        print()

        # 6. Run the producer system. It emits
        #    `tool.llm.complete.requested`. The system is
        #    pure — it does not call the LLM.
        request_events = await request_summary_on_task_received(
            world=None, event=task
        )
        for e in request_events:
            await log.append(e)
            print(f"[system] emitted: type={e.event_type}")
        print()

        # 7. Run the invoker. It picks the `.requested` event,
        #    calls the tool, and appends `.completed` (or
        #    `.failed`) back to the log. This is the round
        #    trip to Ollama.
        handled = await invoker.run_once(AGENT_ID)
        print(f"[invoker] handled {handled} request(s)")
        print()

    # 8. Read the .completed event back from Redis and
    #    run the consumer system against it.
    completed = await log.read(AGENT_ID)
    completed = [e for e in completed if e.event_type == "tool.llm.complete.completed"]
    assert completed, "invoker should have emitted a .completed event"
    for e in completed:
        consumer_events = await react_to_summary(world=None, event=e)
        for ce in consumer_events:
            await log.append(ce)
            print(
                f"[consumer] emitted: type={ce.event_type}  "
                f"summary={ce.data.get('summary')!r}  "
                f"latency_ms={ce.data.get('tool_latency_ms')}"
            )
    print()

    # 9. Idempotency check: re-run the invoker on the
    #    SAME log. The .requested event is still in the
    #    stream but a .completed was already emitted, so
    #    the invoker must skip it. The Ollama call must
    #    NOT happen a second time.
    handled_again = await invoker.run_once(AGENT_ID)
    assert handled_again == 0, "re-run must not re-handle a completed request"
    print(f"[idempotency] re-run handled {handled_again} (expected 0)")
    print()

    # 10. Show the full stream for the agent.
    print(f"== full EventLog stream for {AGENT_ID} ==")
    all_events = await log.read(AGENT_ID)
    for e in all_events:
        print(f"  {e.event_type:42s}  id={e.event_id}")

    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
