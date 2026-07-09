# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
02 — LLMConfig with rate limit and cost budget.

Demonstrates the configuration primitives in
`fmh_agents.config`:

  - LLMConfig: model, fallback, rate_limit_rpm, budget
  - RateLimiter: sliding-window rpm enforcement
  - CostBudget: per-hour USD cap

Configuration is loaded from env / `.env`. The script
overrides `rate_limit_rpm=3` for a visible rate-limit hit
on the 4th call. Local Ollama has no cost — the
cost_budget stays None.

By default targets a local Ollama + `qwen3.5:4b`. If
`FMH_LLM_DEFAULT_MODEL` is not set, the example overrides
the framework default (`gpt-4o-mini`) so it runs without
an OpenAI key.

Run:
    python examples/02_llm_with_rate_limit.py
"""

from __future__ import annotations

import asyncio
import time

from kntgraph.agents.config import LLMConfig, load_env
from kntgraph.agents.tools import LiteLLMTool


async def main() -> None:
    load_env()
    cfg = LLMConfig.from_env()
    if cfg.default_model == "gpt-4o-mini":
        cfg = LLMConfig(
            default_model="ollama/qwen3.5:4b",
            timeout_s=60.0,
        )

    # Demo override: tight rpm to make the limit visible.
    demo_cfg = LLMConfig(
        default_model=cfg.default_model,
        rate_limit_rpm=3,
        cost_budget_per_hour_usd=cfg.cost_budget_per_hour_usd,
        timeout_s=cfg.timeout_s,
    )
    tool = LiteLLMTool(
        default_model=demo_cfg.default_model,
        rate_limiter=demo_cfg.rate_limiter(),
        cost_budget=demo_cfg.cost_budget(),
        timeout_s=demo_cfg.timeout_s,
    )

    started = time.monotonic()
    for i in range(4):
        r = await tool.invoke(
            idempotency_key=f"example-02:{i}",
            system="Reply with one short sentence.",
            user=f"Tell me fact #{i}.",
            max_tokens=60,
            # qwen3.5 is a thinking model: skip reasoning
            # so the answer lands in `content` instead of
            # `reasoning`.
            think=False,
        )
        elapsed = time.monotonic() - started
        if r.is_ok():
            resp = r.unwrap()
            print(
                f"[{i:>2}] ok in {elapsed:5.1f}s "
                f"tokens={resp.usage.total_tokens:>3} "
                f"text={resp.text!r}"
            )
        else:
            print(f"[{i:>2}] ERR: {r.err_value()}")


if __name__ == "__main__":
    asyncio.run(main())
