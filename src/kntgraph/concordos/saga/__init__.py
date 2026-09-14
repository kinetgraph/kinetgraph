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

  - ``WorkflowSagaConcordo`` -- the Concordo bundle
    (ADR-069 §3.2 / §3.3).
  - ``SagaConfig`` / ``SagaStepConfig`` -- the typed
    configuration.
  - ``SagaSystem`` / ``SagaTimeoutSystem`` -- the WorldSystems.
  - ``SagaProgressComponent`` -- the execution-state component.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ._components import SagaProgressComponent
from ._config import SagaConfig, SagaStepConfig
from ._state import SagaProjection
from ._system import SagaSystem
from ._timeout_system import SagaTimeoutSystem

if TYPE_CHECKING:
    from kntgraph.core.system import WorldSystem
    from kntgraph.runner.reactive_extensions import WorldProjection

__all__ = [
    "SagaConfig",
    "SagaProgressComponent",
    "SagaProjection",
    "SagaStepConfig",
    "SagaSystem",
    "SagaTimeoutSystem",
    "WorkflowSagaConcordo",
]


@dataclass(frozen=True, slots=True)
class WorkflowSagaConcordo:
    """
    C-02: WorkflowSaga Concordo (ADR-069 §3.2 / §3.3).

    A frozen bundle of ``(name, systems, projections)`` that
    satisfies the structural :class:`Concordo` Protocol. The
    ``__post_init__`` derives ``systems`` and ``projections``
    from the config; the bundle is otherwise immutable.

    Registers two ``WorldSystem``s and one ``WorldProjection``
    on the dispatcher (via the catalog or directly):

    - ``SagaSystem`` -- reads the post-fold ``World`` and
      drives saga execution forward (or compensation).
    - ``SagaTimeoutSystem`` -- scans agents whose archetype
      carries ``SagaProgressComponent`` and emits
      ``saga.<name>.timed_out`` on deadline.
    - ``SagaProjection`` -- materialises
      ``SagaProgressComponent`` from the saga events
      (ADR-069 §9.2 item 6).

    Both systems default to the framework clock via
    ``injectable_clock()``; a vertical that needs them
    aligned passes the same ``now`` callable to both
    constructors explicitly.
    """

    config: SagaConfig
    name: str = field(init=False)
    systems: tuple["WorldSystem", ...] = field(init=False)
    projections: tuple["WorldProjection", ...] = field(init=False)

    def __post_init__(self) -> None:
        # The dataclass is frozen; use ``object.__setattr__``
        # to derive the derived fields. The name convention
        # is ``saga:<SagaName>`` (ADR-069 §3.3).
        object.__setattr__(self, "name", f"saga:{self.config.name}")
        # ``SagaSystem`` drives the saga forward; the
        # ``SagaTimeoutSystem`` runs the per-tick deadline
        # check (mapped by saga name so a single instance
        # services every saga in the bundle).
        object.__setattr__(
            self,
            "systems",
            (
                SagaSystem(self.config),
                SagaTimeoutSystem({self.config.name: self.config}),
            ),
        )
        # ``SagaProjection`` auto-hydrates
        # ``SagaProgressComponent`` so the saga system can
        # read it by class.
        object.__setattr__(self, "projections", (SagaProjection(self.config),))
