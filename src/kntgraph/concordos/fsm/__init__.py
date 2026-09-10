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

  - ``BusinessFSMConcordo`` — the Concordo (registers the
    ``FSMSystem`` on a dispatcher).
  - ``FSMConfig`` / ``FSMTransition`` — the typed
    configuration.
  - ``FSMSystem`` — the WorldSystem.
  - ``FSMAuditComponent`` — the audit component.
"""

from typing import TYPE_CHECKING

from ._components import FSMAuditComponent
from ._config import FSMConfig, FSMTransition
from ._state import FSMProjection
from ._system import FSMSystem

if TYPE_CHECKING:
    from kntgraph.runner.reactive import ReactiveDispatcher

__all__ = [
    "BusinessFSMConcordo",
    "FSMAuditComponent",
    "FSMConfig",
    "FSMProjection",
    "FSMSystem",
    "FSMTransition",
]


class BusinessFSMConcordo:
    """
    C-01: BusinessFSM Concordo (Concordo Protocol §1.3.1).

    Registers a single ``FSMSystem`` on the dispatcher. The
    dispatcher invokes ``install`` idempotently:
    ``ConcordoCatalog.install_all`` (§6.3) dedupes by
    ``name`` before calling it.
    """

    def __init__(self, config: FSMConfig) -> None:
        self._config = config
        self.name = f"fsm:{config.component_type.__name__}"
        self.version = "1.0.0"

    def install(self, dispatcher: "ReactiveDispatcher") -> None:
        dispatcher.add_system(FSMSystem(self._config))
        # Advance the configured ``DomainComponent``'s
        # ``state_field`` from ``fsm.transitioned`` events
        # (ADR-069 §9.2 item 1, option b). The projection runs
        # after the base fold so the component's state advances
        # on the agent's view; the FSMSystem reads it by class.
        dispatcher.add_projection(FSMProjection(self._config))
