# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""agents._definition -- the declarative model of an agent (ADR-088).

``RoleSpec`` carries the persona and the capability policy;
``AgentDefinition`` carries the agent itself.

The definition holds no runtime handle -- no dispatcher, no
credential, no derived state. The runtime is derived from it by
:meth:`AgentDefinition.bundle`.

See ADR-088 §2.2 and §3.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kntgraph.agents._bundle import AgentBundle, derive_bundle
from kntgraph.concordos.fsm import BusinessFSMConcordo
from kntgraph.concordos.saga import WorkflowSagaConcordo
from kntgraph.core.components.role import RoleComponent
from kntgraph.core.world.component import DomainComponent

if TYPE_CHECKING:
    from kntgraph.runner.reactive import ReactiveDispatcher
    from kntgraph.security.principal import Principal


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """The persona and capability policy of an agent.

    The **model** side of the role; ``RoleComponent`` is its ECS
    projection. A spec is static configuration, a component is runtime
    state the event stream can change -- the split ADR-060
    established for ``Role`` to ``RoleComponent``.

    Attributes:
        persona: The role name (e.g. ``"fulfilment"``).
        allowed_tools: The tool names the agent may request. Read by
            gate 2 through the projected component.
    """

    persona: str
    allowed_tools: tuple[str, ...] = ()

    def to_component(
        self,
        *,
        instructions: str = "",
        persona: str | None = None,
    ) -> RoleComponent:
        """Convert the spec into its ECS projection.

        The capability policy always comes from the spec: it is static
        configuration, and must not depend on the well-formedness of a
        runtime event.

        Args:
            instructions: The persona instructions the role event carried.
            persona: The runtime-assigned persona, when it differs from
                the declared one.

        Returns:
            The ``RoleComponent`` the framework reads.
        """
        return RoleComponent(
            persona=persona or self.persona,
            instructions=instructions,
            allowed_tools=list(self.allowed_tools),
        )


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Everything the agent is, declared once.

    Attributes:
        name: The agent's stable name. Seeds the derived bundle's
            identifier (``agent:<name>``).
        persona: The persona and capability policy.
        role_event_type: The event type that assigns the role at
            runtime. The definition names the type, not the schema
            (ADR-088 §3.7).
        fsm: The zero-token governance state machine, if any.
        sagas: The deliberative tool-orchestration workflows.
        domain_types: The ``DomainComponent`` subclasses the agent's
            Specifications read. Inert until the type-indexed domain
            access lands (DEBT §2.42 open item 1).
    """

    name: str
    persona: RoleSpec
    role_event_type: str
    fsm: BusinessFSMConcordo | None = None
    sagas: tuple[WorkflowSagaConcordo, ...] = ()
    domain_types: tuple[type[DomainComponent], ...] = ()

    def bundle(self) -> AgentBundle:
        """Derive the installable runtime.

        Ephemeral: built at install time and discarded. The bundle
        repeats no field of the definition, so the two cannot disagree.
        """
        return derive_bundle(self)

    def install(
        self,
        dispatcher: ReactiveDispatcher,
        *,
        principal: Principal,
    ) -> None:
        """Register the derived systems and projections on the dispatcher.

        The principal is the host's runtime credential; it is never
        stored in the definition (ADR-088 §2.4). Its ``agent_id``
        identifies the agent being installed, so the dispatcher is told
        to watch it.

        Args:
            dispatcher: The dispatcher that owns the runtime.
            principal: The credential of the agent being installed.
        """
        bundle = self.bundle()
        for system in bundle.systems:
            dispatcher.add_system(system)
        for projection in bundle.projections:
            dispatcher.add_projection(projection)
        dispatcher.track_agent(principal.agent_id)


__all__ = ["AgentDefinition", "RoleSpec"]
