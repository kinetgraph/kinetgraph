# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for `WorkerManager.list_descriptors` (ADR-010 Fase 2,
migrated from ``ToolRegistry`` in v0.18 per ADR-066 §4.4).

The ``list_descriptors`` method is the bridge from the
runtime ``@tool_worker`` classes to the
``tools.descriptors.ToolDescriptor`` value object used by
the Solution tier and the HTTP ``GET /agents/{id}/tools``
endpoint. These tests cover:

  - Schema serialisation to JSON.
  - ``None`` schema (degenerate but legal input).
  - Unserialisable schema (rare; the manager skips
    the tool and keeps the rest).
  - Order matches ``names()``.
  - Schema-less tools still produce a descriptor.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from kntgraph.core.result import Ok
from kntgraph.tools.manager import WorkerManager


def _make_manager() -> WorkerManager:
    """Build a ``WorkerManager`` backed by mocks (no
    Redis, no process pool). Suitable for
    ``list_descriptors`` / ``register`` testing.
    """
    redis_mock = MagicMock()
    redis_mock.xack = AsyncMock()
    event_log_mock = MagicMock()
    event_log_mock.append = AsyncMock()
    return WorkerManager(redis=redis_mock, event_log=event_log_mock)


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
        mgr = _make_manager()
        mgr.register(_SimpleTool, acl=None)
        mgr.register(_NoSchemaTool, acl=None)
        descs = mgr.list_descriptors()
        assert len(descs) == 2
        names = {d.name for d in descs}
        assert names == {"invoice.issue", "x"}

    def test_schema_serialised_to_json(self) -> None:
        mgr = _make_manager()
        mgr.register(_SimpleTool, acl=None)
        desc = mgr.list_descriptors()[0]
        parsed = json.loads(desc.input_schema_json)
        assert parsed == _SimpleTool.input_schema

    def test_none_schema_serialised_to_empty_dict(self) -> None:
        mgr = _make_manager()
        mgr.register(_NoSchemaTool, acl=None)
        desc = mgr.list_descriptors()[0]
        assert desc.input_schema_json == "{}"

    def test_empty_dict_schema(self) -> None:
        mgr = _make_manager()
        mgr.register(_EmptyDictSchemaTool, acl=None)
        desc = mgr.list_descriptors()[0]
        assert desc.input_schema_json == "{}"

    def test_order_matches_names(self) -> None:
        mgr = _make_manager()
        mgr.register(_SimpleTool, acl=None)
        mgr.register(_NoSchemaTool, acl=None)
        descs = mgr.list_descriptors()
        names_order = [d.name for d in descs]
        assert names_order == mgr.names()

    def test_empty_manager(self) -> None:
        mgr = _make_manager()
        assert mgr.list_descriptors() == []

    def test_unserialisable_schema_skipped(self) -> None:
        class _WeirdTool:
            name = "weird"
            description = "unserialisable"

            class _NonJSON:
                pass

            input_schema = _NonJSON()

            async def invoke(self, *, idempotency_key, **kwargs):
                return Ok(None)

        mgr = _make_manager()
        mgr.register(_SimpleTool, acl=None)
        mgr.register(_WeirdTool, acl=None)
        descs = mgr.list_descriptors()
        assert len(descs) == 1
        assert descs[0].name == "invoice.issue"
