# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Behaviour tests for the BusinessFSM Concordo (ADR-069 §3.5).

The Concordo is a frozen bundle of ``(name, systems,
projections)`` (ADR-069 §3.2 / §3.3). It is exercised via
the ``ConcordoCatalog.install_all`` API, which iterates each
bundle's ``systems`` and ``projections`` and registers them
on a minimal recording dispatcher (the only collaborator is
``add_system`` / ``add_projection``, which the real
``ReactiveDispatcher`` exposes). The system itself is tested
in ``test_fsm.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from kntgraph.concordos import ConcordoCatalog
from kntgraph.concordos.fsm import (
    BusinessFSMConcordo,
    FSMConfig,
    FSMProjection,
    FSMTransition,
    FSMSystem,
)
from kntgraph.core.world import DomainComponent


@dataclass(frozen=True, slots=True)
class InvoiceDomainComponent(DomainComponent):
    """A minimal domain component for the Concordo tests."""

    status: str = "draft"


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


def _config() -> FSMConfig:
    return FSMConfig(
        component_type=InvoiceDomainComponent,
        state_field="status",
        transitions={
            "draft": {"invoice.submitted": FSMTransition(to="validating")},
        },
    )


def test_concordo_name() -> None:
    """The Concordo exposes a stable name derived from the
    component type."""
    concordo = BusinessFSMConcordo(_config())
    assert concordo.name == "fsm:InvoiceDomainComponent"


def test_concordo_has_no_version() -> None:
    """ADR-069 §11.11: the ``version`` field was removed from
    the Concordo Protocol. The catalog dedupes by ``name``;
    there is no version-driven migration signal on the
    runtime bundle (operators signal migration through the
    bundle's wire-format ``version`` in ``schemas.BundleSchema``).
    """
    concordo = BusinessFSMConcordo(_config())
    assert not hasattr(concordo, "version")


def test_concordo_systems_and_projections() -> None:
    """The bundle exposes ``systems`` (FSMSystem) and
    ``projections`` (FSMProjection) as immutable tuples."""
    concordo = BusinessFSMConcordo(_config())
    assert len(concordo.systems) == 1
    assert isinstance(concordo.systems[0], FSMSystem)
    assert len(concordo.projections) == 1
    assert isinstance(concordo.projections[0], FSMProjection)


def test_concordo_catalog_install_registers_fsm_system() -> None:
    """The catalog iterates ``concordo.systems`` /
    ``concordo.projections`` and registers them on the
    dispatcher via ``add_system`` / ``add_projection``."""
    concordo = BusinessFSMConcordo(_config())
    dispatcher = RecordingDispatcher()
    ConcordoCatalog(concordo).install_all(dispatcher)
    assert len(dispatcher.systems) == 1
    assert isinstance(dispatcher.systems[0], FSMSystem)
    assert len(dispatcher.projections) == 1
    assert isinstance(dispatcher.projections[0], FSMProjection)
