# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Behaviour tests for the WorkflowSaga Concordo (ADR-069 §4.7).

The Concordo is a thin wiring object: it exposes a stable
``name`` / ``version`` and registers its ``SagaSystem`` and
``SagaTimeoutSystem`` on a dispatcher. ``install`` is
exercised against a minimal recording dispatcher (the only
collaborator is ``add_system``, which the real
``ReactiveDispatcher`` exposes); the systems themselves are
tested in ``test_saga.py``.
"""

from __future__ import annotations

from kntgraph.concordos.saga import (
    SagaConfig,
    SagaProjection,
    SagaStepConfig,
    SagaSystem,
    SagaTimeoutSystem,
    WorkflowSagaConcordo,
)


class RecordingDispatcher:
    """Minimal dispatcher that records the systems and projections
    added via ``add_system`` / ``add_projection`` (the methods
    ``install`` calls)."""

    def __init__(self) -> None:
        self.systems: list[object] = []
        self.projections: list[object] = []

    def add_system(self, system: object) -> None:
        self.systems.append(system)

    def add_projection(self, projection: object) -> None:
        self.projections.append(projection)


def _config() -> SagaConfig:
    return SagaConfig(
        name="nfe_emission",
        steps=(SagaStepConfig(name="validate_fiscal", tool_name="sefaz_validator"),),
    )


def test_concordo_name_and_version() -> None:
    """The Concordo exposes a stable name derived from the saga
    name and a semver version."""
    concordo = WorkflowSagaConcordo(_config())
    assert concordo.name == "saga:nfe_emission"
    assert concordo.version == "1.0.0"


def test_concordo_install_registers_both_systems() -> None:
    """``install`` registers the ``SagaSystem``, the
    ``SagaTimeoutSystem``, and the ``SagaProjection`` on the
    dispatcher."""
    concordo = WorkflowSagaConcordo(_config())
    dispatcher = RecordingDispatcher()
    concordo.install(dispatcher)
    assert len(dispatcher.systems) == 2
    assert isinstance(dispatcher.systems[0], SagaSystem)
    assert isinstance(dispatcher.systems[1], SagaTimeoutSystem)
    assert len(dispatcher.projections) == 1
    assert isinstance(dispatcher.projections[0], SagaProjection)
