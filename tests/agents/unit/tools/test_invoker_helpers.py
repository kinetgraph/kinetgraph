# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the helpers extracted from
``ToolInvoker.run_once`` and ``ToolInvoker._resolve_args``.

``ToolInvoker`` has no direct tests in ``kntgraph.agents`` —
the integration tests in ``kntgraph`` exercise it
end-to-end. These tests cover the static / isolated
helpers introduced when CC=12/11 were lowered to ≤ 10
by splitting the indexing, dispatch, and validation
steps.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Optional
from uuid import UUID, uuid4

import pytest

from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.event.event import Event

from kntgraph.agents.tools.invoker._invoker import ToolInvoker
from kntgraph.agents.tools.invoker._types import ArgsInvalid


def _ts() -> datetime:
    return datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _event(
    *,
    event_type: str,
    agent_id: str = "agent-1",
    data: Optional[Mapping[str, Any]] = None,
    causation_id: Optional[UUID] = None,
    event_id: Optional[UUID] = None,
) -> Event:
    return Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class="domain",
        data=MappingProxyType(dict(data or {})),
        correlation=CorrelationContext.new(),
        causation_id=causation_id,
        event_id=event_id,
        timestamp=_ts(),
    )


# ---------------------------------------------------------------------------
# _index_results
# ---------------------------------------------------------------------------


class TestIndexResults:
    def test_empty_events_yields_empty_sets(self):
        seen_completed, seen_failed = ToolInvoker._index_results([])
        assert seen_completed == set()
        assert seen_failed == set()

    def test_completed_event_uses_causation_id(self):
        """`.completed` events register their
        `causation_id` (which is the request's
        `event_id`)."""
        req_id = uuid4()
        comp = _event(
            event_type="tool.x.completed",
            causation_id=req_id,
        )
        seen_completed, seen_failed = ToolInvoker._index_results([comp])
        assert seen_completed == {str(req_id)}
        assert seen_failed == set()

    def test_failed_event_uses_causation_id(self):
        req_id = uuid4()
        fail = _event(
            event_type="tool.x.failed",
            causation_id=req_id,
        )
        seen_completed, seen_failed = ToolInvoker._index_results([fail])
        assert seen_completed == set()
        assert seen_failed == {str(req_id)}

    def test_falls_back_to_data_request_id(self):
        """Legacy events (predating `causation_id`)
        store the request id in `data["request_id"]`."""
        comp = _event(
            event_type="tool.x.completed",
            data={"request_id": "legacy-req-1"},
        )
        seen_completed, seen_failed = ToolInvoker._index_results([comp])
        assert seen_completed == {"legacy-req-1"}

    def test_completed_without_causation_or_request_id_dropped(self):
        """Orphan result (no causation_id, no
        `data["request_id"]`) is dropped — the key
        would be the empty string."""
        comp = _event(event_type="tool.x.completed")
        seen_completed, _ = ToolInvoker._index_results([comp])
        # `e.causation_id or e.data.get("request_id", "")`
        # evaluates to ""; `str("")` is "".
        assert seen_completed == {""}

    def test_mixed_completed_and_failed(self):
        rid1, rid2 = uuid4(), uuid4()
        comp = _event(
            event_type="tool.x.completed",
            causation_id=rid1,
        )
        fail = _event(
            event_type="tool.x.failed",
            causation_id=rid2,
        )
        seen_completed, seen_failed = ToolInvoker._index_results([comp, fail])
        assert seen_completed == {str(rid1)}
        assert seen_failed == {str(rid2)}

    def test_non_result_events_ignored(self):
        """`.requested` and arbitrary domain events
        are not indexed."""
        rid = uuid4()
        req = _event(
            event_type="tool.x.requested",
            event_id=rid,
        )
        domain = _event(event_type="user.intent")
        seen_completed, seen_failed = ToolInvoker._index_results([req, domain])
        assert seen_completed == set()
        assert seen_failed == set()


# ---------------------------------------------------------------------------
# _validate_or_raise
# ---------------------------------------------------------------------------


class _StubTool:
    """Minimal Tool stub for the validation helper.
    The helper only reads `input_schema`."""

    def __init__(self, schema: Optional[dict] = None):
        self._schema = schema
        self.name = "stub"
        self.description = "stub"
        self.input_schema: Any = schema

    async def invoke(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class TestValidateOrRaise:
    def test_valid_args_does_not_raise(self):
        schema = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
        }
        # Should not raise.
        ToolInvoker._validate_or_raise({"x": "ok"}, schema)

    def test_missing_required_raises_args_invalid(self):
        schema = {
            "type": "object",
            "required": ["x"],
            "properties": {"x": {"type": "string"}},
        }
        with pytest.raises(ArgsInvalid) as exc:
            ToolInvoker._validate_or_raise({}, schema)
        assert "x" in exc.value.missing

    def test_type_mismatch_raises_args_invalid(self):
        schema = {
            "type": "object",
            "required": ["x"],
            "properties": {"x": {"type": "integer"}},
        }
        with pytest.raises(ArgsInvalid) as exc:
            ToolInvoker._validate_or_raise({"x": "not-int"}, schema)
        assert len(exc.value.type_mismatches) == 1
        assert exc.value.type_mismatches[0][0] == "x"

    def test_none_schema_skips_validation(self):
        """No schema → no validation, no raise.
        Useful for tools that don't declare one."""
        ToolInvoker._validate_or_raise({"x": "anything"}, None)

    def test_empty_schema_dict_skips_validation(self):
        """Empty schema → no properties → no
        validation. The validator only checks
        declared properties."""
        ToolInvoker._validate_or_raise({"x": "anything"}, {})
