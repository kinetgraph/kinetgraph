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
from typing import TYPE_CHECKING
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


class FSMSystem:
    """
    C-01: BusinessFSM — WorldSystem (post-ADR-018 shape).

    Reads the DomainComponent state from the post-fold
    ``World``, scans every agent whose archetype carries the
    configured component, and validates each incoming event
    against the declared transition table.

    The trigger is derived from the existing view fields
    (``domain_phase`` + ``last_event_id`` + the component
    keyed by ``domain_phase``) — the framework does NOT add a
    ``view.last_event`` envelope (ADR-069 §11.16). This
    mirrors the ``_BaseRoleSystem`` precedent.
    """

    __slots__ = ("config", "_now")

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

    def _events_for_agent(self, view: "AgentView", world: "World") -> list["Event"]:
        component = view.get_component(self.config.component_type)
        if component is None:
            return []
        current_state: str = getattr(component, self.config.state_field)

        # The trigger is derived from the existing view fields
        # (ADR-069 §11.16): ``domain_phase`` is the last domain
        # event's type, ``last_event_id`` its id. Correlation
        # comes from the middleware (non-None inside a tick,
        # ADR-037).
        trigger_type = view.domain_phase
        if trigger_type is None:
            return []
        trigger = ViewTrigger(
            agent_id=view.agent_id,
            event_type=trigger_type,
            event_id=UUID(str(view.last_event_id))
            if view.last_event_id is not None
            else None,
            data=MappingProxyType({}),
            correlation=correlation_middleware.current(),
        )

        if current_state in self.config.terminal:
            return [self._rejected(trigger, current_state, reason="terminal_state")]

        allowed = self.config.transitions.get(current_state, {})
        transition = allowed.get(trigger.event_type)
        if transition is None:
            return [
                self._rejected(trigger, current_state, reason="transition_not_declared")
            ]

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
                return [self._rejected(trigger, current_state, reason="guard_failed")]

        out = [self._transitioned(trigger, current_state, transition.to)]

        entry_type = self.config.on_entry.get(transition.to)
        if entry_type is not None:
            out.append(self._entry_event(trigger, transition.to, entry_type))

        return out

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
