# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ArchetypeStorage.
"""

import pytest
from dataclasses import dataclass

from kntgraph.core.archetype import ArchetypeId
from kntgraph.core.storage import ArchetypeStorage


@dataclass(slots=True, frozen=True)
class DocumentComponent:
    document_id: str
    total: float


@dataclass(slots=True, frozen=True)
class ClientContextComponent:
    cnpj: str


@dataclass(slots=True, frozen=True)
class TaskComponent:
    task_id: str


class TestArchetypeStorageBasic:
    def test_empty_storage(self):
        s = ArchetypeStorage()
        assert s.num_entities == 0
        assert s.num_archetypes == 0

    def test_add_entity(self):
        s = ArchetypeStorage()
        arch = s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        assert DocumentComponent in arch.components
        assert s.num_entities == 1
        assert s.num_archetypes == 1

    def test_add_entity_duplicate_raises(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        with pytest.raises(KeyError):
            s.add_entity("e1", {"doc": DocumentComponent("d2", 200.0)})

    def test_get_components(self):
        s = ArchetypeStorage()
        doc = DocumentComponent("d1", 100.0)
        s.add_entity("e1", {"doc": doc})
        assert s.get_components("e1") == {"doc": doc}
        assert s.get_components("nonexistent") is None

    def test_has_entity(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        assert s.has_entity("e1")
        assert not s.has_entity("e2")

    def test_remove_entity(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        s.remove_entity("e1")
        assert s.num_entities == 0
        assert s.num_archetypes == 0

    def test_remove_nonexistent_noop(self):
        s = ArchetypeStorage()
        s.remove_entity("nonexistent")  # Should not raise


class TestArchetypeMove:
    def test_add_component_moves_archetype(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        assert s.num_archetypes == 1

        old_arch, new_arch = s.add_component(
            "e1", "ctx", ClientContextComponent("cnpj-1")
        )
        assert old_arch != new_arch
        assert DocumentComponent in old_arch.components
        assert ClientContextComponent not in old_arch.components
        assert DocumentComponent in new_arch.components
        assert ClientContextComponent in new_arch.components
        assert s.num_archetypes == 1
        assert ArchetypeId.of(DocumentComponent) not in s.archetype_ids()
        assert (
            ArchetypeId.of(DocumentComponent, ClientContextComponent)
            in s.archetype_ids()
        )

    def test_remove_component_moves_archetype(self):
        s = ArchetypeStorage()
        s.add_entity(
            "e1",
            {
                "doc": DocumentComponent("d1", 100.0),
                "ctx": ClientContextComponent("cnpj-1"),
            },
        )
        assert s.num_archetypes == 1

        old_arch, new_arch = s.remove_component("e1", "ctx")
        assert old_arch != new_arch
        assert DocumentComponent in new_arch.components
        assert ClientContextComponent not in new_arch.components

    def test_move_entity_same_archetype(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        old_arch, new_arch = s.add_component(
            "e1", "doc", DocumentComponent("d2", 200.0)
        )
        assert old_arch == new_arch
        assert s.num_archetypes == 1

    def test_add_component_unknown_entity_raises(self):
        s = ArchetypeStorage()
        with pytest.raises(KeyError):
            s.add_component("e1", "doc", DocumentComponent("d1", 100.0))

    def test_remove_component_unknown_entity_raises(self):
        s = ArchetypeStorage()
        with pytest.raises(KeyError):
            s.remove_component("e1", "doc")


class TestArchetypeStorageQuery:
    def test_query_empty(self):
        s = ArchetypeStorage()
        assert list(s.query(DocumentComponent)) == []

    def test_query_by_single_component(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        s.add_entity("e2", {"ctx": ClientContextComponent("cnpj-1")})

        results = list(s.query(DocumentComponent))
        assert len(results) == 1
        eid, comps = results[0]
        assert eid == "e1"
        assert isinstance(comps["doc"], DocumentComponent)

    def test_query_by_multiple_components_and(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        s.add_entity(
            "e2",
            {
                "doc": DocumentComponent("d2", 200.0),
                "ctx": ClientContextComponent("cnpj-1"),
            },
        )
        s.add_entity("e3", {"task": TaskComponent("t1")})

        results = list(s.query(DocumentComponent, ClientContextComponent))
        assert len(results) == 1
        assert results[0][0] == "e2"

    def test_query_no_filter(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        s.add_entity("e2", {"ctx": ClientContextComponent("cnpj-1")})

        results = list(s.query())
        assert len(results) == 2

    def test_query_one(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        result = s.query_one(DocumentComponent)
        assert result is not None
        assert result[0] == "e1"
        assert s.query_one(TaskComponent) is None

    def test_count(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        s.add_entity(
            "e2",
            {
                "doc": DocumentComponent("d2", 200.0),
                "ctx": ClientContextComponent("cnpj-1"),
            },
        )

        assert s.count(DocumentComponent) == 2
        assert s.count(DocumentComponent, ClientContextComponent) == 1
        assert s.count(TaskComponent) == 0


class TestArchetypeStorageIteration:
    def test_entities_in(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        s.add_entity("e2", {"doc": DocumentComponent("d2", 200.0)})
        s.add_entity("e3", {"ctx": ClientContextComponent("cnpj-1")})

        doc_arch = ArchetypeId.of(DocumentComponent)
        assert sorted(s.entities_in(doc_arch)) == ["e1", "e2"]

    def test_archetype_ids(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        s.add_entity("e2", {"ctx": ClientContextComponent("cnpj-1")})

        archs = s.archetype_ids()
        assert len(archs) == 2
        assert ArchetypeId.of(DocumentComponent) in archs
        assert ArchetypeId.of(ClientContextComponent) in archs

    def test_clear(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        s.clear()
        assert s.num_entities == 0
        assert s.num_archetypes == 0


class TestArchetypeStorageToMap:
    def test_to_map(self):
        s = ArchetypeStorage()
        s.add_entity("e1", {"doc": DocumentComponent("d1", 100.0)})
        s.add_entity("e2", {"ctx": ClientContextComponent("cnpj-1")})

        m = s.to_map()
        assert "e1" in m
        assert "e2" in m
        assert m["e1"]["doc"].document_id == "d1"
