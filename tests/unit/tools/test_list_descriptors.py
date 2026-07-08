# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for `ToolRegistry.list_descriptors` (ADR-010 Fase 2).

The `list_descriptors` method is the bridge from the
runtime `Tool` Protocol to the
`memory.solutions.ToolDescriptor` value object used by
the Solution tier. These tests cover:

  - Schema serialisation to JSON.
  - `None` schema (degenerate but legal input).
  - Unserialisable schema (rare; the registry skips
    the tool and keeps the rest).
  - Order matches `names()`.
  - Schema-less tools still produce a descriptor.
"""

from __future__ import annotations

import json


from kntgraph.core.result import Ok
from kntgraph.agents.tools.protocol import ToolRegistry


class _SimpleTool:
    name = "invoice.issue"
    description = "Issues an invoice via external service."
    input_schema = {
        "type": "object",
        "properties": {
            "xml": {"type": "string"},
            "document_id": {"type": "string"},
        },
        "required": ["xml", "document_id"],
    }

    async def invoke(self, *, idempotency_key, **kwargs):
        return Ok({"status": "ok"})


class _NoSchemaTool:
    name = "x"
    description = "no schema"
    input_schema = None

    async def invoke(self, *, idempotency_key, **kwargs):
        return Ok(None)


class _EmptyDictSchemaTool:
    name = "y"
    description = "empty dict"
    input_schema = {}

    async def invoke(self, *, idempotency_key, **kwargs):
        return Ok(None)


class TestListDescriptors:
    def test_returns_one_per_tool(self) -> None:
        reg = ToolRegistry()
        reg.register(_SimpleTool())
        reg.register(_NoSchemaTool())
        descs = reg.list_descriptors()
        assert len(descs) == 2
        names = {d.name for d in descs}
        assert names == {"invoice.issue", "x"}

    def test_schema_serialised_to_json(self) -> None:
        reg = ToolRegistry()
        reg.register(_SimpleTool())
        desc = reg.list_descriptors()[0]
        # Round-trip back to dict and assert equality.
        parsed = json.loads(desc.input_schema_json)
        assert parsed == _SimpleTool.input_schema

    def test_none_schema_serialised_to_empty_dict(self) -> None:
        reg = ToolRegistry()
        reg.register(_NoSchemaTool())
        desc = reg.list_descriptors()[0]
        assert desc.input_schema_json == "{}"

    def test_empty_dict_schema(self) -> None:
        reg = ToolRegistry()
        reg.register(_EmptyDictSchemaTool())
        desc = reg.list_descriptors()[0]
        assert desc.input_schema_json == "{}"

    def test_order_matches_names(self) -> None:
        reg = ToolRegistry()
        reg.register(_SimpleTool())
        reg.register(_NoSchemaTool())
        descs = reg.list_descriptors()
        names_order = [d.name for d in descs]
        assert names_order == reg.names()

    def test_empty_registry(self) -> None:
        reg = ToolRegistry()
        assert reg.list_descriptors() == []

    def test_unserialisable_schema_skipped(self) -> None:
        class _WeirdTool:
            name = "weird"
            description = "unserialisable"

            class _NonJSON:
                pass

            input_schema = _NonJSON()

            async def invoke(self, *, idempotency_key, **kwargs):
                return Ok(None)

        reg = ToolRegistry()
        reg.register(_SimpleTool())
        reg.register(_WeirdTool())
        descs = reg.list_descriptors()
        # The weird tool is skipped; the simple one
        # remains.
        assert len(descs) == 1
        assert descs[0].name == "invoice.issue"
