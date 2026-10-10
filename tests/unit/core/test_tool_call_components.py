# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the ToolCall ECS components.

Iter 28 FU 8 (ADR-034): ``ToolCallRequest`` and
``ToolCallCompletion`` are the framework-level
primitives that materialise the in-flight tool call
state in the World. They are derived from
``tool.requested`` / ``tool.completed`` / ``tool.failed``
events via the ``project_tool_calls`` projection.

These components are **immutable** (frozen + slots);
state transitions are archetype migrations, not
field mutations. The component instances are
re-derivable from the EventLog at any time.

This test is the deletion gate for the components. If
a future refactor removes them, this test fails.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from kntgraph.core.world.components import (
    ToolCallCompletion,
    ToolCallRequest,
)


class TestToolCallRequest:
    def test_request_is_frozen(self) -> None:
        """`ToolCallRequest` is frozen: a ToolCall in
        flight is immutable (the event created it; the
        component is a cache of the event)."""
        request_event_id = str(uuid4())
        ts = datetime.now(UTC)
        params = {"tool": "llm.complete", "n": 1}
        req = ToolCallRequest(
            request_event_id=request_event_id,
            tool_name="llm.complete",
            agent_id="agent-1",
            params=params,
            requested_at=ts,
            expires_at=ts + timedelta(seconds=300),
        )
        with pytest.raises((AttributeError, Exception)):
            req.tool_name = "other"  # type: ignore[misc]

    def test_request_carries_event_provenance(self) -> None:
        """The request event_id, agent_id, and tool_name
        are the canonical fields the SolutionExtractor
        joins on. They MUST be present (no defaults)."""
        ts = datetime.now(UTC)
        params = {"tool": "x"}
        req = ToolCallRequest(
            request_event_id="req-1",
            tool_name="x",
            agent_id="agent-1",
            params=params,
            requested_at=ts,
            expires_at=ts + timedelta(seconds=300),
        )
        assert req.request_event_id == "req-1"
        assert req.tool_name == "x"
        assert req.agent_id == "agent-1"
        assert req.params == {"tool": "x"}
        assert req.requested_at == ts

    def test_request_required_fields(self) -> None:
        """All four required fields are mandatory —
        there is no default for `request_event_id`,
        `tool_name`, `agent_id`, or `requested_at`.
        The projection fills all four from the event.
        """
        with pytest.raises(TypeError):
            ToolCallRequest()  # type: ignore[call-arg]

    def test_request_params_is_immutable(self) -> None:
        """`params` is typed ``Mapping[str, JsonValue]``
        (ADR-079); the ``frozen=True`` dataclass enforces
        structural immutability -- assigning a new dict to
        the field raises ``FrozenInstanceError`` rather than
        passing through.

        The legacy assertion expected ``TypeError`` from a
        ``MappingProxyType.setitem`` call; the runtime
        wrapper was removed (ADR-079 §3.2). The
        ``frozen=True`` guarantee is the only one we keep.
        """
        ts = datetime.now(UTC)
        params = {"x": 1}
        req = ToolCallRequest(
            request_event_id="r1",
            tool_name="t",
            agent_id="a",
            params=params,
            requested_at=ts,
            expires_at=ts + timedelta(seconds=300),
        )

        with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
            req.params = {"x": 2}  # type: ignore[misc]


class TestToolCallCompletion:
    def test_completion_status_required(self) -> None:
        """`status` is required and must be one of
        "completed" or "failed". The projection enforces
        this; the dataclass accepts the str as-is.
        """
        with pytest.raises(TypeError):
            ToolCallCompletion()  # type: ignore[call-arg]

    def test_completion_completed_has_result(self) -> None:
        """A `status="completed"` completion carries
        `result` and `completed_at`."""
        ts = datetime.now(UTC)
        result = {"text": "ok"}
        comp = ToolCallCompletion(
            request_event_id="req-1",
            status="completed",
            result=result,
            completed_at=ts,
            latency_ms=12.0,
        )
        assert comp.status == "completed"
        assert comp.result == {"text": "ok"}
        assert comp.completed_at == ts
        assert comp.latency_ms == 12.0
        assert comp.error is None

    def test_completion_failed_has_error(self) -> None:
        """A `status="failed"` completion carries
        `error` and `completed_at`. `result` is None."""
        ts = datetime.now(UTC)
        comp = ToolCallCompletion(
            request_event_id="req-1",
            status="failed",
            error="rate_limited",
            completed_at=ts,
            latency_ms=100.0,
        )
        assert comp.status == "failed"
        assert comp.error == "rate_limited"
        assert comp.completed_at == ts
        assert comp.latency_ms == 100.0
        assert comp.result is None

    def test_completion_is_frozen(self) -> None:
        """`ToolCallCompletion` is frozen: the
        completion is a cache of a `tool.completed`/
        `tool.failed` event. It cannot be mutated."""
        ts = datetime.now(UTC)
        comp = ToolCallCompletion(
            request_event_id="req-1",
            status="completed",
            completed_at=ts,
        )
        with pytest.raises((AttributeError, Exception)):
            comp.status = "failed"  # type: ignore[misc]

    def test_completion_result_is_immutable(self) -> None:
        """`result` is typed ``Mapping[str, JsonValue] | None``
        (ADR-079); the ``frozen=True`` dataclass enforces
        structural immutability (ADR-079 §3.1 + skill §1.4).

        The legacy assertion expected ``TypeError`` from the
        ``MappingProxyType.setitem`` call; the runtime
        wrapper was removed (ADR-079 §3.2). Assigning to the
        field raises ``FrozenInstanceError`` now.
        """
        ts = datetime.now(UTC)
        result = {"text": "ok"}
        comp = ToolCallCompletion(
            request_event_id="req-1",
            status="completed",
            result=result,
            completed_at=ts,
        )

        with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
            comp.result = {"new": "value"}  # type: ignore[misc]


class TestToolCallPairing:
    """The two components pair by `request_event_id`."""

    def test_pair_by_request_event_id(self) -> None:
        """A request and completion with the same
        ``request_event_id`` are a single logical
        tool call. The SolutionExtractor joins on this.
        """
        ts = datetime.now(UTC)
        params = {"tool": "x"}
        req = ToolCallRequest(
            request_event_id="req-42",
            tool_name="x",
            agent_id="a",
            params=params,
            requested_at=ts,
            expires_at=ts + timedelta(seconds=300),
        )
        comp = ToolCallCompletion(
            request_event_id="req-42",
            status="completed",
            completed_at=ts,
        )
        assert req.request_event_id == comp.request_event_id
