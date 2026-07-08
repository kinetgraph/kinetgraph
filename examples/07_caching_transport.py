# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
07 — Caching LLM transport for at-most-once semantics.

The `LiteLLMTool` forwards `idempotency_key` (from the
caller — usually a Role) to the underlying transport via
kwarg. `CachingLLMTransport` is a decorator transport that
memoizes completions by that key.

Use case: when the same prompt is asked twice (e.g.
re-running a Role after a crash, or a session asking the
same follow-up question), the second call returns from
the cache without hitting the LLM provider.

Demonstrates:

  - Wrapping the default `LiteLLMTransportAdapter` with
    `CachingLLMTransport`.
  - The same Role called twice with the same inputs →
    second call is a cache hit.
  - The same Role called with different inputs → cache
    miss, distinct entry.
  - Inspecting `cache.metrics` to see hits/misses.

Configuration is loaded from env / `.env`.

Run:
    python examples/07_caching_transport.py
"""

from __future__ import annotations

import asyncio

from kntgraph.agents.config import LLMConfig, load_env
from kntgraph.agents.roles import SummarizerRole, Summary
from kntgraph.agents.tools import CachingLLMTransport, LiteLLMTool
from kntgraph.agents.tools.llm import LiteLLMTransportAdapter


SAMPLE_TEXT = """\
Event Sourcing is a pattern where state changes are stored
as a sequence of events. Instead of persisting the current
state of an entity, the application persists each
transformation as a discrete event. The current state is
derived by folding the event stream.
"""


async def main() -> None:
    load_env()
    cfg = LLMConfig.from_env()
    if cfg.default_model == "gpt-4o-mini":
        cfg = LLMConfig(
            default_model="ollama/qwen3.5:4b",
            rate_limit_rpm=cfg.rate_limit_rpm,
            cost_budget_per_hour_usd=cfg.cost_budget_per_hour_usd,
            timeout_s=60.0,
        )

    # Compose: cache wraps the real LiteLLM transport.
    # (LiteLLMTransportAdapter lives in tools.llm — it's the
    # default transport that LiteLLMTool uses internally;
    # we instantiate it here so we can wrap it.)
    inner = LiteLLMTransportAdapter()
    cache = CachingLLMTransport(inner, name="example-cache")

    llm = LiteLLMTool(
        default_model=cfg.default_model,
        rate_limiter=cfg.rate_limiter(),
        cost_budget=cfg.cost_budget(),
        timeout_s=cfg.timeout_s,
        transport=cache,
    )
    summarizer = SummarizerRole(llm=llm)

    # First call: cache miss → hits the LLM.
    # `think=False` is forwarded via the Role's
    # **invoke_kwargs; required for thinking Ollama
    # models.
    r1 = await summarizer.summarize(SAMPLE_TEXT, max_words=30, think=False)
    assert r1.is_ok()
    s1: Summary = r1.unwrap()
    print(f"[1] {s1.summary}")
    print(f"    metrics: {cache.metrics}")

    # Second call with the same input: cache hit.
    r2 = await summarizer.summarize(SAMPLE_TEXT, max_words=30, think=False)
    assert r2.is_ok()
    s2: Summary = r2.unwrap()
    print(f"\n[2] {s2.summary}")
    print(f"    metrics: {cache.metrics}")
    # Same content, but no second LLM call.
    assert s1.summary == s2.summary

    # Third call with different max_words: cache miss.
    r3 = await summarizer.summarize(SAMPLE_TEXT, max_words=80, think=False)
    assert r3.is_ok()
    s3: Summary = r3.unwrap()
    print(f"\n[3] {s3.summary}")
    print(f"    metrics: {cache.metrics}")
    # The key includes max_words, so this is a new entry.
    assert s1.summary != s3.summary

    print(
        f"\nfinal: hits={cache.metrics['hits']} "
        f"misses={cache.metrics['misses']} "
        f"size={cache.metrics['size']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
