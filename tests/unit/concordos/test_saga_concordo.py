# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Behaviour tests for the WorkflowSaga Concordo (ADR-069 §4.7).

The Concordo is a frozen bundle of ``(name, systems,
projections)`` (ADR-069 §3.2 / §3.3). It is exercised via
the ``ConcordoCatalog.install_all`` API, which iterates each
bundle's ``systems`` and ``projections`` and registers them
on a minimal recording dispatcher. The systems themselves
are tested in ``test_saga.py``.
"""

from __future__ import annotations

from kntgraph.concordos import ConcordoCatalog
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
    added via ``add_system`` / ``add_projection`` (the catalog
    iterates ``concordo.systems`` / ``concordo.projections``
    and calls these)."""

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


def test_concordo_name() -> None:
    """The Concordo exposes a stable name derived from the saga
    name."""
    concordo = WorkflowSagaConcordo(_config())
    assert concordo.name == "saga:nfe_emission"


def test_concordo_has_no_version() -> None:
    """ADR-069 §11.11: the ``version`` field was removed from
    the Concordo Protocol."""
    concordo = WorkflowSagaConcordo(_config())
    assert not hasattr(concordo, "version")


def test_concordo_systems_and_projections() -> None:
    """The bundle exposes ``systems`` (SagaSystem,
    SagaTimeoutSystem) and ``projections`` (SagaProjection)
    as immutable tuples."""
    concordo = WorkflowSagaConcordo(_config())
    assert len(concordo.systems) == 2
    assert isinstance(concordo.systems[0], SagaSystem)
    assert isinstance(concordo.systems[1], SagaTimeoutSystem)
    assert len(concordo.projections) == 1
    assert isinstance(concordo.projections[0], SagaProjection)


def test_concordo_catalog_install_registers_both_systems() -> None:
    """The catalog iterates ``concordo.systems`` /
    ``concordo.projections`` and registers them on the
    dispatcher."""
    concordo = WorkflowSagaConcordo(_config())
    dispatcher = RecordingDispatcher()
    ConcordoCatalog(concordo).install_all(dispatcher)
    assert len(dispatcher.systems) == 2
    assert isinstance(dispatcher.systems[0], SagaSystem)
    assert isinstance(dispatcher.systems[1], SagaTimeoutSystem)
    assert len(dispatcher.projections) == 1
    assert isinstance(dispatcher.projections[0], SagaProjection)
