# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos.fsm._state -- FSM state-advance projection (ADR-069 §9.2 item 1).

The FSM emits ``fsm.transitioned``; the ``DomainComponent``
projection (ADR-059) must know to update ``state_field`` when it
sees that event. This module provides the dedicated
``FSMProjection`` (option b of ADR-069 §9.2 item 1) that overlays
the default projection: it reads ``fsm.transitioned`` events and
re-derives the configured ``DomainComponent`` with the new
``state_field`` value.

The projection is a :class:`WorldProjection` (ADR-069 §11.10):
pure, composed into the dispatcher fold after the base fold and
before the tool overlay. It needs the ``FSMConfig`` to know the
``component_type`` and ``state_field``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from kntgraph.core.event import Event
from kntgraph.core.world.view import AgentView
from kntgraph.core.world.world import World

from ._config import FSMConfig

if TYPE_CHECKING:
    from kntgraph.core.world.component import DomainComponent

__all__ = ["FSMProjection"]


def _transitioned_to_state(e: Event) -> str | None:
    """The target state carried by a ``fsm.transitioned`` event."""
    to = e.data.get("to")
    return str(to) if to is not None else None


def _fold_agent(
    config: FSMConfig,
    agent_events: Sequence[Event],
    base: "DomainComponent | None",
) -> "DomainComponent | None":
    """Fold a batch of ``fsm.transitioned`` events into the
    configured ``DomainComponent``, advancing ``state_field``.

    Returns ``None`` when the batch has no ``fsm.transitioned``
    event for this agent AND the base view has no component (the
    caller keeps the base view unchanged — no allocation).
    """
    component = base
    saw_event = False
    for e in agent_events:
        if e.event_type != "fsm.transitioned":
            continue
        to_state = _transitioned_to_state(e)
        if to_state is None:
            continue
        saw_event = True
        if component is None:
            # No base component to advance; the FSM cannot
            # materialise one from a transition alone (it needs
            # the component's other fields). Skip.
            continue
        component = cast(
            "DomainComponent", replace(component, **{config.state_field: to_state})
        )
    if not saw_event:
        return None
    return component


def reconcile_fsm_state(
    config: FSMConfig,
    events: Sequence[Event],
    base_views: "Mapping[str, AgentView]",
) -> dict[str, AgentView]:
    """Pure fold: ``fsm.transitioned`` events → AgentView with the
    configured ``DomainComponent``'s ``state_field`` advanced.

    Mirrors ``project_memory`` (ADR-042 §6.1): the returned dict
    mirrors the input (every base view is in the output) and
    overlays the advanced component on agents whose events included
    a ``fsm.transitioned``. Agents with no such event are passed
    through unchanged (no allocation).
    """
    out: dict[str, AgentView] = dict(base_views)
    for agent_id, view in base_views.items():
        base = view.get_component(config.component_type)
        agent_events = [e for e in events if e.agent_id == agent_id]
        updated = _fold_agent(config, agent_events, base)
        if updated is None:
            continue
        new_components: dict[Any, Any] = dict(view.components)
        new_components[config.component_type] = updated
        out[agent_id] = replace(view, components=new_components)
    return out


class FSMProjection:
    """A :class:`WorldProjection` that advances the configured
    ``DomainComponent``'s ``state_field`` from ``fsm.transitioned``
    events (ADR-069 §9.2 item 1, option b).

    Composed into the dispatcher fold after the base fold and
    before the tool overlay. Pure: same ``(world, events)`` ⇒ same
    ``World``.
    """

    __slots__ = ("_config",)

    def __init__(self, config: FSMConfig) -> None:
        self._config = config

    def __call__(self, world: "World", events: list[Event]) -> "World":
        new_views = reconcile_fsm_state(self._config, events, world.views)
        if not new_views:
            return world
        changed: bool = False
        new_storage = world.storage
        for agent_id, projected_view in new_views.items():
            if world.views.get(agent_id) is projected_view:
                continue
            new_storage = new_storage.clone_with_entity(
                agent_id,
                dict(projected_view.components),
            )
            changed = True
        if not changed:
            return world
        return World(tick=world.tick, storage=new_storage, views=new_views)
