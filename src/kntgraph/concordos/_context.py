# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
#
"""
concordos._context -- the ``StepContext`` carrier and its builder.

``StepContext`` is the read-only view a Specification evaluates
against. :func:`build_step_context` is the single construction
path the FSM and the Saga systems share, so a Specification
observes the same memory tiers in both execution cycles.

The domain components are collected under their concrete classes,
so a Specification reads one with ``ctx.get_component(MyComponent)``
and gets exactly that class or nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, TypeVar

from kntgraph.core.components.memory import (
    ContinuityComponent,
    ProfileComponent,
    SessionComponent,
)
from kntgraph.core.world.component import DomainComponent

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue
    from kntgraph.core.world.view import AgentView
    from kntgraph.core.world.world import World

    T = TypeVar("T", bound=DomainComponent)

__all__ = [
    "StepContext",
    "build_step_context",
    "collect_domain_components",
]


@dataclass(frozen=True, slots=True)
class StepContext:
    """
    Read-only view passed to Specifications for evaluation.

    Contains the framework components that might be
    relevant to a business rule. All component fields are
    optional because not every agent has every component
    populated.

    The carrier is built by :func:`build_step_context`, the
    single construction path shared by the FSM and the Saga
    systems. Building it in one place is what makes a
    Specification portable: the same Specification reads the
    same memory tiers whether it is an FSM guard, a Saga
    ``skip_when``, a Saga ``proceed_when`` or a Saga
    ``compensate_when``.

    **Domain components are read by concrete class.** The
    carrier holds ``domain_components``, a mapping from a
    component's concrete class to the instance the agent
    carries. A Specification reads its component with
    :meth:`get_component`:

    .. code-block:: python

        order = ctx.get_component(OrderDomainComponent)
        if order is not None and order.status == "paid":
            ...

    The mapping is keyed by class rather than resolved through
    the ``DomainComponent`` superclass because that superclass
    matches every domain component -- including
    ``RoleComponent`` -- so a supertype match returns an
    arbitrary component when the agent carries several.

    ``now`` is the dispatcher's current tick timestamp
    (injected by the system that builds the context).
    Specifications MUST treat ``now`` as the only clock
    source; reading ``datetime.now()`` inside
    ``is_satisfied_by`` would break replay determinism.

    ``trigger_data`` is the data payload of the event
    that triggered the current evaluation (e.g., the
    transition event for an FSM guard, the trigger event
    for a saga). The mini-language's ``event.data.*``
    paths resolve via this field. ``None`` when no
    trigger is in scope (e.g., for a saga step-result
    evaluation that only cares about ``step_results``).

    **Cross-agent access is opt-in.** A Specification
    that needs to read another agent's view (e.g.
    "proceed iff the financial-control agent's tier
    is VIP") declares an opt-in by accepting a
    ``cross_agent_resolver`` callable and calling it
    inside ``is_satisfied_by``. The resolver is built by
    the system that constructs the ``StepContext``
    (typically a closure over the post-fold ``World``).

    The ``StepContext`` itself does NOT carry the
    ``World``: that would make every guard FSM an O(N)
    scan over ``world.agents`` and break the "FSM
    guard is cheap" contract. Specifications that do
    NOT need cross-agent access receive
    ``cross_agent_resolver=None`` and cannot reach
    the World.

    Specifications MUST NOT mutate any agent view,
    emit events, or perform I/O. The
    ``is_satisfied_by`` method is pure; the emitted
    events belong to the system that called the
    Specification, not to the Specification itself.
    """

    step_results: Mapping[str, JsonValue]
    step_states: Mapping[str, str]
    continuity: ContinuityComponent | None
    profile: ProfileComponent | None
    session: SessionComponent | None
    domain_components: Mapping[type, DomainComponent]
    agent_id: str
    now: datetime
    trigger_data: Mapping[str, JsonValue] | None = None
    cross_agent_resolver: Callable[[str], AgentView | None] | None = None

    def get_component(self, component_type: type[T]) -> T | None:
        """The agent's domain component of the given concrete class.

        Returns ``None`` when the agent does not carry a
        component of that class. The lookup is by exact class,
        not by ``isinstance``: a superclass match would return
        an arbitrary component when the agent carries several
        domain components.
        """
        component = self.domain_components.get(component_type)
        if not isinstance(component, component_type):
            return None
        return component


def collect_domain_components(
    view: AgentView,
) -> Mapping[type, DomainComponent]:
    """The agent's domain components, keyed by concrete class.

    Every domain component the view carries is collected, each
    under its own concrete class. The lookup is exact rather
    than ``isinstance``-based: ``RoleComponent`` and every other
    domain component share the ``DomainComponent`` superclass,
    so a supertype match would pick an arbitrary component when
    the agent carries several.

    The collection is deliberately complete. A guard may read a
    domain component other than the one its transition table is
    configured against -- the FSM's ``component_type`` names the
    component whose field drives the state machine, not the only
    component a Specification may consult. Because the mapping
    is keyed by concrete class, a complete collection carries no
    ambiguity: ``get_component`` reads exactly the class the
    caller asks for.

    Two components of the same class cannot coexist in one
    archetype -- the ECS bag keys one value per class -- so the
    mapping has no collisions.

    The mapping is a plain ``dict``; ``MappingProxyType`` was
    removed from the framework by ADR-079 §6.2.
    """
    collected: dict[type, DomainComponent] = {}
    for value in view.components.values():
        if isinstance(value, DomainComponent):
            collected[type(value)] = value
    return collected


def build_step_context(
    view: AgentView,
    world: World,
    now: datetime,
    step_results: Mapping[str, JsonValue] | None = None,
    step_states: Mapping[str, str] | None = None,
    *,
    trigger_data: Mapping[str, JsonValue] | None = None,
) -> StepContext:
    """Build the canonical ``StepContext`` for one agent view.

    Every FSM and Saga evaluation reads the same memory tiers
    through this builder, so a Specification behaves identically
    in both execution cycles.

    The memory tiers are read from the view: ``session``
    (the active dialogue), ``continuity`` (the sliding window)
    and ``profile`` (identity and preferences). A tier the view
    does not carry is ``None`` -- the agent simply does not have
    that tier populated.

    The domain components are collected under their concrete
    classes, so a Specification reads one with
    ``ctx.get_component(MyComponent)`` and gets exactly that
    class or nothing.

    ``cross_agent_resolver`` is a closure over the post-fold
    world. A Specification that does not need cross-agent access
    never calls it, so the common case stays a cheap attribute
    read rather than a scan over every agent.
    """
    return StepContext(
        step_results=step_results or {},
        step_states=step_states or {},
        continuity=view.get_component(ContinuityComponent),
        profile=view.get_component(ProfileComponent),
        session=view.get_component(SessionComponent),
        domain_components=collect_domain_components(view),
        agent_id=view.agent_id,
        now=now,
        trigger_data=trigger_data,
        cross_agent_resolver=lambda agent_id: world.views.get(agent_id),
    )
