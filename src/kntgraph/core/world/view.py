# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
world.view -- The `AgentView` dataclass.

A derived, immutable view of one agent at a tick T.

  - `components` is the dict[slot_name, component_value]
    derived from the agent's last domain event per
    component type. The default projection takes the
    data payload of the last domain event as a
    single 'state' slot.

  - `operational_phase` is the value of the agent's
    last lifecycle event. `operational_at` is the
    timestamp of that event.

  - `domain_phase` is the value of the agent's last
    domain event (the application's "current step"
    concept).

  - `last_event_id` / `last_event_at` are convenience
    pointers used by idempotent projections.

Two derived properties (`is_terminated`, `is_running`)
expose the most common operational-state checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, TypeVar, Type

from ..lifecycle import OperationalPhase

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class AgentView:
    """
    Derived view of an agent at tick T.

    `components` is the dict[slot_name, component_value] derived from
    the agent's last domain event per component type. Application code
    decides the projection (default: take the data payload of the last
    domain event as a single 'state' slot).

    `operational_phase` is the value of the agent's last lifecycle
    event. `operational_at` is the timestamp of that event.

    `domain_phase` is the value of the agent's last domain event (the
    application's "current step" concept).

    `last_event_id` / `last_event_at` are convenience pointers used by
    idempotent projections.

    The `components` mapping is a plain ``dict`` -- the read-only
    contract is enforced by the dataclass being frozen (systems
    cannot reassign ``view.components``). Mutation of the dict's
    contents is not blocked at runtime; the framework treats it
    as a discipline enforced by the project rules.

    Component value type
    --------------------

    ``components`` is a heterogeneous bag: some slots are
    JSON-serialisable payloads (event data, tool args) and
    others are ECS components (``ToolCallRequest``,
    ``ToolCallCompletion`` — frozen dataclasses, not JSON).
    The framework encodes this as ``Mapping[str, Any]`` to
    avoid either:

      - forcing callers to serialise the ECS components
        just to satisfy ``Mapping[str, JsonValue]`` (which
        is wrong — components live in-memory, not on the
        wire);
      - introducing a Union per slot, which would force
        callers to dispatch at every read.

    The trade-off is documented in AGENTS.md §1: this is
    one of two **legitimate** uses of ``Any`` in the
    framework (the other is the public-facing ``Event.data``
    which we just tightened to ``Mapping[str, JsonValue]``).
    The migration was attempted in 2026-07 and reverted:
    see DEBT_TECHNICAL.md item 6 for the reasoning.
    """

    agent_id: str
    components: Mapping[str | type[Any], Any] = field(default_factory=dict)
    operational_phase: OperationalPhase = "spawned"
    operational_at: Optional[datetime] = None
    domain_phase: Optional[str] = None
    domain_at: Optional[datetime] = None
    last_event_id: Optional[str] = None
    last_event_at: Optional[datetime] = None

    @property
    def is_terminated(self) -> bool:
        return self.operational_phase == "terminated"

    @property
    def is_running(self) -> bool:
        return self.operational_phase == "running"

    def get_component(self, component_type: Type[T]) -> Optional[T]:
        """
        Type-safe accessor for ECS Components.
        Returns the component if it exists and matches the requested type,
        otherwise returns None.
        """
        # Fast path: check if the type itself is the key (Type Discipline)
        val = self.components.get(component_type)
        if isinstance(val, component_type):
            return val
            
        # Fallback: search by type if a legacy string key was used
        for comp in self.components.values():
            if isinstance(comp, component_type):
                return comp
                
        return None


__all__ = ["AgentView"]
