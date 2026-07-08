# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for Component metadata (v2.0).

The core no longer imposes Pydantic. A Component is any immutable
value object with a stable class identity. The core needs only
ComponentMeta to key things.
"""

from __future__ import annotations

from dataclasses import dataclass

from kntgraph.core.component import ComponentMeta, component_meta


@dataclass(slots=True, frozen=True)
class DocumentComponent:
    document_id: str


@dataclass(slots=True, frozen=True)
class ClientContextComponent:
    cnpj: str


class TestComponentMeta:
    def test_meta_of_class(self):
        m = ComponentMeta.of(DocumentComponent)
        assert m.module == DocumentComponent.__module__
        assert m.qualname == "DocumentComponent"

    def test_helper(self):
        m = component_meta(DocumentComponent)
        assert m.qualname == "DocumentComponent"

    def test_str(self):
        m = ComponentMeta.of(DocumentComponent)
        s = str(m)
        assert "DocumentComponent" in s

    def test_equality(self):
        a = ComponentMeta.of(DocumentComponent)
        b = ComponentMeta.of(DocumentComponent)
        assert a == b

    def test_immutable(self):
        m = ComponentMeta.of(DocumentComponent)
        try:
            m.qualname = "other"  # type: ignore[misc]
            assert False, "should have raised"
        except Exception:
            pass
