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

# Event types the FSM itself emits (ADR-071). When the
# framework re-folds a tick's emitted events into the
# next tick's World, the FSM's own output becomes the
# ``view.domain_phase`` of the next tick. The cursor
# gate (ADR-074) handles the dedup: ``view.cursors[<FSM>]``
# matches ``view.last_event_id`` after the FSM ran, so
# the FSM does NOT re-process its own emitted events as
# triggers.
#
# The legacy filter-on-input path (``fsm.transitioned``
# appearing in ``new_events``) was REMOVED when the
# ``new_events`` kwarg was dropped from the
# ``WorldSystem`` Protocol. Cursor-based gating is the
# canonical mechanism.
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

    **Source-of-truth discipline.** Per the ``WorldSystem``
    Protocol, the FSM receives only the post-fold World —
    it does NOT receive the raw event batch from the
    dispatcher's tick loop. The framework invariant holds:

      - EventLog (Redis) is the source of truth.
      - World is the deterministic projection, folded at
        the start of every tick.
      - Systems read from the World; new events are
        observable through the projection (the
        ``domain_phase`` slot carries the latest event's
        type; the ``components`` slot carries its data).

    **Single-event-per-tick.** When multiple events arrive
    in one batch, the World reflects the LATEST event in
    the batch (``view.domain_phase`` /
    ``view.last_event_id``). The FSM processes that one
    event per tick. Intermediate events in the same batch
    are skipped — they will be re-folded into the
    projection only on subsequent ticks, and the FSM
    will process them then. Operators who need
    multi-event tick semantics must split their batch at
    the producer side (one event per stream write, or
    per-event publish).

    **Cursor integration (ADR-074).** The FSM reads the
    framework cursor from ``view.cursors["FSMSystem"]``. If
    the cursor matches ``view.last_event_id``, the FSM has
    already processed everything the view has and emits no
    events. This gives replay-safety and idle-tick fast-path
    without per-system state on the FSM instance.

    The cursor advances when the FSM RAN this tick, even
    if it emitted nothing (e.g., the trigger was filtered).
    A system that runs without emitting still "saw"
    everything; the cursor reflects that.
    """

    __slots__ = ("config", "_now")
    __cursor_key__: ClassVar[str] = _FSM_CURSOR_KEY

    def __init__(
        self,
        config: FSMConfig,
        *,
        now: "Clock | None" = None,
    ) -> None:
        self.config = config
        self._now = injectable_clock(now)

    def __call__(self, world: "World") -> list["Event"]:
        out: list[Event] = []
        for _agent_id, view in world.query_agents(self.config.component_type):
            out.extend(self._events_for_agent(view, world))
        return out

    def _events_for_agent(
        self,
        view: "AgentView",
        world: "World",
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

        # Single-event-per-tick discipline: derive the
        # trigger from the World (the post-fold projection).
        # ``view.domain_phase`` carries the latest event's
        # type; ``view.components[domain_phase]`` carries its
        # data; ``view.last_event_id`` carries its id. When
        # the cursor gate above passes, we know
        # ``last_event_id`` is the trigger's id (it
        # diverged from the previous cursor position).
        if view.domain_phase is None:
            return []

        # Skip FSM-emitted events (replayed through the
        # EventLog on subsequent ticks). Cursor gating
        # ALREADY handles dedup for events we ourselves
        # emitted — the cursor advances to last_event_id
        # right after the FSM ran, so when our own
        # output is re-folded the cursor gate will skip
        # this code path. The explicit check here is a
        # safety net for the cursor-less legacy path.
        if view.domain_phase in self._own_event_types():
            return []

        payload = view.components.get(view.domain_phase, {})
        trigger = ViewTrigger(
            agent_id=view.agent_id,
            event_type=view.domain_phase,
            event_id=UUID(str(view.last_event_id))
            if view.last_event_id is not None
            else None,
            data=MappingProxyType(dict(payload))
            if isinstance(payload, dict)
            else MappingProxyType({}),
            correlation=correlation_middleware.current(),
        )

        emitted, _new_state = self._process_trigger(
            trigger, current_state, component, view, world
        )
        return emitted

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
            # ADR-069 §2.1: cross-agent access via an
            # opt-in resolver. The resolver is a pure
            # closure over the post-fold World; specs
            # that don't need it receive ``None``.
            ctx = StepContext(
                step_results=MappingProxyType({}),
                step_states=MappingProxyType({}),
                domain=component,
                continuity=view.get_component(ContinuityComponent),
                profile=view.get_component(ProfileComponent),
                agent_id=view.agent_id,
                now=self._now(),
                trigger_data=MappingProxyType(dict(trigger.data))
                if isinstance(getattr(trigger, "data", None), dict)
                else None,
                cross_agent_resolver=lambda aid: world.views.get(aid),
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
            emitted.append(self._entry_event(trigger, transition.to, entry_type))

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
