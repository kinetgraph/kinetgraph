# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for example 05b's projection wiring.

Example 05b is the canonical reference implementation
of the ADR-042 §6.1 hydration pipeline. Earlier
versions of the example monkey-patched
:class:`ReactiveDispatcher._fold_with_filter` to
compose the framework's default fold with the memory
hydration projection and the tool-call overlay; that
shim is no longer needed (the framework runs both
built-in projections natively in
:func:`kntgraph.runner._folding.fold_with_filter`).

These tests now exercise the dispatcher's native
projection pipeline through the example's classes:

  - :func:`MemoryHydrationProjection` (built-in) is
    applied to a batch of events; the resulting
    ``World`` carries ``SessionComponent`` on the
    affected agents.

  - The tool-call overlay (ADR-044) still runs LAST
    regardless of the ``projections`` kwarg; the
    example's :class:`SessionChatSystem` continues to
    react to the hydrated SessionComponent.

The full round-trip (dispatcher + worker pool) is
exercised in a separate integration test that
requires a real Redis (fakeredis has known issues
with concurrent ``xreadgroup`` consumers + process
pools; see DEBT.md §2.18 follow-up).
"""

from __future__ import annotations

import importlib.util
from uuid import UUID

import pytest

from kntgraph.core.components.memory import SessionComponent
from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.runner import MemoryHydrationProjection


def _load_05b():
    """Load the example 05b module by path."""
    spec = importlib.util.spec_from_file_location(
        "_05b", "examples/05b_session_chat_ecs.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def mod_05b():
    return _load_05b()


def _ctx() -> CorrelationContext:
    return CorrelationContext.new()


def test_projection_module_does_not_monkey_patch_reactive_dispatcher(mod_05b):
    """Loading the example does NOT monkey-patch
    ``ReactiveDispatcher``. The legacy ``_memory_shim_applied``
    attribute is no longer set on import."""
    import kntgraph.runner.reactive as _reactive_mod

    assert (
        getattr(_reactive_mod.ReactiveDispatcher, "_memory_shim_applied", False)
        is False
    )


def test_memory_hydration_projection_hydrates_session_component_for_user_intent(
    mod_05b,
):
    """``MemoryHydrationProjection`` installs the
    ``SessionComponent`` on the agent's view when a
    ``session.started`` event lands."""
    # The ``session_id`` is derived from the
    # ``agent_id`` (``session:ecs-demo`` -> ``ecs-demo``)
    # by ``_build_session_component``; the
    # ``data.session_id`` field is currently unused
    # (ADR-042 §6.1 follow-up note).
    expected_session_id = mod_05b.SESSION_AGENT_ID.removeprefix("session:")
    session_ev = Event.create(
        event_type="session.started",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={
            "session_id": expected_session_id,
            "user_id": "u",
            "tenant_id": "t",
        },
        correlation=_ctx(),
    )
    intent_ev = Event.create(
        event_type="user.intent",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"intent": "chat", "message": "hello"},
        correlation=_ctx(),
    )

    projection = MemoryHydrationProjection()
    new_world = projection(World.empty(), [session_ev, intent_ev])

    view = new_world.views[mod_05b.SESSION_AGENT_ID]
    session: SessionComponent = view.components[SessionComponent]
    assert session.session_id == expected_session_id
    assert session.user_id == "u"
    assert session.tenant_id == "t"
    # ``intent_event_id`` is the eid of the LAST domain
    # event in the agent's stream (the user.intent
    # that triggered the system).
    assert session.intent_event_id == str(intent_ev.event_id)
    assert session.messages == ()


def test_memory_hydration_projection_preserves_session_component_across_ticks(mod_05b):
    """Tick N+1 with no memory event keeps the
    ``SessionComponent`` from tick N (no clobber).

    The ``session_id`` is captured from the
    ``session.started`` event payload (DEBT §2.33
    fix); the projection honours the wire value
    across ticks.
    """
    wire_session_id = "wire-session-id-42"
    session_ev = Event.create(
        event_type="session.started",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={
            "session_id": wire_session_id,
            "user_id": "u",
            "tenant_id": "t",
        },
        correlation=_ctx(),
    )
    intent_ev = Event.create(
        event_type="user.intent",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"intent": "chat", "message": "hello"},
        correlation=_ctx(),
    )

    projection = MemoryHydrationProjection()

    # Tick 1: session.started + user.intent.
    world_n = projection(World.empty(), [session_ev, intent_ev])
    session_n: SessionComponent = world_n.views[mod_05b.SESSION_AGENT_ID].components[
        SessionComponent
    ]
    assert session_n.intent_event_id == str(intent_ev.event_id)

    # Tick 2: a NEW user.intent (no memory event in
    # this tick; the SessionComponent should be
    # preserved from tick N).
    intent_ev_2 = Event.create(
        event_type="user.intent",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"intent": "chat", "message": "world"},
        correlation=_ctx(),
    )
    world_n_plus_1 = projection(world_n, [intent_ev_2])

    # The SessionComponent is still on the view
    # (preserved by ``_apply_event``).
    view = world_n_plus_1.views[mod_05b.SESSION_AGENT_ID]
    assert SessionComponent in view.components
    # ``MemoryHydrationProjection`` updates the
    # ``intent_event_id`` whenever ANY domain event
    # lands in the batch for the agent (the
    # ``user.intent`` is a domain event; see
    # :func:`_fold_session` in
    # :mod:`kntgraph.core.world.projection_memory`).
    # The new ``intent_event_id`` therefore points
    # to ``intent_ev_2``, the last domain event in
    # tick 2.
    assert view.components[SessionComponent].intent_event_id == str(
        intent_ev_2.event_id
    )
    # The session identity fields are preserved
    # across ticks (no clobber). The ``session_id``
    # was captured from the ``session.started``
    # event payload in tick 1 (DEBT §2.33 fix);
    # it survives across ticks unchanged because
    # no subsequent event re-derived it.
    assert view.components[SessionComponent].session_id == wire_session_id
    assert view.components[SessionComponent].user_id == "u"
    assert view.components[SessionComponent].tenant_id == "t"


def test_dispatcher_tool_overlay_installs_tool_request_slot(mod_05b):
    """A ``tool.<name>.requested`` event installs the
    ``tool_requests`` slot on the view (the tool
    overlay runs LAST regardless of the projections
    kwarg)."""
    request_ev = Event.create(
        event_type="tool.chat_llm.requested",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"tool": "chat_llm", "params": {}},
        correlation=_ctx(),
    )

    # Drive the fold through the dispatcher's own
    # helper so we exercise the SAME path the
    # production dispatcher uses (base fold ->
    # memory hydration -> caller projections ->
    # tool overlay).
    from kntgraph.runner._folding import fold_with_filter

    inst = mod_05b.ReactiveDispatcher.__new__(mod_05b.ReactiveDispatcher)
    inst._filter = None
    inst._tool_ttls = None
    inst._projections = [MemoryHydrationProjection()]
    new_world, _ = fold_with_filter(inst, World.empty(), [request_ev])

    view = new_world.views[mod_05b.SESSION_AGENT_ID]
    assert "tool_requests" in view.components
    assert str(request_ev.event_id) in view.components["tool_requests"]


def test_dispatcher_tool_overlay_accumulates_request_across_ticks(mod_05b):
    """ADR-044: a request emitted in tick N remains
    visible in tick N+1 (no completion yet)."""
    request_ev = Event.create(
        event_type="tool.chat_llm.requested",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"tool": "chat_llm", "params": {}},
        correlation=_ctx(),
    )

    from kntgraph.runner._folding import fold_with_filter

    inst = mod_05b.ReactiveDispatcher.__new__(mod_05b.ReactiveDispatcher)
    inst._filter = None
    inst._tool_ttls = None
    inst._projections = [MemoryHydrationProjection()]

    # Tick N: request lands.
    world_n, _ = fold_with_filter(inst, World.empty(), [request_ev])

    # Tick N+1: an unrelated event. The request
    # should remain in the slot.
    user_intent = Event.create(
        event_type="user.intent",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"intent": "chat", "message": "hi"},
        correlation=_ctx(),
    )
    world_n_plus_1, _ = fold_with_filter(inst, world_n, [user_intent])

    view = world_n_plus_1.views[mod_05b.SESSION_AGENT_ID]
    # The request is still there.
    assert str(request_ev.event_id) in view.components["tool_requests"]


def test_system_emits_chat_llm_request_on_user_intent(mod_05b):
    """The ``SessionChatSystem`` reads the hydrated
    ``SessionComponent`` and emits a
    ``tool.chat_llm.requested`` event."""
    session_ev = Event.create(
        event_type="session.started",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={
            "session_id": "s",
            "user_id": "u",
            "tenant_id": "t",
        },
        correlation=_ctx(),
    )
    intent_ev = Event.create(
        event_type="user.intent",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"intent": "chat", "message": "hello"},
        correlation=_ctx(),
    )

    from kntgraph.runner._folding import fold_with_filter

    inst = mod_05b.ReactiveDispatcher.__new__(mod_05b.ReactiveDispatcher)
    inst._filter = None
    inst._tool_ttls = None
    inst._projections = [MemoryHydrationProjection()]
    new_world, _ = fold_with_filter(inst, World.empty(), [session_ev, intent_ev])

    system = mod_05b.SessionChatSystem()
    events = system(new_world)
    # The system emits exactly one event: the
    # chat_llm request.
    assert len(events) == 1
    assert events[0].event_type == "tool.chat_llm.requested"
    assert events[0].data["tool"] == "chat_llm"
    # The causation_id is the user.intent's eid
    # (the SessionComponent's intent_event_id).
    assert events[0].causation_id == str(intent_ev.event_id)


def test_system_emits_session_recorder_on_completion(mod_05b):
    """The ``SessionChatSystem`` reacts to the LLM
    completion by emitting two
    ``tool.session_recorder.requested`` events
    (append_user + append_assistant)."""
    session_ev = Event.create(
        event_type="session.started",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={
            "session_id": "s",
            "user_id": "u",
            "tenant_id": "t",
        },
        correlation=_ctx(),
    )
    intent_ev = Event.create(
        event_type="user.intent",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"intent": "chat", "message": "hello"},
        correlation=_ctx(),
    )

    from kntgraph.runner._folding import fold_with_filter

    inst = mod_05b.ReactiveDispatcher.__new__(mod_05b.ReactiveDispatcher)
    inst._filter = None
    inst._tool_ttls = None
    inst._projections = [MemoryHydrationProjection()]

    # Drive the system tick by tick the way the
    # production dispatcher would: each tick
    # processes a batch of events between
    # ``asyncio.sleep`` boundaries; the worker
    # pool runs in a different process and its
    # emitted events land in the EventLog in a
    # subsequent tick.
    #
    # Tick 1: the session.started + user.intent
    # batch. The system reads the
    # ``user.intent`` component (last-event-
    # wins; the user.intent is the only domain
    # event in this tick) and emits the
    # chat_llm request.
    world_after_intent, _ = fold_with_filter(
        inst, World.empty(), [session_ev, intent_ev]
    )
    evs1 = mod_05b.SessionChatSystem()(world_after_intent)
    assert len(evs1) == 1
    assert evs1[0].event_type == "tool.chat_llm.requested"
    # Tick 2: the chat_llm request event lands
    # (the WorkerManager ran the tool between
    # tick 1 and tick 2). The system sees the
    # tool.<name>.requested event but the
    # request is in flight (no completion
    # yet), so it does nothing.
    request_ev = evs1[0]
    world_after_request, _ = fold_with_filter(inst, world_after_intent, [request_ev])
    evs2 = mod_05b.SessionChatSystem()(world_after_request)
    assert evs2 == []
    # Tick 3: the chat_llm completion lands.
    completion_ev = Event.create(
        event_type="tool.chat_llm.completed",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"text": "[mock reply]"},
        correlation=CorrelationContext(correlation_id=UUID(str(intent_ev.event_id))),
        causation_id=str(request_ev.event_id),
    )
    world_after_completion, _ = fold_with_filter(
        inst, world_after_request, [completion_ev]
    )
    events = mod_05b.SessionChatSystem()(world_after_completion)
    # Two session_recorder requests: append_user
    # and append_assistant.
    assert len(events) == 2
    assert all(e.event_type == "tool.session_recorder.requested" for e in events)
    commands = [e.data.get("params", {}).get("command") for e in events]
    assert "append_user" in commands
    assert "append_assistant" in commands
