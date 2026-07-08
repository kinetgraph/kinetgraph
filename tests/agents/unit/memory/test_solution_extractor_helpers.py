# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the helpers extracted from
``SolutionExtractor.extract_from_events``.

The full ``extract_from_events`` is exercised
end-to-end by ``SolutionExtractorSystem`` (see
``test_solution_extractor.py``). These tests focus
on the static helpers introduced when CC=14 was
lowered to ≤ 10 by splitting the indexing step out
of the outer loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Optional
from uuid import UUID

from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.event.event import Event

from kntgraph.agents.memory.solutions._extractor import SolutionExtractor


def _ts() -> datetime:
    return datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _event(
    *,
    event_type: str,
    agent_id: str = "agent-1",
    data: Optional[Mapping[str, Any]] = None,
    causation_id: Optional[UUID] = None,
) -> Event:
    return Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class="domain",
        data=MappingProxyType(dict(data or {})),
        correlation=CorrelationContext.new(),
        causation_id=causation_id,
        timestamp=_ts(),
    )


class TestGroupEventsByAgent:
    def test_groups_by_agent_id(self):
        e1 = _event(event_type="x", agent_id="a-1")
        e2 = _event(event_type="x", agent_id="a-2")
        e3 = _event(event_type="x", agent_id="a-1")
        result = SolutionExtractor._group_events_by_agent([e1, e2, e3], agent_ids=None)
        assert set(result.keys()) == {"a-1", "a-2"}
        assert result["a-1"] == [e1, e3]
        assert result["a-2"] == [e2]

    def test_preserves_input_order_within_group(self):
        e1 = _event(event_type="x", agent_id="a-1")
        e2 = _event(event_type="y", agent_id="a-1")
        e3 = _event(event_type="z", agent_id="a-1")
        result = SolutionExtractor._group_events_by_agent([e1, e2, e3], agent_ids=None)
        assert result["a-1"] == [e1, e2, e3]

    def test_filter_includes_only_listed_agents(self):
        e1 = _event(event_type="x", agent_id="a-1")
        e2 = _event(event_type="x", agent_id="a-2")
        e3 = _event(event_type="x", agent_id="a-3")
        result = SolutionExtractor._group_events_by_agent(
            [e1, e2, e3], agent_ids=["a-1", "a-3"]
        )
        assert set(result.keys()) == {"a-1", "a-3"}
        assert result["a-1"] == [e1]
        assert result["a-3"] == [e3]

    def test_empty_filter_list_excludes_everything(self):
        e1 = _event(event_type="x", agent_id="a-1")
        result = SolutionExtractor._group_events_by_agent([e1], agent_ids=[])
        assert result == {}

    def test_empty_input(self):
        assert SolutionExtractor._group_events_by_agent([], None) == {}


class TestIndexResultsAndRequests:
    def test_pairs_completed_with_request_causation(self):
        request = _event(
            event_type="tool.lookup.requested",
            agent_id="a-1",
        )
        completion = _event(
            event_type="tool.lookup.completed",
            agent_id="a-1",
            causation_id=request.event_id,
        )
        result_by_req, requests = SolutionExtractor._index_results_and_requests(
            [request, completion]
        )
        assert result_by_req == {request.event_id: completion}
        assert requests == [request]

    def test_pairs_failed_with_request_causation(self):
        """`.failed` is also indexed (same kind mask as
        `.completed`)."""
        request = _event(event_type="tool.lookup.requested", agent_id="a-1")
        failure = _event(
            event_type="tool.lookup.failed",
            agent_id="a-1",
            causation_id=request.event_id,
        )
        result_by_req, requests = SolutionExtractor._index_results_and_requests(
            [request, failure]
        )
        assert result_by_req == {request.event_id: failure}
        assert requests == [request]

    def test_result_without_causation_id_is_dropped(self):
        """A result with `causation_id=None` is not
        indexed — it cannot be matched to any request."""
        request = _event(event_type="tool.lookup.requested", agent_id="a-1")
        orphan = _event(
            event_type="tool.lookup.completed",
            agent_id="a-1",
            causation_id=None,
        )
        result_by_req, requests = SolutionExtractor._index_results_and_requests(
            [request, orphan]
        )
        assert result_by_req == {}
        assert requests == [request]

    def test_multiple_requests_each_pair_with_own_result(self):
        r1 = _event(event_type="tool.a.requested", agent_id="x")
        r2 = _event(event_type="tool.b.requested", agent_id="x")
        c1 = _event(
            event_type="tool.a.completed",
            agent_id="x",
            causation_id=r1.event_id,
        )
        c2 = _event(
            event_type="tool.b.completed",
            agent_id="x",
            causation_id=r2.event_id,
        )
        result_by_req, requests = SolutionExtractor._index_results_and_requests(
            [r1, r2, c1, c2]
        )
        assert result_by_req == {
            r1.event_id: c1,
            r2.event_id: c2,
        }
        assert requests == [r1, r2]

    def test_non_tool_events_are_ignored(self):
        """Domain events (not tool events) are neither
        indexed as results nor kept as requests."""
        request = _event(event_type="tool.a.requested", agent_id="x")
        domain = _event(event_type="user.intent", agent_id="x")
        completion = _event(
            event_type="tool.a.completed",
            agent_id="x",
            causation_id=request.event_id,
        )
        result_by_req, requests = SolutionExtractor._index_results_and_requests(
            [domain, request, completion]
        )
        assert result_by_req == {request.event_id: completion}
        assert requests == [request]

    def test_request_without_completion_yields_empty_result(self):
        """A lone request (no result yet) does NOT
        index anything; the caller decides to skip it."""
        request = _event(event_type="tool.a.requested", agent_id="x")
        result_by_req, requests = SolutionExtractor._index_results_and_requests(
            [request]
        )
        assert result_by_req == {}
        assert requests == [request]

    def test_empty_input(self):
        result_by_req, requests = SolutionExtractor._index_results_and_requests([])
        assert result_by_req == {}
        assert requests == []
