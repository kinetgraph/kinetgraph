# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the example 05b projection shim.

The shim composes the framework's default fold with
the memory hydration projection (ADR-042 §6.1) and
the tool-call overlay (ADR-044). It is monkey-patched
onto :class:`ReactiveDispatcher._fold_with_filter`
so the example can run without a framework release
that exposes a proper "compose projections" hook.

These tests exercise the shim in isolation: a
:class:`World` is folded through the shim's logic
(the same logic the dispatcher would call) and the
post-fold views are asserted to carry the expected
derived components (SessionComponent, tool_requests,
tool_completions).

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


def _load_05b():
    """Load the example 05b module by path.

    The module's top-level code installs the
    projection shim on import, so the tests can
    use the shim's helpers (``_install_projection_shim``,
    ``SessionChatSystem``) directly.
    """
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


def test_shim_installed_on_import(mod_05b):
    """The shim monkey-patches ``ReactiveDispatcher`` on import."""
    import kntgraph.runner.reactive as _reactive_mod

    assert (
        getattr(_reactive_mod.ReactiveDispatcher, "_memory_shim_applied", False) is True
    )


def test_shim_is_idempotent(mod_05b):
    """Calling the shim a second time is a no-op."""
    from kntgraph.runner import reactive as _reactive_mod

    # Re-install (should be a no-op).
    mod_05b._install_projection_shim()
    assert (
        getattr(_reactive_mod.ReactiveDispatcher, "_memory_shim_applied", False) is True
    )


def test_shim_hydrates_session_component_for_user_intent(mod_05b):
    """The shim installs the SessionComponent on the agent's
    view when a ``session.started`` event lands."""
    # ``SESSION_AGENT_ID`` is ``"session:ecs-demo"``; the
    # ``project_memory`` derives ``session_id`` by stripping
    # the ``session:`` prefix.
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

    # Apply the shim's fold logic directly.
    inst = mod_05b.ReactiveDispatcher.__new__(mod_05b.ReactiveDispatcher)
    inst._filter = None
    new_world, new_event_count = inst._fold_with_filter(
        World.empty(), [session_ev, intent_ev]
    )
    assert new_event_count == 2

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


def test_shim_preserves_session_component_across_ticks(mod_05b):
    """Tick N+1 with no memory event keeps the
    SessionComponent from tick N (no clobber)."""
    session_id = "shim-test-2"
    session_ev = Event.create(
        event_type="session.started",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={
            "session_id": session_id,
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

    inst = mod_05b.ReactiveDispatcher.__new__(mod_05b.ReactiveDispatcher)
    inst._filter = None

    # Tick 1: session.started + user.intent.
    world_n, _ = inst._fold_with_filter(World.empty(), [session_ev, intent_ev])
    session_n: SessionComponent = world_n.views[mod_05b.SESSION_AGENT_ID].components[
        SessionComponent
    ]
    assert session_n.intent_event_id == str(intent_ev.event_id)

    # Tick 2: a NEW user.intent (no memory event in
    # this tick; the SessionComponent should be
    # preserved from tick N). The default fold's
    # _apply_event preserves derived components, so
    # the new view's SessionComponent survives
    # (with the OLD intent_event_id; the projection
    # will NOT have rebuilt it because no
    # session.* event landed in this batch).
    intent_ev_2 = Event.create(
        event_type="user.intent",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"intent": "chat", "message": "world"},
        correlation=_ctx(),
    )
    world_n_plus_1, _ = inst._fold_with_filter(world_n, [intent_ev_2])

    # The SessionComponent is still on the view
    # (preserved by ``_apply_event``).
    view = world_n_plus_1.views[mod_05b.SESSION_AGENT_ID]
    assert SessionComponent in view.components
    # The new view also has the new ``user.intent``
    # component (last-event-wins for the new domain
    # event's data).
    assert "user.intent" in view.components
    # The new component's data reflects intent_ev_2.
    assert view.components["user.intent"]["message"] == "world"


def test_shim_overlay_installs_tool_request_slot(mod_05b):
    """A ``tool.<name>.requested`` event installs
    the ``tool_requests`` slot on the view."""
    request_ev = Event.create(
        event_type="tool.chat_llm.requested",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"tool": "chat_llm", "params": {}},
        correlation=_ctx(),
    )

    inst = mod_05b.ReactiveDispatcher.__new__(mod_05b.ReactiveDispatcher)
    inst._filter = None
    new_world, _ = inst._fold_with_filter(World.empty(), [request_ev])

    view = new_world.views[mod_05b.SESSION_AGENT_ID]
    assert "tool_requests" in view.components
    assert str(request_ev.event_id) in view.components["tool_requests"]


def test_shim_overlay_accumulates_request_across_ticks(mod_05b):
    """ADR-044: a request emitted in tick N remains
    visible in tick N+1 (no completion yet)."""
    request_ev = Event.create(
        event_type="tool.chat_llm.requested",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"tool": "chat_llm", "params": {}},
        correlation=_ctx(),
    )

    inst = mod_05b.ReactiveDispatcher.__new__(mod_05b.ReactiveDispatcher)
    inst._filter = None

    # Tick N: request lands.
    world_n, _ = inst._fold_with_filter(World.empty(), [request_ev])

    # Tick N+1: an unrelated event. The request
    # should remain in the slot.
    user_intent = Event.create(
        event_type="user.intent",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"intent": "chat", "message": "hi"},
        correlation=_ctx(),
    )
    world_n_plus_1, _ = inst._fold_with_filter(world_n, [user_intent])

    view = world_n_plus_1.views[mod_05b.SESSION_AGENT_ID]
    # The request is still there.
    assert str(request_ev.event_id) in view.components["tool_requests"]


def test_system_emits_chat_llm_request_on_user_intent(mod_05b):
    """The SessionChatSystem reads the hydrated
    SessionComponent and emits a
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

    inst = mod_05b.ReactiveDispatcher.__new__(mod_05b.ReactiveDispatcher)
    inst._filter = None
    new_world, _ = inst._fold_with_filter(World.empty(), [session_ev, intent_ev])

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
    """The SessionChatSystem reacts to the LLM
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
    request_ev = Event.create(
        event_type="tool.chat_llm.requested",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"tool": "chat_llm", "params": {}},
        correlation=CorrelationContext(correlation_id=UUID(str(intent_ev.event_id))),
    )
    completion_ev = Event.create(
        event_type="tool.chat_llm.completed",
        agent_id=mod_05b.SESSION_AGENT_ID,
        event_class="domain",
        data={"text": "[mock reply]"},
        correlation=CorrelationContext(correlation_id=UUID(str(intent_ev.event_id))),
        causation_id=str(request_ev.event_id),
    )

    inst = mod_05b.ReactiveDispatcher.__new__(mod_05b.ReactiveDispatcher)
    inst._filter = None
    new_world, _ = inst._fold_with_filter(
        World.empty(), [session_ev, intent_ev, request_ev, completion_ev]
    )

    system = mod_05b.SessionChatSystem()
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
    world_after_intent, _ = inst._fold_with_filter(
        World.empty(), [session_ev, intent_ev]
    )
    evs1 = system(world_after_intent)
    assert len(evs1) == 1
    assert evs1[0].event_type == "tool.chat_llm.requested"
    # Tick 2: the chat_llm request event lands
    # (the WorkerManager ran the tool between
    # tick 1 and tick 2). The system sees the
    # tool.<name>.requested event but the
    # request is in flight (no completion
    # yet), so it does nothing.
    request_ev = evs1[0]
    world_after_request, _ = inst._fold_with_filter(world_after_intent, [request_ev])
    evs2 = system(world_after_request)
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
    world_after_completion, _ = inst._fold_with_filter(
        world_after_request, [completion_ev]
    )
    events = system(world_after_completion)
    # Two session_recorder requests: append_user
    # and append_assistant.
    assert len(events) == 2
    assert all(e.event_type == "tool.session_recorder.requested" for e in events)
    commands = [e.data.get("params", {}).get("command") for e in events]
    assert "append_user" in commands
    assert "append_assistant" in commands
