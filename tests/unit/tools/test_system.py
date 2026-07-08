# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for ToolAwareSystem.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from kntgraph.core.world.components import ToolCallCompletion, ToolCallRequest
from kntgraph.core.world.view import AgentView
from kntgraph.tools.system import ToolAwareSystem


def test_tool_aware_system_request_tool():
    """Test that request_tool creates a valid tool.requested event."""
    from kntgraph.core.event import CorrelationContext

    system = ToolAwareSystem()
    causation_id = str(uuid.uuid4())
    correlation = CorrelationContext.new(correlation_id=uuid.uuid4())
    event = system.request_tool(
        agent_id="a-1",
        tool_name="math_doubler",
        params={"x": 2},
        causation_id=causation_id,
        correlation=correlation,
    )

    assert event.event_type == "tool.requested"
    assert event.agent_id == "a-1"
    assert event.data == {"tool": "math_doubler", "params": {"x": 2}}
    assert event.correlation.correlation_id == correlation.correlation_id


def test_tool_aware_system_getters():
    """Test that the system correctly reads components from AgentView."""
    system = ToolAwareSystem()

    req_id = "req-1"

    req = ToolCallRequest(
        request_event_id=req_id,
        tool_name="math_doubler",
        agent_id="a-1",
        params={"x": 2},
        requested_at=datetime.now(timezone.utc),
    )

    comp = ToolCallCompletion(
        request_event_id=req_id,
        status="completed",
        result={"value": 4},
        completed_at=datetime.now(timezone.utc),
        latency_ms=100.0,
    )

    view = AgentView(
        agent_id="a-1",
        components={
            "tool_requests": {req_id: req},
            "tool_completions": {req_id: comp},
        },
        last_event_id="evt-2",
    )

    # Check getters
    assert system.get_request(view, req_id) == req
    assert system.get_completion(view, req_id) == comp

    # Check booleans
    assert system.has_requested(view, req_id) is True
    # It is requested, but ALSO completed. So pending = False
    assert system.is_pending(view, req_id) is False


def test_tool_aware_system_pending_state():
    """Test that is_pending is True only when no completion exists."""
    system = ToolAwareSystem()

    req_id = "req-2"

    req = ToolCallRequest(
        request_event_id=req_id,
        tool_name="pii_redactor",
        agent_id="a-1",
        params={"text": "John"},
        requested_at=datetime.now(timezone.utc),
    )

    view = AgentView(
        agent_id="a-1",
        components={
            "tool_requests": {req_id: req},
            # No completion yet
        },
        last_event_id="evt-1",
    )

    assert system.has_requested(view, req_id) is True
    assert system.is_pending(view, req_id) is True
    assert system.get_completion(view, req_id) is None


def test_tool_aware_system_unknown_request():
    """Test behavior with an unknown request_id."""
    system = ToolAwareSystem()
    view = AgentView(agent_id="a-1", components={}, last_event_id="evt-1")

    assert system.has_requested(view, "unknown") is False
    assert system.is_pending(view, "unknown") is False
    assert system.get_request(view, "unknown") is None
    assert system.get_completion(view, "unknown") is None
