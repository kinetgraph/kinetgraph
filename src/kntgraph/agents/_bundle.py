# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""agents._bundle -- the installable runtime derived from a definition (ADR-088).

The only place where a definition's parts become live objects. The
bundle repeats no field of the definition, so the two cannot disagree.

Satisfies the ``Concordo`` Protocol (ADR-069 §3.2), so the catalog
path works::

    catalog = ConcordoCatalog(*(d.bundle() for d in definitions))
    catalog.install_all(dispatcher)

The dependency direction is one-way: ``AgentDefinition`` is referred
to only under ``TYPE_CHECKING``, so importing this module from
``_definition`` creates no cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kntgraph.agents._projection import RoleProjection

if TYPE_CHECKING:
    from kntgraph.agents._definition import AgentDefinition
    from kntgraph.core.system import WorldSystem
    from kntgraph.runner.reactive_extensions import WorldProjection


@dataclass(frozen=True, slots=True)
class AgentBundle:
    """The installable artifact derived from an ``AgentDefinition``.

    Ephemeral: built at install time and discarded. Exists so the
    derivation has a name and satisfies the ``Concordo`` Protocol
    without the definition storing derived state.

    Attributes:
        name: The derived identifier, ``agent:<definition name>``. The
            catalog dedupes by it, so two agents sharing a name would
            collide.
        systems: The ``WorldSystem`` instances to register.
        projections: The ``WorldProjection`` instances to register.
    """

    name: str
    systems: tuple[WorldSystem, ...]
    projections: tuple[WorldProjection, ...]


def derive_bundle(definition: AgentDefinition) -> AgentBundle:
    """Derive the installable runtime from a declaration.

    Args:
        definition: The agent to derive.

    Returns:
        The bundle of systems and projections to register.
    """
    systems: list[WorldSystem] = []
    projections: list[WorldProjection] = []
    if definition.fsm is not None:
        systems.extend(definition.fsm.systems)
        projections.extend(definition.fsm.projections)
    for saga in definition.sagas:
        systems.extend(saga.systems)
        projections.extend(saga.projections)
    projections.append(
        RoleProjection(spec=definition.persona, event_type=definition.role_event_type)
    )
    return AgentBundle(
        name=f"agent:{definition.name}",
        systems=tuple(systems),
        projections=tuple(projections),
    )


__all__ = ["AgentBundle", "derive_bundle"]
