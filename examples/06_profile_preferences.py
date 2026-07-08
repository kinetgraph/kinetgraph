# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
06 — Profile-aware LLM responses.

Demonstrates how to use a long-lived `ProfileState` to
condition the LLM output:

  - The user has a `language`, `tone`, and `verbosity`
    preference stored in the profile.
  - `PersonalizedRole` reads the profile, builds a
    system-prompt prefix, and asks the LLM to respond
    accordingly.
  - The same task is asked twice, with different
    profiles, to show how the LLM output changes.

The profile is durable (no TTL by default). The EventLog
is the source of truth; the Redis Hash is a cache.

Configuration is loaded from env / `.env`. Requires Redis
running on localhost:6379.

Run:
    docker run -d -p 6379:6379 --name fmh-redis redis
    python examples/06_profile_preferences.py
"""

from __future__ import annotations

import asyncio
import redis.asyncio as aioredis

from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._memory import RedisProfileStorage
from kntgraph.memory.profile import ProfileManager
from kntgraph.stream.event_log import EventLog

from kntgraph.agents.config import LLMConfig, load_env
from kntgraph.agents.roles import PersonalizedRole
from kntgraph.agents.tools import LiteLLMTool


TASK = "Explique o que é event sourcing em uma frase."


async def setup_profile(
    profile: ProfileManager,
    tenant_id: str,
    user_id: str,
    *,
    language: str,
    tone: str,
    verbosity: str,
) -> None:
    """Create or update a profile with the given preferences."""
    await profile.create(
        tenant_id=tenant_id,
        user_id=user_id,
        preferences={"language": language, "tone": tone, "verbosity": verbosity},
    )
    # `create` is idempotent for the initial state, but
    # subsequent preference changes go through set_preference.
    await profile.set_preference(tenant_id, user_id, "language", language)
    await profile.set_preference(tenant_id, user_id, "tone", tone)
    await profile.set_preference(tenant_id, user_id, "verbosity", verbosity)


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

    redis = aioredis.from_url("redis://localhost:6379")
    log = EventLog(RedisEventLogAdapter(client=redis))
    profile_mgr = ProfileManager(
        event_log=log, storage=RedisProfileStorage(client=redis)
    )

    llm = LiteLLMTool(
        default_model=cfg.default_model,
        rate_limiter=cfg.rate_limiter(),
        cost_budget=cfg.cost_budget(),
        timeout_s=cfg.timeout_s,
    )
    role = PersonalizedRole(llm=llm)

    # Two users with different profiles.
    await setup_profile(
        profile_mgr,
        tenant_id="t-demo",
        user_id="u-formal-en",
        language="en",
        tone="formal",
        verbosity="low",
    )
    await setup_profile(
        profile_mgr,
        tenant_id="t-demo",
        user_id="u-casual-pt",
        language="pt-BR",
        tone="casual",
        verbosity="high",
    )

    for user_id in ("u-formal-en", "u-casual-pt"):
        p = await profile_mgr.read("t-demo", user_id)
        if p is None:
            print(f"profile not found: {user_id}")
            continue
        print(
            f"\n# {user_id} "
            f"(lang={p.preferences.get('language')}, "
            f"tone={p.preferences.get('tone')}, "
            f"verbosity={p.preferences.get('verbosity')})"
        )
        # `think=False` is forwarded to LiteLLM via the
        # Role's **invoke_kwargs; required for thinking
        # Ollama models so the answer lands in `content`
        # instead of `reasoning`.
        r = await role.respond(p, TASK, think=False)
        if r.is_err():
            print(f"  ERR: {r.err_value()}")
        else:
            print(f"  > {r.unwrap()}")

    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
