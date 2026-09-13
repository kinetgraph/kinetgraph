# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos.fsm._system -- BusinessFSM WorldSystem (ADR-069 §3.4).

``FSMSystem`` is a pure reactive system: no I/O, no tool
calls. It reads the ``DomainComponent`` state from the
post-fold ``World``, scans every agent whose archetype
carries the configured component, and validates each
incoming event against the declared transition table.

Emits (per matching event):

  - ``fsm.transitioned``         on success
  - ``fsm.transition_rejected``  on invalid transition,
    failed guard, or terminal-state violation

The system is pure: same ``World`` ⇒ same ``list[Event]``.
``now`` is injected (defaults to the framework clock) so a
replayed log re-evaluates guards with the same timestamp.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from kntgraph.core.clock import injectable_clock
from kntgraph.core.components.memory import (
    ContinuityComponent,
    ProfileComponent,
)
from kntgraph.core.event.correlation import correlation_middleware

from ..base import StepContext, ViewTrigger
from ._config import FSMConfig

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue
    from kntgraph.core.clock import Clock
    from kntgraph.core.event.event import Event
    from kntgraph.core.world.view import AgentView
    from kntgraph.core.world.world import World

__all__ = ["FSMSystem"]


# ADR-074 §2.1: cursor key for the FSM in
# ``view.cursors``. Declared as a ClassVar so it survives
# class renames; ``_system_name(system)`` picks it up at
# cursor-write time.
_FSM_CURSOR_KEY = "FSMSystem"

# Event types the FSM itself emits (ADR-071). In the next
# tick, these events appear in ``new_events`` (the dispatcher
# reads them from the EventLog). The FSM filters them out
# so it does not re-process its own emitted events as
# triggers — that would emit ``fsm.transition_rejected``
# noise for every previous tick.
_FSM_OWN_EVENT_TYPES = frozenset(
    {
        "fsm.transitioned",
        "fsm.transition_rejected",
    }
)


class FSMSystem:
    """
    C-01: BusinessFSM — WorldSystem (post-ADR-018 shape).

    Reads the DomainComponent state from the post-fold
    ``World``, scans every agent whose archetype carries the
    configured component, and validates each incoming event
    against the declared transition table.

    **Cursor integration (ADR-074).** The FSM reads the
    framework cursor from ``view.cursors["FSMSystem"]``. If
    the cursor matches ``view.last_event_id``, the FSM has
    already processed everything the view has and emits no
    events. This gives replay-safety and idle-tick fast-path
    without per-system state on the FSM instance.

    **Multi-event tick.** The dispatcher passes the events
    it just folded (``new_events``) to every system. The
    FSM iterates them in order, cascading state locally:
    an event that transitions ``draft → validating``
    updates the local state so the next event in the
    same batch sees ``validating`` as its source state.
    Each emitted ``fsm.transitioned`` carries the
    ``causation_id`` of the EVENT that triggered it, so
    the FSMProjection (which folds them on the next tick)
    applies them in order to the component.

    Events the FSM itself emitted in a previous tick are
    in the EventLog (framework invariant: persisted =
    valid). The FSM filters its own event types
    (``fsm.transitioned``, ``fsm.transition_rejected``,
    and the configured ``on_entry`` event types) so it
    does not re-process its own output as input.

    The cursor advances when the FSM RAN this tick, even
    if it emitted nothing (e.g., filtered all events). A
    system that runs without emitting still "saw"
    everything; the cursor reflects that.
    """

    __slots__ = ("config", "_now")
    __fsm_system_name__: ClassVar[str] = _FSM_CURSOR_KEY

    def __init__(
        self,
        config: FSMConfig,
        *,
        now: "Clock | None" = None,
    ) -> None:
        self.config = config
        self._now = injectable_clock(now)

    def __call__(
        self,
        world: "World",
        *,
        new_events: "list[Event] | None" = None,
    ) -> list["Event"]:
        out: list[Event] = []
        for _agent_id, view in world.query_agents(self.config.component_type):
            out.extend(
                self._events_for_agent(view, world, new_events)
            )
        return out

    def _events_for_agent(
        self,
        view: "AgentView",
        world: "World",
        new_events: "list[Event] | None",
    ) -> list["Event"]:
        # ADR-074: replay-safety / idle-tick fast path. The
        # dispatcher advances ``view.cursors["FSMSystem"]``
        # to ``view.last_event_id`` after every tick the FSM
        # ran. If the cursor already matches the latest
        # event the view has seen, there is nothing new to
        # process.
        cursor_id = view.cursors.get(_FSM_CURSOR_KEY)
        if cursor_id is not None and cursor_id == view.last_event_id:
            return []

        component = view.get_component(self.config.component_type)
        if component is None:
            return []
        current_state: str = getattr(component, self.config.state_field)

        # Multi-event tick (ADR-074 §5): process all events
        # the dispatcher just folded. Fall back to a
        # single-event view (derived from ``view.domain_phase``)
        # when the dispatcher did not pass ``new_events``
        # (legacy path, or systems that never opt in).
        events: list[Event]
        if new_events is not None:
            events = [
                e for e in new_events
                if e.agent_id == view.agent_id
                and e.event_type not in self._own_event_types()
                and e.event_type not in self.config.on_entry.values()
            ]
        elif view.domain_phase is not None:
            # Build a synthetic single-event list from the
            # view (the framework invariant keeps ``last_event_id``
            # equal to the event the view's domain slot
            # refers to).
            from kntgraph.core.event.event import Event

            payload = view.components.get(view.domain_phase, {})
            events = [
                Event.create(
                    agent_id=view.agent_id,
                    event_type=view.domain_phase,
                    event_class="domain",
                    data=dict(payload),
                    # The view does NOT carry the event's id;
                    # the cursor match check above already
                    # gated this code path so last_event_id
                    # is the synthetic event's id.
                )
            ] if view.last_event_id is None else [
                # When the cursor check above passes (no
                # cursor or cursor diverges), the view's
                # ``last_event_id`` is the trigger event id.
                # We can synthesise the event WITHOUT a
                # real Event object because the cursor
                # gate already passed; the FSM only needs
                # ``event_type`` and ``data`` here.
                type("E", (), {
                    "event_type": view.domain_phase,
                    "event_id": UUID(str(view.last_event_id)),
                    "agent_id": view.agent_id,
                    "data": dict(payload),
                })()
            ]
        else:
            return []

        out: list[Event] = []
        for event in events:
            trigger = ViewTrigger(
                agent_id=view.agent_id,
                event_type=event.event_type,
                event_id=UUID(str(event.event_id))
                if getattr(event, "event_id", None) is not None
                else None,
                data=MappingProxyType(dict(event.data))
                if isinstance(getattr(event, "data", None), dict)
                else MappingProxyType({}),
                correlation=correlation_middleware.current(),
            )

            emitted, new_state = self._process_trigger(
                trigger, current_state, component, view, world
            )
            out.extend(emitted)
            if new_state is not None:
                current_state = new_state  # local cascade

        return out

    def _own_event_types(self) -> frozenset[str]:
        """Event types the FSM itself emits — used to filter
        its own previous output from ``new_events`` so the
        FSM does not re-process its own emitted events as
        triggers (which would generate
        ``fsm.transition_rejected`` noise every tick).
        """
        return _FSM_OWN_EVENT_TYPES

    def _process_trigger(
        self,
        trigger: "ViewTrigger",
        current_state: str,
        component: object,
        view: "AgentView",
        world: "World",
    ) -> tuple[list["Event"], str | None]:
        """
        Process a single trigger against the FSM's current
        state. Returns ``(emitted_events, new_state)``:
        ``new_state`` is the new state if a transition
        fired (so the caller can cascade), ``None``
        otherwise.
        """
        if current_state in self.config.terminal:
            return (
                [self._rejected(trigger, current_state, reason="terminal_state")],
                None,
            )

        allowed = self.config.transitions.get(current_state, {})
        transition = allowed.get(trigger.event_type)
        if transition is None:
            return (
                [
                    self._rejected(
                        trigger, current_state, reason="transition_not_declared"
                    )
                ],
                None,
            )

        if transition.guard is not None:
            ctx = StepContext(
                step_results=MappingProxyType({}),
                step_states=MappingProxyType({}),
                domain=component,
                continuity=view.get_component(ContinuityComponent),
                profile=view.get_component(ProfileComponent),
                world=world,
                agent_id=view.agent_id,
                now=self._now(),
            )
            if not transition.guard.is_satisfied_by(ctx):
                return (
                    [self._rejected(trigger, current_state, reason="guard_failed")],
                    None,
                )

        emitted: list[Event] = [
            self._transitioned(trigger, current_state, transition.to)
        ]

        entry_type = self.config.on_entry.get(transition.to)
        if entry_type is not None:
            emitted.append(
                self._entry_event(trigger, transition.to, entry_type)
            )

        return emitted, transition.to

    def _transitioned(
        self,
        trigger: "ViewTrigger",
        from_state: str,
        to_state: str,
    ) -> "Event":
        return self._emit(
            trigger,
            event_type="fsm.transitioned",
            data={
                "from": from_state,
                "to": to_state,
                "trigger": trigger.event_type,
                "trigger_event_id": str(trigger.event_id),
            },
        )

    def _rejected(
        self,
        trigger: "ViewTrigger",
        current_state: str,
        reason: str,
    ) -> "Event":
        return self._emit(
            trigger,
            event_type="fsm.transition_rejected",
            data={
                "current_state": current_state,
                "trigger": trigger.event_type,
                "reason": reason,
            },
        )

    def _entry_event(
        self,
        trigger: "ViewTrigger",
        state: str,
        entry_type: str,
    ) -> "Event":
        return self._emit(
            trigger,
            event_type=entry_type,
            data={"state": state},
        )

    def _emit(
        self,
        trigger: "ViewTrigger",
        *,
        event_type: str,
        data: dict[str, "JsonValue"],
    ) -> "Event":
        from kntgraph.core.event.event import Event

        return Event.create(
            agent_id=trigger.agent_id,
            event_type=event_type,
            event_class="domain",
            data=data,
            causation_id=trigger.event_id,
            correlation=trigger.correlation,
        )
