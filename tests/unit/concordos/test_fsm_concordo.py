# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Behaviour tests for the BusinessFSM Concordo (ADR-069 §3.5).

The Concordo is a thin wiring object: it exposes a stable
``name`` / ``version`` and registers its ``FSMSystem`` on a
dispatcher. ``install`` is exercised against a minimal
recording dispatcher (the only collaborator is
``add_system``, which the real ``ReactiveDispatcher``
exposes); the system itself is tested in ``test_fsm.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from kntgraph.concordos.fsm import (
    BusinessFSMConcordo,
    FSMConfig,
    FSMTransition,
    FSMSystem,
)
from kntgraph.core.world import DomainComponent


@dataclass(frozen=True, slots=True)
class InvoiceDomainComponent(DomainComponent):
    """A minimal domain component for the Concordo tests."""

    status: str = "draft"


class RecordingDispatcher:
    """Minimal dispatcher that records the systems added via
    ``add_system`` (the only method ``install`` calls)."""

    def __init__(self) -> None:
        self.systems: list[object] = []

    def add_system(self, system: object) -> None:
        self.systems.append(system)


def _config() -> FSMConfig:
    return FSMConfig(
        component_type=InvoiceDomainComponent,
        state_field="status",
        transitions={
            "draft": {"invoice.submitted": FSMTransition(to="validating")},
        },
    )


def test_concordo_name_and_version() -> None:
    """The Concordo exposes a stable name derived from the
    component type and a semver version."""
    concordo = BusinessFSMConcordo(_config())
    assert concordo.name == "fsm:InvoiceDomainComponent"
    assert concordo.version == "1.0.0"


def test_concordo_install_registers_fsm_system() -> None:
    """``install`` registers an ``FSMSystem`` on the dispatcher."""
    concordo = BusinessFSMConcordo(_config())
    dispatcher = RecordingDispatcher()
    concordo.install(dispatcher)
    assert len(dispatcher.systems) == 1
    assert isinstance(dispatcher.systems[0], FSMSystem)
