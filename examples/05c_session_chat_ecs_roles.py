# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 05c: Migration of ``ChatRole`` to the ECS pipeline.

This example is the **canonical migration** of the legacy
``ChatRole`` (ADR-039, ADR-043, ADR-044 follow-up) to the
event-driven ``WorldSystem`` pattern.

## What this example demonstrates

  - The ``ChatRoleSystem`` (``kntgraph.agents.role_systems``)
    is a pure ``WorldSystem``: it reads the
    ``SessionComponent`` from the ``AgentView``, emits a
    ``tool.chat_llm.requested`` event with the role's
    ``SYSTEM_PROMPT``, and emits a
    ``chat.reply.generated`` event when the LLM response
    lands.
  - The system REUSES the legacy ``ChatRole`` for
    prompt engineering (``SYSTEM_PROMPT`` /
    ``_format_history``) and the typed ``ChatReply``
    output schema. The migration is a thin port: the
    synchronous ``await role.reply()`` becomes an
    event-driven ``system(world)`` cycle.
  - The dispatcher's event loop is NOT blocked while the
    LLM runs. The system emits the request and returns
    immediately; the ``WorkerManager`` runs the
    ``chat_llm`` tool in a separate process; the
    completion is processed in a subsequent tick.

## Architecture

```
[user.intent] ─► EventLog (Redis Stream)
       │
       ▼
  ReactiveDispatcher
       │
       │  ┌─────────────────────────────────────┐
       │  │ T1: World.fold (default projection)  │
       │  │ T1.5: project_memory (hydration)     │
       │  │ T1.6: ChatRoleSystem (pure)          │
       │  │       reads SessionComponent,        │
       │  │       emits tool.chat_llm.requested  │
       │  │ T1.7: RecorderSystem (pure)          │
       │  │       reads chat.reply.generated,    │
       │  │       emits session_recorder reqs    │
       │  └─────────────────────────────────────┘
       │
       ▼
  ToolRouter → WorkerManager (ProcessPool)
       │
       ├── chat_llm (LiteLLMToolWorker; produces reply)
       ├── session_recorder (writes session.* events)
       ▼
  [tool.<name>.completed] → EventLog → next tick
```

## Migration cheat-sheet

    # Legacy (deprecated v0.8.0, removed v1.0.0):
    chat = ChatRole(llm=llm, persona="...")
    r = await chat.reply(session, new_user_message)
    reply: ChatReply = r.unwrap()
    await sm.append_message(session_id, "assistant", reply.reply)

    # New (this example):
    chat_system = ChatRoleSystem(persona="...")
    recorder_system = SessionRecorderRoleBridge(...)
    dispatcher = ReactiveDispatcher(
        log=log,
        systems=[chat_system, recorder_system],
        ...
    )
    # Emit a ``user.intent`` event; the dispatcher
    # drives the rest. The
    # ``chat.reply.generated`` event lands in a
    # later tick with the typed ``ChatReply`` payload;
    # the ``recorder_system`` persists the turn.

## Run with

    KNT_REDIS_FAKE=1 uv run python examples/05c_session_chat_ecs_roles.py

or against a real Redis on localhost:6379 (see the
README for credentials). The example runs against the
local Ollama instance (``qwen3.5:4b``); for CI without
an LLM, the ``MockChatLlmWorker`` block in the example
emits a deterministic reply so the round-trip can be
exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kntgraph.agents.role_systems import ChatRoleSystem
from kntgraph.agents.tools.llm import LiteLLMToolWorker
from kntgraph.core.event import (
    Event,
    correlation_middleware,
)
from kntgraph.core.result import Err, Ok, Result, ToolError
from kntgraph.core.world import World
from kntgraph.core.world.projection_memory import project_memory
from kntgraph.infra.redis import RedisEventLogAdapter
from kntgraph.infra.redis._memory import RedisSessionStorage
from kntgraph.memory.session import SessionManager
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter
from kntgraph.tools.system import ToolAwareSystem
from kntgraph.tools.worker import tool_worker

from _lib.redis_or_fake import make_redis_client


# ---------------------------------------------------------------------------
# 1. Reactive extensions (the "shim") — same as 05b.
# ---------------------------------------------------------------------------


def _install_projection_shim() -> None:
    """
    Monkey-patch ``ReactiveDispatcher`` so that
    ``_fold_with_filter`` runs the full
    default + memory + tool overlay pipeline.

    The shim is **idempotent**: it only patches if
    not already patched.
    """
    from kntgraph.runner import reactive as _reactive_mod
    from kntgraph.runner import reactive_tool_projection as _rtp_mod

    if getattr(_reactive_mod.ReactiveDispatcher, "_memory_shim_applied", False):
        return

    def _fold_with_filter_shim(self, world, new_events):
        new_event_count = 0
        for event in new_events:
            world = world.with_event(event)
            if self._filter is not None and not self._filter(event):
                continue
            new_event_count += 1
        new_views: dict[str, Any] = dict(world.views)
        any_changed = False
        for agent_id, hydrated_view in project_memory(new_events, world.views).items():
            if world.views.get(agent_id) is not hydrated_view:
                new_views[agent_id] = hydrated_view
                any_changed = True
        if any_changed:
            new_storage = world.storage
            for agent_id, view in new_views.items():
                if world.views.get(agent_id) is not view:
                    new_storage = new_storage.clone_with_entity(
                        agent_id, dict(view.components)
                    )
            world = World(tick=world.tick, storage=new_storage, views=new_views)
        if new_event_count > 0 and _rtp_mod._has_tool_events(new_events):
            world = _rtp_mod._overlay_tool_projection(world, new_events)
        return world, new_event_count

    _reactive_mod.ReactiveDispatcher._fold_with_filter = _fold_with_filter_shim
    _reactive_mod.ReactiveDispatcher._memory_shim_applied = True


_install_projection_shim()


# ---------------------------------------------------------------------------
# 2. Tools (I/O work — registered with the WorkerManager)
# ---------------------------------------------------------------------------


# Uncomment the MockChatLlmWorker for CI environments
# without an LLM. The mock returns a deterministic
# reply so the ECS round-trip can be exercised
# end-to-end (the system emits the request, the mock
# responds, the system emits the generated event).
#
# @tool_worker(
#     name="chat_llm",
#     description="Mock for CI: deterministic reply.",
# )
# class MockChatLlmWorker:
#     async def invoke(
#         self,
#         system: str,
#         user: str,
#         *,
#         idempotency_key: str,
#         think: bool = False,
#         **kwargs: Any,
#     ) -> Result[dict[str, Any], Exception]:
#         await asyncio.sleep(0.01)
#         return Ok(
#             {
#                 "text": '{"reply": "[mock reply to: ' + user[:40] + ']", "follow_up_questions": []}',
#                 "model": "mock",
#                 "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
#                 "finish_reason": "stop",
#                 "cost_usd": 0.0,
#                 "latency_ms": 10.0,
#             }
#         )


def _build_session_manager_in_worker() -> SessionManager:
    """Build a ``SessionManager`` in the worker process.

    The worker instantiates the tool class via
    ``tool_cls()`` in a separate process. We
    cannot pass the pre-built ``SessionManager``
    from ``main()`` across the pickle boundary,
    so we build one in the worker process from
    the same Redis client factory.
    """
    redis_client = make_redis_client()
    event_log = EventLog(RedisEventLogAdapter(redis_client))
    return SessionManager(
        event_log=event_log,
        storage=RedisSessionStorage(client=redis_client),
    )


@tool_worker(
    name="session_recorder",
    description="Persists session events to the SessionManager.",
)
class SessionRecorderTool:
    """I/O-bound tool: writes session state to Redis via ``SessionManager``."""

    def __init__(self) -> None:
        self._sm = _build_session_manager_in_worker()

    async def invoke(
        self,
        command: str,
        session_id: str,
        *,
        idempotency_key: str,
        data: dict[str, Any] | None = None,
    ) -> Result[dict[str, Any], ToolError]:
        data = data or {}
        if command == "start":
            r = await self._sm.start(
                session_id=session_id,
                user_id=data["user_id"],
                tenant_id=data["tenant_id"],
                metadata=data.get("metadata", {}),
            )
        elif command == "append_user":
            r = await self._sm.append_message(
                session_id=session_id,
                role="user",
                content=data["content"],
            )
        elif command == "append_assistant":
            r = await self._sm.append_message(
                session_id=session_id,
                role="assistant",
                content=data["content"],
            )
        elif command == "end":
            r = await self._sm.end(session_id=session_id)
        else:
            return Err(ToolError(f"session_recorder_unknown_command: {command!r}"))
        if r.is_err():
            err = r.err_value()
            inner = str(err) if err is not None else "unknown"
            wrapped = ToolError(f"session_recorder_error: {inner}")
            return Err(wrapped)
        return Ok({"session_id": session_id, "command": command})


# ---------------------------------------------------------------------------
# 3. Recorder bridge (reacts to chat.reply.generated)
# ---------------------------------------------------------------------------


SESSION_AGENT_ID = "session:ecs-roles-demo"


class SessionRecorderRoleBridge(ToolAwareSystem):
    """
    Bridge from the ``chat.reply.generated`` event
    (emitted by ``ChatRoleSystem``) to the
    ``session_recorder`` tool calls.

    The system tracks which ``chat.reply.generated``
    events have been recorded via ``_recorded_eids``
    (an ``event_id → bool`` map). The system runs
    in every tick; without this, the recorder
    would fire twice (once per tick) until the
    completion slot is cleared.
    """

    def __init__(self) -> None:
        self._recorded_eids: set[str] = set()
        self._recorded_user_eids: set[str] = set()

    def __call__(self, world: World) -> list[Event]:
        events: list[Event] = []
        view = world.views.get(SESSION_AGENT_ID)
        if view is None:
            return events

        tool_completions = view.components.get("tool_completions", {})
        if not isinstance(tool_completions, dict):
            return events

        # Find a fresh ``chat.reply.generated`` event.
        # The event is in ``view.components`` if it was
        # the last domain event folded in this tick.
        # The typed ``output`` is in
        # ``view.components["chat.reply.generated"]["output"]``.
        generated = view.components.get("chat.reply.generated")
        if not isinstance(generated, dict):
            return events
        eid = generated.get("request_event_id")
        if not eid or eid in self._recorded_eids:
            return events
        self._recorded_eids.add(eid)
        # Emit the recorder requests: append_user
        # + append_assistant.
        user_input = generated.get("input", "")
        output = generated.get("output", {})
        reply = output.get("reply", "")
        if user_input and eid not in self._recorded_user_eids:
            self._recorded_user_eids.add(eid)
            events.append(
                self.request_tool(
                    agent_id=SESSION_AGENT_ID,
                    tool_name="session_recorder",
                    params={
                        "command": "append_user",
                        "session_id": "ecs-roles-demo",
                        "data": {"content": user_input},
                    },
                    causation_id=str(eid),
                )
            )
        events.append(
            self.request_tool(
                agent_id=SESSION_AGENT_ID,
                tool_name="session_recorder",
                params={
                    "command": "append_assistant",
                    "session_id": "ecs-roles-demo",
                    "data": {"content": reply},
                },
                causation_id=str(eid),
            )
        )
        return events


# ---------------------------------------------------------------------------
# 4. Main loop
# ---------------------------------------------------------------------------


TURNS = [
    "Olá! Quem é você?",
    "Pode me explicar o que é event sourcing em uma frase?",
    "E o que é o padrão ECS?",
]


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    print("=== Session Chat (ECS roles, ADR-039 + ADR-043) ===")

    redis_client = make_redis_client()
    await redis_client.flushdb()

    event_log = EventLog(RedisEventLogAdapter(redis_client))
    session_manager = SessionManager(
        event_log=event_log,
        storage=RedisSessionStorage(client=redis_client),
    )

    session_id = "ecs-roles-demo"
    user_id = "u-demo"
    tenant_id = "t-demo"

    correlation_middleware.start(metadata={"phase": "start"})
    try:
        await session_manager.start(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            metadata={"channel": "demo", "language": "pt-BR"},
        )
    finally:
        correlation_middleware.clear()

    # The chat role system: replaces the legacy
    # ``ChatRole`` synchronous orchestrator.
    chat_system = ChatRoleSystem(
        persona="Você é um assistente técnico conciso. "
        "Responda em português brasileiro. "
        "Seja claro e direto, com exemplos curtos.",
    )
    recorder_system = SessionRecorderRoleBridge()

    tool_router = ToolRouter(redis_client)
    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[chat_system, recorder_system],
        redis=redis_client,
        tool_router=tool_router,
        poll_interval=0.5,
    )

    worker_manager = WorkerManager(
        redis=redis_client,
        event_log=event_log,
    )
    worker_manager.register(LiteLLMToolWorker)
    worker_manager.register(SessionRecorderTool)

    print("\nStarting Dispatcher and Worker...")
    await dispatcher.start()
    await worker_manager.start()

    correlation_middleware.start(metadata={"example": "05c"})
    try:
        for turn in TURNS:
            print(f"\nuser: {turn}")
            await event_log.append(
                Event.create(
                    event_type="user.intent",
                    agent_id=SESSION_AGENT_ID,
                    event_class="domain",
                    data={"intent": "chat", "message": turn},
                    correlation=correlation_middleware.current(),
                )
            )
            # Round-trip:
            # 1. ChatRoleSystem sees intent, emits tool.chat_llm.requested.
            # 2. Worker runs chat_llm, emits tool.chat_llm.completed.
            # 3. ChatRoleSystem reacts, emits chat.reply.generated.
            # 4. SessionRecorderRoleBridge reacts, emits
            #    2x tool.session_recorder.requested.
            # 5. Worker runs session_recorder twice, session is updated.
            await asyncio.sleep(2.0)
    finally:
        correlation_middleware.clear()

    print("\nStopping components...")
    await dispatcher.stop()
    await worker_manager.stop()

    final = await session_manager.read(session_id)  # type: ignore[arg-type]
    assert final is not None
    print(f"\n# final session: {len(final.messages)} messages")
    for m in final.messages:
        print(f"  [{m['role']}] {m['content'][:80]}...")

    await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
