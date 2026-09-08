# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Behaviour tests for the Concordo Protocol and ConcordoCatalog
(ADR-069 §1.3.1, §6.3).

The catalog is a bag of ``Concordo`` instances with idempotent
installation: ``install_all`` calls ``install`` once per unique
``name``. A second pass with the same name is a no-op, so
double-imports of the same vertical module are safe.
"""

from __future__ import annotations

from kntgraph.concordos import Concordo, ConcordoCatalog


class FakeConcordo:
    """A minimal Concordo that records its install calls."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.version = "1.0.0"
        self.installed_on: list[object] = []

    def install(self, dispatcher: object) -> None:
        self.installed_on.append(dispatcher)


class RecordingDispatcher:
    """Minimal dispatcher that records the systems added."""

    def __init__(self) -> None:
        self.systems: list[object] = []

    def add_system(self, system: object) -> None:
        self.systems.append(system)


def test_concordo_is_runtime_checkable() -> None:
    """A Concordo instance satisfies the ``Concordo`` Protocol at
    runtime (``runtime_checkable``)."""
    assert isinstance(FakeConcordo("fsm:Invoice"), Concordo)


def test_catalog_installs_each_unique_concordo_once() -> None:
    """``install_all`` calls ``install`` once per unique name."""
    a = FakeConcordo("fsm:Invoice")
    b = FakeConcordo("saga:nfe_emission")
    catalog = ConcordoCatalog(a, b)
    dispatcher = RecordingDispatcher()
    catalog.install_all(dispatcher)
    assert a.installed_on == [dispatcher]
    assert b.installed_on == [dispatcher]


def test_catalog_dedupes_by_name() -> None:
    """A second Concordo with the same name is a no-op (first one
    wins)."""
    first = FakeConcordo("fsm:Invoice")
    duplicate = FakeConcordo("fsm:Invoice")
    catalog = ConcordoCatalog(first, duplicate)
    dispatcher = RecordingDispatcher()
    catalog.install_all(dispatcher)
    assert first.installed_on == [dispatcher]
    assert duplicate.installed_on == []


def test_catalog_install_all_is_idempotent() -> None:
    """Calling ``install_all`` twice installs each Concordo twice
    (the catalog does not dedupe across calls; the dispatcher's
    ``add_system`` is the idempotency boundary)."""
    a = FakeConcordo("fsm:Invoice")
    catalog = ConcordoCatalog(a)
    dispatcher = RecordingDispatcher()
    catalog.install_all(dispatcher)
    catalog.install_all(dispatcher)
    assert a.installed_on == [dispatcher, dispatcher]
