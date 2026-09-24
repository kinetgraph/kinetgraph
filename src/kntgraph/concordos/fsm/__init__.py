# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos.fsm -- C-01 BusinessFSM Concordo (ADR-069 §3).

A BusinessFSM declares the lifecycle of a business object as
an explicit state machine over a ``DomainComponent``. It is
a pure reactive system: no I/O, no tool calls. It reacts to
domain events, validates transitions, and emits
``fsm.transitioned`` or ``fsm.transition_rejected``.

Public surface:

  - ``BusinessFSMConcordo`` -- the Concordo bundle
    (ADR-069 §3.2 / §3.3).
  - ``FSMConfig`` / ``FSMTransition`` -- the typed
    configuration.
  - ``FSMSystem`` -- the WorldSystem.
  - ``FSMAuditComponent`` -- the audit component.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ._components import FSMAuditComponent
from ._config import FSMConfig, FSMTransition
from ._state import FSMProjection
from ._system import FSMSystem

if TYPE_CHECKING:
    from kntgraph.core.system import WorldSystem
    from kntgraph.runner.reactive_extensions import WorldProjection

__all__ = [
    "BusinessFSMConcordo",
    "FSMAuditComponent",
    "FSMConfig",
    "FSMProjection",
    "FSMSystem",
    "FSMTransition",
]


@dataclass(frozen=True, slots=True)
class BusinessFSMConcordo:
    """
    C-01: BusinessFSM Concordo (ADR-069 §3.2 / §3.3).

    A frozen bundle of ``(name, systems, projections)`` that
    satisfies the structural :class:`Concordo` Protocol. The
    ``__post_init__`` derives ``systems`` and ``projections``
    from the config; the bundle is otherwise immutable.

    The catalog (``concordos.ConcordoCatalog.install_all``)
    iterates each Concordo's ``systems`` and ``projections``
    and registers them on the dispatcher via
    ``dispatcher.add_system(...)`` /
    ``dispatcher.add_projection(...)`` -- the same registration
    API the framework exposes for any custom system. The
    catalog dedupes by ``name`` (first one wins).

    Side effects (DLQ ingestion, metrics, notifications) are
    wired by the application via ``dispatcher.subscribe``
    (ADR-069 §5.2), not by the Concordo.
    """

    config: FSMConfig
    name: str = field(init=False)
    systems: tuple["WorldSystem", ...] = field(init=False)
    projections: tuple["WorldProjection", ...] = field(init=False)

    def __post_init__(self) -> None:
        # The dataclass is frozen; use ``object.__setattr__``
        # to derive the derived fields. The name convention
        # is ``fsm:<ComponentTypeName>`` (ADR-069 §3.3).
        object.__setattr__(self, "name", f"fsm:{self.config.component_type.__name__}")
        # ``FSMSystem`` validates transitions + guards on
        # every tick; ``FSMProjection`` advances the
        # configured ``DomainComponent``'s ``state_field``
        # from ``fsm.transitioned`` events (ADR-069 §9.2
        # item 1, option b). The projection runs after the
        # base fold so the component's state advances on the
        # agent's view; the FSMSystem reads it by class.
        object.__setattr__(self, "systems", (FSMSystem(self.config),))
        object.__setattr__(self, "projections", (FSMProjection(self.config),))
