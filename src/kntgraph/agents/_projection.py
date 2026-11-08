# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""agents._projection -- materialise ``RoleComponent`` from the event stream.

ADR-088 §2.6: the framework has no projection that materialises
``RoleComponent``; each vertical invents its own. This is the
framework's member of the projection family, alongside
``FSMProjection`` and ``SagaProjection``.

The role event payload
----------------------
The definition names the event *type*; the schema is the vertical's
(ADR-088 §3.7). Two keys are read:

``persona``
    The runtime-assigned persona. Falls back to ``RoleSpec.persona``.
``instructions``
    The persona instructions. Defaults to the empty string.

The capability policy is **never** read from the event. It comes
from ``RoleSpec.allowed_tools``: dropping it would silently disable
gate 2, because ``has_tool_access`` is permissive when no role is
installed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from kntgraph.core.components.role import RoleComponent
from kntgraph.core.world import World

if TYPE_CHECKING:
    from kntgraph.agents._definition import RoleSpec
    from kntgraph.core.event import Event


class RoleProjection:
    """Materialise ``RoleComponent`` from the role event stream.

    The spec supplies the capability policy; the event supplies the
    persona and instructions the runtime assigned.

    Pure: the same ``(world, events)`` yields the same ``World``. The
    dispatcher replays ticks during recovery, and a projection that is
    not pure breaks replay.

    Attributes:
        spec: The declared persona and capability policy.
        event_type: The role event type this projection listens to.
    """

    __slots__ = ("_event_type", "_spec")

    def __init__(self, spec: RoleSpec, event_type: str) -> None:
        self._spec = spec
        self._event_type = event_type

    def __call__(self, world: World, events: list[Event]) -> World:
        """Project the role events of this batch onto their agents.

        Args:
            world: The post-base-fold world.
            events: The batch of new events.

        Returns:
            A new world with the role component materialised, or the
            same world when the batch carries no role event.
        """
        role_events = [
            event for event in events if event.event_type == self._event_type
        ]
        if not role_events:
            return world

        new_views = dict(world.views)
        changed = False
        for event in role_events:
            view = new_views.get(event.agent_id)
            if view is None:
                # The base fold did not create a view for this agent.
                continue
            components = dict(view.components)
            components[RoleComponent] = self._component_from(event)
            projected = replace(view, components=components)
            if projected is view:
                continue
            new_views[event.agent_id] = projected
            changed = True

        if not changed:
            return world

        storage = world.storage
        for agent_id, view in new_views.items():
            if world.views.get(agent_id) is view:
                continue
            storage = storage.clone_with_entity(agent_id, dict(view.components))
        return World(tick=world.tick, storage=storage, views=new_views)

    def _component_from(self, event: Event) -> RoleComponent:
        """Build the component the role event describes.

        The policy always comes from the spec; the event only
        contributes the runtime-assigned persona and instructions.
        """
        data = event.data
        persona = data.get("persona")
        instructions = data.get("instructions")
        return self._spec.to_component(
            instructions=instructions if isinstance(instructions, str) else "",
            persona=persona if isinstance(persona, str) and persona else None,
        )


__all__ = ["RoleProjection"]
