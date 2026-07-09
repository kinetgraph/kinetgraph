# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
03 — Using Roles (SummarizerRole, PlannerRole).

A `Role` is a typed wrapper around `LiteLLMTool` that knows
the domain prompt and output schema. Multiple roles share
the same Tool instance (and therefore the same rate limit,
cost budget, fallback chain).

Configuration is loaded from env / `.env`. By default the
example targets a local Ollama + `qwen3.5:4b`; if no env
var is set, `LLMConfig.from_env()` falls back to
`gpt-4o-mini`, and we override to Ollama so the example
runs without an OpenAI key.

The example passes `think=False` to the Roles — required
for thinking Ollama models (qwen3.5, deepseek-r1, etc.) so
the answer lands in `message.content` instead of
`message.reasoning` and the JSON parser doesn't see an
empty string.

Run:
    python examples/03_role_usage.py
"""

from __future__ import annotations

import asyncio

from kntgraph.agents.config import LLMConfig, load_env
from kntgraph.agents.roles import SummarizerRole, PlannerRole, Summary, Plan
from kntgraph.agents.tools import LiteLLMTool


SAMPLE_TEXT = """\
Event Sourcing is a pattern where state changes are stored
as a sequence of events. Instead of persisting the current
state of an entity, the application persists each
transformation as a discrete event. The current state is
derived by folding the event stream. This approach provides
a complete audit trail and enables time-travel debugging,
but requires the application to design events carefully and
to handle schema evolution over time. Event Sourcing pairs
naturally with CQRS, where the write model emits events and
read models project them into queryable views.
"""


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
    llm = LiteLLMTool(
        default_model=cfg.default_model,
        rate_limiter=cfg.rate_limiter(),
        cost_budget=cfg.cost_budget(),
        timeout_s=cfg.timeout_s,
    )

    summarizer = SummarizerRole(llm=llm)
    planner = PlannerRole(llm=llm, max_tokens=600)

    # ---- Summarize ----
    # `think=False` is forwarded to LiteLLM via the Role's
    # **invoke_kwargs. Required for thinking Ollama models
    # (qwen3.5, deepseek-r1, etc.) — without it the response
    # lands in `message.reasoning` and the parser sees an
    # empty `content`, failing JSONDecodeError.
    sum_result = await summarizer.summarize(
        SAMPLE_TEXT,
        max_words=40,
        think=False,
    )
    if sum_result.is_err():
        print(f"summarize error: {sum_result.err_value()}")
    else:
        s: Summary = sum_result.unwrap()
        print(f"summary ({s.word_count} words): {s.summary}")
        print("key points:")
        for p in s.key_points:
            print(f"  - {p}")

    # ---- Plan ----
    plan_result = await planner.plan(
        "Add a rate limiter to an LLM tool",
        context="Already have cost budget. Want sliding-window rpm.",
        think=False,
    )
    if plan_result.is_err():
        print(f"plan error: {plan_result.err_value()}")
    else:
        p: Plan = plan_result.unwrap()
        print(f"\ngoal: {p.goal}")
        print(f"rationale: {p.rationale}")
        print("steps:")
        for step in p.steps:
            deps = f"  (after: {', '.join(step.depends_on)})" if step.depends_on else ""
            print(f"  - {step.name}: {step.description}{deps}")
        if p.risks:
            print("risks:")
            for r in p.risks:
                print(f"  - {r}")


if __name__ == "__main__":
    asyncio.run(main())
