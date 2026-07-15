# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
01 — LiteLLMToolWorker basic usage (ADR-043).

The canonical way to call an LLM via fmh_agents
(post-ADR-043). One ``LiteLLMToolWorker`` instance,
one ``await worker.invoke(...)`` call, ``Ok(dict)``
result.

``LiteLLMToolWorker`` is the new ``@tool_worker``-
based class (the legacy ``LiteLLMTool`` Tool class
is deprecated; removal target: v0.9.0). The worker
returns a JSON-serialisable dict envelope (``text``
/ ``model`` / ``usage`` / ``finish_reason`` /
``cost_usd`` / ``latency_ms``) — the same shape the
``WorkerManager`` consumes when the worker is run in
a separate ``ProcessPoolExecutor`` (production path).
For one-shot scripts like this example, the worker
can be called directly without the
``WorkerManager`` infrastructure.

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
from kntgraph.agents.tools.llm import LiteLLMToolWorker


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
    # LiteLLMToolWorker reads ``default_model`` /
    # ``timeout_s`` from the LLMConfig at construction
    # time. Per-call kwargs (temperature / max_tokens /
    # think) are passed to ``invoke()``.
    worker = LiteLLMToolWorker()

    result = await worker.invoke(
        system="You are a concise assistant. Answer in one sentence.",
        user="What is event sourcing?",
        idempotency_key="example-01:hello",
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

    payload = result.unwrap()
    print(f"model:   {payload['model']}")
    print(f"latency: {payload['latency_ms']:.1f}ms")
    print(f"tokens:  {payload['usage']['total_tokens']}")
    print(f"cost:    {payload['cost_usd']}  (None for local models)")
    print(f"text:    {payload['text']}")


if __name__ == "__main__":
    asyncio.run(main())
