# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
01 — LiteLLMTool basic usage.

The most direct way to call an LLM via fmh_agents. One
LiteLLMTool instance, one call, Ok(text) result.

Configuration is read from environment variables (or
`.env` file in the project root). See `.env.example` for
the full list. The default target is Ollama running
locally with `qwen3.5:4b`; if `KNT_LLM_DEFAULT_MODEL` is
not set, the example overrides the framework default
(`gpt-4o-mini`) so it runs without an OpenAI key.

Run:
    python examples/01_llm_basic.py
"""

from __future__ import annotations

import asyncio

from kntgraph.agents.config import LLMConfig, load_env
from kntgraph.agents.tools import LiteLLMTool


async def main() -> None:
    # Load .env (does not override existing env vars).
    load_env()
    cfg = LLMConfig.from_env()
    if cfg.default_model == "gpt-4o-mini":
        # No KNT_LLM_DEFAULT_MODEL set; default to local
        # Ollama so the example runs out-of-the-box.
        cfg = LLMConfig(
            default_model="ollama/qwen3.5:4b",
            timeout_s=60.0,
        )
    tool = LiteLLMTool(
        default_model=cfg.default_model,
        timeout_s=cfg.timeout_s,
    )

    result = await tool.invoke(
        idempotency_key="example-01:hello",
        system="You are a concise assistant. Answer in one sentence.",
        user="What is event sourcing?",
        temperature=0.0,
        max_tokens=200,
        # qwen3.5 is a thinking model: skip reasoning so
        # the answer lands in `content` instead of
        # `reasoning`.
        think=False,
    )

    if result.is_err():
        print(f"error: {result.err_value()}")
        return

    resp = result.unwrap()
    print(f"model:   {resp.model}")
    print(f"latency: {resp.latency_ms:.1f}ms")
    print(f"tokens:  {resp.usage.total_tokens}")
    print(f"cost:    {resp.cost_usd}  (None for local models)")
    print(f"text:    {resp.text}")


if __name__ == "__main__":
    asyncio.run(main())
