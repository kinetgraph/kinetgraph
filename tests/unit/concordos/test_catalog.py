# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Behaviour tests for the Concordo Protocol and ConcordoCatalog
(ADR-069 §3.2, §4).

The catalog is a bag of ``Concordo`` instances. ``install_all``
iterates each bundle's ``systems`` and ``projections`` and
calls ``dispatcher.add_system(...)`` /
``dispatcher.add_projection(...)``. The catalog dedupes by
``name`` (first one wins); a double-import of the same
vertical module is safe.
"""

from __future__ import annotations

from kntgraph.concordos import Concordo, ConcordoCatalog


class FakeSystem:
    """Minimal marker for a WorldSystem (no methods required for
    these tests beyond identity)."""

    def __init__(self, tag: str = "") -> None:
        self.tag = tag


class FakeProjection:
    """Minimal marker for a WorldProjection."""

    def __init__(self, tag: str = "") -> None:
        self.tag = tag


class FakeConcordo:
    """A minimal structural ``Concordo`` -- satisfies the
    Protocol via ``(name, systems, projections)``."""

    def __init__(self, name: str, systems=(), projections=()) -> None:
        self.name = name
        self.systems = tuple(systems)
        self.projections = tuple(projections)


class RecordingDispatcher:
    """Minimal dispatcher that records the systems and projections
    added by the catalog."""

    def __init__(self) -> None:
        self.systems: list[object] = []
        self.projections: list[object] = []

    def add_system(self, system: object) -> None:
        self.systems.append(system)

    def add_projection(self, projection: object) -> None:
        self.projections.append(projection)


def test_concordo_is_runtime_checkable() -> None:
    """A Concordo instance satisfies the ``Concordo`` Protocol at
    runtime (``runtime_checkable``)."""
    assert isinstance(
        FakeConcordo("fsm:Invoice", systems=(FakeSystem(),)), Concordo
    )


def test_catalog_installs_each_unique_concordo_once() -> None:
    """``install_all`` registers every unique bundle's systems
    and projections on the dispatcher."""
    a = FakeConcordo("fsm:Invoice", systems=(FakeSystem("a"),))
    b = FakeConcordo("saga:nfe_emission", systems=(FakeSystem("b"),))
    catalog = ConcordoCatalog(a, b)
    dispatcher = RecordingDispatcher()
    catalog.install_all(dispatcher)
    assert dispatcher.systems == [a.systems[0], b.systems[0]]


def test_catalog_dedupes_by_name() -> None:
    """A second Concordo with the same name is a no-op (first one
    wins). Only the first bundle's systems / projections are
    registered."""
    first = FakeConcordo(
        "fsm:Invoice", systems=(FakeSystem("first"),)
    )
    duplicate = FakeConcordo(
        "fsm:Invoice", systems=(FakeSystem("duplicate"),)
    )
    catalog = ConcordoCatalog(first, duplicate)
    dispatcher = RecordingDispatcher()
    catalog.install_all(dispatcher)
    assert dispatcher.systems == [first.systems[0]]


def test_catalog_install_all_registers_projections() -> None:
    """The catalog iterates ``concordo.projections`` and calls
    ``dispatcher.add_projection`` for each."""
    a = FakeConcordo(
        "fsm:Invoice",
        systems=(FakeSystem("s"),),
        projections=(FakeProjection("p"),),
    )
    catalog = ConcordoCatalog(a)
    dispatcher = RecordingDispatcher()
    catalog.install_all(dispatcher)
    assert dispatcher.systems == [a.systems[0]]
    assert dispatcher.projections == [a.projections[0]]


def test_catalog_install_all_across_calls_re_runs() -> None:
    """Calling ``install_all`` twice re-registers the bundles
    (the catalog does not dedupe across calls; the dispatcher's
    ``add_system`` is the idempotency boundary)."""
    a = FakeConcordo("fsm:Invoice", systems=(FakeSystem(),))
    catalog = ConcordoCatalog(a)
    dispatcher = RecordingDispatcher()
    catalog.install_all(dispatcher)
    catalog.install_all(dispatcher)
    assert dispatcher.systems == [a.systems[0], a.systems[0]]
