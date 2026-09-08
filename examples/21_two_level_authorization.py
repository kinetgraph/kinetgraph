# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 21: Two-level authorization via a business system (ADR-060 §3.0).

This example demonstrates the **two-level authorization
model** applied to a **business system** that encapsulates
a tool. The policy is: *a tool is accessed only through
its owning system* — the caller (the intent) talks to the
system, never to the tool directly.

  - **Gate 2 — persona of the agent** (ADR-060 §3.0):
    the agent's ``RoleComponent.allowed_tools`` must admit
    the **system** (e.g. ``"chat"``). Enforced by the
    system before it emits ``tool.<name>.requested``. A
    persona that forbids the system blocks the request at
    the source (the system emits ``intent.validation_failed``
    instead).

  - **Gate 1 — RBAC of the request** (ADR-017, ADR-066
    §4.1): the ``ToolACL`` attached to the registered
    tool must admit the inbound ``Principal``. Enforced by
    the ``WorkerManager`` before it consumes a worker slot.
    A principal whose level is too low, or whose tenant
    does not match a pinned tool, is denied.

The two gates are **independent and sequential**: a request
that survives gate 2 still has to pass gate 1. A higher
RBAC level on the request does NOT bypass a persona that
forbids the system, and a persona that admits the system
does NOT bypass a tenant-pinned ACL.

## The business system

``ChatSystem`` is a ``WorldSystem`` that owns the
``chat_llm`` tool. The intent maps to the **system**
(``"chat"``), not to the tool. The system:

  1. reads the agent's ``RoleComponent`` and checks
     ``has_tool_access(role, "chat")`` (gate 2 on the
     system name);
  2. if allowed, emits ``tool.chat_llm.requested`` (the
     tool is internal to the system);
  3. reacts to ``tool.chat_llm.completed`` and emits
     ``chat.reply.generated``.

Because the tool is internal, renaming or swapping the
underlying tool does not change the caller's contract —
the caller always talks to ``"chat"``.

## Personas are fixed per agent

Each agent carries a fixed ``RoleComponent`` projected
from its ``role.swapped`` event. The example uses three
agents with distinct personas so the two gates are
demonstrated without mutating a persona at runtime:

  - ``tenant-a.assistant`` — persona admits ``"chat"``.
  - ``tenant-a.atendente`` — persona forbids ``"chat"``.
  - ``tenant-b.assistant`` — persona admits ``"chat"``,
    but the principal is in ``tenant-b`` (gate 1 blocks).

## Architecture

```
[user.intent] ─► EventLog (Redis Stream)
       │
       ▼
  ReactiveDispatcher
       │
       │  Gate 2: ChatSystem reads RoleComponent,
       │          checks has_tool_access(role, "chat"),
       │          emits tool.chat_llm.requested
       │          (or intent.validation_failed)
       ▼
  ToolRouter → WorkerManager (ProcessPool)
       │
       │  Gate 1: ToolACL.check(principal) before invoke
       ▼
  [tool.chat_llm.completed] → EventLog → next tick
```

## Run with

    KNT_REDIS_FAKE=1 uv run python examples/21_two_level_authorization.py

or against a real Redis on localhost:6379 (see the
README for credentials). The example uses a mock LLM
worker so the round-trip runs without an external model.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from kntgraph.core.components.role import RoleComponent, has_tool_access
from kntgraph.core.event import (
    CorrelationContext,
    Event,
    correlation_middleware,
)
from kntgraph.core.result import Ok, Result
from kntgraph.core.world import World
from kntgraph.core.world.component import domain_component
from kntgraph.infra.redis import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.security import PrincipalLevel
from kntgraph.stream.event_log import EventLog
from kntgraph.tools.acl import ToolACL
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter
from kntgraph.tools.system import ToolAwareSystem
from kntgraph.tools.worker import tool_worker

from _lib.redis_or_fake import make_redis_client


# ---------------------------------------------------------------------------
# Event factories — centralise the wire format of the events the
# business system emits, so the system and the tests share one shape.
# ---------------------------------------------------------------------------


def tool_requested(
    *,
    agent_id: str,
    tool_name: str,
    params: dict[str, Any],
    causation_id: str | None,
    correlation: CorrelationContext,
    producer_principal_id: str | None,
) -> Event:
    """Build a ``tool.<name>.requested`` event (ADR-036)."""
    return Event.create(
        event_type=f"tool.{tool_name}.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": tool_name, "params": params},
        causation_id=UUID(causation_id) if causation_id else None,
        correlation=correlation,
        producer_principal_id=producer_principal_id,
    )


def tool_failed(
    *,
    agent_id: str,
    tool_name: str,
    error: str,
    causation_id: str | None,
    correlation: CorrelationContext,
    extra: dict[str, Any] | None = None,
) -> Event:
    """Build a ``tool.<name>.failed`` event (ADR-036)."""
    data: dict[str, Any] = {"error": error, "tool": tool_name}
    if extra:
        data.update(extra)
    return Event.create(
        event_type=f"tool.{tool_name}.failed",
        agent_id=agent_id,
        event_class="domain",
        data=data,
        causation_id=UUID(causation_id) if causation_id else None,
        correlation=correlation,
    )


def chat_reply_generated(
    *,
    agent_id: str,
    request_event_id: str,
    reply: str,
    input_text: str,
    causation_id: str | None,
    correlation: CorrelationContext,
) -> Event:
    """Build a ``chat.reply.generated`` event (ADR-039)."""
    return Event.create(
        event_type="chat.reply.generated",
        agent_id=agent_id,
        event_class="domain",
        data={
            "request_event_id": request_event_id,
            "output": {"reply": reply, "follow_up_questions": []},
            "input": input_text,
        },
        causation_id=UUID(causation_id) if causation_id else None,
        correlation=correlation,
    )


# ---------------------------------------------------------------------------
# 1. The tool (I/O work — internal to the business system)
# ---------------------------------------------------------------------------


@tool_worker(name="chat_llm", description="Mock LLM: deterministic reply.")
class MockChatLlmWorker:
    """A mock LLM worker so the round-trip runs without an
    external model. Returns a deterministic reply."""

    async def invoke(
        self,
        system: str,
        user: str,
        *,
        idempotency_key: str,
        think: bool = False,
        **kwargs: Any,
    ) -> Result[dict[str, Any], Exception]:
        await asyncio.sleep(0.01)
        return Ok(
            {
                "text": '{"reply": "[mock reply to: '
                + user[:40]
                + ']", "follow_up_questions": []}',
                "model": "mock",
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                "finish_reason": "stop",
                "cost_usd": 0.0,
                "latency_ms": 10.0,
            }
        )


# ---------------------------------------------------------------------------
# 2. The business system (owns the tool; gate 2 on the system name)
# ---------------------------------------------------------------------------


@domain_component("role.swapped")
@dataclass(frozen=True, slots=True)
class DemoRoleComponent(RoleComponent):
    """A ``RoleComponent`` projected from ``role.swapped`` events.

    Registered via ``@domain_component`` so the default fold
    materialises it on the agent's view when a ``role.swapped``
    event lands — the same way the framework projects any
    ``DomainComponent`` (ADR-059). The system reads it by class
    to enforce gate 2.
    """


class ChatSystem(ToolAwareSystem):
    """
    A business system that owns the ``chat_llm`` tool.

    The intent maps to the **system** (``"chat"``), not to
    the tool. Gate 2 (ADR-060 §3.0) checks
    ``has_tool_access(role, "chat")`` — the persona must
    admit the system. When allowed, the system emits
    ``tool.chat_llm.requested`` (the tool is internal);
    when forbidden, it emits ``intent.validation_failed``.
    """

    SYSTEM_NAME = "chat"
    TOOL_NAME = "chat_llm"
    REQUEST_EVENT_TYPE = "user.intent"
    GENERATED_EVENT_TYPE = "chat.reply.generated"

    def __init__(self, *, persona: str = "") -> None:
        self._persona = persona
        self._pending_inputs: dict[str, str] = {}
        self._pending_agents: dict[str, str] = {}
        self._last_seen_event_id: dict[str, str] = {}

    def __call__(self, world: World) -> list[Event]:
        events: list[Event] = []
        for agent_id, view in world.views.items():
            if not isinstance(view.components, dict):
                continue
            last_eid = view.last_event_id
            if self._last_seen_event_id.get(agent_id) == last_eid:
                continue
            self._last_seen_event_id[agent_id] = last_eid
            # React to a completed tool call: emit the generated reply.
            events.extend(self._consume_completions(agent_id, view))
            # Only react to the trigger (user.intent), not to
            # every domain event (role.swapped, tool.*.failed, ...).
            if view.domain_phase != self.REQUEST_EVENT_TYPE:
                continue
            # Gate 2 on the SYSTEM name, not the tool.
            role = view.components.get(DemoRoleComponent)
            if not has_tool_access(role, self.SYSTEM_NAME):
                events.append(self._emit_access_denied(agent_id, view))
                continue
            # The intent maps to the system; the tool is internal.
            intent = view.components.get(self.REQUEST_EVENT_TYPE)
            if isinstance(intent, dict):
                message = intent.get("message") or intent.get("text")
                if message:
                    events.append(self._emit_request(agent_id, view, message))
        return events

    def _emit_access_denied(
        self,
        agent_id: str,
        view: "Any",
    ) -> Event:
        """Emit ``tool.chat_llm.failed`` with
        ``error="access_denied"`` when the persona does not
        admit the system (gate 2).

        The tool call is never dispatched, but the intent
        still terminates in a ``failed`` event so downstream
        consumers see a uniform "every intent ends in
        completed/failed" contract. The ``error`` field
        distinguishes the reason (access denied vs a real
        tool failure).
        """
        last_eid = view.last_event_id
        correlation = CorrelationContext(correlation_id=UUID(str(last_eid)))
        return tool_failed(
            agent_id=agent_id,
            tool_name=self.TOOL_NAME,
            error="access_denied",
            causation_id=last_eid,
            correlation=correlation,
            extra={"system": self.SYSTEM_NAME},
        )

    def _emit_request(
        self,
        agent_id: str,
        view: "Any",
        message: str,
    ) -> Event:
        """Emit ``tool.chat_llm.requested`` (the tool is
        internal to the system).

        The ``producer_principal_id`` is propagated from the
        triggering intent (via ``view.last_event_principal_id``)
        so the WorkerManager's gate-1 ACL check (ADR-066 §4.1)
        sees the inbound principal.
        """
        last_eid = view.last_event_id
        correlation = CorrelationContext(correlation_id=UUID(str(last_eid)))
        system_prompt = (
            f"{self._persona}\n\nVocê é um assistente técnico conciso."
            if self._persona
            else "Você é um assistente técnico conciso."
        )
        e = tool_requested(
            agent_id=agent_id,
            tool_name=self.TOOL_NAME,
            params={
                "system": system_prompt,
                "user": message,
            },
            causation_id=last_eid,
            correlation=correlation,
            producer_principal_id=view.last_event_principal_id,
        )
        self._pending_inputs[str(e.event_id)] = message
        self._pending_agents[str(e.event_id)] = agent_id
        return e

    def _consume_completions(
        self,
        agent_id: str,
        view: "Any",
    ) -> list[Event]:
        """Emit ``chat.reply.generated`` for each completed
        ``chat_llm`` call that this system dispatched."""
        out: list[Event] = []
        completions = view.components.get("tool_completions", {})
        if not isinstance(completions, dict):
            return out
        for rid, comp in completions.items():
            if comp.status != "completed":
                continue
            if rid not in self._pending_agents:
                continue
            pending_input = self._pending_inputs.pop(rid, None)
            self._pending_agents.pop(rid, None)
            if pending_input is None:
                continue
            result = comp.result or {}
            text = result.get("text", "")
            out.append(
                chat_reply_generated(
                    agent_id=agent_id,
                    request_event_id=rid,
                    reply=text,
                    input_text=pending_input,
                    causation_id=rid,
                    correlation=CorrelationContext.new(),
                )
            )
        return out


# ---------------------------------------------------------------------------
# 3. Main loop
# ---------------------------------------------------------------------------


def _banner(msg: str) -> None:
    print("\n" + "=" * 72)
    print(msg)
    print("=" * 72)


async def _spawn_agent(
    event_log: EventLog,
    agent_id: str,
    *,
    persona: str,
    instructions: str,
    allowed_systems: list[str],
) -> None:
    """Spawn an agent with a fixed persona by emitting a
    ``role.swapped`` event. The fold projects the
    ``DemoRoleComponent`` onto the agent's view."""
    await event_log.append(
        Event.create(
            event_type="role.swapped",
            agent_id=agent_id,
            event_class="domain",
            data={
                "persona": persona,
                "instructions": instructions,
                "allowed_tools": allowed_systems,
            },
            correlation=correlation_middleware.current(),
        )
    )


async def _emit_intent(
    event_log: EventLog,
    agent_id: str,
    message: str,
    *,
    producer_principal_id: str | None = None,
) -> None:
    """Emit a ``user.intent`` event for the given agent.

    The ``producer_principal_id`` is stamped on the event
    envelope (ADR-066 §4.1). The business system propagates it
    to the ``tool.requested`` via ``view.last_event_principal_id``,
    so the WorkerManager's gate-1 ACL check sees the caller.
    """
    await event_log.append(
        Event.create(
            event_type="user.intent",
            agent_id=agent_id,
            event_class="domain",
            data={"intent": "chat", "message": message},
            correlation=correlation_middleware.current(),
            producer_principal_id=producer_principal_id,
        )
    )


async def _read_events(event_log: EventLog, agent_id: str) -> list[Event]:
    """Read an agent's full event stream."""
    return await event_log.read(agent_id)


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    print("=== Two-level authorization via a business system (ADR-060 §3.0) ===")

    redis_client = make_redis_client()
    await redis_client.flushdb()

    event_log = EventLog(RedisEventLogAdapter(redis_client))

    # The business system owns the chat_llm tool.
    chat_system = ChatSystem(persona="Você é um assistente técnico conciso.")

    tool_router = ToolRouter(redis_client)
    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[chat_system],
        redis=redis_client,
        tool_router=tool_router,
        poll_interval=0.5,
        rediscovery_interval_seconds=0.5,
        fallback_poll_interval=0.5,
        wake_on_event=False,
    )

    worker_manager = WorkerManager(
        redis=redis_client,
        event_log=event_log,
    )
    # Gate 1: the tool is registered with a tenant-pinned
    # ACL. Only principals of ``tenant-a`` (or admins) may
    # invoke it.
    worker_manager.register(
        MockChatLlmWorker,
        acl=ToolACL(
            required_level=PrincipalLevel.agent,
            tenant_pinned=True,
            tenant_id="tenant-a",
        ),
    )

    print("\nStarting Dispatcher and Worker...")
    await dispatcher.start()
    await worker_manager.start()

    correlation_middleware.start(metadata={"example": "21"})
    try:
        # ------------------------------------------------------------------
        # Spawn the three agents with fixed personas.
        # ------------------------------------------------------------------
        _banner("Spawning agents with fixed personas")
        await _spawn_agent(
            event_log,
            "tenant-a.assistant",
            persona="assistente",
            instructions="Acesso ao sistema chat.",
            allowed_systems=["chat"],
        )
        await _spawn_agent(
            event_log,
            "tenant-a.atendente",
            persona="atendente",
            instructions="Sem acesso ao sistema chat.",
            allowed_systems=["other_system"],
        )
        await _spawn_agent(
            event_log,
            "tenant-b.assistant",
            persona="assistente",
            instructions="Acesso ao sistema chat.",
            allowed_systems=["chat"],
        )
        # Wait for the folds to project the personas AND for
        # the dispatcher's rediscovery to pick up the new
        # agents (rediscovery_interval_seconds=0.5).
        await asyncio.sleep(2.5)

        # ------------------------------------------------------------------
        # Scenario 1: persona admits 'chat' + principal in tenant-a.
        # Both gates pass → the tool runs (via the system).
        # ------------------------------------------------------------------
        _banner("Scenario 1: persona admits 'chat' + principal in tenant-a → ALLOWED")
        await _emit_intent(
            event_log,
            "tenant-a.assistant",
            "Olá! Quem é você?",
            producer_principal_id="tenant-a.assistant",
        )
        await asyncio.sleep(1.5)
        events = await _read_events(event_log, "tenant-a.assistant")
        types = [e.event_type for e in events]
        print(f"  events: {types}")
        assert "tool.chat_llm.requested" in types, "gate 2 should allow the chat system"
        assert "chat.reply.generated" in types, (
            "gate 1 should allow the tenant-a principal"
        )

        # ------------------------------------------------------------------
        # Scenario 2: persona forbids the system → gate 2 blocks at the source.
        # ------------------------------------------------------------------
        _banner("Scenario 2: persona forbids 'chat' → gate 2 BLOCKS")
        await _emit_intent(
            event_log,
            "tenant-a.atendente",
            "Pode me ajudar?",
            producer_principal_id="tenant-a.atendente",
        )
        await asyncio.sleep(1.5)
        events = await _read_events(event_log, "tenant-a.atendente")
        types = [e.event_type for e in events]
        print(f"  events: {types}")
        assert "tool.chat_llm.failed" in types, "gate 2 should emit a failed event"
        assert "tool.chat_llm.requested" not in types, (
            "gate 2 should not emit the request"
        )

        # ------------------------------------------------------------------
        # Scenario 3: persona admits the system, but the principal is in
        # tenant-b → gate 1 blocks at the worker.
        # ------------------------------------------------------------------
        _banner(
            "Scenario 3: persona admits 'chat' + principal in tenant-b → gate 1 BLOCKS"
        )
        await _emit_intent(
            event_log,
            "tenant-b.assistant",
            "Teste tenant-b",
            producer_principal_id="tenant-b.assistant",
        )
        await asyncio.sleep(1.5)
        events = await _read_events(event_log, "tenant-b.assistant")
        types = [e.event_type for e in events]
        print(f"  events: {types}")
        assert "tool.chat_llm.requested" in types, "gate 2 should allow the chat system"
        assert "chat.reply.generated" not in types, (
            "gate 1 should block the tenant-b principal"
        )

    finally:
        correlation_middleware.clear()

    print("\nStopping components...")
    await dispatcher.stop()
    await worker_manager.stop()

    await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
