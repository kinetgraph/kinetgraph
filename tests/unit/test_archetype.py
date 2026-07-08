# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ArchetypeId (v2.0).

Note: ArchetypeSpec (Cypher labels) was removed in F0 along with
PyArrow. ArchetypeId is the only artifact left.
"""

from __future__ import annotations

from dataclasses import dataclass

from kntgraph.core.archetype import ArchetypeId, archetype_of


@dataclass(slots=True, frozen=True)
class DocumentComponent:
    document_id: str


@dataclass(slots=True, frozen=True)
class ClientContextComponent:
    cnpj: str


@dataclass(slots=True, frozen=True)
class TaskComponent:
    task_id: str


class TestArchetypeId:
    def test_empty_archetype(self):
        arch = ArchetypeId.of()
        assert arch.components == frozenset()

    def test_single_component(self):
        arch = ArchetypeId.of(DocumentComponent)
        assert DocumentComponent in arch.components

    def test_multiple_components(self):
        arch = ArchetypeId.of(DocumentComponent, ClientContextComponent)
        assert DocumentComponent in arch.components
        assert ClientContextComponent in arch.components
        assert len(arch.components) == 2

    def test_hash_stable_across_instances(self):
        a = ArchetypeId.of(DocumentComponent, ClientContextComponent)
        b = ArchetypeId.of(ClientContextComponent, DocumentComponent)
        assert hash(a) == hash(b)
        assert a == b

    def test_inequality(self):
        a = ArchetypeId.of(DocumentComponent)
        b = ArchetypeId.of(ClientContextComponent)
        assert a != b

    def test_contains(self):
        arch = ArchetypeId.of(DocumentComponent, ClientContextComponent)
        assert arch.contains(DocumentComponent)
        assert arch.contains(DocumentComponent, ClientContextComponent)
        assert not arch.contains(TaskComponent)

    def test_intersects(self):
        a = ArchetypeId.of(DocumentComponent, ClientContextComponent)
        b = ArchetypeId.of(DocumentComponent, TaskComponent)
        c = ArchetypeId.of(TaskComponent)
        assert a.intersects(b)
        assert not a.intersects(c)

    def test_metas(self):
        arch = ArchetypeId.of(DocumentComponent)
        metas = arch.metas()
        assert len(metas) == 1
        (m,) = metas
        assert m.qualname == "DocumentComponent"

    def test_immutable(self):
        arch = ArchetypeId.of(DocumentComponent)
        try:
            arch.components = frozenset()  # type: ignore[misc]
            assert False, "should have raised"
        except Exception:
            pass


class TestArchetypeOf:
    def test_archetype_of_helper(self):
        arch = archetype_of(DocumentComponent, ClientContextComponent)
        assert isinstance(arch, ArchetypeId)
        assert DocumentComponent in arch.components
