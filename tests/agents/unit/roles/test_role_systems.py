# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Gate 2 (ADR-060 §3.0, ADR-066 §5 follow-up):
role-system authorisation via ``RoleComponent.allowed_tools``.

The :class:`_BaseRoleSystem` reads
``view.components[RoleComponent]`` before emitting a
tool request and short-circuits with
``intent.validation_failed`` when the target tool is
not in the allow-list. When the view does NOT carry
a ``RoleComponent``, the system falls back to the
legacy unconditional emission (opt-in enforcement).

The previous contents of this file (15 tests covering
``ChatRoleSystem``, ``PlannerRoleSystem``,
``SummarizerRoleSystem``, ``PersonalizedRoleSystem``,
and ``RuleBasedChatSystem``) were deleted on 2026-08-26.

Reason: they relied on a ``ReactiveDispatcher._fold_with_filter``
monkey-patch that simulated memory hydration
(``project_memory``) — a projection that the **production
dispatcher does not invoke**. The tests were passing
against a simulated dispatcher that does not match
production behaviour; the role systems they exercised
do not function in production either (no ``SessionComponent``
ever reaches the ``AgentView``). See the file's git
history for the deleted code; the bug is tracked in
the roadmap under "Project Memory composition in
production dispatcher" (future ADR).
"""

from __future__ import annotations

from uuid import UUID

from kntgraph.agents.role_systems._base import _BaseRoleSystem
from kntgraph.core.components.memory import SessionComponent
from kntgraph.core.components.role import (
    RoleComponent,
    has_tool_access,
)
from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import AgentView, World

from pydantic import BaseModel


class _OutputModel(BaseModel):
    pass


class _StubRoleSystem(_BaseRoleSystem):
    """A minimal role system that targets the
    ``chat_llm`` tool and reads ``user.intent`` events.

    Used to exercise the gate 2 check without
    pulling in the LLM-backed chat systems' heavy
    prompt machinery.
    """

    TOOL_NAME = "chat_llm"
    REQUEST_EVENT_TYPE = "user.intent"
    GENERATED_EVENT_TYPE = "chat.reply.generated"
    OUTPUT_MODEL = _OutputModel

    def _build_system_prompt(self) -> str:
        return "system"

    def _build_user_prompt(self, view, session, new_input: str) -> str:
        return new_input


def _make_world(
    *,
    user_intent_message: str = "hello",
    role: RoleComponent | None = None,
) -> World:
    """Build a World with one agent carrying the
    ``user.intent`` event (and an optional
    ``RoleComponent``). The ``last_event_id`` points
    to the ``user.intent`` event so the system
    treats it as a new request."""
    ctx = CorrelationContext.new()
    user_intent_ev = Event.create(
        event_type="user.intent",
        agent_id="agent-1",
        event_class="domain",
        data={"intent": "chat", "message": user_intent_message},
        correlation=ctx,
    )
    components: dict = {
        "user.intent": {"intent": "chat", "message": user_intent_message},
        SessionComponent: SessionComponent(
            session_id="agent-1",
            user_id="u",
            tenant_id="t",
            messages=(),
            context={},
            started_at=0.0,
            ended_at=None,
            intent_event_id=str(user_intent_ev.event_id),
        ),
        "last_event_id": str(user_intent_ev.event_id),
        "last_event_at": user_intent_ev.timestamp.timestamp(),
    }
    if role is not None:
        components[RoleComponent] = role
    view = AgentView(
        agent_id="agent-1",
        components=components,
        last_event_id=str(user_intent_ev.event_id),
        last_event_at=user_intent_ev.timestamp.timestamp(),
    )
    world = World.empty()
    world = world.with_event(user_intent_ev)
    # Replace the auto-generated view with our
    # hand-crafted one (carrying the optional
    # RoleComponent). The dispatcher's
    # ``_build_request_event`` reads the view that
    # is in ``world.views`` at call time; the
    # monkey-patch-free path here lets the test
    # build a deterministic view.
    world_with_view = World(
        tick=world.tick,
        storage=world.storage,
        views={"agent-1": view},
    )
    return world_with_view


def test_has_tool_access_permits_none_role() -> None:
    """``has_tool_access(None, ...)`` returns ``True``
    (the legacy fallback; the view does not carry
    a RoleComponent)."""
    assert has_tool_access(None, "chat_llm") is True


def test_has_tool_access_permits_listed_tool() -> None:
    role = RoleComponent(
        persona="chat",
        instructions="x",
        allowed_tools=["chat_llm"],
    )
    assert has_tool_access(role, "chat_llm") is True


def test_has_tool_access_denies_unlisted_tool() -> None:
    role = RoleComponent(
        persona="service",
        instructions="x",
        allowed_tools=["other_tool"],
    )
    assert has_tool_access(role, "chat_llm") is False


def test_role_component_in_view_with_chat_llm_in_allow_list_emits_tool_request() -> None:
    """A role with ``chat_llm`` in ``allowed_tools``
    emits a ``tool.chat_llm.requested`` event
    (gate 2 passes; the request goes through)."""
    role = RoleComponent(
        persona="chat",
        instructions="x",
        allowed_tools=["chat_llm"],
    )
    world = _make_world(role=role)
    events = _StubRoleSystem()(world)
    assert len(events) == 1
    assert events[0].event_type == "tool.chat_llm.requested"


def test_role_component_in_view_without_chat_llm_in_allow_list_emits_validation_failed() -> None:
    """A role that does NOT include ``chat_llm`` in
    ``allowed_tools`` causes the system to emit an
    ``intent.validation_failed`` event instead of a
    ``tool.chat_llm.requested`` event (gate 2
    blocks)."""
    role = RoleComponent(
        persona="service",
        instructions="x",
        allowed_tools=["other_tool"],
    )
    world = _make_world(role=role)
    events = _StubRoleSystem()(world)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "intent.validation_failed"
    assert event.data["reason"] == "role_does_not_allow_tool"
    assert event.data["tool"] == "chat_llm"
    assert event.data["role_persona"] == "service"
    # The event's correlation_id matches the
    # request event's eid (the user.intent) so
    # the SSE endpoint can correlate the
    # validation failure with the user input.
    assert event.correlation.correlation_id == UUID(
        world.views["agent-1"].last_event_id
    )


def test_no_role_component_in_view_emits_tool_request_legacy_path() -> None:
    """When the view does NOT carry a RoleComponent,
    the system falls back to the legacy
    unconditional emission (gate 2 is opt-in)."""
    world = _make_world()  # role=None
    events = _StubRoleSystem()(world)
    assert len(events) == 1
    assert events[0].event_type == "tool.chat_llm.requested"


def test_role_component_with_empty_allow_list_always_denies() -> None:
    """A role with ``allowed_tools=[]`` (the
    default) is forbidden to request any tool.
    This is the strictest configuration: a
    deployment that installs a RoleComponent but
    forgets to populate the allow-list will see
    every request denied."""
    role = RoleComponent(persona="locked", instructions="x")
    world = _make_world(role=role)
    events = _StubRoleSystem()(world)
    assert len(events) == 1
    assert events[0].event_type == "intent.validation_failed"
    assert events[0].data["role_persona"] == "locked"