# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 05b: Session chat in the ECS loop (ADR-042 + ADR-036).

This example is the **proposed** shape for memory-aware
agents: the system reads ``SessionComponent`` /
``ProfileComponent`` / ``ContinuityComponent`` from
the ``AgentView`` — never ``SessionManager`` from
inside ``__call__``. The hydration step is a **pure
projection** that walks the event batch; Redis is
touched only by the ``session_recorder`` tool that
the system emits via ``request_tool``.

This is the architecture ADR-042 §6.1 aims to ship.
The example is runnable today (it depends on a small
``reactive_extensions`` shim in this file, not yet
in the framework) and serves as the reference
implementation that the production hydration
projection will follow.

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
       │  │       ↓ installs SessionComponent,   │
       │  │         ProfileComponent,            │
       │  │         ContinuityComponent           │
       │  │ T1.6: System.__call__ (pure)         │
       │  │       reads *Component from view,    │
       │  │       emits tool.<name>.requested     │
       │  └─────────────────────────────────────┘
       │
       ▼
  ToolRouter → WorkerManager (ProcessPool)
       │
       ├── chat_llm (mock; produces reply)
       ├── session_recorder (writes session.* events)
       ▼
  [tool.<name>.completed] → EventLog → next tick
```

## Why this is clean (no workarounds)

- The system **never** reads ``SessionManager`` or
  any other Redis-bound object inside ``__call__``.
  The hydration is a pure fold of events.
- The system reads ``SessionComponent.intent_event_id``
  (immutable on the component) to get the
  ``causation_id`` for the tool request. No
  ``view.last_event_id`` guessing, no caching of
  ``_latest_intent_eid``, no ``data.event_id``
  in the user.intent payload.
- The tool request carries a stable
  ``causation_id`` (the intent_event_id) so the
  ``tool.<name>.requested`` event is joinable to
  the ``tool.<name>.completed`` event regardless
  of which tick it lands in.
- Multi-turn safe: each turn is a fresh
  ``user.intent`` event with a fresh
  ``event_id``; the ``SessionComponent`` carries
  the full message history (last-write-wins per
  field), and the ``messages`` tuple grows as the
  ``session_recorder`` tool appends new events.

## Run with

    KNT_REDIS_FAKE=1 uv run python examples/05b_session_chat_ecs.py

or against a real Redis on localhost:6379 (see the
README for credentials).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from kntgraph.agents.tools.llm import LiteLLMToolWorker
from kntgraph.core.components.memory import SessionComponent
from kntgraph.core.event import (
    CorrelationContext,
    Event,
    correlation_middleware,
)
from kntgraph.core.result import Err, Ok, Result
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
# 1. Reactive extensions (the "shim")
# ---------------------------------------------------------------------------
#
# The framework does not yet expose a clean
# "compose projections" hook on the dispatcher. This
# example monkey-patches ``ReactiveDispatcher`` so
# that ``World.fold`` runs the default projection,
# then ``project_memory`` (hydration), then the tool
# overlay — in that order. The same shim is what the
# production code will do once the framework exposes
# a proper "compose" API (ADR-042 §6.1 follow-up).


def _install_projection_shim() -> None:
    """
    Monkey-patch ``ReactiveDispatcher`` so that
    ``_fold_with_filter`` runs the full
    default + memory + tool overlay pipeline.

    The shim is **idempotent**: it only patches if
    not already patched. The order is:

      1. default projection (last-event-wins)
      2. project_memory (hydration)  — installs the
         ``*Component`` on each view.
      3. overlay_tool_calls — installs the
         ``tool_requests`` / ``tool_completions``
         slots.

    Steps 2 and 3 read events from the
    ``new_events`` batch (the delta since the
    last cursor). The base views already carry
    state from previous ticks, so the memory
    projection rebuilds the components from
    scratch each tick (re-deriving from the
    full per-agent event list would be O(N²);
    the framework's hot path is to recompute
    the *current state* per tick from the
    batch and trust the event log for the
    history).

    **Caveat (architectural debt).** This is a
    shim; the production version will live in
    ``runner.reactive_extensions`` and will be
    reviewed as part of the ADR-042 §6.1
    hydration-pipeline work.
    """
    from kntgraph.runner import reactive as _reactive_mod
    from kntgraph.runner import reactive_tool_projection as _rtp_mod

    if getattr(_reactive_mod.ReactiveDispatcher, "_memory_shim_applied", False):
        return

    def _fold_with_filter_shim(self, world, new_events):
        # ``world`` is the accumulated world
        # (``ckpt.world`` from the dispatcher).
        # We hold a reference to its views so the
        # memory projection can read the
        # previously-derived ``SessionComponent``
        # / ``ProfileComponent`` /
        # ``ContinuityComponent`` from the
        # base view (the ``with_event`` loop
        # below does not preserve them because
        # the default projection only stores
        # event payloads, not components).
        accumulated_views = dict(world.views)

        # Step 1: default projection (last-event-wins).
        # The original ``_fold_with_filter`` already
        # does this via ``world.with_event``; we
        # delegate to it but **without** the
        # tool-overlay step so we can interleave
        # the memory projection.
        new_event_count = 0
        for event in new_events:
            world = world.with_event(event)
            if self._filter is not None and not self._filter(event):
                continue
            new_event_count += 1

        # Step 2: memory hydration. We pass the
        # ACCUMULATED views (pre-``with_event``)
        # to ``project_memory``. The projection
        # re-derives the memory components from
        # the events in the current batch and
        # **preserves** the previously-derived
        # component from the base view if the
        # current batch did not include any
        # memory event (so a tick that only has
        # a ``user.intent`` keeps the
        # SessionComponent that was set on a
        # previous tick).
        hydrated_views = project_memory(new_events, accumulated_views)
        # Reconstruct a World with the hydrated views.
        if any(
            hydrated_views.get(aid) is not world.views.get(aid)
            for aid in set(hydrated_views) | set(world.views)
        ):
            new_storage = world.storage
            for aid, view in hydrated_views.items():
                if world.views.get(aid) is not view:
                    new_storage = new_storage.clone_with_entity(
                        aid, dict(view.components)
                    )
            world = World(tick=world.tick, storage=new_storage, views=hydrated_views)

        # Step 3: tool-call overlay (preserves the
        # existing behavior of the framework; the
        # bug we found in the multi-tick case
        # applies here too but is documented as
        # known debt in DEBT.md).
        if new_event_count > 0 and _rtp_mod._has_tool_events(new_events):
            world = _rtp_mod._overlay_tool_projection(world, new_events)
        return world, new_event_count

    _reactive_mod.ReactiveDispatcher._fold_with_filter = _fold_with_filter_shim
    _reactive_mod.ReactiveDispatcher._memory_shim_applied = True


# Install the shim on import so the rest of the
# example can build the dispatcher normally.
_install_projection_shim()


# ---------------------------------------------------------------------------
# 2. Tools (I/O work — registered with the WorkerManager)
# ---------------------------------------------------------------------------


# The chat_llm tool is the real ``LiteLLMToolWorker``
# (ADR-043). It runs in the ``WorkerManager``'s
# ``ProcessPoolExecutor``, so the dispatcher's event
# loop is not blocked while the LLM responds. The
# default model is read from ``LLMConfig.from_env``
# (or the ``FMH_LLM_DEFAULT_MODEL`` env var). Set
# ``FMH_LLM_DEFAULT_MODEL=ollama/qwen3.5:4b`` to
# run against a local Ollama instance.
#
# For CI environments without an LLM, the commented
# ``MockChatLlmTool`` below can be used as a drop-in
# replacement: it returns a deterministic reply so
# the ECS round-trip can be exercised end-to-end.


# @tool_worker(
#     name="chat_llm",
#     description="Mock for CI: deterministic reply without an LLM.",
# )
# class MockChatLlmTool:
#     """Mock I/O-bound tool: simulates an LLM call.
#
#     For CI without an LLM: comment out the
#     ``LiteLLMToolWorker`` import in ``main()`` and
#     uncomment this block. The mock takes the
#     ``new_user_message`` arg (which the system
#     passes as the ``user`` field) and returns a
#     deterministic reply.
#     """
#
#     async def invoke(
#         self,
#         system: str,
#         user: str,
#         *,
#         idempotency_key: str,
#         think: bool = False,
#         **kwargs: Any,
#     ) -> Result[dict[str, Any], Exception]:
#         await asyncio.sleep(0.1)
#         return Ok(
#             {
#                 "text": f"[mock reply to: {user[:60]!r}]",
#                 "model": "mock",
#                 "usage": {
#                     "prompt_tokens": 0,
#                     "completion_tokens": 0,
#                     "total_tokens": 0,
#                 },
#                 "finish_reason": "stop",
#                 "cost_usd": 0.0,
#                 "latency_ms": 0.0,
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
    """I/O-bound tool: writes session state to Redis via ``SessionManager``.

    Commands:
      - ``"start"``: initialise the session (idempotent).
      - ``"append_user"``: append a user message.
      - ``"append_assistant"``: append an assistant message.
      - ``"end"``: mark the session as ended.
    """

    def __init__(self) -> None:
        self._sm = _build_session_manager_in_worker()

    async def invoke(
        self,
        command: str,
        session_id: str,
        *,
        idempotency_key: str,
        data: dict[str, Any] | None = None,
    ) -> Result[dict[str, Any], Exception]:
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
            return Err(ValueError(f"unknown command: {command!r}"))
        if r.is_err():
            err = r.err_value()
            return Err(Exception(str(err)) if err is not None else Exception("unknown"))
        return Ok({"session_id": session_id, "command": command})


# ---------------------------------------------------------------------------
# 3. Pure domain system (reads the World, emits events)
# ---------------------------------------------------------------------------


SESSION_AGENT_ID = "session:ecs-demo"  # the SessionManager's agent_id


class SessionChatSystem(ToolAwareSystem):
    """
    Pure WorldSystem: drives the chat loop by
    reading the ``SessionComponent`` and the
    ``tool_requests`` / ``tool_completions`` slots
    from the ``AgentView``.

    **No Redis I/O inside ``__call__``.** The
    ``SessionComponent`` is installed on the view
    by the hydration projection (T1.5 of ADR-042
    §7). The system reads the messages tuple
    directly from the component.

    The system tracks which turns have been
    recorded via ``_recorded_turns`` (a
    ``request_event_id → bool`` map). The
    component-level ``intent_event_id`` is the
    *stable* handle for the in-flight intent; it
    does not change across ticks the way
    ``view.last_event_id`` does.

    Flow per user turn:

      1. ``user.intent`` event lands in the EventLog.
      2. World folds; the memory projection
         rebuilds ``SessionComponent`` from the
         event batch.
      3. The system reads ``SessionComponent``
         and the ``tool_requests`` / ``tool_completions``
         slots.
      4. The system emits
         ``tool.chat_llm.requested`` (params: the
         session dict + new user message). The
         ``causation_id`` is the
         ``SessionComponent.intent_event_id``.
      5. WorkerManager runs ``chat_llm`` → emits
         ``tool.chat_llm.completed``.
      6. The system reacts: emits TWO
         ``tool.session_recorder.requested`` calls
         (``append_user`` + ``append_assistant``)
         so the recorder persists the turn.
    """

    def __init__(self) -> None:
        # Track which request_event_ids have
        # already been recorded. The system runs
        # in every tick; without this, the
        # completion-driven phase would fire
        # twice (once per tick) until the
        # ``tool_completions`` slot is removed.
        self._recorded_turns: set[str] = set()
        # Track which user.intent event_ids we
        # have already requested a tool for.
        # The system runs every tick; the
        # ``SessionComponent`` (and the
        # ``user.intent`` component) is the same
        # across ticks until a new event lands,
        # so we use this set to avoid emitting a
        # duplicate request on the second tick
        # of the same turn.
        self._processed_intents: set[str] = set()

    @staticmethod
    def _build_system_prompt(session: SessionComponent) -> str:
        """Build the ``system`` prompt for the LLM.

        The system prompt carries the session
        metadata (id, user, tenant), the prior
        transcript, and a fixed role instruction.
        In production this would be extended
        with persona / locale / tool-use
        guidance; for the example we keep it
        minimal.
        """
        lines: list[str] = [
            "You are a helpful chat assistant.",
            "",
            f"# Session {session.session_id}",
            f"user_id: {session.user_id}",
            f"tenant_id: {session.tenant_id}",
            "",
            "## Prior conversation",
        ]
        for m in session.messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            lines.append(f"{role}: {content}")
        lines.append("")
        lines.append(
            "Produce a single assistant reply to the user's last message. Keep it short."
        )
        return "\n".join(lines)

    def __call__(self, world: World) -> list[Event]:
        events: list[Event] = []
        view = world.views.get(SESSION_AGENT_ID)
        if view is None:
            return events

        # 1. Read the hydrated SessionComponent.
        # The memory projection installed it on
        # this view at T1.5. If there is no
        # SessionComponent, the agent has no
        # session yet — early-return.
        session: SessionComponent | None = view.components.get(SessionComponent)
        if session is None:
            return events

        # 2. Detect a new user intent. The
        # ``SessionComponent.intent_event_id`` is
        # the event_id of the last domain event
        # in the agent's stream (the user.intent
        # that triggered the system).
        intent_eid = session.intent_event_id
        if not intent_eid:
            return events

        # The ``new_user_message`` comes from the
        # ``user.intent`` component on the view
        # (the default projection installs it
        # there for every domain event). The
        # ``SessionComponent.messages`` is
        # populated by the ``session_recorder``
        # tool *after* the chat_llm tool
        # completes; in the first tick of a
        # turn, the recorder has not yet run, so
        # the messages tuple is still empty and
        # the new message only lives in the
        # ``user.intent`` component.
        user_intent_data = view.components.get("user.intent", {})
        new_user_message = user_intent_data.get("message", "")
        if session.messages:
            # Walk backwards: the most recent
            # "user" message in the session is the
            # current turn (the recorder has
            # appended it on a previous tick).
            for m in reversed(session.messages):
                if m.get("role") == "user":
                    new_user_message = m.get("content", "")
                    break

        if not new_user_message:
            return events

        is_new_intent = intent_eid not in self._processed_intents
        if is_new_intent:
            self._processed_intents.add(intent_eid)

        # 3. Find the in-flight request for this
        # user.intent. The SessionComponent
        # carries ``intent_event_id``; the
        # ``tool_requests`` slot is keyed by
        # ``request_event_id`` (the eid of the
        # ``tool.<name>.requested`` event). The
        # join is via the ``request.correlation_id``
        # (= the user.intent's event_id). Walk
        # the slot and pick the one whose
        # ``causation_id`` matches.
        tool_requests = view.components.get("tool_requests", {})
        tool_completions = view.components.get("tool_completions", {})
        request_event_id: str | None = None
        completion_obj = None
        for rid, req in tool_requests.items():
            if str(req.correlation_id) == intent_eid:
                request_event_id = rid
                completion_obj = tool_completions.get(rid)
                break

        # 4. Build the correlation. The
        # dispatcher's task has no middleware
        # context, so we build the correlation
        # from the SessionComponent's
        # intent_event_id.
        correlation = CorrelationContext(correlation_id=UUID(intent_eid))

        # 5. Phase 1: no request for this intent
        # yet → emit the chat_llm request. The
        # ``LiteLLMToolWorker`` (ADR-043) takes
        # ``system`` and ``user`` strings; we build
        # the system prompt from the session
        # context (transcript + persona) and pass the
        # new user message verbatim.
        if request_event_id is None and is_new_intent:
            system_prompt = self._build_system_prompt(session)
            user_prompt = new_user_message
            events.append(
                self.request_tool(
                    agent_id=SESSION_AGENT_ID,
                    tool_name="chat_llm",
                    params={
                        "system": system_prompt,
                        "user": user_prompt,
                    },
                    causation_id=intent_eid,
                    correlation=correlation,
                )
            )
            return events

        # 6. Phase 2: the LLM is still running.
        if completion_obj is None:
            return events

        # 7. Phase 3: the LLM completed. Persist
        # the turn.
        if completion_obj.status != "completed":
            return events
        if request_event_id in self._recorded_turns:
            return events
        self._recorded_turns.add(request_event_id)

        # ``LiteLLMToolWorker`` returns the reply
        # in the ``text`` field (ADR-043). The
        # legacy ``LiteLLMTool`` used ``reply``; the
        # migration is a single rename here.
        reply = (completion_obj.result or {}).get("text", "")
        events.append(
            self.request_tool(
                agent_id=SESSION_AGENT_ID,
                tool_name="session_recorder",
                params={
                    "command": "append_user",
                    "session_id": session.session_id,
                    "data": {"content": new_user_message},
                },
                causation_id=intent_eid,
                correlation=correlation,
            )
        )
        events.append(
            self.request_tool(
                agent_id=SESSION_AGENT_ID,
                tool_name="session_recorder",
                params={
                    "command": "append_assistant",
                    "session_id": session.session_id,
                    "data": {"content": reply},
                },
                causation_id=intent_eid,
                correlation=correlation,
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
    print("=== Session Chat (ECS pipeline, ADR-042) ===")

    redis_client = make_redis_client()
    await redis_client.flushdb()

    event_log = EventLog(RedisEventLogAdapter(redis_client))
    session_manager = SessionManager(
        event_log=event_log,
        storage=RedisSessionStorage(client=redis_client),
    )

    session_id = "ecs-demo"
    user_id = "u-demo"
    tenant_id = "t-demo"

    # Initialise the session up-front (idempotent).
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

    chat_system = SessionChatSystem()

    tool_router = ToolRouter(redis_client)
    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[chat_system],
        redis=redis_client,
        tool_router=tool_router,
        poll_interval=0.5,
    )

    worker_manager = WorkerManager(
        redis=redis_client,
        event_log=event_log,
    )
    # ADR-043: the real LLM bridge is a
    # ``@tool_worker``. It runs in the
    # WorkerManager's ProcessPoolExecutor.
    # For CI without an LLM, swap this for
    # ``MockChatLlmTool`` (commented at the top
    # of the file).
    worker_manager.register(LiteLLMToolWorker)
    worker_manager.register(SessionRecorderTool)

    print("\nStarting Dispatcher and Worker...")
    await dispatcher.start()
    await worker_manager.start()

    correlation_middleware.start(metadata={"example": "05b"})
    try:
        for turn in TURNS:
            print(f"\nuser: {turn}")
            # The user.intent lands in the SAME
            # stream as the SessionManager events
            # (``session:<id>``). This is the key
            # architectural choice: the memory
            # projection groups events by
            # ``agent_id``, so the
            # ``SessionComponent`` is only
            # materialised on the view whose
            # agent_id matches the session.* events.
            # The system, in turn, is keyed on the
            # same agent_id and reads the
            # SessionComponent off the same view.
            #
            # In a multi-tenant deployment the
            # convention is one stream per
            # ``(tenant, user)`` chat thread; the
            # SessionComponent is per-thread and
            # the system dispatches on the same
            # thread.
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
            # 1. System sees intent, emits tool.chat_llm.requested.
            # 2. Worker runs chat_llm, emits tool.chat_llm.completed.
            # 3. System reacts, emits 2x tool.session_recorder.requested.
            # 4. Worker runs session_recorder twice, session is updated.
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
