# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
05 — Conversational session with LLM.

Demonstrates end-to-end chat using:
  - SessionManager (fmh_backend.memory) for short-term
    conversational history, persisted as events in
    EventLog and cached in Redis JSON.
  - ChatRole (fmh_agents.roles) to generate the next
    assistant reply, given the session history and the
    new user message.

The flow per turn:
  1. Read the SessionState (cache hit; fold on miss).
  2. Call ChatRole.reply(session, new_user_message).
  3. On Ok, append both messages to the session
     (user message + assistant reply).
  4. Print the assistant's reply.

After all turns, the EventLog has the full history; the
session can be replayed to reconstruct state.

Configuration is loaded from env / `.env`. Requires Redis
running on localhost:6379.

Run:
    docker run -d -p 6379:6379 --name fmh-redis redis
    python examples/05_session_chat.py
"""

from __future__ import annotations

import asyncio
import redis.asyncio as aioredis

from kntgraph.core.event import correlation_middleware
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._memory import RedisSessionStorage
from kntgraph.memory.session import SessionManager
from kntgraph.stream.event_log import EventLog

from kntgraph.agents.config import LLMConfig, load_env
from kntgraph.agents.roles import ChatRole
from kntgraph.agents.tools import LiteLLMTool


# The conversation the user wants to have.
TURNS: list[str] = [
    "Olá, quem é você?",
    "Pode me dar um exemplo de event sourcing?",
    "Como isso se relaciona com CQRS?",
]


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

    redis = aioredis.from_url("redis://:redispassword@localhost:6379")
    log = EventLog(RedisEventLogAdapter(client=redis))
    session_mgr = SessionManager(
        event_log=log, storage=RedisSessionStorage(client=redis)
    )

    llm = LiteLLMTool(
        default_model=cfg.default_model,
        rate_limiter=cfg.rate_limiter(),
        cost_budget=cfg.cost_budget(),
        timeout_s=cfg.timeout_s,
    )
    chat = ChatRole(
        llm=llm,
        persona="Você é um assistente técnico conciso. "
        "Responda em português brasileiro. "
        "Seja claro e direto, com exemplos curtos.",
    )

    session_id = "demo-session-001"
    user_id = "u-demo"
    tenant_id = "t-demo"

    with correlation_middleware.scope(
        metadata={
            "example": "05",
            "session_id": session_id,
            "user_id": user_id,
            "tenant_id": tenant_id,
        }
    ):
        # Start the session (idempotent).
        await session_mgr.start(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            metadata={"channel": "demo", "language": "pt-BR"},
        )

        for user_msg in TURNS:
            print(f"\nuser: {user_msg}")

            # 1. Read the session (cache → fold on miss).
            state = await session_mgr.read(session_id)

            # 2. Generate the next reply. `think=False` is
            # forwarded to LiteLLM via the Role's
            # **invoke_kwargs; required for thinking Ollama
            # models so the answer lands in `content`
            # instead of `reasoning`.
            r = await chat.reply(state, user_msg, think=False)
            if r.is_err():
                print(f"  ERR: {r.err_value()}")
                continue

            reply = r.unwrap()
            print(f"assistant: {reply.reply}")
            if reply.follow_up_questions:
                for q in reply.follow_up_questions:
                    print(f"  suggested: {q}")

            # 3. Persist the turn (user + assistant).
            await session_mgr.append_message(
                session_id, role="user", content=user_msg
            )
            await session_mgr.append_message(
                session_id, role="assistant", content=reply.reply
            )

    # Inspect the final state.
    final = await session_mgr.read(session_id)
    print(f"\n# final session: {len(final.messages)} messages")
    for m in final.messages:
        print(f"  [{m['role']}] {m['content'][:80]}...")

    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
