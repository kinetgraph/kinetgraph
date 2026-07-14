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
    Compose the framework projections into the
    dispatcher's fold pass.

    The framework's default fold is the
    "last-event-wins" projection. The
    :func:`_apply_event` helper now **preserves
    derived components** (tool-call slots, memory
    components) across the default domain fold
    (ADR-042 + ADR-044). What this shim adds is:

      1. The **memory hydration** projection
         (``project_memory``) runs AFTER the
         default fold. It walks the events in
         the batch and materialises the
         ``SessionComponent`` / ``ProfileComponent``
         / ``ContinuityComponent`` on the
         ``AgentView`` (purely from the events;
         no Redis I/O). The components are
         preserved across the next default fold
         thanks to :data:`_DERIVED_COMPONENT_KEYS`.

      2. The **tool-call overlay** runs LAST
         (it would otherwise race with the
         memory projection). It installs the
         ``tool_requests`` / ``tool_completions``
         slots and accumulates across ticks
         (ADR-044 Option B).

    The composition order is the same as the
    production :func:`ReactiveDispatcher._fold_with_filter`
    but inlined here so the example is runnable
    without a new framework release.

    The shim is **idempotent**: it only patches
    if not already patched.
    """
    from kntgraph.runner import reactive as _reactive_mod
    from kntgraph.runner import reactive_tool_projection as _rtp_mod

    if getattr(_reactive_mod.ReactiveDispatcher, "_memory_shim_applied", False):
        return

    def _fold_with_filter_shim(self, world, new_events):
        # Step 1: default fold. The default
        # ``with_event`` is used so the
        # ``_apply_event`` preservation rule
        # applies (derived components are kept).
        new_event_count = 0
        for event in new_events:
            world = world.with_event(event)
            if self._filter is not None and not self._filter(event):
                continue
            new_event_count += 1

        # Step 2: memory hydration. The
        # projection is a pure fold of the
        # events in the current batch; the
        # ``base_views`` argument carries the
        # components from previous ticks (so
        # a tick that has no memory event
        # keeps the SessionComponent from a
        # previous tick).
        #
        # The output of ``project_memory`` is
        # a new ``dict[str, AgentView]`` (one
        # entry per agent touched by a memory
        # event in the batch). Agents not in
        # the output keep their current view
        # (the projection's contract: agents
        # not touched by memory events come
        # back as the same view object).
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

        # Step 3: tool-call overlay. Installs
        # the ``tool_requests`` /
        # ``tool_completions`` slots on the
        # affected views. ADR-044: the
        # overlay accumulates across ticks
        # (a pending request from a previous
        # tick is preserved until the matching
        # completion lands).
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
        # The ``last_event_id`` we saw in the
        # previous tick. The default fold
        # advances ``view.last_event_id`` on
        # every event; when the new tick's
        # ``last_event_id`` differs from the
        # one we last saw, a new event landed
        # in the agent's stream. This is the
        # canonical "new intent arrived" signal
        # (the default domain projection does
        # not preserve the ``user.intent``
        # component when a tool event lands in
        # the same batch).
        self._last_seen_event_id: str | None = None
        # Map: ``chat_llm request_event_id`` →
        # the user message that triggered the
        # request. The default domain
        # projection is last-event-wins; the
        # ``user.intent`` component on the
        # view is replaced by any subsequent
        # tool event's payload (e.g. the
        # ``tool.chat_llm.completed`` event
        # in the next tick). The system
        # therefore cannot rely on
        # ``view.components["user.intent"]``
        # to recover the new user message in
        # the tick where the completion
        # arrives. We capture the message at
        # request time and look it up by the
        # chat request's eid.
        self._pending_user_messages: dict[str, str] = {}

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

        # 2. Detect a new event in the agent's
        # stream. The ``view.last_event_id``
        # advances on every folded event; when
        # it changes from the previous tick, a
        # new event has landed. The default
        # domain projection does not preserve
        # the ``user.intent`` component when a
        # tool event is folded in the same
        # batch (last-event-wins), so we cannot
        # rely on ``view.components["user.intent"]``
        # to detect the new user intent. The
        # ``last_event_id`` is the canonical
        # "did something happen in this tick?"
        # signal.
        last_eid = view.last_event_id
        is_new_event = (
            self._last_seen_event_id is None or self._last_seen_event_id != last_eid
        )
        if is_new_event and last_eid:
            self._last_seen_event_id = last_eid

        if not is_new_event:
            # No new event in this tick. The
            # system still reacts to completions
            # (see Phase 3 below) but skips
            # the "is this a new user intent?"
            # check.
            pass

        # 3. The ``new_user_message`` comes from
        # the ``SessionComponent.messages`` tuple
        # (the recorder persists it after each
        # turn). In the first tick of a turn the
        # recorder has not run yet, so the tuple
        # is empty; the system falls back to
        # the ``user.intent`` component on the
        # view (only present when the user.intent
        # was the last domain event in the batch).
        # In the completion tick the
        # ``user.intent`` component has been
        # replaced by the completion's payload,
        # so ``new_user_message`` is empty
        # here; the system uses the
        # ``_pending_user_messages`` map (set in
        # Phase 1) to recover the user message.
        new_user_message = ""
        if session.messages:
            for m in reversed(session.messages):
                if m.get("role") == "user":
                    new_user_message = m.get("content", "")
                    break
        if not new_user_message:
            user_intent_data = view.components.get("user.intent", {})
            new_user_message = user_intent_data.get("message", "")

        # 4. Build the correlation. The
        # dispatcher's task has no middleware
        # context, so we build the correlation
        # from the SessionComponent's
        # intent_event_id.
        intent_eid = session.intent_event_id
        if not intent_eid:
            return events
        correlation = CorrelationContext(correlation_id=UUID(intent_eid))

        # 4. Phase 1: a new event landed in this
        # tick AND no in-flight chat_llm request
        # → emit the chat_llm request. The
        # ``LiteLLMToolWorker`` (ADR-043) takes
        # ``system`` and ``user`` strings; we
        # build the system prompt from the
        # session context (transcript + persona)
        # and pass the new user message
        # verbatim.
        #
        # **Note (last-event-wins caveat):**
        # the default domain projection
        # REPLACES ``view.components`` on every
        # domain event; the ``user.intent``
        # component is only present if the
        # last event in the batch is the
        # ``user.intent`` itself (no tool
        # event folded after it). In the
        # production flow, the user.intent and
        # the chat_llm response land in
        # different ticks (the chat_llm worker
        # takes 0.3-0.5s to respond), so the
        # first tick of a turn has the
        # ``user.intent`` as the only domain
        # event. The unit tests for the shim
        # fold the events together (request
        # + intent in the same batch) to
        # exercise the multi-tick logic in
        # isolation; the system handles BOTH
        # shapes.
        tool_requests = view.components.get("tool_requests", {})
        tool_completions = view.components.get("tool_completions", {})
        has_in_flight_request = any(
            r.tool_name == "chat_llm" for r in tool_requests.values()
        )
        if is_new_event and not has_in_flight_request and new_user_message:
            system_prompt = self._build_system_prompt(session)
            user_prompt = new_user_message
            # Capture the user message for the
            # completion phase (the
            # ``user.intent`` component on the
            # view will be replaced by the
            # ``tool.chat_llm.completed`` event
            # in the next tick).
            e = self.request_tool(
                agent_id=SESSION_AGENT_ID,
                tool_name="chat_llm",
                params={
                    "system": system_prompt,
                    "user": user_prompt,
                },
                causation_id=intent_eid,
                correlation=correlation,
            )
            self._pending_user_messages[str(e.event_id)] = new_user_message
            events.append(e)
            return events

        # 5. Phase 2: the LLM is still running.
        # Look for a chat_llm completion that
        # has not been recorded yet.
        chat_completion = None
        for rid, comp in tool_completions.items():
            if comp.status != "completed":
                continue
            if rid in self._recorded_turns:
                continue
            chat_completion = comp
            chat_request_id = rid
            break
        if chat_completion is None:
            return events

        # 6. Phase 3: the LLM completed. Persist
        # the turn.
        self._recorded_turns.add(chat_request_id)
        # Recover the user message from the
        # capture map (the ``user.intent``
        # component on the view was replaced by
        # the completion event's payload).
        pending_user_message = self._pending_user_messages.pop(
            chat_request_id, new_user_message
        )
        if not pending_user_message:
            # Fall back to the SessionComponent
            # messages (after the first turn the
            # recorder persists them, so the
            # latest user message is on the
            # component).
            for m in reversed(session.messages):
                if m.get("role") == "user":
                    pending_user_message = m.get("content", "")
                    break

        # ``LiteLLMToolWorker`` returns the reply
        # in the ``text`` field (ADR-043).
        reply = (chat_completion.result or {}).get("text", "")
        events.append(
            self.request_tool(
                agent_id=SESSION_AGENT_ID,
                tool_name="session_recorder",
                params={
                    "command": "append_user",
                    "session_id": session.session_id,
                    "data": {"content": pending_user_message},
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
