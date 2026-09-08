# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos.saga -- C-02 WorkflowSaga Concordo (ADR-069 §4).

A WorkflowSaga orchestrates a sequence of tool calls with
context enrichment, skip conditions, failure policies, and
compensation. It is built entirely on top of existing
framework primitives (ADR-034 tool calls, ADR-045 TTL,
ADR-042 memory).

Public surface:

  - ``WorkflowSagaConcordo`` — the Concordo (registers the
    ``SagaSystem`` and ``SagaTimeoutSystem`` on a dispatcher).
  - ``SagaConfig`` / ``SagaStepConfig`` — the typed
    configuration.
  - ``SagaSystem`` / ``SagaTimeoutSystem`` — the WorldSystems.
  - ``SagaProgressComponent`` — the execution-state component.
"""

from typing import TYPE_CHECKING

from ._components import SagaProgressComponent
from ._config import SagaConfig, SagaStepConfig
from ._system import SagaSystem
from ._timeout_system import SagaTimeoutSystem

if TYPE_CHECKING:
    from kntgraph.runner.reactive import ReactiveDispatcher

__all__ = [
    "SagaConfig",
    "SagaProgressComponent",
    "SagaStepConfig",
    "SagaSystem",
    "SagaTimeoutSystem",
    "WorkflowSagaConcordo",
]


class WorkflowSagaConcordo:
    """
    C-02: WorkflowSaga Concordo (Concordo Protocol §1.3.1).

    Registers two ``WorldSystem``s on the dispatcher:

    - ``SagaSystem`` — reads the post-fold ``World`` and
      drives saga execution forward (or compensation).
    - ``SagaTimeoutSystem`` — scans agents whose archetype
      carries ``SagaProgressComponent`` and emits
      ``saga.<name>.timed_out`` on deadline.

    Both systems default to the framework clock via
    ``injectable_clock()``; a vertical that needs them
    aligned passes the same ``now`` callable to both
    constructors explicitly.
    """

    def __init__(self, config: "SagaConfig") -> None:
        self._config = config
        self.name = f"saga:{config.name}"
        self.version = "1.0.0"

    def install(self, dispatcher: "ReactiveDispatcher") -> None:
        dispatcher.add_system(SagaSystem(self._config))
        dispatcher.add_system(SagaTimeoutSystem({self._config.name: self._config}))
