# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the ECS-shaped role systems
(``kntgraph.agents.role_systems``).

The systems are the **migration path** for the legacy
``ChatRole`` / ``PlannerRole`` / ``SummarizerRole`` /
``PersonalizedRole`` (ADR-039 + ADR-043 + ADR-044
follow-up). Each test drives the system through the
shim's fold logic (the same way the dispatcher would)
and asserts the emitted events.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.runner import reactive as _reactive_mod
from kntgraph.runner.reactive_tool_projection import _overlay_tool_projection


SESSION_AGENT_ID = "session:ecs-roles-test"


def _ctx() -> CorrelationContext:
    return CorrelationContext.new()


def _install_shim() -> None:
    """Install the same projection shim that examples
    05b / 05c use (default fold + memory hydration +
    tool overlay)."""
    from kntgraph.core.world.projection_memory import project_memory as _pm

    if getattr(_reactive_mod.ReactiveDispatcher, "_memory_shim_applied", False):
        return

    def _fold_with_filter_shim(self, world, new_events):
        new_event_count = 0
        for event in new_events:
            world = world.with_event(event)
            if self._filter is not None and not self._filter(event):
                continue
            new_event_count += 1
        new_views: dict = dict(world.views)
        any_changed = False
        for agent_id, hydrated_view in _pm(new_events, world.views).items():
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
        if new_event_count > 0 and any(
            e.event_type.startswith("tool.")
            and (
                e.event_type.endswith(".requested")
                or e.event_type.endswith(".completed")
                or e.event_type.endswith(".failed")
            )
            for e in new_events
        ):
            world = _overlay_tool_projection(world, new_events)
        return world, new_event_count

    _reactive_mod.ReactiveDispatcher._fold_with_filter = _fold_with_filter_shim
    _reactive_mod.ReactiveDispatcher._memory_shim_applied = True


@pytest.fixture(autouse=True)
def _shim():
    _install_shim()


def _make_session_event() -> Event:
    return Event.create(
        event_type="session.started",
        agent_id=SESSION_AGENT_ID,
        event_class="domain",
        data={
            "session_id": SESSION_AGENT_ID.removeprefix("session:"),
            "user_id": "u",
            "tenant_id": "t",
        },
        correlation=_ctx(),
    )


def _make_intent_event(message: str) -> Event:
    return Event.create(
        event_type="user.intent",
        agent_id=SESSION_AGENT_ID,
        event_class="domain",
        data={"intent": "chat", "message": message},
        correlation=_ctx(),
    )


def _make_completion_event(
    request_event_id: UUID,
    text: str,
    correlation_id: UUID,
    agent_id: str = SESSION_AGENT_ID,
) -> Event:
    return Event.create(
        event_type="tool.chat_llm.completed",
        agent_id=agent_id,
        event_class="domain",
        data={"text": text},
        correlation=CorrelationContext(correlation_id=correlation_id),
        causation_id=str(request_event_id),
    )


def _fold(world: World, events: list[Event]) -> World:
    inst = _reactive_mod.ReactiveDispatcher.__new__(_reactive_mod.ReactiveDispatcher)
    inst._filter = None
    new_world, _ = inst._fold_with_filter(world, events)
    return new_world


# ---------------------------------------------------------------------------
# ChatRoleSystem
# ---------------------------------------------------------------------------


def test_chat_role_system_emits_chat_llm_request_on_user_intent():
    """Tick 1: a ``user.intent`` event lands. The
    ``ChatRoleSystem`` reads the ``SessionComponent`` and
    emits a ``tool.chat_llm.requested`` event with the
    role's ``SYSTEM_PROMPT`` and the formatted transcript.
    """
    from kntgraph.agents.role_systems import ChatRoleSystem

    system = ChatRoleSystem(persona="be concise")
    world = _fold(World.empty(), [_make_session_event(), _make_intent_event("hi")])
    events = system(world)
    assert len(events) == 1
    assert events[0].event_type == "tool.chat_llm.requested"
    assert events[0].data["tool"] == "chat_llm"
    # The system_prompt is the ChatRole's SYSTEM_PROMPT.
    assert "conversation" in events[0].data["params"]["system"].lower()
    # The user_prompt is the formatted transcript
    # (includes the new user message + prior history).
    assert "hi" in events[0].data["params"]["user"]


def test_chat_role_system_emits_generated_event_on_completion():
    """Tick 2: a ``tool.chat_llm.completed`` event lands
    (the LLM response). The system parses the JSON
    reply into a ``ChatReply`` and emits a
    ``chat.reply.generated`` event.
    """
    from kntgraph.agents.role_systems import (
        EVENT_TYPE_CHAT_REPLY_GENERATED,
        ChatRoleSystem,
    )

    system = ChatRoleSystem()
    world = _fold(World.empty(), [_make_session_event(), _make_intent_event("hi")])
    request_event = system(world)[0]
    # The dispatcher would have appended the
    # request event to the EventLog; the next tick's
    # fold sees BOTH the request (in-flight) AND
    # the completion (just landed).
    reply_json = '{"reply": "Hello!", "follow_up_questions": ["How are you?"]}'
    completion = _make_completion_event(
        request_event.event_id,
        reply_json,
        UUID(str(request_event.correlation.correlation_id)),
    )
    world2 = _fold(world, [request_event, completion])
    events = system(world2)
    # The system emits a ``chat.reply.generated`` event
    # with the typed ``output`` payload.
    assert len(events) == 1
    assert events[0].event_type == EVENT_TYPE_CHAT_REPLY_GENERATED
    output = events[0].data["output"]
    assert output["reply"] == "Hello!"
    assert output["follow_up_questions"] == ["How are you?"]


def test_chat_role_system_does_not_re_emit_on_same_completion():
    """Calling the system twice on the same World (with
    the same completion) does NOT re-emit the generated
    event. The system deduplicates by request_event_id."""
    from kntgraph.agents.role_systems import ChatRoleSystem

    system = ChatRoleSystem()
    world = _fold(World.empty(), [_make_session_event(), _make_intent_event("hi")])
    request_event = system(world)[0]
    completion = _make_completion_event(
        request_event.event_id,
        '{"reply": "Hello!", "follow_up_questions": []}',
        UUID(str(request_event.correlation.correlation_id)),
    )
    world2 = _fold(world, [request_event, completion])
    # First call emits the generated event.
    events1 = system(world2)
    assert len(events1) == 1
    # Second call (same world) emits nothing.
    events2 = system(world2)
    assert events2 == []


# ---------------------------------------------------------------------------
# PlannerRoleSystem
# ---------------------------------------------------------------------------


def test_planner_role_system_emits_plan_request():
    """The ``PlannerRoleSystem`` reacts to a
    ``plan.request`` event and emits a
    ``tool.chat_llm.requested`` event."""
    from kntgraph.agents.role_systems import PlannerRoleSystem

    system = PlannerRoleSystem()
    plan_request = Event.create(
        event_type="plan.request",
        agent_id="agent-1",
        event_class="domain",
        data={"task": "Emitir NF-e para cliente X"},
        correlation=_ctx(),
    )
    world = _fold(World.empty(), [plan_request])
    events = system(world)
    assert len(events) == 1
    assert events[0].event_type == "tool.chat_llm.requested"
    assert events[0].data["tool"] == "chat_llm"
    assert "Emitir NF-e" in events[0].data["params"]["user"]


def test_planner_role_system_emits_plan_generated():
    """The ``PlannerRoleSystem`` parses the LLM's
    response into a ``Plan`` and emits a
    ``plan.generated`` event."""
    from kntgraph.agents.role_systems import (
        EVENT_TYPE_PLAN_GENERATED,
        PlannerRoleSystem,
    )

    system = PlannerRoleSystem()
    plan_request = Event.create(
        event_type="plan.request",
        agent_id="agent-1",
        event_class="domain",
        data={"task": "Emitir NF-e"},
        correlation=_ctx(),
    )
    world = _fold(World.empty(), [plan_request])
    request_event = system(world)[0]
    plan_json = (
        '{"goal": "Emitir NF-e",'
        ' "steps": [{"name": "validate", "description": "validate", "depends_on": []}],'
        ' "rationale": "simple", "risks": []}'
    )
    completion = _make_completion_event(
        request_event.event_id,
        plan_json,
        UUID(str(request_event.correlation.correlation_id)),
        agent_id="agent-1",
    )
    world2 = _fold(world, [request_event, completion])
    events = system(world2)
    assert len(events) == 1
    assert events[0].event_type == EVENT_TYPE_PLAN_GENERATED
    output = events[0].data["output"]
    assert output["goal"] == "Emitir NF-e"
    assert len(output["steps"]) == 1
    assert output["steps"][0]["name"] == "validate"


# ---------------------------------------------------------------------------
# SummarizerRoleSystem
# ---------------------------------------------------------------------------


def test_summarizer_role_system_emits_summary_request():
    """The ``SummarizerRoleSystem`` reacts to a
    ``summary.request`` event."""
    from kntgraph.agents.role_systems import SummarizerRoleSystem

    system = SummarizerRoleSystem()
    summary_request = Event.create(
        event_type="summary.request",
        agent_id="agent-1",
        event_class="domain",
        data={"text": "A long document..."},
        correlation=_ctx(),
    )
    world = _fold(World.empty(), [summary_request])
    events = system(world)
    assert len(events) == 1
    assert events[0].event_type == "tool.chat_llm.requested"
    assert "long document" in events[0].data["params"]["user"]


def test_summarizer_role_system_emits_summary_generated():
    from kntgraph.agents.role_systems import (
        EVENT_TYPE_SUMMARY_GENERATED,
        SummarizerRoleSystem,
    )

    system = SummarizerRoleSystem()
    summary_request = Event.create(
        event_type="summary.request",
        agent_id="agent-1",
        event_class="domain",
        data={"text": "A long document..."},
        correlation=_ctx(),
    )
    world = _fold(World.empty(), [summary_request])
    request_event = system(world)[0]
    summary_json = '{"summary": "Short.", "key_points": ["point 1"], "word_count": 1}'
    completion = _make_completion_event(
        request_event.event_id,
        summary_json,
        UUID(str(request_event.correlation.correlation_id)),
        agent_id="agent-1",
    )
    world2 = _fold(world, [request_event, completion])
    events = system(world2)
    assert len(events) == 1
    assert events[0].event_type == EVENT_TYPE_SUMMARY_GENERATED
    output = events[0].data["output"]
    assert output["summary"] == "Short."
    assert output["key_points"] == ["point 1"]
    assert output["word_count"] == 1


# ---------------------------------------------------------------------------
# PersonalizedRoleSystem
# ---------------------------------------------------------------------------


def test_personalized_role_system_emits_personalized_request():
    """The ``PersonalizedRoleSystem`` reacts to a
    ``personalized.request`` event."""
    from kntgraph.agents.role_systems import PersonalizedRoleSystem

    system = PersonalizedRoleSystem()
    personalized_request = Event.create(
        event_type="personalized.request",
        agent_id="agent-1",
        event_class="domain",
        data={"input": "Tell me about ECS"},
        correlation=_ctx(),
    )
    world = _fold(World.empty(), [personalized_request])
    events = system(world)
    assert len(events) == 1
    assert events[0].event_type == "tool.chat_llm.requested"
    assert "ECS" in events[0].data["params"]["user"]


def test_personalized_role_system_emits_personalized_reply_generated():
    """The ``PersonalizedRoleSystem`` returns the LLM's
    raw text in a ``{"text": "..."}`` envelope (the
    legacy role is free-form)."""
    from kntgraph.agents.role_systems import (
        EVENT_TYPE_PERSONALIZED_REPLY_GENERATED,
        PersonalizedRoleSystem,
    )

    system = PersonalizedRoleSystem()
    personalized_request = Event.create(
        event_type="personalized.request",
        agent_id="agent-1",
        event_class="domain",
        data={"input": "Tell me about ECS"},
        correlation=_ctx(),
    )
    world = _fold(World.empty(), [personalized_request])
    request_event = system(world)[0]
    completion = _make_completion_event(
        request_event.event_id,
        "Free-form reply text",
        UUID(str(request_event.correlation.correlation_id)),
        agent_id="agent-1",
    )
    world2 = _fold(world, [request_event, completion])
    events = system(world2)
    assert len(events) == 1
    assert events[0].event_type == EVENT_TYPE_PERSONALIZED_REPLY_GENERATED
    assert events[0].data["output"]["text"] == "Free-form reply text"
